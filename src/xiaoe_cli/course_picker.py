"""Terminal course picker and lesson selector with pagination.

Every entry point (CLI, Shell menu, scripts) uses these functions so users
always see the same course list, lesson list, and position picker.
"""

from math import ceil
from typing import Callable, List, Optional, Set, TextIO

from xiaoe_core.services import parse_positions


def course_status_label(status: str) -> str:
    return {
        "pending_catalog": "待扫描目录",
        "catalog_ready": "目录已就绪",
        "running": "处理中",
        "completed": "已完成",
        "failed": "处理失败",
    }.get(status, status or "未知")


def lesson_status_label(status: str) -> str:
    return {
        "pending_source": "待解析",
        "resolving_source": "解析中",
        "downloading": "下载中",
        "audio_ready": "已下载",
        "transcribing": "转写中",
        "transcript_ready": "已转写",
        "structuring": "整理中",
        "completed": "已完成",
        "download_failed": "下载失败",
        "source_unavailable": "无音频源",
        "transcription_failed": "转写失败",
        "structure_failed": "整理失败",
    }.get(status, status or "未知")


def pick_course(
    courses: List[dict],
    input_fn: Callable[[str], str],
    output: TextIO,
    page_size: int = 10,
) -> Optional[str]:
    if not courses:
        output.write("目前没有课程，请先添加课程。\n")
        return None
    page = 0
    total_pages = max(1, ceil(len(courses) / page_size))
    while True:
        start = page * page_size
        current = courses[start:start + page_size]
        output.write("\n课程列表（第 {}/{} 页，共 {} 门）\n".format(page + 1, total_pages, len(courses)))
        output.write("{:<4} {:<30} {:<18} {}\n".format("序号", "课程名称", "状态", "课程 ID"))
        output.write("-" * 90 + "\n")
        for index, course in enumerate(current, 1):
            title = str(course.get("title") or "未命名课程")
            if len(title) > 28:
                title = title[:27] + "…"
            output.write("{:<4} {:<30} {:<18} {}\n".format(
                index, title, course_status_label(str(course.get("status") or "")), course.get("id") or ""
            ))
        output.write("\n输入序号选择 | n 下一页 | p 上一页 | q 返回\n")
        output.flush()
        choice = input_fn("请选择：").strip().lower()
        if choice == "q":
            return None
        if choice == "n":
            if page + 1 < total_pages:
                page += 1
            else:
                output.write("已经是最后一页。\n")
            continue
        if choice == "p":
            if page > 0:
                page -= 1
            else:
                output.write("已经是第一页。\n")
            continue
        try:
            selected = int(choice)
        except ValueError:
            output.write("请输入当前页的序号。\n")
            continue
        if 1 <= selected <= len(current):
            return str(current[selected - 1]["id"])
        output.write("当前页没有这个序号。\n")


def display_lessons(
    lessons: List[dict],
    output: TextIO,
    page_size: int = 30,
) -> None:
    """Print a lesson list with status labels.

    Called by every download / transcribe entry point so users always see
    what is available before choosing positions.
    """
    if not lessons:
        output.write("该课程暂无小节内容，请先扫描课程目录。\n")
        return

    output.write("\n课程小节列表（共 {} 节）：\n".format(len(lessons)))
    output.write("{:<6} {:<40} {:<16}\n".format("序号", "标题", "状态"))
    output.write("-" * 66 + "\n")
    for lesson in lessons:
        position = int(lesson.get("position", 0))
        title = str(lesson.get("title") or "未命名")
        if len(title) > 38:
            title = title[:37] + "…"
        status_display = lesson_status_label(str(lesson.get("status") or ""))
        output.write("{:<6} {:<40} {:<16}\n".format(position, title, status_display))
    output.write("-" * 66 + "\n")
    output.flush()


def pick_lesson_positions(
    lessons: List[dict],
    input_fn: Callable[[str], str],
    output: TextIO,
) -> Optional[Set[int]]:
    """Let the user select lesson positions interactively.

    Returns a set of position integers, or ``None`` for "all lessons".
    Returns an empty set if the user cancels.
    """
    display_lessons(lessons, output)

    output.write("\n选择范围（与 CLI --positions 格式相同）：\n")
    output.write("  all / 回车   — 全部\n")
    output.write("  88           — 仅第 88 节\n")
    output.write("  88-93        — 第 88 至 93 节\n")
    output.write("  1,5,10       — 第 1、5、10 节\n")
    output.write("  88-93,1,5   — 混合\n")
    output.flush()

    choice = input_fn("请输入：").strip()
    if not choice or choice.lower() == "all":
        return None  # None means "all"

    try:
        return parse_positions(choice)
    except ValueError as exc:
        output.write("格式错误：{}\n".format(exc))
        return set()  # empty = cancelled / invalid
