import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from xiaoe_cli.main import main


class SchedulerCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = mock.Mock()
        self.service.start.return_value = {
            "platform": "macos",
            "installed": True,
            "running": True,
        }
        self.service.stop.return_value = {
            "platform": "macos",
            "installed": True,
            "running": False,
        }
        self.service.status.return_value = {
            "platform": "macos",
            "installed": True,
            "running": True,
        }
        self.service.install.return_value = self.service.start.return_value
        self.service.uninstall.return_value = {
            "platform": "macos",
            "installed": False,
            "running": False,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch(
            "xiaoe_core.scheduler_service.build_scheduler_service",
            return_value=self.service,
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--data-dir", self.temporary.name] + arguments)
        return code, output.getvalue(), errors.getvalue()

    def add_course(self):
        code, output, errors = self.invoke(
            [
                "course",
                "add",
                "https://school.example.com/course/1",
                "--title",
                "监控课程",
                "--json",
            ]
        )
        self.assertEqual(0, code, errors)
        return json.loads(output)["course"]["id"]

    def test_add_list_disable_and_enable_schedule(self) -> None:
        course_id = self.add_course()
        code, output, errors = self.invoke(
            ["schedule", "add", course_id, "--every", "2h", "--json"]
        )
        self.assertEqual(0, code, errors)
        payload = json.loads(output)
        self.assertEqual("监控课程", payload["course_title"])
        self.assertEqual("2 小时", payload["interval"])
        self.service.start.assert_called()

        code, output, errors = self.invoke(["schedule", "list", "--json"])
        self.assertEqual(0, code, errors)
        self.assertEqual(course_id, json.loads(output)["schedules"][0]["course_id"])

        code, output, errors = self.invoke(["schedule", "list"])
        self.assertEqual(0, code, errors)
        self.assertIn("监控课程", output)
        self.assertIn("2 小时", output)
        self.assertIn("最近结果", output)

        code, output, errors = self.invoke(
            ["schedule", "disable", course_id, "--json"]
        )
        self.assertEqual(0, code, errors)
        self.assertFalse(json.loads(output)["enabled"])
        self.service.stop.assert_called()

        code, output, errors = self.invoke(
            ["schedule", "enable", course_id, "--json"]
        )
        self.assertEqual(0, code, errors)
        self.assertTrue(json.loads(output)["enabled"])

    def test_unknown_course_is_rejected(self) -> None:
        code, _output, errors = self.invoke(
            ["schedule", "add", "course_missing", "--every", "1h", "--json"]
        )
        self.assertEqual(2, code)
        self.assertIn("课程不存在", errors)


if __name__ == "__main__":
    unittest.main()
