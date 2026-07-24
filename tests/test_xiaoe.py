import json
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.services import CourseService
from xiaoe_core.xiaoe import (
    XiaoeAccountCatalogService,
    XiaoeBrowserMediaResolver,
    XiaoeCatalogService,
    find_account_course_items,
    find_catalog_items,
)


class XiaoeParsingTest(unittest.TestCase):
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
        self.assertIn("await wait(900)", expression)

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
        ]
        items = find_account_course_items(rows)
        self.assertEqual(["column_1", "course_1"], [
            item.get("resources_id") or item.get("resource_id") for item in items
        ])


class XiaoeAccountCatalogServiceTest(unittest.TestCase):
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
