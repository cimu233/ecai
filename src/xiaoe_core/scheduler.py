"""Persistent course monitoring and background worker orchestration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from .config import AppPaths
from .database import Database


SCHEDULE_VERSION = 1
MIN_INTERVAL_SECONDS = 60
STALE_LOCK_SECONDS = 24 * 60 * 60
_INTERVAL_PATTERN = re.compile(r"^\s*(\d+)\s*([mhd]?)\s*$", re.IGNORECASE)
_URL_QUERY_PATTERN = re.compile(r"(https?://[^\s?]+)\?[^\s]+")
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._~+/\-=]+", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_interval(value: str) -> int:
    """Convert values such as 30m, 2h, or 1d into seconds."""
    match = _INTERVAL_PATTERN.match(value)
    if not match:
        raise ValueError("检查频率格式无效，请使用 30m、2h 或 1d。")
    amount = int(match.group(1))
    unit = match.group(2).lower() or "m"
    multiplier = {"m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * multiplier
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError("检查频率至少为 1 分钟。")
    return seconds


def format_interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        return "{} 天".format(seconds // 86400)
    if seconds % 3600 == 0:
        return "{} 小时".format(seconds // 3600)
    return "{} 分钟".format(max(1, seconds // 60))


def redact_message(value: str) -> str:
    redacted = _URL_QUERY_PATTERN.sub(r"\1?[redacted]", value)
    return _BEARER_PATTERN.sub("Bearer [redacted]", redacted)


def cli_arguments(paths: AppPaths, arguments: List[str]) -> List[str]:
    """Build a child CLI command for source installs and PyInstaller executables."""
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--data-dir",
            str(paths.data_dir),
        ] + arguments
    return [
        sys.executable,
        "-m",
        "xiaoe_cli",
        "--data-dir",
        str(paths.data_dir),
    ] + arguments


@dataclass(frozen=True)
class CourseSchedule:
    course_id: str
    interval_seconds: int
    enabled: bool
    next_run_at: str
    created_at: str
    updated_at: str
    last_run_at: Optional[str] = None
    last_finished_at: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_new_lessons: int = 0

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CourseSchedule":
        return cls(
            course_id=str(payload["course_id"]),
            interval_seconds=max(
                MIN_INTERVAL_SECONDS, int(payload["interval_seconds"])
            ),
            enabled=bool(payload.get("enabled", True)),
            next_run_at=str(payload["next_run_at"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            last_run_at=payload.get("last_run_at"),
            last_finished_at=payload.get("last_finished_at"),
            last_status=payload.get("last_status"),
            last_error=payload.get("last_error"),
            last_new_lessons=max(0, int(payload.get("last_new_lessons") or 0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ScheduleStore:
    def __init__(self, path: Path, now_fn: Callable[[], datetime] = utc_now) -> None:
        self.path = path
        self.now_fn = now_fn

    def list(self) -> List[CourseSchedule]:
        payload = self._load()
        schedules = []
        for item in payload.get("schedules", []):
            if not isinstance(item, dict):
                continue
            try:
                schedules.append(CourseSchedule.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(schedules, key=lambda item: item.course_id)

    def get(self, course_id: str) -> Optional[CourseSchedule]:
        return next(
            (item for item in self.list() if item.course_id == course_id), None
        )

    def upsert(
        self, course_id: str, interval_seconds: int, enabled: bool = True
    ) -> CourseSchedule:
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError("检查频率至少为 1 分钟。")
        now = self.now_fn()
        existing = self.get(course_id)
        item = CourseSchedule(
            course_id=course_id,
            interval_seconds=interval_seconds,
            enabled=enabled,
            next_run_at=to_iso(now + timedelta(seconds=interval_seconds)),
            created_at=existing.created_at if existing else to_iso(now),
            updated_at=to_iso(now),
            last_run_at=existing.last_run_at if existing else None,
            last_finished_at=existing.last_finished_at if existing else None,
            last_status=existing.last_status if existing else None,
            last_error=existing.last_error if existing else None,
            last_new_lessons=existing.last_new_lessons if existing else 0,
        )
        self._replace(item)
        return item

    def set_enabled(self, course_id: str, enabled: bool) -> CourseSchedule:
        existing = self._require(course_id)
        now = self.now_fn()
        item = CourseSchedule(
            **{
                **existing.to_dict(),
                "enabled": enabled,
                "next_run_at": (
                    to_iso(now + timedelta(seconds=existing.interval_seconds))
                    if enabled
                    else existing.next_run_at
                ),
                "updated_at": to_iso(now),
            }
        )
        self._replace(item)
        return item

    def remove(self, course_id: str) -> bool:
        schedules = self.list()
        remaining = [item for item in schedules if item.course_id != course_id]
        if len(remaining) == len(schedules):
            return False
        self._save(remaining)
        return True

    def due(
        self, now: Optional[datetime] = None, force_course_id: Optional[str] = None
    ) -> List[CourseSchedule]:
        current = now or self.now_fn()
        if force_course_id:
            return [self._require(force_course_id)]
        result = []
        for item in self.list():
            next_run = from_iso(item.next_run_at)
            if item.enabled and (next_run is None or next_run <= current):
                result.append(item)
        return result

    def mark_started(self, course_id: str) -> CourseSchedule:
        existing = self._require(course_id)
        now = self.now_fn()
        item = CourseSchedule(
            **{
                **existing.to_dict(),
                "next_run_at": to_iso(
                    now + timedelta(seconds=existing.interval_seconds)
                ),
                "updated_at": to_iso(now),
                "last_run_at": to_iso(now),
                "last_status": "running",
                "last_error": None,
            }
        )
        self._replace(item)
        return item

    def mark_finished(
        self,
        course_id: str,
        status: str,
        new_lessons: int = 0,
        error: Optional[str] = None,
    ) -> CourseSchedule:
        existing = self._require(course_id)
        now = self.now_fn()
        item = CourseSchedule(
            **{
                **existing.to_dict(),
                "updated_at": to_iso(now),
                "last_finished_at": to_iso(now),
                "last_status": status,
                "last_error": redact_message(error) if error else None,
                "last_new_lessons": max(0, int(new_lessons)),
            }
        )
        self._replace(item)
        return item

    def _require(self, course_id: str) -> CourseSchedule:
        item = self.get(course_id)
        if item is None:
            raise ValueError("尚未配置该课程的定时监控：{}".format(course_id))
        return item

    def _replace(self, item: CourseSchedule) -> None:
        remaining = [
            current for current in self.list() if current.course_id != item.course_id
        ]
        remaining.append(item)
        self._save(remaining)

    def _load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"version": SCHEDULE_VERSION, "schedules": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": SCHEDULE_VERSION, "schedules": []}
        if not isinstance(payload, dict):
            return {"version": SCHEDULE_VERSION, "schedules": []}
        return payload

    def _save(self, schedules: List[CourseSchedule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".partial")
        payload = {
            "version": SCHEDULE_VERSION,
            "schedules": [item.to_dict() for item in schedules],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


class WorkerLock:
    def __init__(
        self,
        path: Path,
        now_fn: Callable[[], datetime] = utc_now,
        stale_seconds: int = STALE_LOCK_SECONDS,
    ) -> None:
        self.path = path
        self.now_fn = now_fn
        self.stale_seconds = stale_seconds
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._is_stale():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        try:
            descriptor = os.open(
                str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "created_at": to_iso(self.now_fn())}, handle
            )
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False

    def _is_stale(self) -> bool:
        try:
            age = self.now_fn().timestamp() - self.path.stat().st_mtime
        except OSError:
            return False
        return age >= self.stale_seconds

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class ScheduledCourseExecutor:
    """Run auth validation and the existing incremental pipeline in child CLIs."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.database = Database(paths.database_file)
        self.database.initialize()

    def __call__(self, course_id: str) -> Dict[str, Any]:
        before = self._lesson_ids(course_id)
        auth = self._run_cli(["auth", "check", "--recover", "--json"])
        if auth.returncode != 0:
            raise RuntimeError(
                self._command_error(auth, "小鹅通登录检查未通过，后台任务已暂停。")
            )

        pipeline = self._run_cli(
            ["run", course_id, "--language", "zh", "--retry-timeout", "0", "--json"]
        )
        payload = self._json_output(pipeline.stdout)
        if pipeline.returncode not in {0, 1}:
            raise RuntimeError(
                self._command_error(pipeline, "课程后台处理启动失败。")
            )
        after = self._lesson_ids(course_id)
        status = str(payload.get("status") or "partial")
        return {
            "status": status,
            "new_lessons": len(after - before),
            "pipeline": payload,
        }

    def _run_cli(self, arguments: List[str]) -> subprocess.CompletedProcess:
        command = cli_arguments(self.paths, arguments)
        environment = os.environ.copy()
        source_dir = Path(__file__).resolve().parents[1]
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(source_dir)
            if not existing
            else str(source_dir) + os.pathsep + existing
        )
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def _lesson_ids(self, course_id: str) -> set:
        return {
            str(row["id"])
            for row in self.database.rows(
                "SELECT id FROM lessons WHERE course_id = ?", (course_id,)
            )
        }

    @staticmethod
    def _json_output(output: str) -> Dict[str, Any]:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _command_error(
        process: subprocess.CompletedProcess, fallback: str
    ) -> str:
        detail = (process.stderr or process.stdout or "").strip()
        return redact_message(detail[-1000:] if detail else fallback)


class CourseMonitorWorker:
    def __init__(
        self,
        store: ScheduleStore,
        lock_path: Path,
        log_path: Path,
        executor: Callable[[str], Dict[str, Any]],
    ) -> None:
        self.store = store
        self.lock_path = lock_path
        self.log_path = log_path
        self.executor = executor

    def run(self, force_course_id: Optional[str] = None) -> Dict[str, Any]:
        with WorkerLock(self.lock_path) as acquired:
            if not acquired:
                return {"status": "busy", "processed": 0, "items": []}
            due = self.store.due(force_course_id=force_course_id)
            items = []
            for schedule in due:
                self.store.mark_started(schedule.course_id)
                self._log("started", schedule.course_id)
                try:
                    result = self.executor(schedule.course_id)
                    status = str(result.get("status") or "completed")
                    new_lessons = int(result.get("new_lessons") or 0)
                    self.store.mark_finished(
                        schedule.course_id, status, new_lessons=new_lessons
                    )
                    item = {
                        "course_id": schedule.course_id,
                        "status": status,
                        "new_lessons": new_lessons,
                    }
                    self._log(status, schedule.course_id, new_lessons=new_lessons)
                except Exception as error:
                    message = redact_message(str(error))
                    self.store.mark_finished(
                        schedule.course_id, "failed", error=message
                    )
                    item = {
                        "course_id": schedule.course_id,
                        "status": "failed",
                        "new_lessons": 0,
                        "error": message,
                    }
                    self._log("failed", schedule.course_id, error=message)
                items.append(item)
            return {
                "status": (
                    "completed"
                    if all(item["status"] == "completed" for item in items)
                    else "partial"
                ),
                "processed": len(items),
                "items": items,
            }

    def recent_logs(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.log_path.is_file():
            return []
        rows = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows[-max(1, limit) :]

    def _log(
        self,
        status: str,
        course_id: str,
        new_lessons: int = 0,
        error: Optional[str] = None,
    ) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "time": to_iso(utc_now()),
            "course_id": course_id,
            "status": status,
            "new_lessons": max(0, new_lessons),
        }
        if error:
            payload["error"] = redact_message(error)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.chmod(self.log_path, 0o600)


def build_monitor_worker(paths: AppPaths) -> CourseMonitorWorker:
    return CourseMonitorWorker(
        store=ScheduleStore(paths.data_dir / "schedules.json"),
        lock_path=paths.data_dir / "scheduler.lock",
        log_path=paths.data_dir / "scheduler.log",
        executor=ScheduledCourseExecutor(paths),
    )
