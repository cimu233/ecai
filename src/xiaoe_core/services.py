"""Application services used by every user interface."""

import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .database import Database
from .models import AddCourseResult, Course, Lesson, PipelineStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_course_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Course URL must be an absolute HTTP or HTTPS URL.")
    return candidate


def course_from_row(row: object) -> Course:
    return Course(
        id=row["id"],
        source_url=row["source_url"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def lesson_from_row(row: object) -> Lesson:
    return Lesson(
        id=row["id"],
        course_id=row["course_id"],
        position=row["position"],
        title=row["title"],
        source_url=row["source_url"],
        status=row["status"],
        error=row["error"],
        attempt_count=row["attempt_count"],
        last_error_code=row["last_error_code"],
        last_error_at=row["last_error_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class CourseService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add_course(self, source_url: str, title: Optional[str] = None) -> AddCourseResult:
        clean_url = validate_course_url(source_url)
        existing = self.get_by_url(clean_url)
        if existing:
            return AddCourseResult(course=existing, created=False)

        parsed = urlparse(clean_url)
        clean_title = (title or parsed.netloc).strip()
        if not clean_title:
            raise ValueError("Course title cannot be empty.")

        course_id = "course_{}".format(uuid.uuid4().hex[:12])
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO courses (id, source_url, title, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending_catalog', ?, ?)
                """,
                (course_id, clean_url, clean_title, timestamp, timestamp),
            )
        course = self.get(course_id)
        if course is None:
            raise RuntimeError("Course was inserted but could not be loaded.")
        return AddCourseResult(course=course, created=True)

    def get(self, course_id: str) -> Optional[Course]:
        rows = list(self.database.rows("SELECT * FROM courses WHERE id = ?", (course_id,)))
        return course_from_row(rows[0]) if rows else None

    def get_by_url(self, source_url: str) -> Optional[Course]:
        rows = list(self.database.rows("SELECT * FROM courses WHERE source_url = ?", (source_url,)))
        return course_from_row(rows[0]) if rows else None

    def list_courses(self) -> List[Course]:
        rows = self.database.rows("SELECT * FROM courses ORDER BY created_at DESC, id DESC")
        return [course_from_row(row) for row in rows]

    def status(self) -> PipelineStatus:
        course_statuses = self._count_by_status("courses")
        lesson_statuses = self._count_by_status("lessons")
        active = next(
            (course.id for course in self.list_courses() if course.status not in {"completed", "failed"}),
            None,
        )
        return PipelineStatus(
            total_courses=sum(course_statuses.values()),
            course_statuses=course_statuses,
            total_lessons=sum(lesson_statuses.values()),
            lesson_statuses=lesson_statuses,
            active_course_id=active,
        )

    def _count_by_status(self, table: str) -> Dict[str, int]:
        if table not in {"courses", "lessons"}:
            raise ValueError("Unsupported status table.")
        rows = self.database.rows(
            "SELECT status, COUNT(*) AS count FROM {} GROUP BY status ORDER BY status".format(table)
        )
        return {row["status"]: row["count"] for row in rows}


class LessonService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def upsert(
        self,
        course_id: str,
        position: int,
        title: str,
        source_url: Optional[str] = None,
        lesson_id: Optional[str] = None,
    ) -> Lesson:
        if position < 1:
            raise ValueError("Lesson position must be at least 1.")
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Lesson title cannot be empty.")
        if not list(self.database.rows("SELECT id FROM courses WHERE id = ?", (course_id,))):
            raise ValueError("Course does not exist: {}".format(course_id))

        if lesson_id:
            resolved_id = lesson_id if lesson_id.startswith(course_id + "_") else "{}_{}".format(course_id, lesson_id)
            existing = list(self.database.rows("SELECT * FROM lessons WHERE id = ?", (resolved_id,)))
        else:
            resolved_id = "{}_lesson_{:04d}".format(course_id, position)
            existing = list(
                self.database.rows(
                    "SELECT * FROM lessons WHERE course_id = ? AND position = ?",
                    (course_id, position),
                )
            )
        timestamp = utc_now()
        if existing:
            current = lesson_from_row(existing[0])
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE lessons
                    SET position = ?, title = ?, source_url = COALESCE(?, source_url), updated_at = ?
                    WHERE id = ?
                    """,
                    (position, clean_title, source_url, timestamp, current.id),
                )
            loaded = self.get(current.id)
            if loaded is None:
                raise RuntimeError("Lesson was updated but could not be loaded.")
            return loaded

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO lessons (
                    id, course_id, position, title, source_url, status,
                    error, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending_source', NULL, 0, ?, ?)
                """,
                (resolved_id, course_id, position, clean_title, source_url, timestamp, timestamp),
            )
        loaded = self.get(resolved_id)
        if loaded is None:
            raise RuntimeError("Lesson was inserted but could not be loaded.")
        return loaded

    def get(self, lesson_id: str) -> Optional[Lesson]:
        rows = list(self.database.rows("SELECT * FROM lessons WHERE id = ?", (lesson_id,)))
        return lesson_from_row(rows[0]) if rows else None

    def list_for_download(
        self,
        course_id: str,
        lesson_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Lesson]:
        parameters = [course_id]
        query = "SELECT * FROM lessons WHERE course_id = ?"
        if lesson_id:
            query += " AND id = ?"
            parameters.append(lesson_id)
        query += " ORDER BY position, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("Download limit must be at least 1.")
            query += " LIMIT ?"
            parameters.append(limit)
        return [lesson_from_row(row) for row in self.database.rows(query, tuple(parameters))]

    def begin_attempt(self, lesson_id: str) -> None:
        self._update(
            lesson_id,
            "resolving_source",
            "attempt_count = attempt_count + 1, error = NULL, last_error_code = NULL, last_error_at = NULL",
        )

    def set_status(self, lesson_id: str, status: str) -> None:
        self._update(lesson_id, status, "error = NULL, last_error_code = NULL, last_error_at = NULL")

    def set_failure(self, lesson_id: str, status: str, code: str, message: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE lessons
                SET status = ?, error = ?, last_error_code = ?, last_error_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, message, code, timestamp, timestamp, lesson_id),
            )

    def record_audio_artifact(
        self,
        lesson_id: str,
        file_path: str,
        checksum: str,
        size_bytes: int,
        duration_seconds: Optional[float],
        container: str,
        codec: Optional[str],
    ) -> None:
        artifact_id = "artifact_{}_audio".format(lesson_id)
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, lesson_id, kind, file_path, checksum, size_bytes,
                    duration_seconds, container, codec, created_at
                ) VALUES (?, ?, 'audio_source', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lesson_id, kind) DO UPDATE SET
                    file_path = excluded.file_path,
                    checksum = excluded.checksum,
                    size_bytes = excluded.size_bytes,
                    duration_seconds = excluded.duration_seconds,
                    container = excluded.container,
                    codec = excluded.codec,
                    created_at = excluded.created_at
                """,
                (
                    artifact_id,
                    lesson_id,
                    file_path,
                    checksum,
                    size_bytes,
                    duration_seconds,
                    container,
                    codec,
                    timestamp,
                ),
            )

    def audio_artifact(self, lesson_id: str) -> Optional[object]:
        rows = list(
            self.database.rows(
                "SELECT * FROM artifacts WHERE lesson_id = ? AND kind = 'audio_source'",
                (lesson_id,),
            )
        )
        return rows[0] if rows else None

    def transcript(self, lesson_id: str) -> Optional[object]:
        rows = list(self.database.rows("SELECT * FROM transcripts WHERE lesson_id = ?", (lesson_id,)))
        return rows[0] if rows else None

    def begin_transcription(self, lesson_id: str, provider: str, model: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO transcripts (
                    lesson_id, provider, model, status, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, 'transcribing', 1, ?, ?)
                ON CONFLICT(lesson_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    status = 'transcribing',
                    error_code = NULL,
                    error = NULL,
                    attempt_count = transcripts.attempt_count + 1,
                    updated_at = excluded.updated_at
                """,
                (lesson_id, provider, model, timestamp, timestamp),
            )
        self.set_status(lesson_id, "transcribing")

    def complete_transcription(
        self,
        lesson_id: str,
        text: str,
        segments: List[Dict[str, object]],
        raw_file_path: str,
    ) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE transcripts
                SET status = 'transcript_ready', text = ?, segments_json = ?, raw_file_path = ?,
                    error_code = NULL, error = NULL, updated_at = ?
                WHERE lesson_id = ?
                """,
                (text, json.dumps(segments, ensure_ascii=False), raw_file_path, timestamp, lesson_id),
            )
        self.set_status(lesson_id, "transcript_ready")

    def fail_transcription(self, lesson_id: str, code: str, message: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE transcripts
                SET status = 'transcription_failed', error_code = ?, error = ?, updated_at = ?
                WHERE lesson_id = ?
                """,
                (code, message, timestamp, lesson_id),
            )
        self.set_failure(lesson_id, "transcription_failed", code, message)

    def structured_note(self, lesson_id: str) -> Optional[object]:
        rows = list(self.database.rows("SELECT * FROM structured_notes WHERE lesson_id = ?", (lesson_id,)))
        return rows[0] if rows else None

    def begin_structure(self, lesson_id: str, provider: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO structured_notes (
                    lesson_id, provider, status, attempt_count, created_at, updated_at
                ) VALUES (?, ?, 'structuring', 1, ?, ?)
                ON CONFLICT(lesson_id) DO UPDATE SET
                    provider = excluded.provider,
                    status = 'structuring',
                    error_code = NULL,
                    error = NULL,
                    attempt_count = structured_notes.attempt_count + 1,
                    updated_at = excluded.updated_at
                """,
                (lesson_id, provider, timestamp, timestamp),
            )
        self.set_status(lesson_id, "structuring")

    def complete_structure(self, lesson_id: str, markdown_file_path: str, data_file_path: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE structured_notes
                SET status = 'completed', markdown_file_path = ?, data_file_path = ?,
                    error_code = NULL, error = NULL, updated_at = ?
                WHERE lesson_id = ?
                """,
                (markdown_file_path, data_file_path, timestamp, lesson_id),
            )
        self.set_status(lesson_id, "completed")

    def fail_structure(self, lesson_id: str, code: str, message: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE structured_notes
                SET status = 'structure_failed', error_code = ?, error = ?, updated_at = ?
                WHERE lesson_id = ?
                """,
                (code, message, timestamp, lesson_id),
            )
        self.set_failure(lesson_id, "structure_failed", code, message)

    def _update(self, lesson_id: str, status: str, assignments: str) -> None:
        timestamp = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE lessons SET status = ?, {}, updated_at = ? WHERE id = ?".format(assignments),
                (status, timestamp, lesson_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Lesson does not exist: {}".format(lesson_id))
