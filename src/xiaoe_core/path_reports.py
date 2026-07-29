"""Human-readable indexes for files produced by each pipeline stage."""

from pathlib import Path
from typing import Iterable, Optional, Tuple


STAGE_REPORT_NAMES = {
    "audio": "audio-files.txt",
    "transcript": "transcript-files.txt",
    "structure": "structure-files.txt",
}


def course_output_dir(courses_dir: Path, course_id: str) -> Path:
    return courses_dir / course_id


def write_stage_report(
    courses_dir: Path,
    course_id: str,
    stage: str,
    rows: Iterable[Tuple[int, str, Optional[str]]],
) -> Path:
    report_path = course_output_dir(courses_dir, course_id) / STAGE_REPORT_NAMES[stage]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["序号\t课程标题\t文件路径"]
    for position, title, file_path in rows:
        if file_path:
            lines.append("{}\t{}\t{}".format(position, title, file_path))
    temporary = report_path.with_suffix(report_path.suffix + ".partial")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(report_path)
    return report_path
