import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from xiaoe_core.file_opener import open_with_default_app


class FileOpenerTest(unittest.TestCase):
    def test_each_platform_uses_its_default_open_command(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.txt"
            path.write_text("prompt", encoding="utf-8")
            commands = []

            def runner(command, **kwargs):
                commands.append(command)

                class Result:
                    returncode = 0

                return Result()

            open_with_default_app(path, platform="darwin", runner=runner)
            open_with_default_app(path, platform="win32", runner=runner)
            open_with_default_app(path, platform="linux", runner=runner)
            self.assertEqual("open", commands[0][0])
            self.assertEqual(["cmd", "/c", "start", ""], commands[1][:4])
            self.assertEqual("xdg-open", commands[2][0])

    def test_missing_file_is_rejected_before_opening(self):
        with self.assertRaises(RuntimeError):
            open_with_default_app(Path("/missing/prompt.txt"))


if __name__ == "__main__":
    unittest.main()
