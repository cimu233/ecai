"""Interactive selection of failed lessons with an automatic timeout."""

import os
import select
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, TextIO, Tuple

from xiaoe_core.services import parse_positions


def failed_lesson_positions(result: Any, lessons: List[dict]) -> Dict[int, Set[str]]:
    lesson_positions = {
        str(lesson.get("id")): int(lesson.get("position") or 0)
        for lesson in lessons
    }
    failures: Dict[int, Set[str]] = {}
    payload = result.to_dict()
    for stage, label in (
        ("download", "音频下载"),
        ("transcription", "语音转文字"),
        ("structure", "结构化整理"),
    ):
        for item in payload.get(stage, {}).get("items", []):
            if not item.get("error"):
                continue
            position = lesson_positions.get(str(item.get("lesson_id")))
            if position:
                failures.setdefault(position, set()).add(label)
    return failures


def prompt_failed_retry(
    result: Any,
    lessons: List[dict],
    timeout_seconds: int,
    output: TextIO,
    timed_reader: Optional[Callable[[str, int, TextIO], Tuple[bool, str]]] = None,
    input_fn: Callable[[str], str] = input,
) -> Set[int]:
    failures = failed_lesson_positions(result, lessons)
    if not failures:
        return set()

    output.write("\n仍有 {} 个失败项目：\n".format(len(failures)))
    output.write("序号\t失败阶段\n")
    for position in sorted(failures):
        output.write("{}\t{}\n".format(position, "、".join(sorted(failures[position]))))
    output.write(
        "\n{} 秒内没有输入，将自动重新执行全部失败项目一次。\n".format(
            max(0, timeout_seconds)
        )
    )
    output.write("输入 all/回车=全部，n=跳过，也可输入 4、12-15、4,12-15,20。\n")
    output.flush()

    reader = timed_reader or timed_input
    received, choice = reader("请选择：", timeout_seconds, output)
    if not received:
        output.write("\n等待超时，自动重新执行全部失败项目。\n")
        output.flush()
        return set(failures)
    cleaned = choice.strip().lower()
    if cleaned in {"n", "no", "q"}:
        return set()
    if not cleaned or cleaned in {"a", "all"}:
        return set(failures)

    while True:
        try:
            selected = parse_positions(cleaned) or set(failures)
        except ValueError as error:
            output.write("格式错误：{}\n".format(error))
            cleaned = input_fn("请重新输入：").strip().lower()
            continue
        matched = set(selected) & set(failures)
        ignored = set(selected) - set(failures)
        if ignored:
            output.write(
                "以下序号当前没有失败项目，已忽略：{}\n".format(
                    ",".join(str(value) for value in sorted(ignored))
                )
            )
        if matched:
            return matched
        output.write("选取范围内没有失败项目。\n")
        return set()


def timed_input(prompt: str, timeout_seconds: int, output: TextIO) -> Tuple[bool, str]:
    output.write(prompt)
    output.flush()
    if timeout_seconds < 0:
        return True, input()
    if os.name == "nt":
        return _timed_input_windows(timeout_seconds, output)
    ready, _, _ = select.select([sys.stdin], [], [], max(0, timeout_seconds))
    if not ready:
        return False, ""
    return True, sys.stdin.readline().rstrip("\r\n")


def _timed_input_windows(timeout_seconds: int, output: TextIO) -> Tuple[bool, str]:
    import msvcrt

    deadline = time.monotonic() + max(0, timeout_seconds)
    characters: List[str] = []
    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            output.write("\n")
            output.flush()
            return True, "".join(characters)
        if character == "\b":
            if characters:
                characters.pop()
                output.write("\b \b")
                output.flush()
            continue
        if character in {"\x00", "\xe0"}:
            msvcrt.getwch()
            continue
        characters.append(character)
        output.write(character)
        output.flush()
    return False, ""
