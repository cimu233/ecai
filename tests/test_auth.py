import json
import stat
import tempfile
import unittest

from xiaoe_core.auth import XiaoeCredentialStore, XiaoeCredentials, XiaoePasswordLogin
from xiaoe_core.config import AppPaths


class FakeKeychain:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value, label=None):
        self.values[account] = value


class XiaoeCredentialStoreTest(unittest.TestCase):
    def test_keychain_has_priority_over_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.resolve(directory)
            paths.create()
            keychain = FakeKeychain(
                {
                    "xiaoe.login.username": "keychain-user",
                    "xiaoe.login.password": "keychain-password",
                }
            )
            store = XiaoeCredentialStore(paths, keychain)
            path = store.ensure_file_template()
            path.write_text(
                json.dumps({"username": "file-user", "password": "file-password"}),
                encoding="utf-8",
            )
            credentials, source = store.load()
            self.assertEqual("keychain", source)
            self.assertEqual("keychain-user", credentials.username)

    def test_template_is_private_and_complete_file_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.resolve(directory)
            paths.create()
            store = XiaoeCredentialStore(paths, FakeKeychain())
            path = store.ensure_file_template()
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            path.write_text(
                json.dumps({"username": "local-user", "password": "local-password"}),
                encoding="utf-8",
            )
            credentials, source = store.load()
            self.assertEqual("file", source)
            self.assertEqual("local-user", credentials.username)
            self.assertEqual("local-password", credentials.password)

    def test_incomplete_file_is_reported_as_unconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.resolve(directory)
            paths.create()
            store = XiaoeCredentialStore(paths, FakeKeychain())
            store.ensure_file_template()
            self.assertFalse(store.status()["configured"])


class XiaoePasswordLoginTest(unittest.TestCase):
    def test_login_submits_form_without_returning_credentials(self):
        class FakePage:
            def collect_events(self, seconds):
                return []

            def command(self, method, params):
                self.expression = params["expression"]
                return {
                    "result": {
                        "value": {
                            "submitted": True,
                            "url": "https://study.xiaoe-tech.com/#/acount",
                            "body": "密码登录",
                        }
                    }
                }

            def close(self):
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.page = FakePage()

            def ensure_running(self, visible=False):
                return "http://127.0.0.1:1234"

            def open_page(self, endpoint, url):
                return self.page

        browser = FakeBrowser()
        result = XiaoePasswordLogin(browser).attempt(XiaoeCredentials("user", "top-secret"))
        self.assertEqual("login_required", result["status"])
        self.assertTrue(result["submitted"])
        self.assertNotIn("top-secret", json.dumps(result))
        self.assertTrue(browser.page.closed)

    def test_slider_challenge_hands_off_ego_page_without_closing_it(self):
        class FakeEgoPage:
            def __init__(self):
                self.closed = False
                self.handed_off = False

            def collect_events(self, seconds):
                return []

            def command(self, method, params):
                return {
                    "result": {
                        "value": {
                            "submitted": True,
                            "challenge": "slider",
                            "url": "https://study.xiaoe-tech.com/#/acount",
                            "body": "请向右拖动滑块完成安全验证",
                        }
                    }
                }

            def handoff(self):
                self.handed_off = True

            def close(self):
                self.closed = True

        class FakeEgoBrowser:
            def __init__(self):
                self.page = FakeEgoPage()

            def ensure_running(self, visible=False):
                return "3"

            def open_page(self, endpoint, url):
                return self.page

        browser = FakeEgoBrowser()
        result = XiaoePasswordLogin(browser).attempt(XiaoeCredentials("user", "password"))
        self.assertEqual("slider", result["challenge"])
        self.assertTrue(result["handed_off"])
        self.assertTrue(browser.page.handed_off)
        self.assertFalse(browser.page.closed)


if __name__ == "__main__":
    unittest.main()
