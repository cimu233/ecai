"""Open a local file with the operating system's default application."""

import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


def open_with_default_app(
    path: Path,
    platform: Optional[str] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError("File does not exist: {}".format(resolved))
    current_platform = platform or sys.platform
    if current_platform == "darwin":
        command = ["open", str(resolved)]
    elif current_platform == "win32":
        command = ["cmd", "/c", "start", "", str(resolved)]
    else:
        command = ["xdg-open", str(resolved)]
    process = runner(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(
            "Could not open the file with the system default application."
        )
