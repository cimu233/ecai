"""Short-lived local cache for signed media sources."""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .media import MediaSource
from .models import Lesson


CACHE_VERSION = 1
DEFAULT_TTL_SECONDS = 30 * 60
EXPIRY_SAFETY_SECONDS = 5 * 60
EPOCH_MIN = 946684800
EPOCH_MAX = 4102444800
PERSISTABLE_HEADERS = {
    "accept",
    "accept-language",
    "origin",
    "referer",
    "user-agent",
}


def media_url_expiry(url: str) -> Optional[float]:
    query = parse_qs(urlparse(url).query)
    for key in ("expires", "expire", "expiry", "deadline", "e"):
        parsed = _parse_epoch((query.get(key) or [None])[0], base=10)
        if parsed is not None:
            return parsed

    token = (query.get("t") or [None])[0]
    if token and re.fullmatch(r"[0-9a-fA-F]{8,16}", token):
        parsed = _parse_epoch(token, base=16)
        if parsed is not None:
            return parsed
    return None


def _parse_epoch(value: Optional[str], base: int) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = int(value, base)
    except ValueError:
        return None
    if parsed > EPOCH_MAX:
        parsed //= 1000
    return float(parsed) if EPOCH_MIN <= parsed <= EPOCH_MAX else None


class MediaSourceCache:
    def __init__(self, safety_seconds: int = EXPIRY_SAFETY_SECONDS) -> None:
        self.safety_seconds = safety_seconds

    def load(
        self,
        path: Path,
        lesson: Lesson,
        now: Optional[float] = None,
    ) -> Optional[MediaSource]:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_at = float(payload["cached_at"])
            expires_at = float(payload["expires_at"])
            source = payload["source"]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            self._remove(path)
            return None

        current = time.time() if now is None else now
        if (
            payload.get("version") != CACHE_VERSION
            or payload.get("lesson_id") != lesson.id
            or payload.get("lesson_url") != lesson.source_url
            or expires_at <= current + self.safety_seconds
            or cached_at > current + 60
        ):
            self._remove(path)
            return None
        try:
            return MediaSource(
                url=str(source["url"]),
                kind=str(source["kind"]),
                headers={
                    str(key): str(value)
                    for key, value in dict(source.get("headers") or {}).items()
                    if str(key).lower() in PERSISTABLE_HEADERS
                },
                page_url=source.get("page_url"),
                expires_at=str(expires_at),
                audio_only=bool(source.get("audio_only")),
                bandwidth=source.get("bandwidth"),
            )
        except (KeyError, TypeError, ValueError):
            self._remove(path)
            return None

    def save(
        self,
        path: Path,
        lesson: Lesson,
        source: MediaSource,
        now: Optional[float] = None,
    ) -> bool:
        # Browser cookies stay in browser memory and are never persisted here.
        if source.cookies:
            return False
        current = time.time() if now is None else now
        expires_at = media_url_expiry(source.url) or current + DEFAULT_TTL_SECONDS
        if expires_at <= current + self.safety_seconds:
            return False
        payload = {
            "version": CACHE_VERSION,
            "lesson_id": lesson.id,
            "lesson_url": lesson.source_url,
            "cached_at": current,
            "expires_at": expires_at,
            "source": {
                "url": source.url,
                "kind": source.kind,
                "headers": {
                    key: value
                    for key, value in source.headers.items()
                    if key.lower() in PERSISTABLE_HEADERS
                },
                "page_url": source.page_url,
                "audio_only": source.audio_only,
                "bandwidth": source.bandwidth,
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".partial")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return True

    @staticmethod
    def invalidate(path: Path) -> None:
        MediaSourceCache._remove(path)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
