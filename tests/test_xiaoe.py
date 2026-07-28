import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoe_core.browser import BrowserError
from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.services import CourseService, LessonService
from xiaoe_core.models import Lesson
from xiaoe_core.xiaoe import (
    XiaoeAccountCatalogService,
    XiaoeBrowserMediaResolver,
    XiaoeCatalogService,
    find_account_course_items,
    find_catalog_items,
)


class XiaoeParsingTest(unittest.TestCase):
    def test_playback_expression_avoids_blanket_player_container_clicks(self):
        expression = XiaoeBrowserMediaResolver._play_expression()

        self.assertIn("diagnostic|network", expression)
        self.assertIn(".vjs-big-play-button", expression)
        self.assertNotIn("wrappers.forEach", expression)
        self.assertNotIn("div[class*=\"play\"]", expression)

    def test_catalog_items_are_found_recursively_and_deduplicated(self):
        payload = {
            "data": {
                "groups": [{
                    "list": [
                        {"resource_id": "r1", "jump_url": "/lesson/1", "title": "One"},
                        {"resource_id": "r1", "jump_url": "/lesson/1", "title": "One"},
                        {"resourceId": "r2", "jumpUrl": "/lesson/2", "name": "Two"},
                    ]
                }]
            }
        }
        items = find_catalog_items(payload)
        self.assertEqual(2, len(items))

    def test_media_urls_keep_hls_and_audio_only(self):
        events = [
            {"method": "Network.responseReceived", "params": {"response": {"url": "https://cdn/a.js", "mimeType": "text/javascript"}}},
            {"method": "Network.responseReceived", "params": {"response": {"url": "https://cdn/master.m3u8?token=x", "mimeType": "application/vnd.apple.mpegurl"}}},
            {"method": "Network.responseReceived", "params": {"response": {"url": "https://cdn/audio.m4a", "mimeType": "audio/mp4"}}},
        ]
        urls = XiaoeBrowserMediaResolver._media_urls(events)
        self.assertEqual(["https://cdn/master.m3u8?token=x", "https://cdn/audio.m4a"], urls)

    def test_catalog_expansion_supports_xiaoe_collapsed_chapters(self):
        expression = XiaoeCatalogService._expand_expression()
        self.assertIn('[aria-label="展开"]', expression)
        self.assertIn("querySelectorAll", expression)
        self.assertIn(".slice(0, 25)", expression)
        self.assertIn("await wait(350)", expression)

    def test_performance_entries_expose_nested_media_url(self):
        media_url = "https://cdn.example.com/audio/playlist_eof.m3u8?t=1234"
        report_url = (
            "https://report.example.com/collect?params%5Bplay_url%5D="
            "https%3A%2F%2Fcdn.example.com%2Faudio%2Fplaylist_eof.m3u8%3Ft%3D1234"
        )

        class Page:
            def command(self, method, params):
                self.method = method
                self.params = params
                return {"result": {"value": json.dumps([report_url])}}

        self.assertEqual(
            [media_url],
            XiaoeBrowserMediaResolver._performance_media_urls(Page()),
        )

    def test_force_refresh_reuses_authenticated_gateway(self):
        browser = mock.Mock()
        browser.capture_media.return_value = {
            "state": {"status": "authenticated"},
            "events": [
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "response": {
                            "url": "https://cdn.example.com/a.m3u8?t=1234",
                            "mimeType": "application/vnd.apple.mpegurl",
                        }
                    },
                }
            ],
            "cookies": [],
        }
        current_lesson = Lesson(
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

        XiaoeBrowserMediaResolver(browser).resolve(
            current_lesson, force_refresh=True
        )

        browser.invalidate_gateway.assert_not_called()

    def test_catalog_position_prefers_global_sort_value(self):
        item = {"sort_value": "49", "sort_c": "1"}
        self.assertEqual(49, XiaoeCatalogService._position(item, 999))

    def test_account_course_items_exclude_individual_live_resources(self):
        rows = [
            {
                "resource_type": 4,
                "resources_id": "live_1",
                "jump_url": "/live/1",
                "detail_resources": {"title": "Single live"},
            },
            {
                "resource_type": 6,
                "resources_id": "column_1",
                "jump_url": "/column/1",
                "detail_resources": {"title": "Column"},
            },
            {
                "resource_type": "50",
                "resource_id": "course_1",
                "jumpUrl": "/course/1",
                "resource_title": "Course",
            },
            {
                "resource_type": 50,
                "resource_id": "course_2",
                "h5_url": "https://store.example.com/course/2",
                "title": "Central account course",
            },
        ]
        items = find_account_course_items(rows)
        self.assertEqual(["column_1", "course_1", "course_2"], [
            item.get("resources_id") or item.get("resource_id") for item in items
        ])


class XiaoeAccountCatalogServiceTest(unittest.TestCase):
    def test_cached_catalog_does_not_hide_ego_control_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.resolve(directory)
            paths.create()
            database = Database(paths.database_file)
            courses = CourseService(database)
            lessons = LessonService(database)
            course = courses.add_course(
                "https://store.example.com/p/course/course_1", "Course"
            ).course
            raw_path = paths.courses_dir / course.id / "catalog.raw.json"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "resource_id": "lesson_1",
                                "jump_url": "/lesson/1",
                                "title": "Cached lesson",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            class PausedBrowser:
                def ensure_running(self, visible=False):
                    return "11"

                def capture_catalog(self, url, expression, wait_seconds):
                    raise BrowserError("ego_user_control", "Agent control stopped.")

            with self.assertRaises(BrowserError):
                XiaoeCatalogService(
                    paths, courses, lessons, PausedBrowser()
                ).refresh(course.id)

    def test_scan_imports_course_containers_and_deduplicates_existing_course(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.resolve(directory)
            paths.create()
            courses = CourseService(Database(paths.database_file))
            existing = courses.add_course(
                "https://store.example.com/p/course/course_1?from=saved",
                "Existing",
            ).course

            class FakeBrowser:
                def capture_account_courses(self, origin):
                    self.origin = origin
                    return {
                        "state": {"status": "authenticated"},
                        "rows": [
                            {
                                "resource_type": 50,
                                "resources_id": "course_1",
                                "jump_url": "/p/course/course_1",
                                "detail_resources": {"title": "Existing remote"},
                            },
                            {
                                "resource_type": 6,
                                "resources_id": "column_2",
                                "jump_url": "/p/column/column_2",
                                "detail_resources": {"title": "New column"},
                            },
                            {
                                "resource_type": 4,
                                "resources_id": "live_3",
                                "jump_url": "/p/live/live_3",
                                "detail_resources": {"title": "Single live"},
                            },
                        ],
                    }

            browser = FakeBrowser()
            result = XiaoeAccountCatalogService(paths, courses, browser).scan()

            self.assertEqual("https://store.example.com", browser.origin)
            self.assertEqual(3, result["scanned_resources"])
            self.assertEqual(2, result["course_candidates"])
            self.assertEqual(1, result["imported"])
            self.assertEqual(1, result["existing"])
            self.assertEqual(1, result["skipped_non_course"])
            self.assertEqual(2, len(courses.list_courses()))
            self.assertIn(existing.id, [course["id"] for course in result["courses"]])
            raw = json.loads(Path(result["raw_file"]).read_text(encoding="utf-8"))
            self.assertEqual(3, len(raw["items"]))


if __name__ == "__main__":
    unittest.main()
