import io
import unittest

from xiaoe_cli.course_picker import (
    course_status_label,
    display_download_overview,
    display_lessons,
    pick_course,
    table_cell,
    terminal_width,
)


class CoursePickerTest(unittest.TestCase):
    def test_internal_course_status_is_displayed_in_chinese(self):
        self.assertEqual("待扫描目录", course_status_label("pending_catalog"))
        self.assertEqual("目录已就绪", course_status_label("catalog_ready"))

    def test_table_cells_align_chinese_by_terminal_width(self):
        chinese = table_cell("中文", 8)
        latin = table_cell("text", 8)
        self.assertEqual(8, terminal_width(chinese))
        self.assertEqual(8, terminal_width(latin))
        self.assertEqual(8, terminal_width(table_cell("很长的中文标题", 8)))

    def courses(self, count):
        return [
            {"id": "course_{:02d}".format(index), "title": "Course {}".format(index), "status": "ready"}
            for index in range(1, count + 1)
        ]

    def test_selects_from_second_page(self):
        choices = iter(["n", "2"])
        output = io.StringIO()
        selected = pick_course(self.courses(12), lambda _: next(choices), output)
        self.assertEqual("course_12", selected)
        self.assertIn("第 2/2 页", output.getvalue())

    def test_invalid_choice_can_be_corrected(self):
        choices = iter(["11", "1"])
        output = io.StringIO()
        selected = pick_course(self.courses(3), lambda _: next(choices), output)
        self.assertEqual("course_01", selected)
        self.assertIn("当前页没有这个序号", output.getvalue())

    def test_empty_list_returns_none(self):
        output = io.StringIO()
        self.assertIsNone(pick_course([], lambda _: "", output))

    def test_lesson_list_displays_catalog_media_type(self):
        output = io.StringIO()
        display_lessons(
            [
                {
                    "position": 1,
                    "title": "Announcement",
                    "content_type": "text",
                    "status": "no_media",
                },
                {
                    "position": 2,
                    "title": "Replay",
                    "content_type": "live_replay",
                    "status": "pending_source",
                },
            ],
            output,
        )

        rendered = output.getvalue()
        self.assertIn("类型", rendered)
        self.assertIn("图文", rendered)
        self.assertIn("直播回放", rendered)

    def test_download_overview_summarizes_complete_partial_and_pending(self):
        output = io.StringIO()
        display_download_overview(
            [
                {
                    "position": 1,
                    "title": "Complete",
                    "content_type": "video",
                    "local_state": "complete",
                    "size_bytes": 100 * 1024 * 1024,
                },
                {
                    "position": 2,
                    "title": "Interrupted",
                    "content_type": "live_replay",
                    "local_state": "partial",
                    "size_bytes": 25 * 1024 * 1024,
                },
                {
                    "position": 3,
                    "title": "Announcement",
                    "content_type": "text",
                    "local_state": "no_media",
                    "size_bytes": 0,
                },
                {
                    "position": 4,
                    "title": "Pending",
                    "content_type": "audio",
                    "local_state": "pending",
                    "size_bytes": 0,
                },
                {
                    "position": 5,
                    "title": "Downloaded",
                    "content_type": "video",
                    "local_state": "downloaded_pending_conversion",
                    "size_bytes": 50 * 1024 * 1024,
                },
            ],
            output,
        )

        rendered = output.getvalue()
        self.assertIn("完整 1｜中断 1｜待下载 1｜无音视频 1", rendered)
        self.assertIn("100.0 MB", rendered)
        self.assertIn("25.0 MB", rendered)
        self.assertIn("1 节网络下载已完成", rendered)
        self.assertIn("已下载/待转码", rendered)
