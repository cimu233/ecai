"""Credential loading and password-based Xiaoe session recovery."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .browser import BrowserError, classify_xiaoe_page
from .config import AppPaths
from .secrets import MacKeychainSecretStore


XIAOE_LOGIN_URL = "https://study.xiaoe-tech.com/#/acount"
XIAOE_USERNAME_ACCOUNT = "xiaoe.login.username"
XIAOE_PASSWORD_ACCOUNT = "xiaoe.login.password"


@dataclass(frozen=True)
class XiaoeCredentials:
    username: str
    password: str


class XiaoeCredentialStore:
    def __init__(self, paths: AppPaths, keychain: Optional[MacKeychainSecretStore] = None) -> None:
        self.paths = paths
        self.keychain = keychain or MacKeychainSecretStore()
        self.file_path = paths.data_dir / "xiaoe-login.json"

    def load(self) -> Tuple[Optional[XiaoeCredentials], Optional[str]]:
        username = self.keychain.get(XIAOE_USERNAME_ACCOUNT)
        password = self.keychain.get(XIAOE_PASSWORD_ACCOUNT)
        if username and password:
            return XiaoeCredentials(username, password), "keychain"
        file_credentials = self._load_file()
        if file_credentials:
            return file_credentials, "file"
        return None, None

    def save_keychain(self, username: str, password: str) -> None:
        clean_username = username.strip()
        if not clean_username or not password:
            raise ValueError("Xiaoe username and password cannot be empty.")
        self.keychain.set(XIAOE_USERNAME_ACCOUNT, clean_username, "Xiaoe Login - Username")
        self.keychain.set(XIAOE_PASSWORD_ACCOUNT, password, "Xiaoe Login - Password")

    def ensure_file_template(self) -> Path:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        if self.file_path.is_symlink():
            raise ValueError("Xiaoe credential file cannot be a symbolic link.")
        if not self.file_path.exists():
            payload = json.dumps({"username": "", "password": ""}, ensure_ascii=False, indent=2) + "\n"
            descriptor = os.open(self.file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
        os.chmod(self.file_path, 0o600)
        return self.file_path

    def status(self) -> Dict[str, Any]:
        credentials, source = self.load()
        return {
            "configured": credentials is not None,
            "source": source,
            "file": str(self.file_path),
        }

    def _load_file(self) -> Optional[XiaoeCredentials]:
        if not self.file_path.is_file() or self.file_path.is_symlink():
            return None
        os.chmod(self.file_path, 0o600)
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        return XiaoeCredentials(username, password) if username and password else None


class XiaoePasswordLogin:
    def __init__(self, browser: Any) -> None:
        self.browser = browser

    def attempt(self, credentials: XiaoeCredentials) -> Dict[str, Any]:
        endpoint = self.browser.ensure_running(visible=False)
        page = self.browser.open_page(endpoint, XIAOE_LOGIN_URL)
        keep_page_open = False
        try:
            page.collect_events(4.0)
            result = page.command(
                "Runtime.evaluate",
                {
                    "expression": self._expression(credentials),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            value = result.get("result", {}).get("value", {})
            if not isinstance(value, dict):
                raise BrowserError("login_form_error", "Xiaoe login form returned an invalid result.")
            value["status"] = classify_xiaoe_page(
                str(value.get("url") or ""),
                str(value.get("body") or ""),
            )
            if value.get("challenge"):
                # Attempt automatic slider solving via OpenCV + CDP mouse
                # simulation before falling back to user handoff.
                try:
                    from .slider_captcha import SliderSolver  # noqa: E402
                except ImportError:
                    SliderSolver = None  # type: ignore[assignment]

                solved = False
                if SliderSolver is not None:
                    solver = SliderSolver(page)
                    try:
                        solved = solver.solve()
                    except Exception:
                        solved = False

                if solved:
                    # Give the page a moment to process the solved captcha
                    # and redirect before the caller inspects the session.
                    page.collect_events(3.0)
                    value["challenge"] = None
                    value["auto_solved"] = True
                elif hasattr(page, "handoff"):
                    page.handoff()
                    value["handed_off"] = True
                    keep_page_open = True
            return value
        finally:
            if not keep_page_open:
                page.close()

    @staticmethod
    def _expression(credentials: XiaoeCredentials) -> str:
        username = json.dumps(credentials.username, ensure_ascii=False)
        password = json.dumps(credentials.password, ensure_ascii=False)
        return """(async () => {{
          const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
          const visible = node => {{
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          }};
          const find = async selector => {{
            for (let attempt = 0; attempt < 40; attempt += 1) {{
              const node = document.querySelector(selector);
              if (visible(node)) return node;
              await wait(250);
            }}
            return null;
          }};
          const passwordTab = document.querySelector('.password-item');
          if (passwordTab && !document.querySelector('input[type="password"]')) {{
            passwordTab.click();
            await wait(1200);
          }}
          const usernameInput = await find('input[placeholder*="帐号"], input[placeholder*="手机号"]');
          const passwordInput = await find('input[type="password"]');
          if (!usernameInput || !passwordInput) {{
            return {{submitted: false, reason: 'form_not_found', url: location.href, body: (document.body.innerText || '').slice(0, 12000)}};
          }}
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          const fill = (input, value) => {{
            setter.call(input, value);
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
          }};
          fill(usernameInput, {username});
          fill(passwordInput, {password});
          const agreement = document.querySelector('input[type="checkbox"]');
          if (agreement && !agreement.checked) agreement.click();
          await wait(300);
          const candidates = [...document.querySelectorAll('button, [role="button"], a, div')].filter(node => {{
            const text = (node.textContent || '').trim();
            return visible(node) && (text === '登录' || text === '登录/注册');
          }});
          const login = candidates.sort((left, right) => left.childElementCount - right.childElementCount)[0];
          if (!login) {{
            return {{submitted: false, reason: 'submit_not_found', url: location.href, body: (document.body.innerText || '').slice(0, 12000)}};
          }}
          login.click();
          await wait(8000);
          const pageText = (document.body.innerText || '').slice(0, 12000);
          const sliderSelector = [
            '[class*="slider"]',
            '[class*="captcha"]',
            '[class*="verify"]',
            '.nc_wrapper',
            '.nc-container',
            'iframe[src*="captcha"]'
          ].find(selector => visible(document.querySelector(selector)));
          const sliderText = /拖动|滑块|向右滑动|安全验证/.test(pageText);
          return {{
            submitted: true,
            challenge: sliderSelector || sliderText ? 'slider' : null,
            url: location.href,
            title: document.title,
            body: pageText
          }};
        }})()""".format(username=username, password=password)
