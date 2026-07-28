"""Small terminal progress helpers shared by pipeline stages."""

import sys
import threading
import time
from typing import Optional, TextIO


ERASE_LINE = "\r\033[2K"
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def update_progress(message: str, stream: Optional[TextIO] = None) -> None:
    print(ERASE_LINE + message, end="", file=stream or sys.stderr, flush=True)


def finish_progress(message: str, stream: Optional[TextIO] = None) -> None:
    print(ERASE_LINE + message, file=stream or sys.stderr, flush=True)


def clear_progress(stream: Optional[TextIO] = None) -> None:
    print(ERASE_LINE, end="", file=stream or sys.stderr, flush=True)


class ProgressSpinner:
    def __init__(
        self,
        message: str,
        enabled: Optional[bool] = None,
        stream: Optional[TextIO] = None,
        interval: float = 0.1,
    ) -> None:
        self.message = message
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = 0.0

    def __enter__(self) -> "ProgressSpinner":
        if not self.enabled:
            return self
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 4))
        clear_progress(self.stream)

    def _animate(self) -> None:
        index = 0
        while not self._stop.is_set():
            elapsed = int(time.monotonic() - self._started_at)
            update_progress(
                "{} {}（{} 秒）".format(
                    SPINNER_FRAMES[index % len(SPINNER_FRAMES)],
                    self.message,
                    elapsed,
                ),
                self.stream,
            )
            index += 1
            self._stop.wait(self.interval)
