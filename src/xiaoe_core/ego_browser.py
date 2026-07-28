"""Ego Browser adapter exposing the subset of CDP used by the pipeline."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .browser import BrowserError, classify_xiaoe_page


EGO_TASK_NAME = "xiaoe audio pipeline"
EGO_MARKER = "__XIAOE_EGO_JSON__"
EGO_FALLBACK_PATH = Path.home() / ".local" / "bin" / "ego-browser"
EGO_MAC_APP = Path("/Applications/ego lite.app")
EGO_MAC_EXECUTABLE = EGO_MAC_APP / "Contents" / "MacOS" / "ego lite"


def resolve_ego_cli() -> Path:
    discovered = shutil.which("ego-browser")
    if discovered:
        return Path(discovered)
    return EGO_FALLBACK_PATH


class EgoCli:
    def __init__(
        self,
        executable: Optional[Path] = None,
        runner: Optional[Callable[[str], Dict[str, Any]]] = None,
        process_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        platform: str = sys.platform,
        app_executable: Path = EGO_MAC_EXECUTABLE,
    ) -> None:
        self.executable = executable or resolve_ego_cli()
        self.runner = runner
        self.process_runner = process_runner
        self.sleeper = sleeper
        self.platform = platform
        self.app_executable = app_executable

    def ensure_browser_running(
        self,
        timeout: float = 15.0,
        poll_interval: float = 0.25,
    ) -> Dict[str, bool]:
        """Start the installed Ego app in the background when its process is absent."""
        if self.platform != "darwin":
            return {"running": True, "started": False, "managed": False}
        if not self.app_executable.is_file():
            raise BrowserError(
                "ego_app_missing",
                "Ego Lite was not found in /Applications.",
            )
        if self._is_browser_running():
            return {"running": True, "started": False, "managed": True}

        try:
            completed = self.process_runner(
                ["/usr/bin/open", "-gj", "-a", "ego lite"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise BrowserError("ego_start_failed", "Ego Lite could not be started.") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise BrowserError("ego_start_failed", detail or "Ego Lite could not be started.")

        interval = max(0.05, poll_interval)
        attempts = max(1, int(max(0.0, timeout) / interval))
        for _ in range(attempts):
            if self._is_browser_running():
                return {"running": True, "started": True, "managed": True}
            self.sleeper(interval)
        raise BrowserError("ego_start_timeout", "Ego Lite did not become ready within the timeout.")

    def _is_browser_running(self) -> bool:
        try:
            completed = self.process_runner(
                ["/usr/bin/pgrep", "-f", str(self.app_executable)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return completed.returncode == 0

    def run(self, script: str, timeout: float = 60.0) -> Any:
        if self.runner is not None:
            result = self.runner(script)
            if "error" in result:
                raise BrowserError("ego_error", str(result["error"]))
            return result.get("value")
        self.ensure_browser_running()
        if not self.executable.is_file():
            raise BrowserError(
                "ego_missing",
                "Ego Browser CLI was not found. Complete Ego Lite onboarding so ~/.local/bin/ego-browser exists.",
            )
        try:
            completed = subprocess.run(
                [str(self.executable), "nodejs"],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BrowserError("ego_start_failed", "Ego Browser could not run the requested operation.") from error
        combined = "{}\n{}".format(completed.stdout, completed.stderr)
        if completed.returncode != 0:
            if "user has taken control" in combined or "browser commands are paused" in combined:
                raise BrowserError(
                    "ego_user_control",
                    "Ego is currently controlled by the user. Finish the browser action, then retry.",
                )
            raise BrowserError("ego_error", self._last_error(combined))
        return self._extract_value(combined)

    @staticmethod
    def _last_error(output: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return lines[-1] if lines else "Ego Browser returned an unknown error."

    @staticmethod
    def _extract_value(output: str) -> Any:
        for line in reversed(output.splitlines()):
            if EGO_MARKER not in line:
                continue
            payload = line.split(EGO_MARKER, 1)[1]
            return json.loads(payload)
        raise BrowserError("ego_protocol_error", "Ego Browser did not return a pipeline result.")


class EgoPage:
    def __init__(self, cli: EgoCli, task_id: int, target_id: str) -> None:
        self.cli = cli
        self.task_id = task_id
        self.target_id = target_id
        self.events: List[Dict[str, Any]] = []

    def command(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        script = self._prefix() + """
const result = await cdp({method}, {params})
cliLog({marker} + JSON.stringify(result))
""".format(
            method=json.dumps(method),
            params=json.dumps(params or {}, ensure_ascii=False),
            marker=json.dumps(EGO_MARKER),
        )
        value = self.cli.run(script)
        return value if isinstance(value, dict) else {}

    def collect_events(self, seconds: float) -> List[Dict[str, Any]]:
        script = self._prefix() + """
await wait({seconds})
const events = await drainEvents()
cliLog({marker} + JSON.stringify(events))
""".format(seconds=max(0.0, seconds), marker=json.dumps(EGO_MARKER))
        value = self.cli.run(script, timeout=max(60.0, seconds + 20.0))
        collected = value if isinstance(value, list) else []
        events, self.events = self.events + collected, []
        return events

    def close(self) -> None:
        script = """
await useOrCreateTaskSpace({task_id})
const tabs = await listTabs()
if (tabs.some(tab => tab.targetId === {target_id})) await closeTab({target_id})
cliLog({marker} + JSON.stringify({{"closed": true}}))
""".format(
            task_id=self.task_id,
            target_id=json.dumps(self.target_id),
            marker=json.dumps(EGO_MARKER),
        )
        try:
            self.cli.run(script)
        except BrowserError:
            pass

    def handoff(self) -> None:
        script = self._prefix() + """
await handOffTaskSpace({task_id})
cliLog({marker} + JSON.stringify({{"handedOff": true}}))
""".format(task_id=self.task_id, marker=json.dumps(EGO_MARKER))
        self.cli.run(script)

    def _prefix(self) -> str:
        return """
await useOrCreateTaskSpace({task_id})
await switchTab({target_id})
""".format(task_id=self.task_id, target_id=json.dumps(self.target_id))


class EgoBrowserManager:
    """Manage an isolated Ego Task Space that inherits the user's Ego login state."""

    browser_name = "ego"

    def __init__(self, cli: Optional[EgoCli] = None, task_name: str = EGO_TASK_NAME) -> None:
        self.cli = cli or EgoCli()
        self.task_name = task_name
        self.task_id: Optional[int] = None

    def ensure_application_running(self) -> Dict[str, bool]:
        return self.cli.ensure_browser_running()

    def ensure_running(self, visible: bool = False, initial_url: str = "about:blank") -> str:
        action = ""
        if visible:
            action = """
await openOrReuseTab({url}, {{wait: true, timeout: 20}})
await handOffTaskSpace(task.id)
""".format(url=json.dumps(initial_url))
        script = """
const task = await useOrCreateTaskSpace({task_name})
{action}
cliLog({marker} + JSON.stringify({{"taskId": task.id}}))
""".format(
            task_name=json.dumps(self.task_name),
            action=action,
            marker=json.dumps(EGO_MARKER),
        )
        value = self.cli.run(script)
        self.task_id = int(value["taskId"])
        return str(self.task_id)

    def check_auth(self, url: str) -> Dict[str, str]:
        script = self._operation_prefix(url) + """
const state = await js(String.raw`({{
  url: location.href,
  title: document.title,
  body: (document.body && document.body.innerText || '').slice(0, 12000),
  loginChallenge: !!document.querySelector(
    '[class*="qrcode"], [class*="qr-code"], img[src*="qrcode"], img[alt*="二维码"]'
  )
}})`)
await closeTab(tab.targetId)
cliLog({marker} + JSON.stringify(state))
""".format(marker=json.dumps(EGO_MARKER))
        state = self.cli.run(script)
        state["status"] = classify_xiaoe_page(
            state.get("url", ""),
            state.get("body", ""),
            bool(state.get("loginChallenge")),
        )
        return state

    def capture_catalog(self, url: str, expression: str, wait_seconds: float = 6.0) -> Dict[str, Any]:
        script = self._operation_prefix(url) + """
const state = await js(String.raw`({{
  url: location.href,
  title: document.title,
  body: (document.body && document.body.innerText || '').slice(0, 12000),
  loginChallenge: !!document.querySelector(
    '[class*="qrcode"], [class*="qr-code"], img[src*="qrcode"], img[alt*="二维码"]'
  )
}})`)
await js({expression})
await wait({wait_seconds})
const events = await drainEvents()
const responses = events.filter(event => {{
  if (event.method !== 'Network.responseReceived') return false
  const response = event.params && event.params.response || {{}}
  return /json/i.test(response.mimeType || '') ||
    /catalog|resource|course|sub_course/i.test(response.url || '')
}})
const payloads = []
for (const event of responses) {{
  try {{
    const result = await cdp('Network.getResponseBody', {{requestId: event.params.requestId}})
    const parsed = JSON.parse(result.body || '')
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) payloads.push(parsed)
  }} catch (error) {{}}
}}
await closeTab(tab.targetId)
cliLog({marker} + JSON.stringify({{state, payloads}}))
""".format(
            expression=json.dumps(expression, ensure_ascii=False),
            wait_seconds=max(0.0, wait_seconds),
            marker=json.dumps(EGO_MARKER),
        )
        value = self.cli.run(script, timeout=max(60.0, wait_seconds + 40.0))
        state = value.get("state", {})
        state["status"] = classify_xiaoe_page(
            state.get("url", ""),
            state.get("body", ""),
            bool(state.get("loginChallenge")),
        )
        return {"state": state, "payloads": value.get("payloads", [])}

    def capture_account_courses(self, origin: str) -> Dict[str, Any]:
        url = origin.rstrip("/") + "/p/t/v1/study/study_h5/my_course"
        script = self._operation_prefix(url) + """
const state = await js(String.raw`({{
  url: location.href,
  title: document.title,
  body: (document.body && document.body.innerText || '').slice(0, 12000),
  loginChallenge: !!document.querySelector(
    '[class*="qrcode"], [class*="qr-code"], img[src*="qrcode"], img[alt*="二维码"]'
  )
}})`)
const rows = await js(String.raw`(async () => {{
  const collected = []
  let pageId = 0
  for (let page = 1; page <= 100; page += 1) {{
    const body = new URLSearchParams()
    body.set('bizData[limit]', '50')
    body.set('bizData[page]', String(page))
    body.set('bizData[page_id]', String(pageId))
    const response = await fetch('/subscribe/resource_list', {{
      method: 'POST',
      credentials: 'include',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: body.toString()
    }})
    const payload = await response.json()
    const data = payload && payload.data || {{}}
    const items = Array.isArray(data.list) ? data.list : []
    collected.push(...items)
    if (data.is_last || items.length === 0) break
    pageId = data.page_id || 0
  }}
  return collected
}})()`)
await closeTab(tab.targetId)
cliLog({marker} + JSON.stringify({{state, rows}}))
""".format(marker=json.dumps(EGO_MARKER))
        value = self.cli.run(script, timeout=90.0)
        state = value.get("state", {})
        state["status"] = classify_xiaoe_page(
            state.get("url", ""),
            state.get("body", ""),
            bool(state.get("loginChallenge")),
        )
        return {"state": state, "rows": value.get("rows", [])}

    def capture_media(self, url: str, expression: str, wait_seconds: float) -> Dict[str, Any]:
        script = self._operation_prefix(url) + """
const state = await js(String.raw`({{
  url: location.href,
  title: document.title,
  body: (document.body && document.body.innerText || '').slice(0, 12000),
  loginChallenge: !!document.querySelector(
    '[class*="qrcode"], [class*="qr-code"], img[src*="qrcode"], img[alt*="二维码"]'
  )
}})`)
await js({expression})
await wait({wait_seconds})
const events = await drainEvents()
const mediaPattern = /\\.m3u8(?:$|\\?)|\\.(?:mp3|m4a|aac|flac|ogg|wav|mp4|webm)(?:$|\\?)/i
const mediaEvents = events.filter(event => {{
  if (event.method !== 'Network.responseReceived') return false
  const response = event.params && event.params.response || {{}}
  const url = response.url || ''
  const mime = (response.mimeType || '').toLowerCase()
  return mediaPattern.test(url) ||
    mime.includes('mpegurl') || mime.startsWith('audio/')
}})
const discoveredUrls = []
const collectMediaUrls = value => {{
  if (typeof value === 'string') {{
    const normalized = value.replace(/\\\\u0026/g, '&')
    if (/^https?:\\/\\//i.test(normalized) && mediaPattern.test(normalized)) discoveredUrls.push(normalized)
    return
  }}
  if (Array.isArray(value)) {{
    value.forEach(collectMediaUrls)
    return
  }}
  if (value && typeof value === 'object') Object.values(value).forEach(collectMediaUrls)
}}
const playInfoResponses = events.filter(event =>
  event.method === 'Network.responseReceived' &&
  /getPlayUrl|detail_info|get_play_info/i.test(
    event.params && event.params.response && event.params.response.url || ''
  )
)
for (const event of playInfoResponses) {{
  try {{
    const result = await cdp('Network.getResponseBody', {{requestId: event.params.requestId}})
    collectMediaUrls(JSON.parse(result.body || ''))
  }} catch (error) {{}}
}}
for (const url of [...new Set(discoveredUrls)]) {{
  mediaEvents.push({{
    method: 'Network.responseReceived',
    params: {{
      response: {{
        url,
        mimeType: /\\.m3u8(?:$|\\?)/i.test(url) ? 'application/vnd.apple.mpegurl' : 'audio/mpeg'
      }}
    }}
  }})
}}
const cookieResult = await cdp('Storage.getCookies')
await closeTab(tab.targetId)
cliLog({marker} + JSON.stringify({{
  state,
  events: mediaEvents,
  cookies: cookieResult.cookies || []
}}))
""".format(
            expression=json.dumps(expression, ensure_ascii=False),
            wait_seconds=max(0.0, wait_seconds),
            marker=json.dumps(EGO_MARKER),
        )
        value = self.cli.run(script, timeout=max(60.0, wait_seconds + 40.0))
        state = value.get("state", {})
        state["status"] = classify_xiaoe_page(
            state.get("url", ""),
            state.get("body", ""),
            bool(state.get("loginChallenge")),
        )
        return {
            "state": state,
            "events": value.get("events", []),
            "cookies": value.get("cookies", []),
        }

    def open_page(self, endpoint: str, url: str) -> EgoPage:
        task_id = int(endpoint)
        script = """
await useOrCreateTaskSpace({task_id})
await openOrReuseTab('about:blank', {{wait: true, timeout: 20}})
await cdp('Page.enable')
await cdp('Network.enable')
await drainEvents()
await gotoAndWait({url}, {{timeout: 30, settle: 1}})
const tab = await currentTab()
cliLog({marker} + JSON.stringify({{"targetId": tab.targetId}}))
""".format(task_id=task_id, url=json.dumps(url), marker=json.dumps(EGO_MARKER))
        value = self.cli.run(script)
        return EgoPage(self.cli, task_id, str(value["targetId"]))

    def cookies(self, endpoint: str) -> List[Dict[str, Any]]:
        task_id = int(endpoint)
        script = """
await useOrCreateTaskSpace({task_id})
await ensureRealTab()
const result = await cdp('Storage.getCookies')
cliLog({marker} + JSON.stringify(result.cookies || []))
""".format(task_id=task_id, marker=json.dumps(EGO_MARKER))
        value = self.cli.run(script)
        return value if isinstance(value, list) else []

    def stop(self) -> None:
        script = """
const result = await completeTaskSpace({task_name}, {{keep: false}})
cliLog({marker} + JSON.stringify(result))
""".format(task_name=json.dumps(self.task_name), marker=json.dumps(EGO_MARKER))
        try:
            self.cli.run(script)
        except BrowserError:
            pass

    def _operation_prefix(self, url: str) -> str:
        return """
const task = await useOrCreateTaskSpace({task_name})
await openOrReuseTab('about:blank', {{wait: true, timeout: 20}})
await cdp('Page.enable')
await cdp('Network.enable')
await drainEvents()
await gotoAndWait({url}, {{timeout: 30, settle: 1}})
const tab = await currentTab()
""".format(task_name=json.dumps(self.task_name), url=json.dumps(url))
