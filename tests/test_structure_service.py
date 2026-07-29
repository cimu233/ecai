import json
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.database import Database
from xiaoe_core.services import CourseService, LessonService
from xiaoe_core.structure_service import StructureService
from xiaoe_core.structurer import (
    AnthropicStructurer,
    ClaudeCodeStructurer,
    CodexCliStructurer,
    DEFAULT_PROMPT_TEMPLATE,
    OpenAICompatibleStructurer,
    OpenAIResponsesStructurer,
    build_structure_prompt,
    render_markdown,
)


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

    def test_catalog_declared_text_lesson_skips_structuring(self):
        self.lessons.upsert(
            self.course.id,
            2,
            "Announcement",
            content_type="text",
            media_hint="no_media",
        )
        provider = FakeStructurer()
        service = StructureService(
            self.courses, self.lessons, provider, show_progress=False
        )

        result = service.structure_course(self.course.id)

        self.assertEqual(1, result.succeeded)
        self.assertEqual(1, result.skipped)
        self.assertEqual(1, provider.calls)

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
            self.assertIn('model_reasoning_effort="high"', command)
            self.assertIn("<TRANSCRIPT>", kwargs["input"])

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        provider = CodexCliStructurer(
            executable="codex", effort="high", runner=runner
        )
        data = provider.structure("Body", "Lesson", Path(self.temp_dir.name) / "codex")
        self.assertEqual("Lesson", data["title"])

    def test_claude_code_uses_schema_without_tools_or_session_persistence(self):
        output = {
            "title": "Lesson",
            "summary": "Summary",
            "sections": [{"heading": "Part", "content": "Body", "key_points": []}],
        }

        def runner(command, **kwargs):
            self.assertIn("--json-schema", command)
            self.assertEqual("", command[command.index("--tools") + 1])
            self.assertEqual("3", command[command.index("--max-turns") + 1])
            self.assertEqual("dontAsk", command[command.index("--permission-mode") + 1])
            self.assertEqual("medium", command[command.index("--effort") + 1])
            self.assertIn("--no-session-persistence", command)
            self.assertIn("<TRANSCRIPT>", kwargs["input"])

            class Result:
                returncode = 0
                stdout = json.dumps({"structured_output": output})
                stderr = ""

            return Result()

        provider = ClaudeCodeStructurer(
            executable="claude", effort="medium", runner=runner
        )
        data = provider.structure("Body", "Lesson", Path(self.temp_dir.name) / "claude")
        self.assertEqual("Lesson", data["title"])

    def test_openai_responses_disables_storage_and_uses_strict_schema(self):
        output = {
            "title": "Lesson",
            "summary": "Summary",
            "sections": [{"heading": "Part", "content": "Body", "key_points": []}],
        }

        class Http:
            def request(self, url, headers, payload, timeout=300.0):
                self.url = url
                self.headers = headers
                self.payload = payload
                return {"output_text": json.dumps(output)}

        http = Http()
        provider = OpenAIResponsesStructurer(
            "secret", "gpt-test", effort="high", http=http
        )
        data = provider.structure("Body", "Lesson", Path(self.temp_dir.name))
        self.assertEqual("Lesson", data["title"])
        self.assertTrue(http.url.endswith("/responses"))
        self.assertFalse(http.payload["store"])
        self.assertTrue(http.payload["text"]["format"]["strict"])
        self.assertEqual("high", http.payload["reasoning"]["effort"])
        self.assertNotIn("secret", json.dumps(http.payload))

    def test_openai_compatible_and_anthropic_normalize_structured_output(self):
        output = {
            "title": "Lesson",
            "summary": "Summary",
            "sections": [{"heading": "Part", "content": "Body", "key_points": []}],
        }

        class CompatibleHttp:
            def request(self, url, headers, payload, timeout=300.0):
                self.url = url
                self.payload = payload
                return {"choices": [{"message": {"content": json.dumps(output)}}]}

        compatible_http = CompatibleHttp()
        compatible = OpenAICompatibleStructurer(
            "deepseek_api", "secret", "deepseek-chat",
            "https://api.deepseek.com/v1", http=compatible_http
        )
        self.assertEqual(
            "Lesson",
            compatible.structure("Body", "Lesson", Path(self.temp_dir.name))["title"],
        )
        self.assertTrue(compatible_http.url.endswith("/chat/completions"))
        self.assertEqual("json_object", compatible_http.payload["response_format"]["type"])

        alibaba_http = CompatibleHttp()
        alibaba = OpenAICompatibleStructurer(
            "alibaba_api",
            "secret",
            "qwen-plus",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            effort="high",
            effort_style="alibaba",
            http=alibaba_http,
        )
        alibaba.structure("Body", "Lesson", Path(self.temp_dir.name))
        self.assertEqual("high", alibaba_http.payload["reasoning_effort"])
        self.assertNotIn("response_format", alibaba_http.payload)

        class AnthropicHttp:
            def request(self, url, headers, payload, timeout=300.0):
                self.url = url
                self.payload = payload
                return {"content": [{"type": "text", "text": json.dumps(output)}]}

        anthropic_http = AnthropicHttp()
        anthropic = AnthropicStructurer(
            "secret", "claude-test", effort="medium", http=anthropic_http
        )
        self.assertEqual(
            "Lesson",
            anthropic.structure("Body", "Lesson", Path(self.temp_dir.name))["title"],
        )
        self.assertTrue(anthropic_http.url.endswith("/messages"))
        self.assertEqual(
            "json_schema",
            anthropic_http.payload["output_config"]["format"]["type"],
        )
        self.assertEqual(
            "medium", anthropic_http.payload["output_config"]["effort"]
        )

    def test_openai_compatible_retries_invalid_json_and_saves_response(self):
        output = {
            "title": "Lesson",
            "summary": "Summary",
            "sections": [{"heading": "Part", "content": "Body", "key_points": []}],
        }

        class RetryHttp:
            def __init__(self):
                self.calls = 0

            def request(self, url, headers, payload, timeout=300.0):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "choices": [
                            {
                                "message": {"content": "plain text from the first attempt"},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                return {
                    "choices": [
                        {
                            "message": {"content": json.dumps(output)},
                            "finish_reason": "stop",
                        }
                    ]
                }

        work_dir = Path(self.temp_dir.name) / "retry"
        http = RetryHttp()
        provider = OpenAICompatibleStructurer(
            "deepseek_api",
            "secret",
            "deepseek-chat",
            "https://api.deepseek.com/v1",
            http=http,
        )

        self.assertEqual("Lesson", provider.structure("Body", "Lesson", work_dir)["title"])
        self.assertEqual(2, http.calls)
        failed = json.loads(
            (work_dir / "structure-response.failed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, failed["attempt"])
        self.assertIn("plain text", failed["content"])

    def test_markdown_renderer_is_deterministic(self):
        markdown = render_markdown({
            "title": "T",
            "summary": "S",
            "sections": [{"heading": "H", "content": "C", "key_points": ["K"]}],
        })
        self.assertEqual("# T\n\nS\n\n## H\n\nC\n\n### Key Points\n\n- K\n", markdown)

    def test_custom_prompt_replaces_required_placeholders(self):
        template = (
            "Use a question-first style.\n"
            "Title={{LESSON_TITLE}}\n"
            "Source={{TRANSCRIPT}}"
        )
        prompt = build_structure_prompt("Lesson", "Body", template)
        self.assertIn("question-first", prompt)
        self.assertIn("Title=Lesson", prompt)
        self.assertIn("Source=Body", prompt)
        self.assertNotIn("{{TRANSCRIPT}}", prompt)
        appended = build_structure_prompt(
            "Lesson", "Body", "Missing placeholders"
        )
        self.assertIn("Missing placeholders", appended)
        self.assertIn("LESSON TITLE:\nLesson", appended)
        self.assertIn("<TRANSCRIPT>\nBody\n</TRANSCRIPT>", appended)
        self.assertIn("{{LESSON_TITLE}}", DEFAULT_PROMPT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
