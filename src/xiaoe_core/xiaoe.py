"""Xiaoe catalog discovery and browser-backed media resolution."""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from .auth import XIAOE_SESSION_CHECK_URL
from .browser import BrowserError, cookie_header, inspect_page
from .config import AppPaths
from .media import DIRECT_AUDIO_SUFFIXES, DownloadError, MediaSource, StoredUrlResolver
from .models import Lesson
from .progress import ProgressSpinner
from .services import CourseService, LessonService, utc_now


MEDIA_PATTERN = re.compile(r"\.m3u8(?:$|\?)|\.(?:mp3|m4a|aac|flac|ogg|wav|mp4|webm)(?:$|\?)", re.I)

# A headless player never issues a real media request without a user
# gesture, so the response sniffer in _media_urls sees nothing. The
# getPlayUrl / get_lookback_list APIs already carry the signed source
# URLs, so record them and feed the result through _synthetic_media_events
# the same way the Ego browser path does.
PLAY_URL_RECORDER = """
window.__xePlay = [];
(function () {
  const keep = (url, text) => {
    try {
      if (/getPlayUrl|get_lookback_list/i.test(String(url))) { window.__xePlay.push(String(text)); }
    } catch (e) {}
  };
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const response = await originalFetch.apply(this, args);
    try {
      const url = (args[0] && args[0].url) || args[0];
      if (/getPlayUrl|get_lookback_list/i.test(String(url))) { keep(url, await response.clone().text()); }
    } catch (e) {}
    return response;
  };
  const open_ = XMLHttpRequest.prototype.open;
  const send_ = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__xeUrl = url;
    return open_.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', () => keep(this.__xeUrl, this.responseText));
    return send_.apply(this, arguments);
  };
})();
"""
ACCOUNT_COURSE_TYPES = {5, 6, 8, 25, 50}


def find_catalog_items(payload: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            resource_id = value.get("resource_id") or value.get("resourceId")
            jump_url = value.get("jump_url") or value.get("jumpUrl")
            if resource_id and jump_url:
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    unique: Dict[str, Dict[str, Any]] = {}
    for item in found:
        unique[str(item.get("resource_id") or item.get("resourceId"))] = item
    return list(unique.values())


def find_account_course_items(rows: Any) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    courses: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            resource_type = int(row.get("resource_type"))
        except (TypeError, ValueError):
            continue
        resource_id = str(row.get("resources_id") or row.get("resource_id") or "")
        jump_url = str(row.get("jump_url") or row.get("jumpUrl") or row.get("h5_url") or "")
        details = row.get("detail_resources")
        if (
            resource_type in ACCOUNT_COURSE_TYPES
            and resource_id
            and jump_url
        ):
            courses[resource_id] = row
    return list(courses.values())


def response_json(page: Any, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for event in events:
        if event.get("method") != "Network.responseReceived":
            continue
        params = event.get("params", {})
        response = params.get("response", {})
        mime = str(response.get("mimeType", "")).lower()
        url = str(response.get("url", ""))
        if "json" not in mime and not re.search(r"catalog|resource|course|sub_course", url, re.I):
            continue
        try:
            body = page.command("Network.getResponseBody", {"requestId": params["requestId"]}).get("body", "")
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                payloads.append(parsed)
        except (BrowserError, KeyError, json.JSONDecodeError):
            continue
    return payloads


class XiaoeCatalogService:
    def __init__(
        self,
        paths: AppPaths,
        courses: CourseService,
        lessons: LessonService,
        chrome: Any,
    ) -> None:
        self.paths = paths
        self.courses = courses
        self.lessons = lessons
        self.chrome = chrome

    def refresh(self, course_id: str) -> List[Lesson]:
        course = self.courses.get(course_id)
        if course is None:
            raise ValueError("Course does not exist: {}".format(course_id))
        # Restarting a menu action explicitly resumes the dedicated browser task.
        self.chrome.ensure_running(visible=False)
        raw_path = self.paths.courses_dir / course.id / "catalog.raw.json"
        cached_items = self._cached_items(raw_path)
        try:
            with ProgressSpinner("正在扫描课程目录"):
                items = self._capture_items(course)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_json(raw_path, {"course_url": course.source_url, "items": items})
        except BrowserError as error:
            if error.code in {"login_required", "ego_user_control"} or not raw_path.is_file():
                raise
            items = self._cached_items(raw_path)
            if not items:
                raise

        # Report what changed since the last scan.
        if cached_items:
            self._diff_and_report(cached_items, items)

        items.sort(key=lambda item: self._position(item, 2**31))
        with self.courses.database.connect() as connection:
            connection.execute("UPDATE lessons SET position = -position WHERE course_id = ?", (course.id,))
        lessons = []
        for position, item in enumerate(items, 1):
            resource_id = self._resource_id(item)
            title = str(
                item.get("chapter_title")
                or item.get("resource_title")
                or item.get("title")
                or item.get("resource_name")
                or item.get("name")
                or resource_id
            )
            content_type, media_hint = classify_catalog_media(item)
            lessons.append(
                self.lessons.upsert(
                    course.id,
                    position,
                    title,
                    source_url=urljoin(course.source_url, str(item.get("jump_url") or item.get("jumpUrl"))),
                    lesson_id="resource_{}".format(self._safe_id(resource_id)),
                    content_type=content_type,
                    media_hint=media_hint,
                )
            )
        with self.courses.database.connect() as connection:
            connection.execute(
                "UPDATE courses SET status = 'catalog_ready', updated_at = ? WHERE id = ?", (utc_now(), course.id)
            )
        return sorted(lessons, key=lambda lesson: lesson.position)

    @staticmethod
    def _diff_and_report(
        old_items: List[Dict[str, Any]], new_items: List[Dict[str, Any]]
    ) -> None:
        """Compare cached and fresh catalogs, print a change summary to stderr."""

        def key(item: Dict[str, Any]) -> str:
            return str(item.get("resource_id") or item.get("resourceId"))

        def title(item: Dict[str, Any]) -> str:
            return str(
                item.get("chapter_title")
                or item.get("resource_title")
                or item.get("title")
                or item.get("resource_name")
                or item.get("name")
                or key(item)
            )

        old_by_id: Dict[str, Tuple[int, str]] = {}
        for idx, item in enumerate(old_items):
            old_by_id[key(item)] = (idx + 1, title(item))

        new_by_id: Dict[str, Tuple[int, str]] = {}
        for idx, item in enumerate(new_items):
            new_by_id[key(item)] = (idx + 1, title(item))

        old_ids = set(old_by_id)
        new_ids = set(new_by_id)

        added = new_ids - old_ids
        removed = old_ids - new_ids
        kept = old_ids & new_ids

        position_shifts = 0
        title_changes = 0
        for rid in kept:
            old_pos, old_title = old_by_id[rid]
            new_pos, new_title = new_by_id[rid]
            if old_pos != new_pos:
                position_shifts += 1
            if old_title != new_title:
                title_changes += 1

        if not added and not removed and position_shifts == 0 and title_changes == 0:
            print("  (目录无变化，与上次扫描一致)", file=sys.stderr)
            return

        changes = []
        if added:
            changes.append("新增 {} 节".format(len(added)))
        if removed:
            changes.append("移除 {} 节".format(len(removed)))
        if position_shifts:
            changes.append("{} 节位置变动（已自动重新映射）".format(position_shifts))
        if title_changes:
            changes.append("{} 节标题有变化".format(title_changes))
        if changes:
            print("  目录有更新：" + "，".join(changes), file=sys.stderr)

        if removed and len(removed) <= 10:
            for rid in sorted(removed, key=lambda r: old_by_id[r][0]):
                print("    − [{:d}] {}".format(old_by_id[rid][0], old_by_id[rid][1]), file=sys.stderr)
        elif removed:
            print("    (移除节数较多，已自动清理)", file=sys.stderr)

    def _capture_items(self, course: Any) -> List[Dict[str, Any]]:
        if hasattr(self.chrome, "capture_catalog"):
            captured = self.chrome.capture_catalog(course.source_url, self._expand_expression(), 1.0)
            state = captured["state"]
            payloads = captured["payloads"]
        else:
            endpoint = self.chrome.ensure_running(visible=False)
            page = self.chrome.open_page(endpoint, course.source_url)
            try:
                state = inspect_page(page, 4.0, preserve_events=True)
                page.command(
                    "Runtime.evaluate",
                    {
                        "expression": self._expand_expression(),
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                events = page.collect_events(1.0)
                payloads = response_json(page, events)
            finally:
                page.close()
        if state["status"] == "login_required":
            raise BrowserError("login_required", "Xiaoe login has expired. Run xiaoe auth check --recover.")
        items: List[Dict[str, Any]] = []
        for payload in payloads:
            items.extend(find_catalog_items(payload))
        items = list({self._resource_id(item): item for item in items}.values())
        if not items:
            raise BrowserError(
                "catalog_not_found",
                "该课程页面未找到可下载的内容目录，可能为纯图文公告、免费试听课或暂未开课的课程。",
            )
        return items

    @staticmethod
    def _cached_items(path: Path) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload.get("items", [])
            return items if isinstance(items, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _expand_expression() -> str:
        return """(async () => {
          const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
          let expanded = 0;
          for (let attempt = 0; attempt < 20; attempt += 1) {
            const nodes = [...document.querySelectorAll(
              '[role="button"][aria-label="展开"], [aria-expanded="false"], details:not([open])'
            )].slice(0, 25);
            if (!nodes.length) break;
            for (const node of nodes) {
              node.scrollIntoView({block: 'center'});
              if (node.tagName === 'DETAILS') node.open = true;
              else node.click();
              expanded += 1;
            }
            await wait(350);
          }
          window.scrollTo(0, document.body.scrollHeight);
          return expanded;
        })()"""

    @staticmethod
    def _resource_id(item: Dict[str, Any]) -> str:
        return str(item.get("resource_id") or item.get("resourceId"))

    @staticmethod
    def _position(item: Dict[str, Any], fallback: int) -> int:
        for key in ("sort_value", "sort_c", "sort", "position", "index"):
            try:
                value = int(item[key])
                return value if value >= 1 else fallback
            except (KeyError, TypeError, ValueError):
                continue
        return fallback

    @staticmethod
    def _safe_id(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
        return cleaned[:80] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def classify_catalog_media(item: Dict[str, Any]) -> Tuple[str, str]:
    """Return (content_type, media_hint) using explicit Xiaoe catalog fields."""
    jump_url = str(item.get("jump_url") or item.get("jumpUrl") or "")
    path = urlparse(jump_url).path.lower()
    try:
        resource_type = int(item.get("resource_type"))
    except (TypeError, ValueError):
        resource_type = 0

    if resource_type == 1 or "/text/" in path:
        return "text", "no_media"
    if resource_type == 2 or "/audio/" in path or _positive_number(item.get("audio_length")):
        return "audio", "has_media"
    if resource_type == 3 or "/video/" in path or _positive_number(item.get("video_length")):
        return "video", "has_media"
    if resource_type == 4 or "/alive/" in path:
        lookback = _optional_int(item.get("is_lookback"))
        alive_status = _optional_int(item.get("alive_status"))
        if lookback == 1 and alive_status == 3:
            return "live_replay", "has_media"
        if lookback == 0 and alive_status == 3:
            return "live", "no_media"
        return "live", "probe"
    return "unknown", "probe"


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class XiaoeAccountCatalogService:
    def __init__(self, paths: AppPaths, courses: CourseService, browser: Any) -> None:
        self.paths = paths
        self.courses = courses
        self.browser = browser

    def scan(self, source_url: Optional[str] = None) -> Dict[str, Any]:
        origins = self._origins(source_url)
        capture_origins = origins[:1] if getattr(self.browser, "global_account_catalog", False) else origins
        raw_rows: List[Dict[str, Any]] = []
        for origin in capture_origins:
            captured = self._capture_origin(origin)
            state = captured["state"]
            if state["status"] == "login_required":
                raise BrowserError("login_required", "Xiaoe login has expired. Run xiaoe auth check --recover.")
            for row in captured["rows"]:
                if isinstance(row, dict):
                    item = dict(row)
                    item["_store_origin"] = origin
                    raw_rows.append(item)

        raw_path = self.paths.data_dir / "account-catalog.raw.json"
        self._atomic_json(raw_path, {"origins": origins, "items": raw_rows})
        candidates = find_account_course_items(raw_rows)
        known = self.courses.list_courses()
        imported = []
        existing = []
        for item in candidates:
            resource_id = str(item.get("resources_id") or item.get("resource_id"))
            origin = str(item["_store_origin"])
            source = urljoin(
                origin + "/",
                str(item.get("jump_url") or item.get("jumpUrl") or item.get("h5_url")),
            )
            details = item.get("detail_resources")
            details = details if isinstance(details, dict) else {}
            title = str(
                details.get("title")
                or details.get("resource_name")
                or item.get("resource_title")
                or item.get("title")
                or resource_id
            )
            matched = next(
                (
                    course
                    for course in known
                    if course.source_url == source or resource_id in course.source_url
                ),
                None,
            )
            if matched is not None:
                existing.append(matched.to_dict())
                continue
            result = self.courses.add_course(source, title)
            imported.append(result.course.to_dict())
            known.append(result.course)
        return {
            "origins": origins,
            "scanned_resources": len(raw_rows),
            "course_candidates": len(candidates),
            "imported": len(imported),
            "existing": len(existing),
            "skipped_non_course": len(raw_rows) - len(candidates),
            "courses": imported + existing,
            "raw_file": str(raw_path),
        }

    def _origins(self, source_url: Optional[str]) -> List[str]:
        urls = [source_url] if source_url else [course.source_url for course in self.courses.list_courses()]
        origins = []
        for value in urls:
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                origin = "{}://{}".format(parsed.scheme, parsed.netloc)
                if origin not in origins:
                    origins.append(origin)
        if not origins:
            raise ValueError("Add one Xiaoe course URL or pass --url before scanning the account.")
        return origins

    def _capture_origin(self, origin: str) -> Dict[str, Any]:
        if hasattr(self.browser, "capture_account_courses"):
            return self.browser.capture_account_courses(origin)
        endpoint = self.browser.ensure_running(visible=False)
        page = self.browser.open_page(
            endpoint,
            origin.rstrip("/") + "/p/t/v1/study/study_h5/my_course",
        )
        try:
            state = inspect_page(page, 3.0)
            result = page.command(
                "Runtime.evaluate",
                {
                    "expression": self._account_expression(),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            rows = result.get("result", {}).get("value", [])
        finally:
            page.close()
        return {"state": state, "rows": rows if isinstance(rows, list) else []}

    @staticmethod
    def _account_expression() -> str:
        return """(async () => {
          const collected = [];
          let pageId = 0;
          for (let page = 1; page <= 100; page += 1) {
            const body = new URLSearchParams();
            body.set('bizData[limit]', '50');
            body.set('bizData[page]', String(page));
            body.set('bizData[page_id]', String(pageId));
            const response = await fetch('/subscribe/resource_list', {
              method: 'POST',
              credentials: 'include',
              headers: {'Content-Type': 'application/x-www-form-urlencoded'},
              body: body.toString()
            });
            const payload = await response.json();
            const data = payload && payload.data || {};
            const items = Array.isArray(data.list) ? data.list : [];
            collected.push(...items);
            if (data.is_last || items.length === 0) break;
            pageId = data.page_id || 0;
          }
          return collected;
        })()"""

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


class XiaoeBrowserMediaResolver:
    def __init__(self, chrome: Any, wait_seconds: float = 18.0) -> None:
        self.chrome = chrome
        self.wait_seconds = wait_seconds

    def resolve(self, lesson: Lesson, force_refresh: bool = False) -> List[MediaSource]:
        if not lesson.source_url:
            raise DownloadError("source_unavailable", "Lesson has no Xiaoe page URL.")

        # When force_refresh, wait longer — the first attempt may have
        # raced against a slow-loading player widget.
        wait = self.wait_seconds + (10.0 if force_refresh else 0.0)

        if hasattr(self.chrome, "capture_media"):
            captured = self.chrome.capture_media(lesson.source_url, self._play_expression(), wait)
            state = captured["state"]
            events = captured["events"]
            cookies = captured["cookies"]
        else:
            endpoint = self.chrome.ensure_running(visible=False)
            page = self.chrome.open_page(endpoint, "about:blank")
            try:
                page.command("Page.enable")
                page.command(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": PLAY_URL_RECORDER},
                )
                page.command("Page.navigate", {"url": lesson.source_url})
                state = inspect_page(page, 3.0, preserve_events=True)
                events = self._trigger_playback(page, wait)
                events.extend(
                    self._synthetic_media_events(self._recorded_play_urls(page))
                )
            finally:
                page.close()
            cookies = self.chrome.cookies(endpoint)
        urls = self._media_urls(events)
        if urls:
            return [self._source(url, lesson.source_url, cookies) for url in urls]
        has_player_evidence = bool(
            state.get("mediaPlayer") or state.get("playbackObserved")
        )
        if state["status"] == "login_required" and not has_player_evidence:
            account_state = self._check_account_session()
            if account_state == "authenticated":
                raise DownloadError(
                    "source_unavailable",
                    "The Xiaoe account session is valid, but this lesson exposed no playable media source.",
                )
            raise DownloadError("source_expired", "Xiaoe login has expired.")
        if has_player_evidence:
            raise DownloadError(
                "source_unavailable",
                "A playable media control was detected, but its source URL could not be extracted.",
            )
        raise DownloadError("source_unavailable", "No playable audio or HLS request was observed.")

    def _check_account_session(self) -> Optional[str]:
        checker = getattr(self.chrome, "check_auth", None)
        if not callable(checker):
            return None
        result = checker(XIAOE_SESSION_CHECK_URL)
        return str(result.get("status") or "") if isinstance(result, dict) else None

    def _trigger_playback(self, page: Any, total_wait: float) -> List[Dict[str, Any]]:
        """Trigger playback while returning as soon as a media URL appears."""
        import time

        events: List[Dict[str, Any]] = []
        deadline = time.monotonic() + max(0.0, total_wait)
        next_playback = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_playback:
                page.command("Runtime.evaluate", {"expression": self._play_expression()})
                next_playback = time.monotonic() + 1.0
            events.extend(
                page.collect_events(
                    min(0.25, max(0.05, deadline - time.monotonic()))
                )
            )
            if self._media_urls(events):
                break
            performance_urls = self._performance_media_urls(page)
            if performance_urls:
                events.extend(self._synthetic_media_events(performance_urls))
                break
        return events

    @staticmethod
    def _performance_media_urls(page: Any) -> List[str]:
        expression = """JSON.stringify(
          performance.getEntriesByType('resource').map(entry => entry.name)
        )"""
        try:
            result = page.command(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            value = result.get("result", {}).get("value", "[]")
            entries = json.loads(value)
        except (BrowserError, TypeError, ValueError, json.JSONDecodeError):
            return []
        urls = []
        for entry in entries if isinstance(entries, list) else []:
            try:
                values = parse_qs(urlparse(str(entry)).query).values()
            except ValueError:
                continue
            for group in values:
                for value in group:
                    if value.startswith(("http://", "https://")) and MEDIA_PATTERN.search(value):
                        if value not in urls:
                            urls.append(value)
        return urls

    @staticmethod
    def _recorded_play_urls(page: Any) -> List[str]:
        """Pull signed source URLs out of the recorded play/lookback responses.

        Audio-only mp3 is preferred: it needs no HLS assembly and, unlike the
        720p variant, is not served from the DRM path.
        """
        try:
            evaluated = page.command(
                "Runtime.evaluate",
                {"expression": "JSON.stringify(window.__xePlay || [])", "returnByValue": True},
            )
            payloads = json.loads((evaluated.get("result") or {}).get("value") or "[]")
        except Exception:
            return []
        preferred: List[str] = []
        fallback: List[str] = []
        for payload in payloads:
            try:
                data = json.loads(payload).get("data")
            except Exception:
                continue
            if not isinstance(data, (dict, list)):
                continue
            if isinstance(data, list):
                # get_lookback_list: data[].line_sharpness[].url
                for line in data:
                    if not isinstance(line, dict):
                        continue
                    for variant in line.get("line_sharpness") or []:
                        if isinstance(variant, dict) and variant.get("url"):
                            fallback.append(str(variant["url"]))
                continue
            for entry in data.values():
                play_list = entry.get("play_list") if isinstance(entry, dict) else None
                if not isinstance(play_list, dict):
                    continue
                for name, variant in play_list.items():
                    if not isinstance(variant, dict) or not variant.get("is_support"):
                        continue
                    url = str(variant.get("play_url") or "")
                    if not url:
                        continue
                    (preferred if str(name).lower() == "mp3" else fallback).append(url)
        return preferred or fallback

    @staticmethod
    def _synthetic_media_events(urls: Iterable[str]) -> List[Dict[str, Any]]:
        return [
            {
                "method": "Network.responseReceived",
                "params": {
                    "response": {
                        "url": url,
                        "mimeType": (
                            "application/vnd.apple.mpegurl"
                            if ".m3u8" in urlparse(url).path.lower()
                            else "audio/mpeg"
                        ),
                    }
                },
            }
            for url in urls
        ]

    @staticmethod
    def _play_expression() -> str:
        return """(() => {
          const media = [...document.querySelectorAll('audio,video')];

          const selectors = [
            'button[aria-label*="播放"]',
            'button[title*="播放"]',
            '[role="button"][aria-label*="播放"]',
            'button[aria-label*="play" i]',
            'button[title*="play" i]',
            '[role="button"][aria-label*="play" i]',
            '.vjs-big-play-button',
            '.xgplayer-start',
            '.dplayer-play-icon'
          ];
          const candidates = [...new Set(selectors.flatMap(selector =>
            [...document.querySelectorAll(selector)]
          ))];
          const blocked = /诊断|网络|测速|帮助|反馈|报错|diagnostic|network|speed|help|feedback|error/i;
          const button = candidates.find(node => {
            const label = [
              node.innerText,
              node.getAttribute('aria-label'),
              node.getAttribute('title')
            ].filter(Boolean).join(' ');
            const rect = node.getBoundingClientRect();
            return !blocked.test(label) && rect.width > 0 && rect.height > 0;
          });
          const needsPlayback = media.length === 0 || media.every(node => node.paused);
          let clickPoint = null;
          if (button && needsPlayback) {
            const rect = button.getBoundingClientRect();
            clickPoint = {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
            try { button.click(); } catch(e) {}
          } else if (!button && needsPlayback) {
            media.forEach(node => { try { node.play(); } catch(e) {} });
          }
          return {media: media.length, button_clicked: !!button, clickPoint};
        })()"""

    @staticmethod
    def _media_urls(events: Iterable[Dict[str, Any]]) -> List[str]:
        urls: Dict[Tuple[str, str, str], str] = {}
        for event in events:
            if event.get("method") != "Network.responseReceived":
                continue
            response = event.get("params", {}).get("response", {})
            url = str(response.get("url", ""))
            mime = str(response.get("mimeType", "")).lower()
            if MEDIA_PATTERN.search(url) or "mpegurl" in mime or mime.startswith("audio/"):
                parsed = urlparse(url)
                # A finished live lesson still advertises its push-stream URL,
                # which 404s. Its replay lives behind get_lookback_list instead.
                if (parsed.hostname or "").startswith("liveplay"):
                    continue
                key = (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path)
                if key in urls:
                    del urls[key]
                urls[key] = url
        return list(urls.values())

    @staticmethod
    def _source(url: str, page_url: str, browser_cookies: List[Dict[str, Any]]) -> MediaSource:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix == ".m3u8":
            kind = "hls"
        elif suffix in DIRECT_AUDIO_SUFFIXES:
            kind = "direct_audio"
        else:
            kind = "video_file"
        cookie_value = cookie_header(browser_cookies, urlparse(url).hostname or "")
        cookies = {}
        for pair in cookie_value.split("; ") if cookie_value else []:
            name, value = pair.split("=", 1)
            cookies[name] = value
        return MediaSource(
            url=url,
            kind=kind,
            headers={"Referer": page_url},
            cookies=cookies,
            page_url=page_url,
            audio_only=kind == "direct_audio",
        )


class HybridMediaResolver:
    """Use stored media URLs directly and browser discovery for Xiaoe lesson pages."""

    def __init__(self, browser_resolver: XiaoeBrowserMediaResolver) -> None:
        self.browser_resolver = browser_resolver
        self.stored = StoredUrlResolver()

    def resolve(self, lesson: Lesson, force_refresh: bool = False) -> List[MediaSource]:
        path = urlparse(lesson.source_url or "").path.lower()
        if Path(path).suffix in DIRECT_AUDIO_SUFFIXES | {".m3u8", ".mp4", ".mov", ".mkv", ".webm"}:
            return self.stored.resolve(lesson, force_refresh=force_refresh)
        return self.browser_resolver.resolve(lesson, force_refresh=force_refresh)
