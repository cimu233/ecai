import tempfile
import unittest
from pathlib import Path

from xiaoe_core.database import Database
from xiaoe_core.services import CourseService, LessonService


class CourseServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.service = CourseService(Database(database_path))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_add_course_is_idempotent(self) -> None:
        first = self.service.add_course("https://school.example.com/course/1", "Course One")
        second = self.service.add_course("https://school.example.com/course/1", "Ignored Title")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.course.id, second.course.id)
        self.assertEqual("Course One", second.course.title)

    def test_invalid_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            self.service.add_course("school.example.com/course/1")

    def test_status_counts_courses(self) -> None:
        course = self.service.add_course("https://school.example.com/course/1", "Course One").course
        status = self.service.status()

        self.assertEqual(1, status.total_courses)
        self.assertEqual({"pending_catalog": 1}, status.course_statuses)
        self.assertEqual(course.id, status.active_course_id)
        self.assertEqual(0, status.total_lessons)

    def test_lesson_ids_are_unique_across_courses(self) -> None:
        first_course = self.service.add_course("https://school.example.com/course/1", "Course One").course
        second_course = self.service.add_course("https://school.example.com/course/2", "Course Two").course
        lessons = LessonService(self.service.database)

        first = lessons.upsert(first_course.id, 1, "Lesson One")
        second = lessons.upsert(second_course.id, 1, "Lesson One")

        self.assertNotEqual(first.id, second.id)
        self.assertTrue(first.id.startswith(first_course.id + "_"))
        self.assertTrue(second.id.startswith(second_course.id + "_"))

    def test_stable_lesson_id_survives_position_change(self) -> None:
        course = self.service.add_course("https://school.example.com/course/1", "Course").course
        lessons = LessonService(self.service.database)
        first = lessons.upsert(course.id, 2, "Lesson", lesson_id="resource_r1")
        with self.service.database.connect() as connection:
            connection.execute("UPDATE lessons SET position = -position WHERE course_id = ?", (course.id,))
        moved = lessons.upsert(course.id, 1, "Lesson updated", lesson_id="resource_r1")
        self.assertEqual(first.id, moved.id)
        self.assertEqual(1, moved.position)
        self.assertEqual("Lesson updated", moved.title)

    def test_media_hint_change_reactivates_previous_text_lesson(self) -> None:
        course = self.service.add_course(
            "https://school.example.com/course/1", "Course"
        ).course
        lessons = LessonService(self.service.database)
        text = lessons.upsert(
            course.id,
            1,
            "Announcement",
            lesson_id="resource_1",
            content_type="text",
            media_hint="no_media",
        )
        self.assertEqual("no_media", text.status)

        video = lessons.upsert(
            course.id,
            1,
            "Video lesson",
            lesson_id="resource_1",
            content_type="video",
            media_hint="has_media",
        )

        self.assertEqual("pending_source", video.status)
        self.assertEqual("video", video.content_type)
        self.assertEqual("has_media", video.media_hint)


if __name__ == "__main__":
    unittest.main()
