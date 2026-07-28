import unittest
from pathlib import Path
from unittest import mock

from xiaoe_core.browser import ChromeManager, EdgeManager, classify_xiaoe_page, cookie_header
from xiaoe_core.ego_browser import EGO_MARKER, EgoCli


class BrowserHelpersTest(unittest.TestCase):
    def test_login_redirect_is_detected(self):
        self.assertEqual("login_required", classify_xiaoe_page("https://example.com/login", ""))
        self.assertEqual("login_required", classify_xiaoe_page("https://example.com/course", "请使用微信扫码登录"))
        self.assertEqual(
            "login_required",
            classify_xiaoe_page("https://study.xiaoe-tech.com/#/acount", "密码登录"),
        )

    def test_authorized_course_page_is_accepted(self):
        self.assertEqual("authenticated", classify_xiaoe_page("https://study.xiaoe-tech.com/course", "课程目录"))

    def test_visible_qrcode_on_course_page_requires_login(self):
        self.assertEqual(
            "login_required",
            classify_xiaoe_page("https://example.com/course", "课程介绍", has_login_challenge=True),
        )

    def test_cookie_header_only_contains_matching_domains(self):
        cookies = [
            {"name": "session", "value": "one", "domain": ".xiaoe-tech.com"},
            {"name": "other", "value": "two", "domain": ".example.com"},
        ]
        self.assertEqual("session=one", cookie_header(cookies, "study.xiaoe-tech.com"))

    def test_requested_mode_names_match_chrome_modes(self):
        self.assertEqual("visible", ChromeManager.requested_mode(True))
        self.assertEqual("headless", ChromeManager.requested_mode(False))

    def test_chrome_and_edge_share_the_chromium_manager_contract(self):
        chrome = ChromeManager.__new__(ChromeManager)
        edge = EdgeManager.__new__(EdgeManager)
        ChromeManager.__init__(chrome, Path("/tmp/chrome-profile"))
        EdgeManager.__init__(edge, Path("/tmp/edge-profile"))
        self.assertEqual("chrome", chrome.browser_name)
        self.assertEqual("edge", edge.browser_name)
        self.assertNotEqual(chrome.profile_dir, edge.profile_dir)

    def test_ego_output_parser_reads_stderr_marker(self):
        output = "warning\n{}{}\n".format(EGO_MARKER, '{"status":"ok"}')
        self.assertEqual({"status": "ok"}, EgoCli._extract_value(output))

    def test_ego_running_process_is_reused(self):
        process_runner = mock.Mock(return_value=mock.Mock(returncode=0))
        cli = EgoCli(
            process_runner=process_runner,
            platform="darwin",
            app_executable=Path(__file__),
        )

        result = cli.ensure_browser_running()

        self.assertEqual({"running": True, "started": False, "managed": True}, result)
        process_runner.assert_called_once()
        self.assertEqual("/usr/bin/pgrep", process_runner.call_args.args[0][0])

    def test_ego_is_launched_in_background_when_process_is_absent(self):
        process_runner = mock.Mock(
            side_effect=[
                mock.Mock(returncode=1),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=1),
                mock.Mock(returncode=0),
            ]
        )
        sleeper = mock.Mock()
        cli = EgoCli(
            process_runner=process_runner,
            sleeper=sleeper,
            platform="darwin",
            app_executable=Path(__file__),
        )

        result = cli.ensure_browser_running(timeout=1.0, poll_interval=0.1)

        self.assertEqual({"running": True, "started": True, "managed": True}, result)
        self.assertEqual(
            ["/usr/bin/open", "-gj", "-a", "ego lite"],
            process_runner.call_args_list[1].args[0],
        )
        sleeper.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()
