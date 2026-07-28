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

    def _prefix(self) -> str:
        return """
await useOrCreateTaskSpace({task_id})
await switchTab({target_id})
""".format(task_id=self.task_id, target_id=json.dumps(self.target_id))


class EgoBrowserManager:
    """Manage an isolated Ego Task Space that inherits the user's Ego login state."""

    browser_name = "ego"
    global_account_catalog = True

    def __init__(self, cli: Optional[EgoCli] = None, task_name: str = EGO_TASK_NAME) -> None:
        self.cli = cli or EgoCli()
        self.task_name = task_name
        self.task_id: Optional[int] = None
        self.gateway_ready = False

    def ensure_application_running(self) -> Dict[str, bool]:
        return self.cli.ensure_browser_running()

    def ensure_running(self, visible: bool = False, initial_url: str = "about:blank") -> str:
        script = """
const spaces = await listTaskSpaces()
const existing = spaces.find(space =>
  space.name === {task_name} || space.taskId === {task_name}
)
let task
if (existing && existing.ownership === 'agentDelegatedToUser') {{
  await takeOverTaskSpace(existing.id)
  task = existing
}} else if (existing && existing.ownership !== 'agent') {{
  task = await claimTaskSpace(existing.id)
}} else {{
  task = await useOrCreateTaskSpace({task_name})
}}
const tabs = await listTabs()
const contentTabs = tabs.filter(tab => {{
  const url = String(tab.url || '')
  return url && url !== 'about:blank' && !url.startsWith('chrome://')
}})
for (const tab of tabs) {{
  const url = String(tab.url || '')
  const title = String(tab.title || '')
  if (url.includes('diting.bytedance.com/') || title.includes('字节跳动网络诊断工具')) {{
    await closeTab(tab.targetId)
  }} else if (contentTabs.length > 0 && (url === 'about:blank' || url.startsWith('chrome://'))) {{
    await closeTab(tab.targetId)
  }}
}}
cliLog({marker} + JSON.stringify({{"taskId": task.id}}))
""".format(
            task_name=json.dumps(self.task_name),
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
  mediaPlayer: !!document.querySelector(
    'audio, video, .xgplayer, .video-js, .dplayer, [class*="audio-player"], [class*="video-player"]'
  ),
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
let captureResult
try {{
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
captureResult = {{state, payloads}}
}} finally {{
  await cleanupOperationTabs()
}}
cliLog({marker} + JSON.stringify(captureResult))
""".format(
            expression=json.dumps(expression, ensure_ascii=False),
            wait_seconds=max(0.0, wait_seconds),
            marker=json.dumps(EGO_MARKER),
        )
        value = self.cli.run(script, timeout=max(60.0, wait_seconds + 40.0))
        self.gateway_ready = True
        state = value.get("state", {})
        state["status"] = classify_xiaoe_page(
            state.get("url", ""),
            state.get("body", ""),
            bool(state.get("loginChallenge")),
        )
        return {"state": state, "payloads": value.get("payloads", [])}

    def capture_account_courses(self, origin: str) -> Dict[str, Any]:
        url = "https://study.xiaoe-tech.com/t_l/learnIndex#/muti_index"
        script = self._operation_prefix(url) + """
let captureResult
try {{
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
  for (let page = 1; page <= 100; page += 1) {{
    const response = await fetch('/xe.learn-pc/my_attend_normal_list.get/1.0.1', {{
      method: 'POST',
      credentials: 'include',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        page_size: 16,
        page,
        agent_type: 7,
        resource_type: ['0']
      }})
    }})
    const payload = await response.json()
    if (!payload || payload.code !== 0) {{
      throw new Error(payload && payload.msg || 'Xiaoe account catalog request failed')
    }}
    const data = payload && payload.data || {{}}
    const items = Array.isArray(data.list) ? data.list : []
    collected.push(...items)
    if (data.is_end || items.length === 0) break
  }}
  return collected
}})()`)
captureResult = {{state, rows}}
}} finally {{
  await cleanupOperationTabs()
}}
cliLog({marker} + JSON.stringify(captureResult))
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
        script = self._operation_prefix(
            url,
            bootstrap_gateway=not self.gateway_ready,
            force_reload=True,
        ) + """
let captureResult
try {{
const state = await js(String.raw`({{
  url: location.href,
  title: document.title,
  body: (document.body && document.body.innerText || '').slice(0, 12000),
  loginChallenge: !!document.querySelector(
    '[class*="qrcode"], [class*="qr-code"], img[src*="qrcode"], img[alt*="二维码"]'
  )
}})`)
const mediaPattern = /\\.m3u8(?:$|\\?)|\\.(?:mp3|m4a|aac|flac|ogg|wav|mp4|webm)(?:$|\\?)/i
const events = []
const deadline = Date.now() + {wait_milliseconds}
let nextPlaybackAt = 0
while (Date.now() < deadline) {{
  if (Date.now() >= nextPlaybackAt) {{
    const playback = await js({expression})
    if (playback && playback.clickPoint) {{
      await wait(0.1)
      const playing = await js(String.raw`[...document.querySelectorAll('audio,video')].some(node => !node.paused)`)
      if (!playing) await click([playback.clickPoint.x, playback.clickPoint.y])
    }}
    nextPlaybackAt = Date.now() + 1000
  }}
  await wait(Math.min(0.25, Math.max(0.05, (deadline - Date.now()) / 1000)))
  events.push(...await drainEvents())
  const found = events.some(event => {{
    if (event.method !== 'Network.responseReceived') return false
    const response = event.params && event.params.response || {{}}
    const url = response.url || ''
    const mime = (response.mimeType || '').toLowerCase()
    return mediaPattern.test(url) || mime.includes('mpegurl') || mime.startsWith('audio/')
  }})
  const apiFound = events.some(event =>
    event.method === 'Network.responseReceived' &&
    /getPlayUrl|detail_info|audio\.info\.get|get_lookback_list/i.test(
      event.params && event.params.response && event.params.response.url || ''
    )
  )
  const performanceFound = await js(String.raw`performance.getEntriesByType('resource').some(entry =>
    /playlist_eof\\.m3u8|params(?:%5B|\\[)play_url/i.test(entry.name || '')
  )`)
  if (found || apiFound || performanceFound) {{
    await wait(0.3)
    events.push(...await drainEvents())
    break
  }}
}}
const mediaEvents = events.filter(event => {{
  if (event.method !== 'Network.responseReceived') return false
  const response = event.params && event.params.response || {{}}
  const url = response.url || ''
  const mime = (response.mimeType || '').toLowerCase()
  return mediaPattern.test(url) ||
    mime.includes('mpegurl') || mime.startsWith('audio/')
}})
const playbackEvidence = await js(String.raw`(() => {{
  const media = [...document.querySelectorAll('audio,video')]
  return {{
    mediaPlayer: media.length > 0 || !!document.querySelector(
      '.xgplayer, .video-js, .dplayer, [class*="audio-player"], [class*="video-player"]'
    ),
    playbackObserved: media.some(node =>
      !node.paused ||
      Number.isFinite(node.duration) ||
      /^(?:blob:|https?:)/i.test(node.currentSrc || node.src || '')
    )
  }}
}})()`)
state.mediaPlayer = !!(state.mediaPlayer || playbackEvidence.mediaPlayer)
state.playbackObserved = !!playbackEvidence.playbackObserved
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
const performanceEntries = await js(String.raw`performance.getEntriesByType('resource').map(entry => entry.name)`)
for (const entry of performanceEntries) {{
  try {{
    const parsed = new URL(entry)
    for (const value of parsed.searchParams.values()) collectMediaUrls(value)
  }} catch (error) {{}}
}}
const playInfoResponses = events.filter(event =>
  event.method === 'Network.responseReceived' &&
  /getPlayUrl|detail_info|get_play_info|audio\.info\.get|get_lookback_list/i.test(
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
captureResult = {{
  state,
  events: mediaEvents,
  cookies: cookieResult.cookies || []
}}
}} finally {{
  await cleanupOperationTabs()
}}
cliLog({marker} + JSON.stringify(captureResult))
""".format(
            expression=json.dumps(expression, ensure_ascii=False),
            wait_seconds=max(0.0, wait_seconds),
            wait_milliseconds=max(0, int(wait_seconds * 1000)),
            marker=json.dumps(EGO_MARKER),
        )
        value = self.cli.run(script, timeout=max(60.0, wait_seconds + 40.0))
        state = value.get("state", {})
        state["status"] = classify_xiaoe_page(
            state.get("url", ""),
            state.get("body", ""),
            bool(state.get("loginChallenge")),
            bool(state.get("mediaPlayer") or state.get("playbackObserved")),
        )
        if state["status"] == "authenticated":
            self.gateway_ready = True
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

    def invalidate_gateway(self) -> None:
        self.gateway_ready = False

    def _operation_prefix(
        self,
        url: str,
        bootstrap_gateway: bool = True,
        force_reload: bool = False,
    ) -> str:
        gateway_expression = """
(async () => {
  const requestedUrl = __REQUESTED_URL__
  const resourceMatch = requestedUrl.match(/(?:course|p|l|a|v|i)_[A-Za-z0-9]+/)
  const appMatch = requestedUrl.match(/https:\\/\\/(app[A-Za-z0-9]+)\\./)
  const requestedResourceId = resourceMatch && resourceMatch[0]
  const requestedAppId = appMatch && appMatch[1]
  const rows = []
  for (let page = 1; page <= 100; page += 1) {
    const response = await fetch('/xe.learn-pc/my_attend_normal_list.get/1.0.1', {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        page_size: 16,
        page,
        agent_type: 7,
        resource_type: ['0']
      })
    })
    const payload = await response.json()
    if (!payload || payload.code !== 0) {
      throw new Error(payload && payload.msg || 'Xiaoe account catalog request failed')
    }
    const data = payload.data || {}
    const items = Array.isArray(data.list) ? data.list : []
    rows.push(...items)
    if (data.is_end || items.length === 0) break
  }
  const exact = rows.find(row =>
    String(row.resources_id || row.resource_id || '') === requestedResourceId
  )
  const sameStore = rows.find(row =>
    row.app_id === requestedAppId && [5, 6, 8, 25, 50].includes(Number(row.resource_type))
  )
  const item = exact || sameStore
  if (!item) return null
  const response = await fetch('/xe.learn-pc/get_new_gateway/1.0.0', {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      type: 2,
      app_id: item.app_id,
      user_id: item.user_id,
      resource_type: item.resource_type,
      resource_id: item.resources_id || item.resource_id,
      content_app_id: item.content_app_id || ''
    })
  })
  const payload = await response.json()
  if (!payload || payload.code !== 0) {
    throw new Error(payload && payload.msg || 'Xiaoe course gateway request failed')
  }
  return payload.data || null
})()
""".replace("__REQUESTED_URL__", json.dumps(url))
        lines = [
                "const task = await useOrCreateTaskSpace({})".format(json.dumps(self.task_name)),
                "const tabsBefore = await listTabs()",
                "const baselineTabIds = new Set(tabsBefore.map(tab => tab.targetId))",
                "const requestedUrl = {}".format(json.dumps(url)),
                "const contentTabs = tabsBefore.filter(candidate => {",
                "  const url = String(candidate.url || '')",
                "  return url && url !== 'about:blank' && !url.startsWith('chrome://')",
                "})",
                "let tab = contentTabs[0] || tabsBefore.find(candidate => candidate.url === 'about:blank')",
                "if (!tab) {",
                "  await openOrReuseTab('about:blank', {wait: true, timeout: 20})",
                "  tab = await currentTab()",
                "} else {",
                "  await switchTab(tab.targetId)",
                "}",
                "if (contentTabs.length > 0) {",
                "  for (const candidate of tabsBefore) {",
                "    const url = String(candidate.url || '')",
                "    if (candidate.targetId !== tab.targetId && (url === 'about:blank' || url.startsWith('chrome://'))) {",
                "      await closeTab(candidate.targetId)",
                "    }",
                "  }",
                "}",
                "await cdp('Page.enable')",
                "await cdp('Network.enable')",
                "await js(String.raw`performance.clearResourceTimings()`)",
                "await drainEvents()",
        ]
        if bootstrap_gateway:
            lines.extend([
                "const requestedHost = new URL(requestedUrl).hostname",
                "if (/\\.h5\\.(?:xet\\.pomoho|xiaoeknow)\\.com$/.test(requestedHost)) {",
                "  await gotoAndWait({}, {{timeout: 30, settle: 1}})".format(
                    json.dumps("https://study.xiaoe-tech.com/t_l/learnIndex#/muti_index")
                ),
                "  const gateway = await js({})".format(json.dumps(gateway_expression)),
                "  if (gateway && gateway.url) {",
                "    await gotoAndWait(gateway.url, {timeout: 30, settle: 1})",
                "    await wait(1)",
                "  }",
                "}",
            ])
        lines.extend([
                "const currentUrl = await js(String.raw`location.href`)",
                "if (currentUrl !== requestedUrl || {}) {{".format(
                    str(force_reload).lower()
                ),
                "  if (currentUrl === requestedUrl) await cdp('Page.reload')",
                "  else await gotoUrl(requestedUrl)",
                "  const navigationDeadline = Date.now() + 4000",
                "  while (Date.now() < navigationDeadline) {",
                "    const ready = await js(String.raw`location.href === " + json.dumps(url) + " &&",
                "      document.readyState !== 'loading' && !!document.body &&",
                "      ((document.body.innerText || '').length > 50 ||",
                "       !!document.querySelector('audio,video,.xgplayer-start,[class*=\"qrcode\"]'))`)",
                "    if (ready) break",
                "    await wait(0.2)",
                "  }",
                "}",
                "tab = await currentTab()",
                "const cleanupOperationTabs = async () => {",
                "  const tabsAfter = await listTabs()",
                "  for (const candidate of tabsAfter) {",
                "    if (candidate.targetId !== tab.targetId && !baselineTabIds.has(candidate.targetId)) {",
                "      await closeTab(candidate.targetId)",
                "    }",
                "  }",
                "}",
                "",
            ])
        return "\n".join(lines)
