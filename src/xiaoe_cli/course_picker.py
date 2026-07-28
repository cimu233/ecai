"""Terminal course picker and lesson selector with pagination.

Every entry point (CLI, Shell menu, scripts) uses these functions so users
always see the same course list, lesson list, and position picker.
"""

from math import ceil
from typing import Callable, List, Optional, Set, TextIO
import unicodedata

from xiaoe_core.services import parse_positions


def terminal_width(value: object) -> int:
    width = 0
    for character in str(value):
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def table_cell(value: object, width: int, align: str = "left") -> str:
    text = str(value)
    if terminal_width(text) > width:
        kept = ""
        for character in text:
            if terminal_width(kept + character + "…") > width:
                break
            kept += character
        text = kept + "…"
    padding = max(0, width - terminal_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


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
        "transcoding": "转码中",
        "audio_ready": "已下载",
        "transcribing": "转写中",
        "transcript_ready": "已转写",
        "structuring": "整理中",
        "completed": "已完成",
        "download_failed": "下载失败",
        "source_unavailable": "无音频源",
        "no_media": "图文/无媒体",
        "transcription_failed": "转写失败",
        "structure_failed": "整理失败",
    }.get(status, status or "未知")


def lesson_media_label(content_type: str) -> str:
    return {
        "text": "图文",
        "audio": "音频",
        "video": "视频",
        "live_replay": "直播回放",
        "live": "直播/待探测",
        "unknown": "待探测",
    }.get(content_type, "待探测")


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
        output.write("{}  {}  {}  {}\n".format(
            table_cell("序号", 4),
            table_cell("课程名称", 30),
            table_cell("状态", 18),
            "课程 ID",
        ))
        output.write("-" * 90 + "\n")
        for index, course in enumerate(current, 1):
            title = str(course.get("title") or "未命名课程")
            output.write("{}  {}  {}  {}\n".format(
                table_cell(index, 4),
                table_cell(title, 30),
                table_cell(course_status_label(str(course.get("status") or "")), 18),
                course.get("id") or "",
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
    output.write("{}  {}  {}  {}\n".format(
        table_cell("序号", 6),
        table_cell("标题", 34),
        table_cell("类型", 14),
        table_cell("状态", 16),
    ))
    output.write("-" * 74 + "\n")
    for lesson in lessons:
        position = int(lesson.get("position", 0))
        title = str(lesson.get("title") or "未命名")
        media_display = lesson_media_label(str(lesson.get("content_type") or ""))
        status_display = lesson_status_label(str(lesson.get("status") or ""))
        output.write(
            "{}  {}  {}  {}\n".format(
                table_cell(position, 6),
                table_cell(title, 34),
                table_cell(media_display, 14),
                table_cell(status_display, 16),
            )
        )
    output.write("-" * 74 + "\n")
    output.flush()


def display_download_overview(rows: List[dict], output: TextIO) -> None:
    if not rows:
        output.write("该课程暂无可处理的小节。\n")
        return
    state_labels = {
        "complete": "完整",
        "downloaded_pending_conversion": "已下载/待转码",
        "partial": "中断/残缺",
        "no_media": "无音视频",
        "source_cached": "待下载（源已缓存）",
        "pending": "待下载",
    }
    counts = {
        key: sum(row.get("local_state") == key for row in rows)
        for key in state_labels
    }
    complete_bytes = sum(
        int(row.get("size_bytes") or 0)
        for row in rows
        if row.get("local_state") == "complete"
    )
    output.write("\n下载前状态（共 {} 节）：\n".format(len(rows)))
    output.write(
        "完整 {}｜中断 {}｜待下载 {}｜无音视频 {}｜完整文件 {:.2f} GB\n".format(
            counts["complete"],
            counts["partial"],
            counts["pending"] + counts["source_cached"],
            counts["no_media"],
            complete_bytes / 1024 / 1024 / 1024,
        )
    )
    if counts["downloaded_pending_conversion"]:
        output.write(
            "另有 {} 节网络下载已完成，等待本地转码。\n".format(
                counts["downloaded_pending_conversion"]
            )
        )
    output.write("{}  {}  {}  {}  {}\n".format(
        table_cell("序号", 6),
        table_cell("标题", 31),
        table_cell("类型", 13),
        table_cell("本地状态", 17),
        table_cell("大小", 9, "right"),
    ))
    output.write("-" * 82 + "\n")
    for row in rows:
        title = str(row.get("title") or "未命名")
        size_bytes = int(row.get("size_bytes") or 0)
        size = "{:.1f} MB".format(size_bytes / 1024 / 1024) if size_bytes else "-"
        output.write(
            "{}  {}  {}  {}  {}\n".format(
                table_cell(int(row.get("position") or 0), 6),
                table_cell(title, 31),
                table_cell(lesson_media_label(str(row.get("content_type") or "")), 13),
                table_cell(state_labels.get(str(row.get("local_state") or ""), "未知"), 17),
                table_cell(size, 9, "right"),
            )
        )
    output.write("-" * 82 + "\n")
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
