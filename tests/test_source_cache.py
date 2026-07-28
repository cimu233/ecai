import stat
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.media import MediaSource
from xiaoe_core.models import Lesson
from xiaoe_core.source_cache import MediaSourceCache, media_url_expiry


def lesson() -> Lesson:
    return Lesson(
        id="course_1_lesson_1",
        course_id="course_1",
        position=1,
        title="Lesson",
        source_url="https://school.example.com/lesson/1",
        status="pending_source",
        error=None,
        attempt_count=0,
        last_error_code=None,
        last_error_at=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


class MediaSourceCacheTest(unittest.TestCase):
    def test_xiaoe_hex_expiry_is_parsed(self) -> None:
        self.assertEqual(
            1785298305.0,
            media_url_expiry("https://cdn.example.com/a.m3u8?t=6a697d81"),
        )

    def test_source_round_trip_uses_private_file_without_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media-source.json"
            now = 1785290000.0
            source = MediaSource(
                url="https://cdn.example.com/a.m3u8?t=6a697d81",
                kind="hls",
                headers={
                    "Referer": lesson().source_url or "",
                    "Authorization": "must-not-be-cached",
                },
                page_url=lesson().source_url,
            )
            cache = MediaSourceCache()

            self.assertTrue(cache.save(path, lesson(), source, now=now))
            restored = cache.load(path, lesson(), now=now + 60)

            self.assertIsNotNone(restored)
            self.assertEqual(source.url, restored.url)
            self.assertEqual({}, restored.cookies)
            self.assertNotIn("Authorization", restored.headers)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_expiring_source_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media-source.json"
            source = MediaSource(
                url="https://cdn.example.com/a.m3u8?t=6a697d81",
                kind="hls",
            )
            cache = MediaSourceCache()
            cache.save(path, lesson(), source, now=1785290000.0)

            self.assertIsNone(cache.load(path, lesson(), now=1785298100.0))
            self.assertFalse(path.exists())

    def test_browser_cookies_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media-source.json"
            source = MediaSource(
                url="https://cdn.example.com/a.m3u8?t=6a697d81",
                kind="hls",
                cookies={"session": "secret"},
            )

            self.assertFalse(
                MediaSourceCache().save(path, lesson(), source, now=1785290000.0)
            )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
