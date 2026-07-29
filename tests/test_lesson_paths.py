import hashlib
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.lesson_paths import (
    lesson_directory_names,
    lessons_by_date,
    migrate_course_lesson_directories,
)
from xiaoe_core.services import CourseService, LessonService


class LessonPathsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths.resolve(self.temp_dir.name)
        self.paths.create()
        database = Database(self.paths.database_file)
        self.courses = CourseService(database)
        self.lessons = LessonService(database)
        self.course = self.courses.add_course(
            "https://school.example/course/1", "Course"
        ).course

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_names_sort_by_date_and_keep_human_titles(self):
        newer = self.lessons.upsert(
            self.course.id, 1, "「音频补档」2025.11.30 有时候自由"
        )
        older = self.lessons.upsert(
            self.course.id, 2, "2024/8/4 再谈 PUA"
        )
        undated = self.lessons.upsert(
            self.course.id, 3, "加群须知"
        )

        names = lesson_directory_names([newer, older, undated])

        self.assertEqual(
            "2025-11-30 「音频补档」 有时候自由", names[newer.id]
        )
        self.assertEqual("2024-08-04 再谈 PUA", names[older.id])
        self.assertEqual("无日期-003 加群须知", names[undated.id])
        self.assertEqual(
            [names[older.id], names[newer.id], names[undated.id]],
            sorted(names.values()),
        )
        self.assertEqual(
            [older.id, newer.id, undated.id],
            [lesson.id for lesson in lessons_by_date([newer, undated, older])],
        )

    def test_duplicate_titles_receive_stable_lesson_suffixes(self):
        first = self.lessons.upsert(
            self.course.id, 1, "2025.11.30 同名课程"
        )
        second = self.lessons.upsert(
            self.course.id, 2, "2025.11.30 同名课程"
        )

        names = lesson_directory_names([first, second])

        self.assertTrue(names[first.id].endswith("(第001节)"))
        self.assertTrue(names[second.id].endswith("(第002节)"))

    def test_migration_moves_files_and_updates_every_database_path(self):
        lesson = self.lessons.upsert(
            self.course.id, 1, "2025.9.21 关于买得起和配得上"
        )
        legacy = (
            self.paths.courses_dir
            / self.course.id
            / "lessons"
            / lesson.id
        )
        legacy.mkdir(parents=True)
        audio = legacy / "audio.source.m4a"
        audio.write_bytes(b"audio")
        transcript_raw = legacy / "transcript.raw.json"
        transcript_raw.write_text("{}", encoding="utf-8")
        transcript_text = legacy / "transcript.txt"
        transcript_text.write_text("text", encoding="utf-8")
        notes = legacy / "notes.md"
        notes.write_text("# notes", encoding="utf-8")
        notes_data = legacy / "notes.data.json"
        notes_data.write_text("{}", encoding="utf-8")

        self.lessons.record_audio_artifact(
            lesson.id,
            str(audio),
            hashlib.sha256(audio.read_bytes()).hexdigest(),
            audio.stat().st_size,
            1.0,
            "m4a",
            "aac",
        )
        self.lessons.begin_transcription(lesson.id, "fake", "fake")
        self.lessons.complete_transcription(
            lesson.id, "text", [], str(transcript_raw)
        )
        self.lessons.begin_structure(lesson.id, "fake")
        self.lessons.complete_structure(
            lesson.id, str(notes), str(notes_data)
        )

        moved = migrate_course_lesson_directories(
            self.paths, self.lessons, self.course.id
        )

        target = legacy.parent / "2025-09-21 关于买得起和配得上"
        self.assertEqual([(legacy, target)], moved)
        self.assertFalse(legacy.exists())
        self.assertEqual(b"audio", (target / audio.name).read_bytes())
        self.assertEqual(
            str(target / audio.name),
            self.lessons.audio_artifact(lesson.id)["file_path"],
        )
        self.assertEqual(
            str(target / transcript_raw.name),
            self.lessons.transcript(lesson.id)["raw_file_path"],
        )
        note = self.lessons.structured_note(lesson.id)
        self.assertEqual(str(target / notes.name), note["markdown_file_path"])
        self.assertEqual(str(target / notes_data.name), note["data_file_path"])
        self.assertEqual(
            [], migrate_course_lesson_directories(
                self.paths, self.lessons, self.course.id
            )
        )


if __name__ == "__main__":
    unittest.main()
