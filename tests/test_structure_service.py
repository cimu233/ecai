import json
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.database import Database
from xiaoe_core.services import CourseService, LessonService
from xiaoe_core.structure_service import StructureService
from xiaoe_core.structurer import CodexCliStructurer, render_markdown


class FakeStructurer:
    name = "fake_structurer"

    def __init__(self):
        self.calls = 0

    def structure(self, transcript, title, work_dir):
        self.calls += 1
        return {
            "title": title,
            "summary": "课程概述",
            "sections": [{"heading": "第一部分", "content": transcript, "key_points": ["要点一"]}],
        }


class StructureServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        database = Database(root / "pipeline.sqlite3")
        self.courses = CourseService(database)
        self.lessons = LessonService(database)
        self.course = self.courses.add_course("https://school.example/course/1", "Course").course
        self.lesson = self.lessons.upsert(self.course.id, 1, "Lesson One")
        lesson_dir = root / "lesson"
        lesson_dir.mkdir()
        raw_path = lesson_dir / "transcript.raw.json"
        raw_path.write_text("{}", encoding="utf-8")
        self.lessons.begin_transcription(self.lesson.id, "fake", "fake-model")
        self.lessons.complete_transcription(
            self.lesson.id,
            "先提出问题，然后分析原因，最后得出结论。",
            [],
            str(raw_path),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_structure_writes_markdown_and_skips_completed_work(self):
        provider = FakeStructurer()
        service = StructureService(self.courses, self.lessons, provider)

        first = service.structure_course(self.course.id)
        second = service.structure_course(self.course.id)

        self.assertEqual(1, first.succeeded)
        self.assertEqual(1, second.skipped)
        self.assertEqual(1, provider.calls)
        markdown = Path(first.items[0].markdown_file).read_text(encoding="utf-8")
        self.assertIn("# Lesson One", markdown)
        self.assertIn("先提出问题", markdown)
        self.assertEqual("completed", self.lessons.get(self.lesson.id).status)

    def test_codex_runner_uses_read_only_ephemeral_mode(self):
        output = {
            "title": "Lesson",
            "summary": "Summary",
            "sections": [{"heading": "Part", "content": "Body", "key_points": []}],
        }

        def runner(command, **kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(output), encoding="utf-8")
            self.assertIn("--ephemeral", command)
            self.assertEqual("read-only", command[command.index("--sandbox") + 1])
            self.assertIn("<TRANSCRIPT>", kwargs["input"])

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        provider = CodexCliStructurer(executable="codex", runner=runner)
        data = provider.structure("Body", "Lesson", Path(self.temp_dir.name) / "codex")
        self.assertEqual("Lesson", data["title"])

    def test_markdown_renderer_is_deterministic(self):
        markdown = render_markdown({
            "title": "T",
            "summary": "S",
            "sections": [{"heading": "H", "content": "C", "key_points": ["K"]}],
        })
        self.assertEqual("# T\n\nS\n\n## H\n\nC\n\n### Key Points\n\n- K\n", markdown)


if __name__ == "__main__":
    unittest.main()
