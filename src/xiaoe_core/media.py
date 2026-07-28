"""Media source contracts shared by resolvers and downloaders."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol
from urllib.parse import urlparse

from .models import Lesson


DIRECT_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class MediaSource:
    url: str
    kind: str
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    page_url: Optional[str] = None
    expires_at: Optional[str] = None
    audio_only: bool = False
    bandwidth: Optional[int] = None
    drm: bool = False

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc


@dataclass(frozen=True)
class DownloadRequest:
    lesson: Lesson
    source: MediaSource
    output_dir: Path


@dataclass(frozen=True)
class DownloadResult:
    file_path: Path
    size_bytes: int
    sha256: str
    duration_seconds: Optional[float]
    container: str
    codec: Optional[str]
    source_kind: str
    source_host: str


class MediaResolver(Protocol):
    def resolve(self, lesson: Lesson, force_refresh: bool = False) -> List[MediaSource]:
        ...


class StoredUrlResolver:
    """Temporary resolver for lesson rows that already contain a media URL."""

    def resolve(self, lesson: Lesson, force_refresh: bool = False) -> List[MediaSource]:
        del force_refresh
        if not lesson.source_url:
            raise DownloadError("source_unavailable", "Lesson has no media source URL.")
        path = urlparse(lesson.source_url).path.lower()
        suffix = Path(path).suffix
        if suffix == ".m3u8":
            kind = "hls"
        elif suffix in DIRECT_AUDIO_SUFFIXES:
            kind = "direct_audio"
        elif suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            kind = "video_file"
        else:
            raise DownloadError("source_unavailable", "Stored URL is not a recognized media URL.")
        return [MediaSource(url=lesson.source_url, kind=kind, audio_only=kind == "direct_audio")]


class MediaSelector:
    PRIORITY = {
        "direct_audio": 0,
        "hls_audio": 1,
        "hls": 2,
        "video_file": 3,
    }

    def select(self, sources: List[MediaSource]) -> MediaSource:
        usable = [source for source in sources if not source.drm]
        if not usable:
            if sources:
                raise DownloadError("unsupported_drm", "Only DRM-protected media sources were found.")
            raise DownloadError("source_unavailable", "No media sources were found.")
        return min(
            usable,
            key=lambda source: (
                self.PRIORITY.get(source.kind, 99),
                source.bandwidth if source.bandwidth is not None else 2**63,
            ),
        )
