import hashlib
import mimetypes
import subprocess
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xiaoe_core.downloader import AudioDownloader, ffmpeg_executable
from xiaoe_core.media import DownloadError, DownloadRequest, MediaSource
from xiaoe_core.models import Lesson


class LocalMediaServer:
    def __init__(self, root: Path, require_auth: bool = True) -> None:
        self.root = root
        self.require_auth = require_auth
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers.items())))
                if owner.require_auth:
                    if self.headers.get("Referer") != "https://authorized.example/":
                        self.send_error(403)
                        return
                    if "session=local-test" not in self.headers.get("Cookie", ""):
                        self.send_error(403)
                        return
                relative = self.path.split("?", 1)[0].lstrip("/")
                file_path = (owner.root / relative).resolve()
                if owner.root.resolve() not in file_path.parents or not file_path.is_file():
                    self.send_error(404)
                    return
                payload = file_path.read_bytes()
                start = 0
                range_header = self.headers.get("Range")
                if range_header and range_header.startswith("bytes="):
                    start_text = range_header.split("=", 1)[1].split("-", 1)[0]
                    start = int(start_text)
                    if start >= len(payload):
                        self.send_response(416)
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes {}-{}/{}".format(start, len(payload) - 1, len(payload)))
                else:
                    self.send_response(200)
                content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload) - start))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"local-test-etag"')
                self.send_header("Last-Modified", "Wed, 01 Jan 2026 00:00:00 GMT")
                self.end_headers()
                self.wfile.write(payload[start:])

            def log_message(self, format, *args):
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:{}".format(self.server.server_port)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def make_lesson(source_url: str) -> Lesson:
    return Lesson(
        id="lesson_0001",
        course_id="course_test",
        position=1,
        title="Test Lesson",
        source_url=source_url,
        status="pending_source",
        error=None,
        attempt_count=0,
        last_error_code=None,
        last_error_at=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def auth_source(url: str, kind: str) -> MediaSource:
    return MediaSource(
        url=url,
        kind=kind,
        headers={"Referer": "https://authorized.example/"},
        cookies={"session": "local-test"},
    )


class AudioDownloaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.media_dir = self.root / "media"
        self.media_dir.mkdir()
        self.output_dir = self.root / "output"
        self.ffmpeg = ffmpeg_executable()
        self.server = LocalMediaServer(self.media_dir)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temp_dir.cleanup()

    def test_direct_audio_resumes_and_preserves_bytes(self) -> None:
        source_path = self.media_dir / "audio.wav"
        self._write_wav(source_path)
        payload = source_path.read_bytes()
        self.output_dir.mkdir()
        partial = self.output_dir / "audio.partial.wav"
        partial.write_bytes(payload[:512])
        url = self.server.base_url + "/audio.wav"
        source = auth_source(url, "direct_audio")

        result = AudioDownloader(ffmpeg=self.ffmpeg).download(
            DownloadRequest(lesson=make_lesson(url), source=source, output_dir=self.output_dir)
        )

        self.assertEqual(payload, result.file_path.read_bytes())
        self.assertEqual(hashlib.sha256(payload).hexdigest(), result.sha256)
        self.assertTrue(
            any(headers.get("Range") == "bytes=512-" for path, headers in self.server.requests if path == "/audio.wav")
        )
        self.assertFalse(partial.exists())
        self.assertFalse((self.output_dir / "audio.partial.wav.json").exists())

    def test_hls_master_prefers_independent_audio_and_keeps_auth(self) -> None:
        self._generate_audio_hls("audio")
        self._generate_mixed_hls("mixed")
        (self.media_dir / "master.m3u8").write_text(
            """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="main",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.42e01e,mp4a.40.2",AUDIO="audio"
mixed.m3u8
""",
            encoding="utf-8",
        )
        url = self.server.base_url + "/master.m3u8"
        source = auth_source(url, "hls")

        result = AudioDownloader(ffmpeg=self.ffmpeg).download(
            DownloadRequest(lesson=make_lesson(url), source=source, output_dir=self.output_dir)
        )

        self.assertGreater(result.size_bytes, 0)
        requested_paths = [path for path, _headers in self.server.requests]
        self.assertIn("/audio.m3u8", requested_paths)
        self.assertNotIn("/mixed.m3u8", requested_paths)
        self.assertTrue(all("session=local-test" in headers.get("Cookie", "") for _, headers in self.server.requests))

    def test_mixed_hls_output_has_no_video_track(self) -> None:
        self._generate_mixed_hls("mixed")
        url = self.server.base_url + "/mixed.m3u8"
        source = auth_source(url, "hls")

        result = AudioDownloader(ffmpeg=self.ffmpeg).download(
            DownloadRequest(lesson=make_lesson(url), source=source, output_dir=self.output_dir)
        )

        video_probe = subprocess.run(
            [self.ffmpeg, "-v", "error", "-i", str(result.file_path), "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, video_probe.returncode)
        self.assertIn("matches no streams", video_probe.stderr)

    def test_standard_aes_128_hls_is_supported(self) -> None:
        key_path = self.media_dir / "audio.key"
        key_path.write_bytes(b"0123456789abcdef")
        key_info = self.root / "key-info.txt"
        key_info.write_text(
            "{}\n{}\n{}\n".format(
                self.server.base_url + "/audio.key",
                key_path,
                "0123456789abcdef0123456789abcdef",
            ),
            encoding="utf-8",
        )
        self._run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=750:sample_rate=16000",
                "-t",
                "1.5",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "0.5",
                "-hls_list_size",
                "0",
                "-hls_key_info_file",
                str(key_info),
                str(self.media_dir / "encrypted.m3u8"),
            ]
        )
        url = self.server.base_url + "/encrypted.m3u8"

        result = AudioDownloader(ffmpeg=self.ffmpeg).download(
            DownloadRequest(lesson=make_lesson(url), source=auth_source(url, "hls"), output_dir=self.output_dir)
        )

        self.assertGreater(result.size_bytes, 0)
        self.assertIn("/audio.key", [path for path, _headers in self.server.requests])

    def test_sample_aes_is_rejected_without_download(self) -> None:
        (self.media_dir / "drm.m3u8").write_text(
            """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="license"
#EXTINF:10,
segment.ts
""",
            encoding="utf-8",
        )
        url = self.server.base_url + "/drm.m3u8"

        with self.assertRaises(DownloadError) as raised:
            AudioDownloader(ffmpeg=self.ffmpeg).download(
                DownloadRequest(lesson=make_lesson(url), source=auth_source(url, "hls"), output_dir=self.output_dir)
            )

        self.assertEqual("unsupported_drm", raised.exception.code)

    def test_ffmpeg_error_redacts_url_and_cookie(self) -> None:
        (self.media_dir / "broken.m3u8").write_text(
            """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10,
missing.ts
#EXT-X-ENDLIST
""",
            encoding="utf-8",
        )
        url = self.server.base_url + "/broken.m3u8"

        with self.assertRaises(DownloadError) as raised:
            AudioDownloader(ffmpeg=self.ffmpeg).download(
                DownloadRequest(lesson=make_lesson(url), source=auth_source(url, "hls"), output_dir=self.output_dir)
            )

        message = str(raised.exception)
        self.assertNotIn(url, message)
        self.assertNotIn("local-test", message)

    def _write_wav(self, path: Path) -> None:
        frames = b"\x00\x00" * 16000
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(frames)

    def _generate_audio_hls(self, stem: str) -> None:
        self._run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=16000",
                "-t",
                "1.5",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "0.5",
                "-hls_list_size",
                "0",
                str(self.media_dir / "{}.m3u8".format(stem)),
            ]
        )

    def _generate_mixed_hls(self, stem: str) -> None:
        self._run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=64x64:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=500:sample_rate=16000",
                "-t",
                "1.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "-f",
                "hls",
                "-hls_time",
                "0.5",
                "-hls_list_size",
                "0",
                str(self.media_dir / "{}.m3u8".format(stem)),
            ]
        )

    def _run_ffmpeg(self, arguments) -> None:
        subprocess.run(
            [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + arguments,
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
