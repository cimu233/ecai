"""Chromium browser lifecycle and a small Chrome DevTools Protocol client."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import urlopen

try:
    import websocket
except ImportError:  # pragma: no cover - converted to a clear runtime error below
    websocket = None


def _find_chrome() -> Path:
    """Return the best-guess path to a local Chrome / Chromium executable."""
    env = os.environ.get("XIAOE_CHROME_PATH")
    if env:
        candidate = Path(env)
        if candidate.is_file():
            return candidate

    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    elif sys.platform == "win32":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Google\\Chrome\\Application\\chrome.exe"),
            Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Google\\Chrome\\Application\\chrome.exe"),
            Path(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
        ]
    else:
        candidates = [Path(p) for p in (
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        )] + [Path(shutil.which("google-chrome") or ""), Path(shutil.which("chromium") or "")]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    # On macOS the .app bundle parent is also a valid check.
    if sys.platform == "darwin":
        for candidate in candidates:
            if str(candidate).endswith("/Contents/MacOS/Google Chrome") and candidate.parent.parent.parent.is_dir():
                return candidate

    return Path("chrome")  # let it fail with a clear error later


def _find_edge() -> Path:
    """Return the best-guess path to a local Microsoft Edge executable."""
    env = os.environ.get("XIAOE_EDGE_PATH")
    if env:
        candidate = Path(env)
        if candidate.is_file():
            return candidate

    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    elif sys.platform == "win32":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files"), "Microsoft\\Edge\\Application\\msedge.exe"),
            Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"), "Microsoft\\Edge\\Application\\msedge.exe"),
        ]
    else:
        candidates = [
            Path("/usr/bin/microsoft-edge"),
            Path(shutil.which("microsoft-edge") or ""),
        ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    return Path("msedge")  # let it fail with a clear error later


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CdpClient:
    def __init__(self, websocket_url: str, timeout: float = 10.0) -> None:
        if websocket is None:
            raise BrowserError("missing_dependency", "Install websocket-client to control Chromium browsers.")
        self.socket = websocket.create_connection(websocket_url, timeout=timeout, origin="http://localhost")
        self.next_id = 1
        self.events: List[Dict[str, Any]] = []

    def close(self) -> None:
        self.socket.close()

    def command(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        original_timeout = self.socket.gettimeout()
        command_timeout = float(original_timeout or 10.0)
        if method == "Runtime.evaluate" and bool((params or {}).get("awaitPromise")):
            command_timeout = max(command_timeout, 30.0)
        deadline = time.monotonic() + command_timeout
        try:
            self.socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserError(
                        "cdp_timeout",
                        "Browser CDP command timed out: {}".format(method),
                    )
                self.socket.settimeout(remaining)
                message = json.loads(self.socket.recv())
                if message.get("id") == request_id:
                    if "error" in message:
                        raise BrowserError("cdp_error", "Browser CDP command failed: {}".format(method))
                    return message.get("result", {})
                self.events.append(message)
        except BrowserError:
            raise
        except websocket.WebSocketTimeoutException as error:
            raise BrowserError(
                "cdp_timeout",
                "Browser CDP command timed out: {}".format(method),
            ) from error
        except (OSError, ValueError, websocket.WebSocketException) as error:
            raise BrowserError("cdp_disconnected", "Browser page connection was interrupted.") from error
        finally:
            self.socket.settimeout(original_timeout)

    def collect_events(self, seconds: float) -> List[Dict[str, Any]]:
        deadline = time.monotonic() + seconds
        original_timeout = self.socket.gettimeout()
        try:
            while time.monotonic() < deadline:
                self.socket.settimeout(min(0.5, max(0.05, deadline - time.monotonic())))
                try:
                    self.events.append(json.loads(self.socket.recv()))
                except websocket.WebSocketTimeoutException:
                    continue
                except (OSError, ValueError, websocket.WebSocketException) as error:
                    raise BrowserError("cdp_disconnected", "Browser page connection was interrupted.") from error
        finally:
            self.socket.settimeout(original_timeout)
        events, self.events = self.events, []
        return events


class ChromiumManager:
    def __init__(self, profile_dir: Path, executable: Path, browser_name: str, display_name: str) -> None:
        self.profile_dir = profile_dir
        self.executable = executable
        self.browser_name = browser_name
        self.display_name = display_name
        self.pid_file = profile_dir / "{}.pid".format(browser_name)
        self.mode_file = profile_dir / "{}.mode".format(browser_name)

    def ensure_running(self, visible: bool = False, initial_url: str = "about:blank") -> str:
        # Automation must never steal the user's foreground application.
        visible = False
        endpoint = self.debug_endpoint()
        if endpoint:
            current_mode = self.mode_file.read_text(encoding="ascii").strip() if self.mode_file.is_file() else "unknown"
            requested_mode = self.requested_mode(visible)
            if current_mode in {"visible", "headless"} and current_mode != requested_mode:
                self.stop()
            else:
                return endpoint
        if not self.executable.is_file():
            raise BrowserError(
                "{}_missing".format(self.browser_name),
                "System {} was not found.".format(self.display_name),
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "--user-data-dir={}".format(self.profile_dir),
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not visible:
            command.append("--headless=new")
        command.append(initial_url)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.pid_file.write_text(str(process.pid), encoding="ascii")
        self.mode_file.write_text("visible" if visible else "headless", encoding="ascii")
        for _ in range(100):
            endpoint = self.debug_endpoint()
            if endpoint:
                return endpoint
            if process.poll() is not None:
                break
            time.sleep(0.1)
        raise BrowserError(
            "{}_start_failed".format(self.browser_name),
            "{} did not expose a debugging endpoint.".format(self.display_name),
        )

    def debug_endpoint(self) -> Optional[str]:
        active_port = self.profile_dir / "DevToolsActivePort"
        try:
            port = active_port.read_text(encoding="ascii").splitlines()[0].strip()
            with urlopen("http://127.0.0.1:{}/json/version".format(port), timeout=0.5) as response:
                if response.status == 200:
                    return "http://127.0.0.1:{}".format(port)
        except (OSError, IndexError, URLError):
            return None
        return None

    def stop(self) -> None:
        try:
            pid = int(self.pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
        )
        if str(self.profile_dir) in process.stdout and str(self.executable) in process.stdout:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.pid_file.unlink(missing_ok=True)
        self.mode_file.unlink(missing_ok=True)

    def open_page(self, endpoint: str, url: str) -> CdpClient:
        version = self._json(endpoint + "/json/version")
        browser = CdpClient(version["webSocketDebuggerUrl"])
        try:
            target_id = browser.command(
                "Target.createTarget",
                {"url": "about:blank", "background": True},
            )["targetId"]
        finally:
            browser.close()
        for _ in range(30):
            targets = self._json(endpoint + "/json/list")
            target = next((item for item in targets if item.get("id") == target_id), None)
            if target and target.get("webSocketDebuggerUrl"):
                page = CdpClient(target["webSocketDebuggerUrl"])
                page.command("Page.enable")
                page.command("Network.enable")
                page.command("Page.navigate", {"url": url})
                return page
            time.sleep(0.1)
        raise BrowserError("page_start_failed", "{} could not create a course page.".format(self.display_name))

    @staticmethod
    def requested_mode(visible: bool) -> str:
        return "headless"

    def cookies(self, endpoint: str) -> List[Dict[str, Any]]:
        version = self._json(endpoint + "/json/version")
        client = CdpClient(version["webSocketDebuggerUrl"])
        try:
            return client.command("Storage.getCookies").get("cookies", [])
        finally:
            client.close()

    @staticmethod
    def _json(url: str) -> Any:
        with urlopen(url, timeout=3.0) as response:
            return json.loads(response.read().decode("utf-8"))


class ChromeManager(ChromiumManager):
    def __init__(self, profile_dir: Path, executable: Optional[Path] = None) -> None:
        super().__init__(profile_dir, executable or _find_chrome(), "chrome", "Google Chrome")


class EdgeManager(ChromiumManager):
    def __init__(self, profile_dir: Path, executable: Optional[Path] = None) -> None:
        super().__init__(profile_dir, executable or _find_edge(), "edge", "Microsoft Edge")


def cookie_header(cookies: List[Dict[str, Any]], host: str) -> str:
    matching = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".")
        if host == domain or host.endswith("." + domain):
            matching.append("{}={}".format(cookie["name"], cookie["value"]))
    return "; ".join(matching)


def classify_xiaoe_page(url: str, body_text: str, has_login_challenge: bool = False) -> str:
    lowered_url = url.lower()
    compact = " ".join(body_text.split())
    if any(marker in lowered_url for marker in ("/login", "passport", "account.xiaoe", "#/acount", "#/account")):
        return "login_required"
    login_markers = (
        "微信扫码登录",
        "手机号登录",
        "验证码登录",
        "密码登录",
        "登录/注册",
        "请先登录",
        "登录后观看",
        "扫码观看",
        "微信扫一扫",
    )
    challenge_context = has_login_challenge and any(
        marker in compact for marker in ("登录", "注册", "扫码", "微信")
    )
    if challenge_context or any(marker in compact for marker in login_markers):
        return "login_required"
    if any(marker in compact for marker in ("无权访问", "暂无权限", "课程已下架", "内容已删除")):
        return "access_denied"
    return "authenticated"


def inspect_page(page: CdpClient, wait_seconds: float = 3.0, preserve_events: bool = False) -> Dict[str, str]:
    events = page.collect_events(wait_seconds)
    if preserve_events:
        page.events.extend(events)
    expression = """JSON.stringify({
      url: location.href,
      title: document.title,
      body: (document.body && document.body.innerText || '').slice(0, 12000),
      loginChallenge: !!document.querySelector(
        '[class*="qrcode"], [class*="qr-code"], img[src*="qrcode"], img[alt*="二维码"]'
      )
    })"""
    result = page.command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    value = result.get("result", {}).get("value", "{}")
    state = json.loads(value)
    state["status"] = classify_xiaoe_page(
        state.get("url", ""),
        state.get("body", ""),
        bool(state.get("loginChallenge")),
    )
    return state
