"""Authenticated direct-audio and HLS download implementation."""

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .media import DIRECT_AUDIO_SUFFIXES, DownloadError, DownloadRequest, DownloadResult, MediaSource


StageCallback = Optional[Callable[[str], None]]
CHUNK_SIZE = 1024 * 1024
SAFE_REQUEST_HEADERS = {"accept", "accept-language", "authorization", "origin", "referer", "user-agent"}


def ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise DownloadError("missing_dependency", "imageio-ffmpeg is required.") from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def authenticated_headers(source: MediaSource) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for key, value in source.headers.items():
        if key.lower() in SAFE_REQUEST_HEADERS and value:
            _reject_header_newlines(key, value)
            headers[key] = value
    if source.cookies:
        cookie_value = "; ".join("{}={}".format(name, value) for name, value in source.cookies.items())
        _reject_header_newlines("Cookie", cookie_value)
        headers["Cookie"] = cookie_value
    headers.setdefault("User-Agent", "Mozilla/5.0")
    return headers


def _reject_header_newlines(key: str, value: str) -> None:
    if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
        raise DownloadError("invalid_headers", "Media request headers contain invalid newline characters.")


def ffmpeg_header_blob(headers: Dict[str, str]) -> str:
    return "".join("{}: {}\r\n".format(key, value) for key, value in headers.items())


def redact_error(message: str, source: MediaSource) -> str:
    redacted = message.replace(source.url, "<media-url>")
    for value in list(source.headers.values()) + list(source.cookies.values()):
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted[-1200:]


class AudioDownloader:
    def __init__(self, ffmpeg: Optional[str] = None, retries: int = 3, timeout: int = 60) -> None:
        self.ffmpeg = ffmpeg or ffmpeg_executable()
        self.retries = retries
        self.timeout = timeout

    def download(self, request: DownloadRequest, on_stage: StageCallback = None) -> DownloadResult:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        if request.source.kind == "direct_audio":
            partial_path, final_path = self._download_direct(request)
        elif request.source.kind in {"hls", "hls_audio", "video_file"}:
            partial_path, final_path = self._download_with_ffmpeg(request)
        else:
            raise DownloadError("unsupported_source", "Unsupported media kind: {}".format(request.source.kind))

        if on_stage:
            on_stage("verifying")
        try:
            duration, codec = self._verify_audio(partial_path, request.source)
        except DownloadError:
            if partial_path.exists():
                partial_path.unlink()
            resume_metadata = self._resume_metadata_path(partial_path)
            if resume_metadata.exists():
                resume_metadata.unlink()
            raise
        partial_path.replace(final_path)
        resume_metadata = self._resume_metadata_path(partial_path)
        if resume_metadata.exists():
            resume_metadata.unlink()
        return DownloadResult(
            file_path=final_path,
            size_bytes=final_path.stat().st_size,
            sha256=self._sha256(final_path),
            duration_seconds=duration,
            container=final_path.suffix.lower().lstrip("."),
            codec=codec,
            source_kind=request.source.kind,
            source_host=request.source.host,
        )

    def _download_direct(self, request: DownloadRequest) -> Tuple[Path, Path]:
        source = request.source
        extension = Path(urlparse(source.url).path).suffix.lower()
        if extension not in DIRECT_AUDIO_SUFFIXES:
            extension = ".m4a"
        final_path = request.output_dir / "audio.source{}".format(extension)
        partial_path = request.output_dir / "audio.partial{}".format(extension)
        metadata_path = self._resume_metadata_path(partial_path)
        headers = authenticated_headers(source)

        for attempt in range(1, self.retries + 1):
            offset = partial_path.stat().st_size if partial_path.exists() else 0
            request_headers = dict(headers)
            if offset:
                request_headers["Range"] = "bytes={}-".format(offset)
                metadata = self._read_resume_metadata(metadata_path)
                validator = metadata.get("etag") or metadata.get("last_modified")
                if validator:
                    request_headers["If-Range"] = validator
            try:
                remote_request = Request(source.url, headers=request_headers)
                with urlopen(remote_request, timeout=self.timeout) as response:
                    status = getattr(response, "status", response.getcode())
                    append = offset > 0 and status == 206
                    mode = "ab" if append else "wb"
                    content_length = response.headers.get("Content-Length")
                    total_size = int(content_length) + (offset if append and content_length else 0) if content_length else None
                    metadata_path.write_text(
                        json.dumps(
                            {
                                "etag": response.headers.get("ETag"),
                                "last_modified": response.headers.get("Last-Modified"),
                                "source_host": source.host,
                                "total_size": total_size,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    with partial_path.open(mode) as output:
                        while True:
                            chunk = response.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            output.write(chunk)
                break
            except HTTPError as error:
                if error.code in {401, 403}:
                    raise DownloadError("source_expired", "Media authorization expired.", retryable=True) from error
                if error.code == 416 and partial_path.exists() and partial_path.stat().st_size > 0:
                    break
                if attempt == self.retries:
                    raise DownloadError("http_error", "Media server returned HTTP {}.".format(error.code), True) from error
            except (URLError, OSError) as error:
                if attempt == self.retries:
                    raise DownloadError("network_error", "Direct audio download failed.", retryable=True) from error
            time.sleep(min(2**attempt, 8))

        if not partial_path.exists() or partial_path.stat().st_size == 0:
            raise DownloadError("empty_download", "Direct audio download produced an empty file.")
        return partial_path, final_path

    def _download_with_ffmpeg(self, request: DownloadRequest) -> Tuple[Path, Path]:
        source = request.source
        input_url = source.url
        if source.kind in {"hls", "hls_audio"}:
            input_url = self._select_hls_input(source)

        final_path = request.output_dir / "audio.source.m4a"
        partial_path = request.output_dir / "audio.partial.m4a"
        if partial_path.exists():
            partial_path.unlink()

        copy_command = self._ffmpeg_download_command(source, input_url, partial_path, copy_audio=True)
        try:
            copy_result = subprocess.run(copy_command, capture_output=True, text=True, timeout=10800)
        except subprocess.TimeoutExpired as error:
            raise DownloadError("ffmpeg_timeout", "ffmpeg exceeded the three-hour lesson timeout.", True) from error
        if copy_result.returncode != 0:
            if partial_path.exists():
                partial_path.unlink()
            transcode_command = self._ffmpeg_download_command(source, input_url, partial_path, copy_audio=False)
            try:
                transcode_result = subprocess.run(transcode_command, capture_output=True, text=True, timeout=10800)
            except subprocess.TimeoutExpired as error:
                raise DownloadError("ffmpeg_timeout", "ffmpeg exceeded the three-hour lesson timeout.", True) from error
            if transcode_result.returncode != 0:
                detail = redact_error(transcode_result.stderr or copy_result.stderr, source)
                if "401" in detail or "403" in detail:
                    raise DownloadError("source_expired", "Media authorization expired.", retryable=True)
                raise DownloadError("ffmpeg_failed", "ffmpeg could not extract audio: {}".format(detail), True)

        if not partial_path.exists() or partial_path.stat().st_size == 0:
            raise DownloadError("empty_download", "ffmpeg produced an empty audio file.")
        return partial_path, final_path

    def _ffmpeg_download_command(
        self,
        source: MediaSource,
        input_url: str,
        output_path: Path,
        copy_audio: bool,
    ) -> list:
        headers = authenticated_headers(source)
        command = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto",
            "-allowed_extensions",
            "ALL",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "10",
        ]
        if headers:
            command.extend(["-headers", ffmpeg_header_blob(headers)])
        command.extend(["-i", input_url, "-map", "0:a:0", "-vn"])
        if copy_audio:
            command.extend(["-c:a", "copy"])
        else:
            command.extend(["-c:a", "aac", "-b:a", "96k"])
        command.extend(["-movflags", "+faststart", "-f", "ipod", str(output_path)])
        return command

    def _select_hls_input(self, source: MediaSource) -> str:
        headers = authenticated_headers(source)
        try:
            request = Request(source.url, headers=headers)
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            if error.code in {401, 403}:
                raise DownloadError("source_expired", "HLS authorization expired.", retryable=True) from error
            raise DownloadError("http_error", "HLS server returned HTTP {}.".format(error.code), True) from error
        except (URLError, OSError) as error:
            raise DownloadError("network_error", "Unable to read HLS playlist.", retryable=True) from error
        if len(payload) > 2 * 1024 * 1024:
            raise DownloadError("invalid_playlist", "HLS playlist is unexpectedly large.")
        text = payload.decode("utf-8-sig", errors="replace")
        if "#EXTM3U" not in text:
            raise DownloadError("invalid_playlist", "Media source is not a valid HLS playlist.")
        upper = text.upper()
        if "METHOD=SAMPLE-AES" in upper or "KEYFORMAT=\"COM.APPLE.STREAMINGKEYDELIVERY\"" in upper:
            raise DownloadError("unsupported_drm", "DRM-protected HLS is not supported.")

        audio_urls = []
        variants = []
        lines = [line.strip() for line in text.splitlines()]
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-MEDIA:"):
                attributes = self._parse_attributes(line.split(":", 1)[1])
                if attributes.get("TYPE", "").upper() == "AUDIO" and attributes.get("URI"):
                    preferred = attributes.get("DEFAULT", "").upper() == "YES"
                    audio_urls.append((not preferred, urljoin(source.url, attributes["URI"])))
            elif line.startswith("#EXT-X-STREAM-INF:"):
                attributes = self._parse_attributes(line.split(":", 1)[1])
                next_url = next((item for item in lines[index + 1 :] if item and not item.startswith("#")), None)
                if next_url:
                    bandwidth_text = attributes.get("BANDWIDTH", "0")
                    bandwidth = int(bandwidth_text) if bandwidth_text.isdigit() else 0
                    has_audio_codec = "mp4a" in attributes.get("CODECS", "").lower()
                    variants.append((not has_audio_codec, bandwidth, urljoin(source.url, next_url)))

        if audio_urls:
            return sorted(audio_urls)[0][1]
        if variants:
            return sorted(variants)[0][2]
        return source.url

    @staticmethod
    def _parse_attributes(value: str) -> Dict[str, str]:
        attributes: Dict[str, str] = {}
        pattern = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', re.IGNORECASE)
        for match in pattern.finditer(value):
            raw = match.group(2).strip()
            attributes[match.group(1).upper()] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        return attributes

    def _verify_audio(self, path: Path, source: MediaSource) -> Tuple[Optional[float], Optional[str]]:
        if not path.exists() or path.stat().st_size == 0:
            raise DownloadError("verification_failed", "Audio file is missing or empty.")
        command = [
            self.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10800)
        if result.returncode != 0:
            detail = redact_error(result.stderr, source)
            raise DownloadError("verification_failed", "Audio validation failed: {}".format(detail))

        probe = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        metadata = probe.stderr
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", metadata)
        duration = None
        if duration_match:
            duration = (
                int(duration_match.group(1)) * 3600
                + int(duration_match.group(2)) * 60
                + float(duration_match.group(3))
            )
        codec_match = re.search(r"Audio:\s*([^,\s]+)", metadata)
        codec = codec_match.group(1) if codec_match else None
        return duration, codec

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _resume_metadata_path(partial_path: Path) -> Path:
        return partial_path.with_suffix(partial_path.suffix + ".json")

    @staticmethod
    def _read_resume_metadata(path: Path) -> Dict[str, object]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


def write_download_metadata(result: DownloadResult, target: Path) -> None:
    payload = {
        "file": result.file_path.name,
        "size_bytes": result.size_bytes,
        "sha256": result.sha256,
        "duration_seconds": result.duration_seconds,
        "container": result.container,
        "codec": result.codec,
        "source_kind": result.source_kind,
        "source_host": result.source_host,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
