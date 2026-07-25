#!/usr/bin/python3
"""Print the lesson list for a course so the Shell menu can show it before
asking for positions.  Output goes to /dev/tty so it is visible even when
stdout is captured."""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xiaoe_cli.course_picker import display_lessons
from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.services import LessonService


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: list_lessons.py <course_id>", file=sys.stderr)
        return 2

    course_id = sys.argv[1]
    paths = AppPaths.resolve()
    lessons = LessonService(Database(paths.database_file))
    all_lessons = lessons.list_for_download(course_id)

    with open("/dev/tty", "w", encoding="utf-8") as tty:
        display_lessons([lesson.to_dict() for lesson in all_lessons], tty)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
