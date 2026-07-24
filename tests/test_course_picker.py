import io
import unittest

from xiaoe_cli.course_picker import course_status_label, pick_course


class CoursePickerTest(unittest.TestCase):
    def test_internal_course_status_is_displayed_in_chinese(self):
        self.assertEqual("待扫描目录", course_status_label("pending_catalog"))
        self.assertEqual("目录已就绪", course_status_label("catalog_ready"))

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
