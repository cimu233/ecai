"""Install and control the operating-system background scheduler."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AppPaths
from .scheduler import cli_arguments


LAUNCHD_LABEL = "com.ecai.course-monitor"
WINDOWS_TASK_NAME = "ECAI Course Monitor"


class SchedulerServiceError(RuntimeError):
    pass


class SchedulerService:
    def install(self) -> Dict[str, Any]:
        raise NotImplementedError

    def uninstall(self) -> Dict[str, Any]:
        raise NotImplementedError

    def start(self) -> Dict[str, Any]:
        raise NotImplementedError

    def stop(self) -> Dict[str, Any]:
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        raise NotImplementedError


def worker_arguments(paths: AppPaths) -> List[str]:
    if getattr(sys, "frozen", False):
        return cli_arguments(paths, ["schedule", "worker", "--json"])
    runtime_dir = paths.data_dir / "scheduler-runtime"
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from xiaoe_cli.main import main; "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    return [
        sys.executable,
        "-c",
        bootstrap,
        str(runtime_dir),
        "--data-dir",
        str(paths.data_dir),
        "schedule",
        "worker",
        "--json",
    ]


def prepare_worker_runtime(paths: AppPaths) -> Optional[Path]:
    """Stage source packages outside Desktop so launchd can read them under TCC."""
    if getattr(sys, "frozen", False):
        return None
    source_dir = Path(__file__).resolve().parents[1]
    runtime_dir = paths.data_dir / "scheduler-runtime"
    temporary = paths.data_dir / "scheduler-runtime.partial"
    if temporary.exists():
        shutil.rmtree(str(temporary))
    temporary.mkdir(parents=True, exist_ok=True)
    for package in ("xiaoe_core", "xiaoe_cli"):
        shutil.copytree(
            str(source_dir / package),
            str(temporary / package),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    if runtime_dir.exists():
        shutil.rmtree(str(runtime_dir))
    temporary.replace(runtime_dir)
    return runtime_dir


def worker_environment(paths: AppPaths) -> Dict[str, str]:
    runtime_dir = prepare_worker_runtime(paths)
    return {"PYTHONPATH": str(runtime_dir)} if runtime_dir else {}


class LaunchdSchedulerService(SchedulerService):
    def __init__(
        self,
        paths: AppPaths,
        home: Optional[Path] = None,
        uid: Optional[int] = None,
        run_command: Any = subprocess.run,
    ) -> None:
        self.paths = paths
        self.home = home or Path.home()
        self.uid = os.getuid() if uid is None else uid
        self.run_command = run_command
        self.plist_path = (
            self.home / "Library" / "LaunchAgents" / (LAUNCHD_LABEL + ".plist")
        )
        self.domain = "gui/{}".format(self.uid)
        self.service_target = "{}/{}".format(self.domain, LAUNCHD_LABEL)

    def install(self) -> Dict[str, Any]:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        environment = worker_environment(self.paths)
        payload = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": worker_arguments(self.paths),
            "RunAtLoad": True,
            "StartInterval": 60,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
            "ThrottleInterval": 30,
            "StandardOutPath": str(
                self.paths.data_dir / "scheduler-service.stdout.log"
            ),
            "StandardErrorPath": str(
                self.paths.data_dir / "scheduler-service.stderr.log"
            ),
        }
        if environment:
            payload["EnvironmentVariables"] = environment
        temporary = self.plist_path.with_suffix(".plist.partial")
        with temporary.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
        os.chmod(temporary, 0o600)
        temporary.replace(self.plist_path)
        self._bootout()
        result = self._run(
            ["launchctl", "bootstrap", self.domain, str(self.plist_path)]
        )
        if result.returncode != 0:
            raise SchedulerServiceError(
                self._error(result, "launchd 后台任务安装失败。")
            )
        self._run(
            ["launchctl", "enable", self.service_target]
        )
        return {
            "platform": "macos",
            "installed": True,
            "running": True,
            "service": LAUNCHD_LABEL,
            "file": str(self.plist_path),
        }

    def uninstall(self) -> Dict[str, Any]:
        self._bootout()
        try:
            self.plist_path.unlink()
        except FileNotFoundError:
            pass
        return {
            "platform": "macos",
            "installed": False,
            "running": False,
            "service": LAUNCHD_LABEL,
            "file": str(self.plist_path),
        }

    def start(self) -> Dict[str, Any]:
        if not self.plist_path.is_file():
            return self.install()
        worker_environment(self.paths)
        self._bootout()
        result = self._run(
            ["launchctl", "bootstrap", self.domain, str(self.plist_path)]
        )
        if result.returncode != 0:
            raise SchedulerServiceError(
                self._error(result, "launchd 后台任务启动失败。")
            )
        self._run(["launchctl", "enable", self.service_target])
        self._run(["launchctl", "kickstart", self.service_target])
        return {
            "platform": "macos",
            "installed": True,
            "running": True,
            "service": LAUNCHD_LABEL,
            "file": str(self.plist_path),
        }

    def stop(self) -> Dict[str, Any]:
        self._bootout()
        return {
            "platform": "macos",
            "installed": self.plist_path.is_file(),
            "running": False,
            "service": LAUNCHD_LABEL,
            "file": str(self.plist_path),
        }

    def status(self) -> Dict[str, Any]:
        installed = self.plist_path.is_file()
        result = self._run(["launchctl", "print", self.service_target])
        return {
            "platform": "macos",
            "installed": installed,
            "running": installed and result.returncode == 0,
            "service": LAUNCHD_LABEL,
            "file": str(self.plist_path),
        }

    def _bootout(self) -> None:
        self._run(
            ["launchctl", "bootout", self.domain, str(self.plist_path)]
        )

    def _run(self, command: List[str]) -> subprocess.CompletedProcess:
        return self.run_command(
            command, capture_output=True, text=True, check=False
        )

    @staticmethod
    def _error(process: subprocess.CompletedProcess, fallback: str) -> str:
        return (process.stderr or process.stdout or fallback).strip()


class WindowsTaskSchedulerService(SchedulerService):
    def __init__(
        self, paths: AppPaths, run_command: Any = subprocess.run
    ) -> None:
        self.paths = paths
        self.run_command = run_command

    def install(self) -> Dict[str, Any]:
        worker_environment(self.paths)
        command = subprocess.list2cmdline(worker_arguments(self.paths))
        result = self._run(
            [
                "schtasks",
                "/Create",
                "/TN",
                WINDOWS_TASK_NAME,
                "/TR",
                command,
                "/SC",
                "MINUTE",
                "/MO",
                "1",
                "/RL",
                "LIMITED",
                "/F",
            ]
        )
        if result.returncode != 0:
            raise SchedulerServiceError(
                self._error(result, "Windows 后台任务安装失败。")
            )
        return self._state(True, True)

    def uninstall(self) -> Dict[str, Any]:
        self._run(
            ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"]
        )
        return self._state(False, False)

    def start(self) -> Dict[str, Any]:
        result = self._run(
            ["schtasks", "/Change", "/TN", WINDOWS_TASK_NAME, "/ENABLE"]
        )
        if result.returncode != 0:
            return self.install()
        self._run(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
        return self._state(True, True)

    def stop(self) -> Dict[str, Any]:
        self._run(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
        result = self._run(
            ["schtasks", "/Change", "/TN", WINDOWS_TASK_NAME, "/DISABLE"]
        )
        installed = result.returncode == 0
        return self._state(installed, False)

    def status(self) -> Dict[str, Any]:
        result = self._run(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME, "/FO", "LIST"]
        )
        output = (result.stdout or "").lower()
        running = result.returncode == 0 and "disabled" not in output
        return self._state(result.returncode == 0, running)

    def _run(self, command: List[str]) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        runtime_dir = self.paths.data_dir / "scheduler-runtime"
        if runtime_dir.is_dir():
            environment["PYTHONPATH"] = str(runtime_dir)
        return self.run_command(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    @staticmethod
    def _error(process: subprocess.CompletedProcess, fallback: str) -> str:
        return (process.stderr or process.stdout or fallback).strip()

    @staticmethod
    def _state(installed: bool, running: bool) -> Dict[str, Any]:
        return {
            "platform": "windows",
            "installed": installed,
            "running": running,
            "service": WINDOWS_TASK_NAME,
        }


def build_scheduler_service(
    paths: AppPaths, platform: Optional[str] = None
) -> SchedulerService:
    selected = platform or sys.platform
    if selected == "darwin":
        return LaunchdSchedulerService(paths)
    if selected in {"win32", "cygwin"}:
        return WindowsTaskSchedulerService(paths)
    raise SchedulerServiceError(
        "当前系统尚未提供后台调度适配：{}".format(selected)
    )
