"""Persistent transcript-to-notes orchestration."""

import json
from pathlib import Path
from typing import Optional

from .models import StructureBatchResult, StructureItemResult
from .services import CourseService, LessonService
from .structurer import StructureError, StructureProvider, render_markdown


class StructureService:
    def __init__(self, courses: CourseService, lessons: LessonService, provider: StructureProvider) -> None:
        self.courses = courses
        self.lessons = lessons
        self.provider = provider

    def structure_course(
        self, course_id: str, lesson_id: Optional[str] = None, limit: Optional[int] = None, force: bool = False
    ) -> StructureBatchResult:
        if self.courses.get(course_id) is None:
            raise ValueError("Course does not exist: {}".format(course_id))
        lessons = self.lessons.list_for_download(course_id, lesson_id, limit)
        if lesson_id and not lessons:
            raise ValueError("Lesson does not exist in course: {}".format(lesson_id))
        items = [self._structure_lesson(lesson.id, lesson.title, force) for lesson in lessons]
        succeeded = sum(item.status == "completed" for item in items)
        skipped = sum(item.status == "skipped" for item in items)
        return StructureBatchResult(
            course_id, len(items), succeeded, skipped, len(items) - succeeded - skipped, items
        )

    def _structure_lesson(self, lesson_id: str, title: str, force: bool) -> StructureItemResult:
        existing = self.lessons.structured_note(lesson_id)
        if not force and existing is not None and existing["status"] == "completed":
            markdown = Path(existing["markdown_file_path"])
            if markdown.is_file():
                return StructureItemResult(lesson_id, "skipped", str(markdown))
        transcript = self.lessons.transcript(lesson_id)
        if transcript is None or transcript["status"] != "transcript_ready":
            return StructureItemResult(
                lesson_id, "transcript_missing", error_code="transcript_missing", error="Transcribe audio first."
            )
        work_dir = Path(transcript["raw_file_path"]).parent
        self.lessons.begin_structure(lesson_id, self.provider.name)
        try:
            data = self.provider.structure(transcript["text"], title, work_dir)
            data_path = work_dir / "notes.data.json"
            markdown_path = work_dir / "notes.md"
            self._atomic_write(data_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            self._atomic_write(markdown_path, render_markdown(data))
            self.lessons.complete_structure(lesson_id, str(markdown_path), str(data_path))
            return StructureItemResult(lesson_id, "completed", str(markdown_path))
        except StructureError as error:
            self.lessons.fail_structure(lesson_id, error.code, str(error))
            return StructureItemResult(lesson_id, "structure_failed", error_code=error.code, error=str(error))

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
