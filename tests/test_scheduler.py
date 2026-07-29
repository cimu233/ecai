import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from xiaoe_core.scheduler import (
    CourseMonitorWorker,
    ScheduleStore,
    ScheduledCourseExecutor,
    WorkerLock,
    cli_arguments,
    format_interval,
    parse_interval,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class ScheduleStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = MutableClock()
        self.store = ScheduleStore(self.root / "schedules.json", self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_interval_parser_supports_minutes_hours_and_days(self) -> None:
        self.assertEqual(30 * 60, parse_interval("30m"))
        self.assertEqual(2 * 3600, parse_interval("2h"))
        self.assertEqual(86400, parse_interval("1d"))
        self.assertEqual("2 小时", format_interval(7200))
        with self.assertRaises(ValueError):
            parse_interval("0m")
        with self.assertRaises(ValueError):
            parse_interval("often")

    def test_child_command_supports_source_and_frozen_executables(self) -> None:
        from xiaoe_core.config import AppPaths

        paths = AppPaths.resolve(str(self.root / "data"))
        source = cli_arguments(paths, ["schedule", "worker"])
        self.assertIn("-m", source)
        with mock.patch.object(__import__("sys"), "frozen", True, create=True):
            frozen = cli_arguments(paths, ["schedule", "worker"])
        self.assertNotIn("-m", frozen)
        self.assertEqual("schedule", frozen[-2])

    def test_schedule_can_be_added_disabled_and_reenabled(self) -> None:
        item = self.store.upsert("course_1", parse_interval("30m"))
        self.assertTrue(item.enabled)
        self.assertEqual([], self.store.due())
        self.assertEqual(0o600, self.store.path.stat().st_mode & 0o777)

        disabled = self.store.set_enabled("course_1", False)
        self.assertFalse(disabled.enabled)
        self.clock.value += timedelta(hours=1)
        self.assertEqual([], self.store.due())

        enabled = self.store.set_enabled("course_1", True)
        self.assertTrue(enabled.enabled)
        self.clock.value += timedelta(minutes=30)
        self.assertEqual(["course_1"], [item.course_id for item in self.store.due()])

    def test_marking_started_advances_next_run_and_records_result(self) -> None:
        self.store.upsert("course_1", parse_interval("10m"))
        self.clock.value += timedelta(minutes=10)
        self.store.mark_started("course_1")
        running = self.store.get("course_1")
        self.assertEqual("running", running.last_status)
        self.assertEqual([], self.store.due())

        self.store.mark_finished("course_1", "completed", new_lessons=2)
        completed = self.store.get("course_1")
        self.assertEqual("completed", completed.last_status)
        self.assertEqual(2, completed.last_new_lessons)

    def test_force_run_returns_disabled_schedule(self) -> None:
        self.store.upsert("course_1", 600, enabled=False)
        self.assertEqual(
            ["course_1"],
            [item.course_id for item in self.store.due(force_course_id="course_1")],
        )


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = MutableClock()
        self.store = ScheduleStore(self.root / "schedules.json", self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_worker_runs_only_due_courses_and_records_logs(self) -> None:
        self.store.upsert("course_1", 60)
        self.store.upsert("course_2", 3600)
        self.clock.value += timedelta(minutes=1)
        calls = []

        def execute(course_id):
            calls.append(course_id)
            return {"status": "completed", "new_lessons": 3}

        worker = CourseMonitorWorker(
            self.store,
            self.root / "worker.lock",
            self.root / "scheduler.log",
            execute,
        )
        result = worker.run()
        self.assertEqual(["course_1"], calls)
        self.assertEqual(1, result["processed"])
        self.assertEqual(3, result["items"][0]["new_lessons"])
        self.assertEqual("completed", self.store.get("course_1").last_status)
        self.assertEqual("completed", worker.recent_logs()[-1]["status"])

    def test_worker_records_redacted_failures(self) -> None:
        self.store.upsert("course_1", 60)

        def fail(_course_id):
            raise RuntimeError(
                "failed https://media.example/audio.m3u8?token=secret "
                "Bearer private-token"
            )

        worker = CourseMonitorWorker(
            self.store,
            self.root / "worker.lock",
            self.root / "scheduler.log",
            fail,
        )
        result = worker.run(force_course_id="course_1")
        error = result["items"][0]["error"]
        self.assertNotIn("secret", error)
        self.assertNotIn("private-token", error)
        self.assertEqual("failed", self.store.get("course_1").last_status)

    def test_existing_lock_prevents_overlapping_worker(self) -> None:
        self.store.upsert("course_1", 60)
        lock_path = self.root / "worker.lock"
        lock_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        worker = CourseMonitorWorker(
            self.store,
            lock_path,
            self.root / "scheduler.log",
            lambda _course_id: {"status": "completed"},
        )
        self.assertEqual("busy", worker.run()["status"])

    def test_stale_lock_is_replaced(self) -> None:
        lock_path = self.root / "worker.lock"
        lock_path.write_text("old", encoding="utf-8")
        old = self.clock.value.timestamp() - 120
        os.utime(lock_path, (old, old))
        lock = WorkerLock(
            lock_path, now_fn=self.clock, stale_seconds=60
        )
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertFalse(lock_path.exists())


class ScheduledCourseExecutorTest(unittest.TestCase):
    def test_auth_is_checked_before_incremental_pipeline(self) -> None:
        from xiaoe_core.config import AppPaths

        with tempfile.TemporaryDirectory() as temporary:
            executor = ScheduledCourseExecutor(AppPaths.resolve(temporary))
            executor._lesson_ids = mock.Mock(
                side_effect=[{"lesson_old"}, {"lesson_old", "lesson_new"}]
            )
            executor._run_cli = mock.Mock(
                side_effect=[
                    subprocess.CompletedProcess(
                        ["auth"], 0, stdout='{"status":"authenticated"}', stderr=""
                    ),
                    subprocess.CompletedProcess(
                        ["run"],
                        0,
                        stdout='{"status":"completed","download":{"processed":1}}',
                        stderr="",
                    ),
                ]
            )

            result = executor("course_1")

            self.assertEqual("completed", result["status"])
            self.assertEqual(1, result["new_lessons"])
            self.assertEqual(
                ["auth", "check", "--recover", "--json"],
                executor._run_cli.call_args_list[0].args[0],
            )
            self.assertEqual(
                "run", executor._run_cli.call_args_list[1].args[0][0]
            )

    def test_failed_auth_stops_before_catalog_or_download(self) -> None:
        from xiaoe_core.config import AppPaths

        with tempfile.TemporaryDirectory() as temporary:
            executor = ScheduledCourseExecutor(AppPaths.resolve(temporary))
            executor._lesson_ids = mock.Mock(return_value=set())
            executor._run_cli = mock.Mock(
                return_value=subprocess.CompletedProcess(
                    ["auth"],
                    1,
                    stdout="",
                    stderr=(
                        "expired https://example.com/login?token=secret "
                        "Bearer private-token"
                    ),
                )
            )

            with self.assertRaises(RuntimeError) as raised:
                executor("course_1")

            self.assertEqual(1, executor._run_cli.call_count)
            self.assertNotIn("secret", str(raised.exception))
            self.assertNotIn("private-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
