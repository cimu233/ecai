import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
