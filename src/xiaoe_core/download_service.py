"""Lesson download orchestration and persistent state transitions."""

import hashlib
import re
from pathlib import Path
from typing import Optional

from .config import AppPaths
from .downloader import AudioDownloader, write_download_metadata
from .media import DownloadError, DownloadRequest, MediaResolver, MediaSelector
from .models import DownloadBatchResult, DownloadItemResult, Lesson
from .services import CourseService, LessonService


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

    def download_course(
        self,
        course_id: str,
        lesson_id: Optional[str] = None,
        limit: Optional[int] = None,
        positions: Optional[set] = None,
    ) -> DownloadBatchResult:
        if self.courses.get(course_id) is None:
            raise ValueError("Course does not exist: {}".format(course_id))
        lessons = self.lessons.list_for_download(course_id, lesson_id, limit, positions)
        if lesson_id and not lessons:
            raise ValueError("Lesson does not exist in course: {}".format(lesson_id))

        total = len(lessons)
        items = []
        for idx, lesson in enumerate(lessons, 1):
            existing = self.lessons.audio_artifact(lesson.id)
            if lesson.status == "audio_ready" and existing is not None and self._artifact_is_valid(existing):
                items.append(
                    DownloadItemResult(
                        lesson_id=lesson.id,
                        status="skipped",
                        file_path=existing["file_path"],
                    )
                )
                continue
            self.downloader.set_progress_context(idx, total)
            items.append(self._download_lesson(lesson))

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

    def _download_lesson(self, lesson: Lesson) -> DownloadItemResult:
        self.lessons.begin_attempt(lesson.id)
        for force_refresh in (False, True):
            try:
                source = self.selector.select(self.resolver.resolve(lesson, force_refresh=force_refresh))
                self.lessons.set_status(lesson.id, "downloading")
                output_dir = (
                    self.paths.courses_dir
                    / self._safe_component(lesson.course_id)
                    / "lessons"
                    / self._safe_component(lesson.id)
                )
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
                self.lessons.set_status(lesson.id, "audio_ready")
                return DownloadItemResult(
                    lesson_id=lesson.id,
                    status="audio_ready",
                    file_path=str(result.file_path),
                )
            except DownloadError as error:
                if error.code == "source_expired" and not force_refresh:
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
        if not path.is_file() or path.stat().st_size != row["size_bytes"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest() == row["checksum"]
