"""Human-readable lesson directory names and safe legacy migration."""

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import AppPaths
from .models import Lesson
from .path_reports import write_stage_report
from .services import LessonService


DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[./\-年](?P<month>\d{1,2})[./\-月](?P<day>\d{1,2})(?:日)?"
)


def lesson_directory_names(lessons: Iterable[Lesson]) -> Dict[str, str]:
    prepared: List[Tuple[Lesson, str]] = []
    counts: Dict[str, int] = {}
    for lesson in lessons:
        base = _lesson_directory_base(lesson)
        prepared.append((lesson, base))
        counts[base] = counts.get(base, 0) + 1
    return {
        lesson.id: (
            "{} (第{:03d}节)".format(base, lesson.position)
            if counts[base] > 1
            else base
        )
        for lesson, base in prepared
    }


def lessons_by_date(lessons: Iterable[Lesson]) -> List[Lesson]:
    return sorted(lessons, key=_lesson_date_sort_key)


def lesson_output_directory(
    paths: AppPaths,
    lesson: Lesson,
    all_lessons: Optional[Iterable[Lesson]] = None,
) -> Path:
    lessons = list(all_lessons) if all_lessons is not None else [lesson]
    name = lesson_directory_names(lessons).get(lesson.id) or _lesson_directory_base(lesson)
    return paths.courses_dir / lesson.course_id / "lessons" / name


def migrate_course_lesson_directories(
    paths: AppPaths,
    lessons_service: LessonService,
    course_id: str,
) -> List[Tuple[Path, Path]]:
    lessons = lessons_by_date(lessons_service.list_for_download(course_id))
    names = lesson_directory_names(lessons)
    root = paths.courses_dir / course_id / "lessons"
    root.mkdir(parents=True, exist_ok=True)
    moves: List[Tuple[Lesson, Path, Path]] = []

    for lesson in lessons:
        target = root / names[lesson.id]
        sources = _existing_lesson_directories(root, lesson, lessons_service)
        source = next((candidate for candidate in sources if candidate != target), None)
        if source is None:
            continue
        if target.exists():
            raise RuntimeError(
                "Lesson directory migration conflict: {} -> {}".format(source, target)
            )
        moves.append((lesson, source, target))

    target_paths = [target for _, _, target in moves]
    if len(set(target_paths)) != len(target_paths):
        raise RuntimeError("Lesson directory migration produced duplicate targets.")

    completed: List[Tuple[Path, Path]] = []
    for lesson, source, target in moves:
        source.rename(target)
        try:
            _update_database_paths(lessons_service, lesson.id, source, target)
        except Exception:
            target.rename(source)
            raise
        completed.append((source, target))
    return completed


def migrate_all_lesson_directories(
    paths: AppPaths,
    lessons_service: LessonService,
    course_ids: Iterable[str],
) -> List[Tuple[Path, Path]]:
    completed: List[Tuple[Path, Path]] = []
    for course_id in course_ids:
        completed.extend(
            migrate_course_lesson_directories(paths, lessons_service, course_id)
        )
    return completed


def rebuild_course_path_reports(
    paths: AppPaths,
    lessons_service: LessonService,
    course_id: str,
) -> None:
    lessons = lessons_service.list_for_download(course_id)

    def audio_path(lesson_id: str) -> Optional[str]:
        row = lessons_service.audio_artifact(lesson_id)
        if row is None or not Path(row["file_path"]).is_file():
            return None
        return str(row["file_path"])

    def transcript_path(lesson_id: str) -> Optional[str]:
        row = lessons_service.transcript(lesson_id)
        if row is None or row["status"] != "transcript_ready":
            return None
        path = Path(row["raw_file_path"]).with_name("transcript.txt")
        return str(path) if path.is_file() else None

    def structure_path(lesson_id: str) -> Optional[str]:
        row = lessons_service.structured_note(lesson_id)
        if row is None or row["status"] != "completed":
            return None
        path = Path(row["markdown_file_path"])
        return str(path) if path.is_file() else None

    for stage, resolver in (
        ("audio", audio_path),
        ("transcript", transcript_path),
        ("structure", structure_path),
    ):
        write_stage_report(
            paths.courses_dir,
            course_id,
            stage,
            (
                (lesson.position, lesson.title, resolver(lesson.id))
                for lesson in lessons
            ),
        )


def _lesson_directory_base(lesson: Lesson) -> str:
    title = unicodedata.normalize("NFC", lesson.title).strip()
    match = DATE_PATTERN.search(title)
    if match:
        try:
            parsed = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            prefix = parsed.isoformat()
            remainder = (title[:match.start()] + " " + title[match.end():]).strip()
            remainder = re.sub(r"^[\s._\-—:：]+|[\s._\-—:：]+$", "", remainder)
            readable = "{} {}".format(prefix, remainder or title)
        except ValueError:
            readable = "无日期-{:03d} {}".format(lesson.position, title)
    else:
        readable = "无日期-{:03d} {}".format(lesson.position, title)
    return _safe_human_component(readable)


def _lesson_date_sort_key(lesson: Lesson) -> Tuple[int, str, int, str]:
    match = DATE_PATTERN.search(lesson.title)
    if match:
        try:
            parsed = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            return (0, parsed.isoformat(), lesson.position, lesson.title)
        except ValueError:
            pass
    return (1, "", lesson.position, lesson.title)


def _safe_human_component(value: str) -> str:
    cleaned = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cs"}
    )
    # Windows rejects \ / : * ? " < > | in a path component; macOS only rejects
    # the separator. Map each to its fullwidth twin so the name stays readable.
    for illegal, replacement in (
        ("/", "／"), ("\\", "＼"), (":", "："), ("*", "＊"),
        ("?", "？"), ('"', "＂"), ("<", "＜"), (">", "＞"), ("|", "｜"),
    ):
        cleaned = cleaned.replace(illegal, replacement)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:156].rstrip(" .") or "未命名课程")


def _existing_lesson_directories(
    root: Path,
    lesson: Lesson,
    lessons_service: LessonService,
) -> List[Path]:
    candidates: List[Path] = []
    artifact = lessons_service.audio_artifact(lesson.id)
    transcript = lessons_service.transcript(lesson.id)
    note = lessons_service.structured_note(lesson.id)
    stored_paths = [
        artifact["file_path"] if artifact is not None else None,
        transcript["raw_file_path"] if transcript is not None else None,
        note["markdown_file_path"] if note is not None else None,
        note["data_file_path"] if note is not None else None,
    ]
    for stored in stored_paths:
        if stored:
            parent = Path(str(stored)).parent
            if parent.is_dir() and parent not in candidates:
                candidates.append(parent)
    legacy = root / lesson.id
    if legacy.is_dir() and legacy not in candidates:
        candidates.append(legacy)
    return candidates


def _update_database_paths(
    lessons_service: LessonService,
    lesson_id: str,
    source: Path,
    target: Path,
) -> None:
    def moved(path_value: Optional[str]) -> Optional[str]:
        if not path_value:
            return path_value
        path = Path(path_value)
        try:
            relative = path.relative_to(source)
        except ValueError:
            return path_value
        return str(target / relative)

    artifact = lessons_service.audio_artifact(lesson_id)
    transcript = lessons_service.transcript(lesson_id)
    note = lessons_service.structured_note(lesson_id)
    with lessons_service.database.connect() as connection:
        if artifact is not None:
            connection.execute(
                "UPDATE artifacts SET file_path = ? WHERE lesson_id = ? AND kind = 'audio_source'",
                (moved(artifact["file_path"]), lesson_id),
            )
        if transcript is not None:
            connection.execute(
                "UPDATE transcripts SET raw_file_path = ? WHERE lesson_id = ?",
                (moved(transcript["raw_file_path"]), lesson_id),
            )
        if note is not None:
            connection.execute(
                """
                UPDATE structured_notes
                SET markdown_file_path = ?, data_file_path = ?
                WHERE lesson_id = ?
                """,
                (
                    moved(note["markdown_file_path"]),
                    moved(note["data_file_path"]),
                    lesson_id,
                ),
            )
