"""Authenticated direct-audio and HLS download implementation."""

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .media import DIRECT_AUDIO_SUFFIXES, DownloadError, DownloadRequest, DownloadResult, MediaSource
from .progress import ProgressSpinner, clear_progress, finish_progress, update_progress


StageCallback = Optional[Callable[[str], None]]
CHUNK_SIZE = 1024 * 1024
PROGRESS_BAR_WIDTH = 30
SAFE_REQUEST_HEADERS = {"accept", "accept-language", "authorization", "origin", "referer", "user-agent"}


def ffmpeg_executable() -> str:
    # When packaged by PyInstaller, ffmpeg.exe is bundled alongside the exe.
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent) / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
    # Fall back to imageio-ffmpeg (downloads on first use).
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
    def __init__(
        self,
        ffmpeg: Optional[str] = None,
        retries: int = 3,
        timeout: int = 60,
        show_progress: bool = True,
    ) -> None:
        self.ffmpeg = ffmpeg or ffmpeg_executable()
        self.retries = retries
        self.timeout = timeout
        self.show_progress = show_progress
        self._progress_index = 0
        self._progress_total = 0
        self._progress_label = ""

    def set_progress_context(self, index: int, total: int) -> None:
        """Set the current file index and total for progress display."""
        self._progress_index = index
        self._progress_total = total

    def download(self, request: DownloadRequest, on_stage: StageCallback = None) -> DownloadResult:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        self._progress_label = request.lesson.title[:24]

        if self.show_progress and self._progress_total > 0:
            label = request.lesson.title[:48]
            update_progress(
                "  [{}/{}] 正在下载：{}...".format(
                    self._progress_index, self._progress_total, label
                )
            )

        if request.source.kind == "direct_audio":
            partial_path, final_path = self._download_direct(request)
        elif request.source.kind in {"hls", "hls_audio", "video_file"}:
            partial_path, final_path = self._download_with_ffmpeg(request, on_stage)
        else:
            raise DownloadError("unsupported_source", "Unsupported media kind: {}".format(request.source.kind))

        if on_stage:
            on_stage("verifying")
        try:
            with ProgressSpinner(
                "  [{}/{}] 正在校验音频：{}".format(
                    self._progress_index,
                    self._progress_total,
                    self._progress_label,
                ),
                enabled=None if self.show_progress else False,
            ):
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

        if self.show_progress and self._progress_total > 0:
            size_mb = final_path.stat().st_size / (1024 * 1024)
            finish_progress(
                "  [{}/{}] 处理完成 ✓  {}  ({:.1f} MB)".format(
                    self._progress_index,
                    self._progress_total,
                    request.lesson.title[:40],
                    size_mb,
                )
            )

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
                        downloaded = offset
                        transfer_started = time.monotonic()
                        transfer_start_bytes = offset
                        while True:
                            chunk = response.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            output.write(chunk)
                            downloaded += len(chunk)
                            if self.show_progress and total_size:
                                self._print_progress_bar(
                                    downloaded,
                                    total_size,
                                    request.lesson.title,
                                    transfer_started,
                                    transfer_start_bytes,
                                )
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

    def _download_with_ffmpeg(
        self,
        request: DownloadRequest,
        on_stage: StageCallback = None,
    ) -> Tuple[Path, Path]:
        source = request.source
        input_url = source.url
        expected_download_duration = None
        if source.kind in {"hls", "hls_audio"}:
            input_url = self._select_hls_input(source)
            expected_download_duration = self._hls_duration_seconds(input_url, source)

        final_path = request.output_dir / "audio.source.m4a"
        partial_path = request.output_dir / "audio.partial.m4a"
        downloaded_path = request.output_dir / "audio.downloaded.mka"
        download_partial_path = request.output_dir / "audio.download.partial.mka"
        if download_partial_path.exists():
            if self.show_progress and download_partial_path.stat().st_size > 0:
                finish_progress(
                    "  [{}/{}] 检测到未完成的媒体下载，将重新下载本节网络阶段。".format(
                        self._progress_index, self._progress_total
                    )
                )
            download_partial_path.unlink()

        if not downloaded_path.is_file() or downloaded_path.stat().st_size == 0:
            if on_stage:
                on_stage("downloading")
            download_command = self._ffmpeg_download_command(
                source,
                input_url,
                download_partial_path,
                output_format="matroska",
            )
            download_ok, download_stderr = self._run_ffmpeg_with_progress(
                download_command,
                source,
                action="下载中",
                output_path=download_partial_path,
                expected_duration=expected_download_duration,
            )
            if not download_ok:
                detail = redact_error(download_stderr, source)
                if "401" in detail or "403" in detail:
                    raise DownloadError("source_expired", "Media authorization expired.", retryable=True)
                raise DownloadError("ffmpeg_failed", "ffmpeg could not download audio: {}".format(detail), True)
            if not download_partial_path.exists() or download_partial_path.stat().st_size == 0:
                raise DownloadError("empty_download", "ffmpeg produced an empty downloaded audio file.")
            download_partial_path.replace(downloaded_path)

        if partial_path.exists():
            partial_path.unlink()
        if on_stage:
            on_stage("transcoding")
        duration, _codec = self._probe_audio(downloaded_path)
        remux_command = self._ffmpeg_local_command(downloaded_path, partial_path, copy_audio=True)
        remux_ok, remux_stderr = self._run_ffmpeg_with_progress(
            remux_command,
            source,
            action="转码中",
            output_path=partial_path,
            expected_duration=duration,
        )
        if not remux_ok:
            if partial_path.exists():
                partial_path.unlink()
            transcode_command = self._ffmpeg_local_command(
                downloaded_path,
                partial_path,
                copy_audio=False,
            )
            transcode_ok, transcode_stderr = self._run_ffmpeg_with_progress(
                transcode_command,
                source,
                action="转码中",
                output_path=partial_path,
                expected_duration=duration,
            )
            if not transcode_ok:
                detail = redact_error(transcode_stderr or remux_stderr, source)
                raise DownloadError("ffmpeg_failed", "ffmpeg could not convert audio: {}".format(detail), True)

        if not partial_path.exists() or partial_path.stat().st_size == 0:
            raise DownloadError("empty_download", "ffmpeg produced an empty audio file.")
        downloaded_path.unlink()
        return partial_path, final_path

    def _run_ffmpeg_with_progress(
        self,
        command: list,
        source: MediaSource,
        action: str,
        output_path: Path,
        expected_duration: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Run ffmpeg and stream progress to stderr. Returns (success, stderr_tail)."""
        progress_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
        speed_re = re.compile(r"speed=\s*([\d.]+)x")
        collected: list = []
        process = None
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise DownloadError("ffmpeg_failed", "Could not launch ffmpeg: {}".format(error)) from error

        try:
            if process.stderr is not None:
                for line in process.stderr:
                    collected.append(line)
                    if self.show_progress and self._progress_total > 0:
                        match = progress_re.search(line)
                        if match:
                            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
                            speed_match = speed_re.search(line)
                            speed = float(speed_match.group(1)) if speed_match else 0.0
                            processed = h * 3600 + m * 60 + s
                            percent = (
                                min(100.0, processed / expected_duration * 100)
                                if expected_duration
                                else None
                            )
                            eta = (
                                max(0.0, (expected_duration - processed) / speed)
                                if expected_duration and speed > 0
                                else None
                            )
                            size_mb = output_path.stat().st_size / (1024 * 1024) if output_path.exists() else 0.0
                            elapsed = max(0.001, time.monotonic() - started_at)
                            rate_mb = size_mb / elapsed
                            progress_text = (
                                "{:5.1f}%  预计剩余 {}".format(percent, self._format_eta(eta))
                                if percent is not None
                                else "已处理 {:02d}:{:02d}:{:04.1f}".format(h, m, s)
                            )
                            rate_text = (
                                "{:.2f} MB/s，已下载 {:.1f} MB".format(rate_mb, size_mb)
                                if action == "下载中"
                                else "{:.1f}x".format(speed) if speed > 0 else "?x"
                            )
                            update_progress(
                                "  [{}/{}] {}：{}  {}  {}".format(
                                    self._progress_index,
                                    self._progress_total,
                                    action,
                                    self._progress_label,
                                    progress_text,
                                    rate_text,
                                )
                            )
            process.wait(timeout=10800)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise DownloadError("ffmpeg_timeout", "ffmpeg exceeded the three-hour lesson timeout.", True)
        finally:
            if process is not None:
                if process.stderr is not None:
                    process.stderr.close()
                if process.stdout is not None:
                    process.stdout.close()
            if self.show_progress:
                clear_progress()

        stderr_tail = "".join(collected[-20:] if len(collected) > 20 else collected)
        return process.returncode == 0, stderr_tail

    def _ffmpeg_download_command(
        self,
        source: MediaSource,
        input_url: str,
        output_path: Path,
        output_format: str,
    ) -> list:
        headers = authenticated_headers(source)
        command = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-stats",
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
        command.extend([
            "-i",
            input_url,
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "copy",
            "-f",
            output_format,
            str(output_path),
        ])
        return command

    def _ffmpeg_local_command(
        self,
        input_path: Path,
        output_path: Path,
        copy_audio: bool,
    ) -> list:
        command = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-stats",
            "-i",
            str(input_path),
            "-map",
            "0:a:0",
            "-vn",
        ]
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

    def _hls_duration_seconds(self, url: str, source: MediaSource) -> Optional[float]:
        try:
            request = Request(url, headers=authenticated_headers(source))
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                return None
            text = payload.decode("utf-8-sig", errors="replace")
        except (HTTPError, URLError, OSError):
            return None
        durations = re.findall(r"#EXTINF:([\d.]+)", text, re.IGNORECASE)
        return sum(float(value) for value in durations) if durations else None

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

        return self._probe_audio(path)

    def _probe_audio(self, path: Path) -> Tuple[Optional[float], Optional[str]]:
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

    def _print_progress_bar(
        self,
        downloaded: int,
        total: int,
        title: str,
        started_at: float,
        starting_bytes: int,
    ) -> None:
        pct = min(downloaded / total, 1.0)
        filled = int(PROGRESS_BAR_WIDTH * pct)
        bar = "█" * filled + "░" * (PROGRESS_BAR_WIDTH - filled)
        current_mb = downloaded / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        unit = "MB"
        if total_mb >= 1000:
            current_mb = downloaded / (1024**3)
            total_mb = total / (1024**3)
            unit = "GB"
        elapsed = max(0.001, time.monotonic() - started_at)
        transferred = max(0, downloaded - starting_bytes)
        bytes_per_second = transferred / elapsed
        eta = (total - downloaded) / bytes_per_second if bytes_per_second > 0 else None
        update_progress(
            "  [{}/{}] 下载中：{}  {} {:5.1f}%  {:.1f}/{:.1f} {}  {:.2f} MB/s  剩余 {}".format(
                self._progress_index,
                self._progress_total,
                title[:24],
                bar,
                pct * 100,
                current_mb,
                total_mb,
                unit,
                bytes_per_second / (1024 * 1024),
                self._format_eta(eta),
            )
        )

    @staticmethod
    def _format_eta(seconds: Optional[float]) -> str:
        if seconds is None:
            return "计算中"
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
        return "{:02d}:{:02d}".format(minutes, seconds)

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
