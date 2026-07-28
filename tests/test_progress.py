import io
import time
import unittest

from xiaoe_core.progress import ProgressSpinner


class ProgressSpinnerTest(unittest.TestCase):
    def test_spinner_animates_and_clears_the_terminal_line(self):
        output = io.StringIO()

        with ProgressSpinner(
            "正在载入课程", enabled=True, stream=output, interval=0.005
        ):
            time.sleep(0.025)

        rendered = output.getvalue()
        self.assertIn("正在载入课程", rendered)
        self.assertIn("秒", rendered)
        self.assertGreaterEqual(rendered.count("\033[2K"), 2)


if __name__ == "__main__":
    unittest.main()
