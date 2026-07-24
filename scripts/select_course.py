#!/usr/bin/python3
"""Desktop launcher helper that prints only the selected course ID to stdout."""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xiaoe_cli.course_picker import pick_course
from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.services import CourseService


def main() -> int:
    paths = AppPaths.resolve()
    courses = [course.to_dict() for course in CourseService(Database(paths.database_file)).list_courses()]
    with open("/dev/tty", "r", encoding="utf-8") as terminal_input, open(
        "/dev/tty", "w", encoding="utf-8"
    ) as terminal_output:
        selected = pick_course(courses, lambda prompt: _read(terminal_input, terminal_output, prompt), terminal_output)
    if selected:
        print(selected)
        return 0
    return 1


def _read(terminal_input, terminal_output, prompt: str) -> str:
    terminal_output.write(prompt)
    terminal_output.flush()
    return terminal_input.readline()


if __name__ == "__main__":
    raise SystemExit(main())
