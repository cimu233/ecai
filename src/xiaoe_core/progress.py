"""Small terminal progress helpers shared by pipeline stages."""

import sys
from typing import Optional, TextIO


ERASE_LINE = "\r\033[2K"


def update_progress(message: str, stream: Optional[TextIO] = None) -> None:
    print(ERASE_LINE + message, end="", file=stream or sys.stderr, flush=True)


def finish_progress(message: str, stream: Optional[TextIO] = None) -> None:
    print(ERASE_LINE + message, file=stream or sys.stderr, flush=True)


def clear_progress(stream: Optional[TextIO] = None) -> None:
    print(ERASE_LINE, end="", file=stream or sys.stderr, flush=True)
