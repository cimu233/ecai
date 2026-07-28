import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoe_cli.main import (
    DEFAULT_AUTH_URL,
    emit_catalog,
    emit_pipeline_result,
    inspect_saved_session,
    main,
    resolve_auth_check_url,
)
from xiaoe_core.auth import XiaoeCredentials
from xiaoe_core.browser import BrowserError
from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.services import CourseService


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def invoke(self, arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exit_code = main(["--data-dir", self.temp_dir.name] + arguments)
        return exit_code, output.getvalue(), errors.getvalue()

    def test_add_list_and_status_json(self) -> None:
        exit_code, output, errors = self.invoke(
            ["course", "add", "https://school.example.com/course/1", "--title", "Course One", "--json"]
        )
        self.assertEqual(0, exit_code, errors)
        self.assertTrue(json.loads(output)["created"])

        exit_code, output, errors = self.invoke(["course", "list", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertEqual("Course One", json.loads(output)["courses"][0]["title"])

        exit_code, output, errors = self.invoke(["status", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertEqual(1, json.loads(output)["total_courses"])

    def test_text_course_list_contains_full_url(self) -> None:
        url = "https://school.example.com/course/one?from=saved"
        self.invoke(["course", "add", url, "--title", "Course One", "--json"])
        exit_code, output, errors = self.invoke(["course", "list"])
        self.assertEqual(0, exit_code, errors)
        self.assertIn("课程名称：Course One", output)
        self.assertIn("课程链接：" + url, output)

    def test_browser_backend_can_be_saved(self) -> None:
        exit_code, output, errors = self.invoke(["browser", "use", "edge", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertEqual("edge", json.loads(output)["browser"])
        exit_code, output, errors = self.invoke(["browser", "use", "ego", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertEqual("ego", json.loads(output)["browser"])
        exit_code, output, errors = self.invoke(["browser", "current", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertEqual("ego", json.loads(output)["browser"])

    def test_structurer_backend_can_be_saved_and_read(self) -> None:
        exit_code, output, errors = self.invoke(
            [
                "structurer", "use", "claude-code",
                "--model", "sonnet", "--effort", "medium", "--json",
            ]
        )
        self.assertEqual(0, exit_code, errors)
        self.assertEqual("claude-code", json.loads(output)["provider"])
        exit_code, output, errors = self.invoke(["structurer", "current", "--json"])
        self.assertEqual(0, exit_code, errors)
        payload = json.loads(output)
        self.assertEqual("claude-code", payload["provider"])
        self.assertEqual("sonnet", payload["model"])
        self.assertEqual("medium", payload["effort"])

    def test_custom_structure_prompt_can_be_initialized_and_reset(self) -> None:
        exit_code, output, errors = self.invoke(
            ["structurer", "prompt", "init", "--json"]
        )
        self.assertEqual(0, exit_code, errors)
        payload = json.loads(output)
        self.assertEqual("custom", payload["mode"])
        prompt_file = Path(payload["prompt_file"])
        self.assertTrue(prompt_file.is_file())
        self.assertIn("{{TRANSCRIPT}}", prompt_file.read_text(encoding="utf-8"))

        exit_code, output, errors = self.invoke(
            ["structurer", "prompt", "reset", "--json"]
        )
        self.assertEqual(0, exit_code, errors)
        self.assertEqual("built_in", json.loads(output)["mode"])

    def test_prompt_open_initializes_file_and_uses_default_application(self) -> None:
        with mock.patch(
            "xiaoe_core.file_opener.open_with_default_app"
        ) as opener:
            exit_code, output, errors = self.invoke(
                ["structurer", "prompt", "open", "--json"]
            )
        self.assertEqual(0, exit_code, errors)
        payload = json.loads(output)
        self.assertTrue(payload["opened"])
        prompt_file = Path(payload["prompt_file"])
        self.assertTrue(prompt_file.is_file())
        opener.assert_called_once_with(prompt_file)

    def test_text_browser_output_is_human_readable(self) -> None:
        exit_code, output, errors = self.invoke(["browser", "current"])
        self.assertEqual(0, exit_code, errors)
        self.assertIn("当前浏览器：Chrome", output)
        self.assertNotIn("{", output)

    def test_browser_ensure_starts_selected_ego(self) -> None:
        self.invoke(["browser", "use", "ego", "--json"])
        manager = mock.Mock()
        manager.ensure_application_running.return_value = {
            "running": True,
            "started": True,
            "managed": True,
        }
        with mock.patch("xiaoe_cli.main.EgoBrowserManager", return_value=manager):
            exit_code, output, errors = self.invoke(["browser", "ensure", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertTrue(json.loads(output)["started"])
        manager.ensure_application_running.assert_called_once_with()

    def test_browser_ensure_ignores_non_ego_selection(self) -> None:
        with mock.patch("xiaoe_cli.main.EgoBrowserManager") as manager_class:
            exit_code, output, errors = self.invoke(["browser", "ensure", "--json"])
        self.assertEqual(0, exit_code, errors)
        self.assertFalse(json.loads(output)["managed"])
        manager_class.assert_not_called()

    def test_catalog_text_output_contains_titles_without_json(self) -> None:
        lesson = mock.Mock(position=1, title="第一讲")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            emit_catalog("示例课程", [lesson], False, "course_1")
        self.assertIn("目录扫描完成：示例课程，共 1 条内容", output.getvalue())
        self.assertIn("1. 第一讲", output.getvalue())
        self.assertNotIn('"lessons"', output.getvalue())

    def test_pipeline_text_output_summarizes_each_stage(self) -> None:
        result = mock.Mock(status="completed")
        result.to_dict.return_value = {
            "download": {"processed": 1, "succeeded": 1, "skipped": 0, "failed": 0, "items": []},
            "transcription": {"processed": 1, "succeeded": 1, "skipped": 0, "failed": 0, "items": []},
            "structure": {"processed": 1, "succeeded": 1, "skipped": 0, "failed": 0, "items": []},
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            emit_pipeline_result(result, False)
        self.assertIn("课程处理完成", output.getvalue())
        self.assertIn("音频下载：处理 1，成功 1", output.getvalue())
        self.assertIn("语音转文字：处理 1，成功 1", output.getvalue())
        self.assertNotIn("{", output.getvalue())

    def test_auth_check_defaults_to_dedicated_session_page(self) -> None:
        paths = AppPaths.resolve(self.temp_dir.name)
        service = CourseService(Database(paths.database_file))
        self.assertEqual(DEFAULT_AUTH_URL, resolve_auth_check_url(service, None))
        saved = "https://school.example.com/course/saved"
        service.add_course(saved, "Saved")
        self.assertEqual(DEFAULT_AUTH_URL, resolve_auth_check_url(service, None))
        explicit = "https://school.example.com/course/explicit"
        self.assertEqual(explicit, resolve_auth_check_url(service, explicit))

    def test_auth_check_retries_a_transient_cdp_disconnect(self) -> None:
        page = mock.Mock()
        chrome = mock.Mock(spec=["ensure_running", "open_page"])
        chrome.ensure_running.return_value = "http://127.0.0.1:1234"
        chrome.open_page.return_value = page
        authenticated = {"status": "authenticated"}
        with mock.patch(
            "xiaoe_cli.main.inspect_page",
            side_effect=[BrowserError("cdp_disconnected", "temporary"), authenticated],
        ) as inspect:
            self.assertEqual(authenticated, inspect_saved_session(chrome, "https://example.com/course"))
        self.assertEqual(2, inspect.call_count)
        self.assertEqual(2, page.close.call_count)

    def test_auth_recovery_requests_private_file_when_credentials_are_missing(self) -> None:
        browser = mock.Mock()
        credential_store = mock.Mock()
        credential_store.load.return_value = (None, None)
        credential_store.ensure_file_template.return_value = (
            AppPaths.resolve(self.temp_dir.name).data_dir / "xiaoe-login.json"
        )
        with mock.patch("xiaoe_cli.main.build_browser_manager", return_value=browser), mock.patch(
            "xiaoe_cli.main.inspect_saved_session",
            return_value={"status": "login_required", "url": "https://study.xiaoe-tech.com/#/acount"},
        ), mock.patch("xiaoe_cli.main.XiaoeCredentialStore", return_value=credential_store):
            exit_code, output, errors = self.invoke(["auth", "check", "--recover", "--json"])
        self.assertEqual(3, exit_code, errors)
        self.assertEqual("credentials_required", json.loads(output)["recovery"])

    def test_auth_login_requests_credentials_without_asking_for_course_url(self) -> None:
        browser = mock.Mock()
        browser.browser_name = "edge"
        credential_store = mock.Mock()
        credential_store.load.return_value = (None, None)
        credential_store.ensure_file_template.return_value = (
            AppPaths.resolve(self.temp_dir.name).data_dir / "xiaoe-login.json"
        )
        with mock.patch("xiaoe_cli.main.build_browser_manager", return_value=browser), mock.patch(
            "xiaoe_cli.main.XiaoeCredentialStore", return_value=credential_store
        ):
            exit_code, output, errors = self.invoke(["auth", "login", "--json"])
        self.assertEqual(3, exit_code, errors)
        payload = json.loads(output)
        self.assertEqual("credentials_required", payload["status"])
        self.assertEqual("edge", payload["browser"])

    def test_auth_start_prepares_a_background_session(self) -> None:
        browser = mock.Mock()
        browser.browser_name = "ego"
        browser.ensure_running.return_value = "9"
        with mock.patch("xiaoe_cli.main.build_browser_manager", return_value=browser):
            exit_code, output, errors = self.invoke(["auth", "start", "--json"])
        self.assertEqual(0, exit_code, errors)
        payload = json.loads(output)
        self.assertEqual("background", payload["mode"])
        browser.ensure_running.assert_called_once_with(
            visible=False,
            initial_url="https://study.xiaoe-tech.com",
        )

    def test_slider_verification_never_hands_off_or_checks_an_incomplete_login(self) -> None:
        browser = mock.Mock()
        browser.browser_name = "ego"
        credential_store = mock.Mock()
        credential_store.load.return_value = (XiaoeCredentials("user", "password"), "keychain")
        login = mock.Mock()
        login.attempt.return_value = {
            "submitted": True,
            "challenge": "slider",
            "manual_login_url": "https://study.xiaoe-tech.com/#/acount",
        }
        with mock.patch("xiaoe_cli.main.build_browser_manager", return_value=browser), mock.patch(
            "xiaoe_cli.main.XiaoeCredentialStore", return_value=credential_store
        ), mock.patch("xiaoe_cli.main.XiaoePasswordLogin", return_value=login), mock.patch(
            "xiaoe_cli.main.inspect_saved_session"
        ) as inspect:
            exit_code, output, errors = self.invoke(["auth", "login", "--json"])
        self.assertEqual(1, exit_code, errors)
        payload = json.loads(output)
        self.assertFalse(payload["handed_off"])
        self.assertEqual("manual_verification_required", payload["login"])
        inspect.assert_not_called()

    def test_auth_login_uses_saved_credentials_and_verifies_the_session(self) -> None:
        browser = mock.Mock()
        browser.browser_name = "chrome"
        credential_store = mock.Mock()
        credential_store.load.return_value = (XiaoeCredentials("user", "password"), "file")
        login = mock.Mock()
        login.attempt.return_value = {"submitted": True, "status": "authenticated"}
        with mock.patch("xiaoe_cli.main.build_browser_manager", return_value=browser), mock.patch(
            "xiaoe_cli.main.XiaoeCredentialStore", return_value=credential_store
        ), mock.patch("xiaoe_cli.main.XiaoePasswordLogin", return_value=login), mock.patch(
            "xiaoe_cli.main.inspect_saved_session",
            return_value={"status": "authenticated", "url": "https://school.example.com/course"},
        ):
            exit_code, output, errors = self.invoke(["auth", "login", "--json"])
        self.assertEqual(0, exit_code, errors)
        payload = json.loads(output)
        self.assertEqual("completed", payload["login"])
        self.assertEqual("file", payload["credential_source"])
        self.assertEqual("https://study.xiaoe-tech.com/t_l/learnIndex#/muti_index", payload["checked_url"])
        login.attempt.assert_called_once()

    def test_auth_recovery_verifies_course_after_password_submission(self) -> None:
        browser = mock.Mock()
        credential_store = mock.Mock()
        credential_store.load.return_value = (XiaoeCredentials("user", "password"), "keychain")
        login = mock.Mock()
        login.attempt.return_value = {"submitted": True, "status": "login_required"}
        with mock.patch("xiaoe_cli.main.build_browser_manager", return_value=browser), mock.patch(
            "xiaoe_cli.main.inspect_saved_session",
            side_effect=[
                {"status": "login_required", "url": "https://study.xiaoe-tech.com/#/acount"},
                {"status": "authenticated", "url": "https://school.example.com/course"},
            ],
        ), mock.patch("xiaoe_cli.main.XiaoeCredentialStore", return_value=credential_store), mock.patch(
            "xiaoe_cli.main.XiaoePasswordLogin", return_value=login
        ):
            exit_code, output, errors = self.invoke(["auth", "check", "--recover", "--json"])
        self.assertEqual(0, exit_code, errors)
        payload = json.loads(output)
        self.assertEqual("automatic_login_completed", payload["recovery"])
        self.assertEqual("keychain", payload["credential_source"])


if __name__ == "__main__":
    unittest.main()
