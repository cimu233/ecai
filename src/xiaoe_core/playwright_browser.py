"""Playwright browser adapter exposing the same duck-type interface as ChromiumManager."""

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .browser import BrowserError, classify_xiaoe_page


class PlaywrightPage:
    """Wraps a Playwright Page + CDP session to mirror the CdpClient / EgoPage interface."""

    def __init__(self, page: Any, cdp_session: Any) -> None:
        self._page = page
        self._cdp = cdp_session
        self.events: List[Dict[str, Any]] = []
        self._response_bodies: Dict[str, bytes] = {}

        def _on_response(response: Any) -> None:
            rid = response.headers.get("x-cdp-rid") or str(id(response))
            try:
                body = response.body()
            except Exception:
                body = b""
            self._response_bodies[rid] = body
            self.events.append(
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "requestId": rid,
                        "response": {
                            "url": response.url,
                            "mimeType": response.headers.get("content-type", ""),
                            "status": response.status,
                        },
                    },
                }
            )

        page.on("response", _on_response)

    def command(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}

        # Intercept high-frequency CDP commands and use Playwright equivalents.
        if method == "Network.getResponseBody":
            rid = params.get("requestId", "")
            body = self._response_bodies.get(rid, b"")
            try:
                text = body.decode("utf-8")
                base64_encoded = False
            except UnicodeDecodeError:
                text = base64.b64encode(body).decode()
                base64_encoded = True
            return {"body": text, "base64Encoded": base64_encoded}

        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            await_promise = params.get("awaitPromise", False)
            return_by_value = params.get("returnByValue", False)
            try:
                if await_promise:
                    result = self._page.evaluate(
                        """async () => {{ const __fn = {expr}; return __fn(); }}""".format(expr=expression)
                    )
                else:
                    result = self._page.evaluate(expression)
            except Exception as error:
                raise BrowserError("playwright_eval", str(error)) from error
            if return_by_value or not await_promise:
                return {"result": {"value": result}}
            return {"result": {"value": json.dumps(result) if not isinstance(result, str) else result}}

        if method == "Page.captureScreenshot":
            fmt = params.get("format", "png")
            clip = params.get("clip")
            options: Dict[str, Any] = {"type": fmt.lower()}
            if clip:
                options["clip"] = {
                    "x": clip["x"],
                    "y": clip["y"],
                    "width": clip["width"],
                    "height": clip["height"],
                }
            data = self._page.screenshot(**options)
            return {"data": base64.b64encode(data).decode()}

        if method == "Input.dispatchMouseEvent":
            mouse_type = params.get("type", "")
            x = params.get("x", 0)
            y = params.get("y", 0)
            button = params.get("button", "left")
            click_count = params.get("clickCount", 1)
            if mouse_type == "mousePressed":
                self._page.mouse.move(x, y)
                self._page.mouse.down(button=button, click_count=click_count)
            elif mouse_type == "mouseReleased":
                self._page.mouse.move(x, y)
                self._page.mouse.up(button=button, click_count=click_count)
            elif mouse_type == "mouseMoved":
                self._page.mouse.move(x, y)
            return {}

        # Fall back to the raw CDP session for everything else.
        try:
            return self._cdp.send(method, params)
        except Exception as error:
            raise BrowserError("cdp_error", "Playwright CDP command failed: {} — {}".format(method, error)) from error

    def collect_events(self, seconds: float) -> List[Dict[str, Any]]:
        self._page.wait_for_timeout(int(max(0.0, seconds) * 1000))
        events, self.events = self.events, []
        return events

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass


class PlaywrightBrowserManager:
    """Manage a Chromium browser via Playwright."""

    def __init__(
        self,
        browser_name: str = "playwright-chrome",
        profile_dir: Optional[Path] = None,
    ) -> None:
        self.browser_name = browser_name
        self.profile_dir = profile_dir
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def ensure_running(self, visible: bool = False, initial_url: str = "about:blank") -> str:
        if self._browser is not None and self._context is not None:
            try:
                self._context.pages
                return "playwright-active"
            except Exception:
                pass

        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
        except ImportError as error:
            raise BrowserError(
                "playwright_missing",
                "Playwright is not installed. Run: pip install playwright && playwright install chromium",
            ) from error

        self._playwright = sync_playwright().start()
        launch_options: Dict[str, Any] = {"headless": not visible}

        if self.profile_dir is not None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                **launch_options,
            )
        else:
            self._browser = self._playwright.chromium.launch(**launch_options)
            self._context = self._browser.new_context()

        if not self._context.pages:
            page = self._context.new_page()
            page.goto(initial_url, wait_until="domcontentloaded")

        return "playwright-active"

    def open_page(self, endpoint: str, url: str) -> PlaywrightPage:
        _ = endpoint
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as error:
            page.close()
            raise BrowserError("playwright_navigation", "Playwright could not open {}: {}".format(url, error)) from error

        try:
            cdp = self._context.new_cdp_session(page)
        except Exception as error:
            page.close()
            raise BrowserError("playwright_cdp", "Playwright CDP session unavailable: {}".format(error)) from error

        cdp.send("Page.enable")
        cdp.send("Network.enable")
        return PlaywrightPage(page, cdp)

    def cookies(self, endpoint: str) -> List[Dict[str, Any]]:
        _ = endpoint
        if self._context is None:
            return []
        raw = self._context.cookies()
        cleaned: List[Dict[str, Any]] = []
        for cookie in raw:
            cleaned.append(
                {
                    "name": cookie.get("name", ""),
                    "value": cookie.get("value", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", "/"),
                    "httpOnly": cookie.get("httpOnly", False),
                    "secure": cookie.get("secure", False),
                }
            )
        return cleaned

    def stop(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
