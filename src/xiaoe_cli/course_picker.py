"""Terminal course picker with ten-row pagination."""

from math import ceil
from typing import Callable, List, Optional, TextIO


def course_status_label(status: str) -> str:
    return {
        "pending_catalog": "待扫描目录",
        "catalog_ready": "目录已就绪",
        "running": "处理中",
        "completed": "已完成",
        "failed": "处理失败",
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
