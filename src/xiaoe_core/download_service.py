"""Lesson download orchestration and persistent state transitions."""

import hashlib
import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from .browser import BrowserError
from .config import AppPaths
from .downloader import AudioDownloader, write_download_metadata
from .media import DownloadError, DownloadRequest, MediaResolver, MediaSelector, MediaSource
from .models import DownloadBatchResult, DownloadItemResult, Lesson
from .progress import ProgressSpinner, finish_progress
from .services import CourseService, LessonService
from .source_cache import MediaSourceCache


class DownloadService:
    def __init__(
        self,
        paths: AppPaths,
        courses: CourseService,
        lessons: LessonService,
        resolver: MediaResolver,
        selector: MediaSelector,
        downloader: AudioDownloader,
    ) -> None:
        self.paths = paths
        self.courses = courses
        self.lessons = lessons
        self.resolver = resolver
        self.selector = selector
        self.downloader = downloader
        self.source_cache = MediaSourceCache()
        self._resolver_lock = threading.Lock()

    def download_course(
        self,
        course_id: str,
        lesson_id: Optional[str] = None,
        limit: Optional[int] = None,
        positions: Optional[set] = None,
    ) -> DownloadBatchResult:
        if self.courses.get(course_id) is None:
            raise ValueError("Course does not exist: {}".format(course_id))
        self.lessons.recover_legacy_browser_failures(course_id)
        lessons = self.lessons.list_for_download(course_id, lesson_id, limit, positions)
        if lesson_id and not lessons:
            raise ValueError("Lesson does not exist in course: {}".format(lesson_id))

        total = len(lessons)
        items = []
        prefetch_future: Optional[Future] = None
        prefetch_lesson_id: Optional[str] = None
        prefetched_ids = set()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-prefetch")
        try:
            for idx, lesson in enumerate(lessons, 1):
                if prefetch_future is not None and prefetch_lesson_id == lesson.id:
                    prefetch_future.result()
                    prefetch_future = None
                    prefetch_lesson_id = None

                existing = self._existing_artifact(lesson)
                if existing is not None:
                    items.append(
                        DownloadItemResult(
                            lesson_id=lesson.id,
                            status="skipped",
                            file_path=existing["file_path"],
                        )
                    )
                    if self.downloader.show_progress:
                        finish_progress(
                            "  [{}/{}] 已跳过 ✓  {}（本地音频完整）".format(
                                idx, total, lesson.title[:40]
                            )
                        )
                    continue

                next_lesson = self._next_download_candidate(lessons, idx)

                def start_prefetch() -> None:
                    nonlocal prefetch_future, prefetch_lesson_id
                    if next_lesson is None or next_lesson.id in prefetched_ids:
                        return
                    prefetched_ids.add(next_lesson.id)
                    prefetch_lesson_id = next_lesson.id
                    prefetch_future = executor.submit(self._prefetch_source, next_lesson)

                self.downloader.set_progress_context(idx, total)
                item = self._download_lesson(
                    lesson,
                    idx,
                    total,
                    on_source_ready=start_prefetch,
                )
                items.append(item)
                if self.downloader.show_progress and item.status != "audio_ready":
                    label = {
                        "source_unavailable": "无音频源",
                        "unsupported_drm": "不支持的 DRM",
                        "download_failed": "下载失败",
                    }.get(item.status, item.status)
                    finish_progress(
                        "  [{}/{}] {} ✗  {}{}".format(
                            idx,
                            total,
                            label,
                            lesson.title[:40],
                            "：" + item.error if item.error else "",
                        )
                    )
        finally:
            executor.shutdown(wait=True)

        succeeded = sum(item.status == "audio_ready" for item in items)
        skipped = sum(item.status == "skipped" for item in items)
        failed = len(items) - succeeded - skipped
        return DownloadBatchResult(
            course_id=course_id,
            processed=len(items),
            succeeded=succeeded,
            skipped=skipped,
            failed=failed,
            items=items,
        )

    def _download_lesson(
        self,
        lesson: Lesson,
        progress_index: int = 0,
        progress_total: int = 0,
        on_source_ready: Optional[Callable[[], None]] = None,
    ) -> DownloadItemResult:
        self.lessons.begin_attempt(lesson.id)
        for force_refresh in (False, True):
            try:
                with ProgressSpinner(
                    "  [{}/{}] 正在解析音频源：{}".format(
                        progress_index, progress_total, lesson.title[:32]
                    ),
                    enabled=None if self.downloader.show_progress else False,
                ):
                    source = self._resolve_source(lesson, force_refresh=force_refresh)
                if on_source_ready is not None:
                    on_source_ready()
                self.lessons.set_status(lesson.id, "downloading")
                output_dir = self._lesson_output_dir(lesson)
                result = self.downloader.download(
                    DownloadRequest(lesson=lesson, source=source, output_dir=output_dir),
                    on_stage=lambda stage: self.lessons.set_status(lesson.id, stage),
                )
                write_download_metadata(result, output_dir / "download.json")
                self.lessons.record_audio_artifact(
                    lesson_id=lesson.id,
                    file_path=str(result.file_path),
                    checksum=result.sha256,
                    size_bytes=result.size_bytes,
                    duration_seconds=result.duration_seconds,
                    container=result.container,
                    codec=result.codec,
                )
                self.source_cache.invalidate(self._source_cache_path(lesson))
                self.lessons.set_status(lesson.id, "audio_ready")
                return DownloadItemResult(
                    lesson_id=lesson.id,
                    status="audio_ready",
                    file_path=str(result.file_path),
                )
            except DownloadError as error:
                if error.code == "source_expired" and not force_refresh:
                    self.source_cache.invalidate(self._source_cache_path(lesson))
                    self.lessons.set_status(lesson.id, "resolving_source")
                    continue
                status = error.code if error.code in {"source_unavailable", "unsupported_drm"} else "download_failed"
                self.lessons.set_failure(lesson.id, status, error.code, str(error))
                return DownloadItemResult(
                    lesson_id=lesson.id,
                    status=status,
                    error_code=error.code,
                    error=str(error),
                )
            except BrowserError:
                self.lessons.set_status(lesson.id, "pending_source")
                raise
            except Exception as error:
                message = "Unexpected download failure: {}".format(type(error).__name__)
                self.lessons.set_failure(lesson.id, "download_failed", "unexpected_error", message)
                return DownloadItemResult(
                    lesson_id=lesson.id,
                    status="download_failed",
                    error_code="unexpected_error",
                    error=message,
                )
        raise RuntimeError("Download refresh loop ended unexpectedly.")

    def _resolve_source(
        self, lesson: Lesson, force_refresh: bool = False
    ) -> MediaSource:
        cache_path = self._source_cache_path(lesson)
        if force_refresh:
            self.source_cache.invalidate(cache_path)
        else:
            cached = self.source_cache.load(cache_path, lesson)
            if cached is not None:
                return cached

        with self._resolver_lock:
            if not force_refresh:
                cached = self.source_cache.load(cache_path, lesson)
                if cached is not None:
                    return cached
            source = self.selector.select(
                self.resolver.resolve(lesson, force_refresh=force_refresh)
            )
            self.source_cache.save(cache_path, lesson, source)
            return source

    def _prefetch_source(self, lesson: Lesson) -> bool:
        try:
            self._resolve_source(lesson)
            return True
        except (BrowserError, DownloadError, OSError):
            return False

    def _next_download_candidate(self, lessons, current_index: int) -> Optional[Lesson]:
        for lesson in lessons[current_index:]:
            if self._existing_artifact(lesson) is None:
                return lesson
        return None

    def _existing_artifact(self, lesson: Lesson) -> Optional[object]:
        existing = self.lessons.audio_artifact(lesson.id)
        if existing is None:
            existing = self._recover_local_artifact(lesson)
        return existing if existing is not None and self._artifact_is_valid(existing) else None

    @staticmethod
    def _safe_component(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        if cleaned == value and cleaned and len(cleaned) <= 100:
            return cleaned
        if cleaned:
            return "{}_{}".format(cleaned[:80], digest)
        return "item_{}".format(digest)

    @staticmethod
    def _artifact_is_valid(row: object) -> bool:
        path = Path(row["file_path"])
        return (
            path.is_file()
            and int(row["size_bytes"] or 0) > 0
            and path.stat().st_size == row["size_bytes"]
        )

    def _lesson_output_dir(self, lesson: Lesson) -> Path:
        return (
            self.paths.courses_dir
            / self._safe_component(lesson.course_id)
            / "lessons"
            / self._safe_component(lesson.id)
        )

    def _source_cache_path(self, lesson: Lesson) -> Path:
        return self._lesson_output_dir(lesson) / "media-source.json"

    def _recover_local_artifact(self, lesson: Lesson) -> Optional[object]:
        output_dir = self._lesson_output_dir(lesson)
        metadata_path = output_dir / "download.json"
        if not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            file_name = str(metadata["file"])
            candidate = output_dir / file_name
            size_bytes = int(metadata["size_bytes"])
            checksum = str(metadata["sha256"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            candidate.parent != output_dir
            or not candidate.name.startswith("audio.source.")
            or not candidate.is_file()
            or size_bytes <= 0
            or candidate.stat().st_size != size_bytes
            or len(checksum) != 64
        ):
            return None
        self.lessons.record_audio_artifact(
            lesson_id=lesson.id,
            file_path=str(candidate),
            checksum=checksum,
            size_bytes=size_bytes,
            duration_seconds=metadata.get("duration_seconds"),
            container=str(metadata.get("container") or candidate.suffix.lstrip(".")),
            codec=metadata.get("codec"),
        )
        return self.lessons.audio_artifact(lesson.id)
