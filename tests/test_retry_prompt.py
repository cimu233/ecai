import io
import unittest

from xiaoe_cli.retry_prompt import (
    failed_lesson_positions,
    prompt_failed_retry,
)


class Result:
    def to_dict(self):
        return {
            "download": {
                "items": [
                    {"lesson_id": "l2", "error": "download failed"},
                    {"lesson_id": "l3", "error": None},
                ]
            },
            "transcription": {
                "items": [{"lesson_id": "l4", "error": "ASR failed"}]
            },
            "structure": {
                "items": [
                    {"lesson_id": "l4", "error": "structure failed"},
                    {"lesson_id": "l8", "error": "structure failed"},
                ]
            },
        }


LESSONS = [
    {"id": "l2", "position": 2},
    {"id": "l3", "position": 3},
    {"id": "l4", "position": 4},
    {"id": "l8", "position": 8},
]


class RetryPromptTest(unittest.TestCase):
    def test_failures_are_grouped_by_lesson_position(self):
        failures = failed_lesson_positions(Result(), LESSONS)
        self.assertEqual({"音频下载"}, failures[2])
        self.assertEqual({"语音转文字", "结构化整理"}, failures[4])
        self.assertEqual({"结构化整理"}, failures[8])

    def test_timeout_automatically_selects_every_failure(self):
        output = io.StringIO()
        selected = prompt_failed_retry(
            Result(),
            LESSONS,
            15,
            output,
            timed_reader=lambda prompt, timeout, stream: (False, ""),
        )
        self.assertEqual({2, 4, 8}, selected)
        self.assertIn("自动重新执行全部", output.getvalue())

    def test_manual_mixed_selection_only_keeps_failed_lessons(self):
        output = io.StringIO()
        selected = prompt_failed_retry(
            Result(),
            LESSONS,
            15,
            output,
            timed_reader=lambda prompt, timeout, stream: (True, "2,4-6,8"),
        )
        self.assertEqual({2, 4, 8}, selected)
        self.assertIn("5,6", output.getvalue())

    def test_manual_skip_returns_empty_selection(self):
        selected = prompt_failed_retry(
            Result(),
            LESSONS,
            15,
            io.StringIO(),
            timed_reader=lambda prompt, timeout, stream: (True, "n"),
        )
        self.assertEqual(set(), selected)


if __name__ == "__main__":
    unittest.main()
