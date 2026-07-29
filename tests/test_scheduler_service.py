import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.config import AppPaths
from xiaoe_core.scheduler_service import (
    LAUNCHD_LABEL,
    LaunchdSchedulerService,
    WindowsTaskSchedulerService,
    build_scheduler_service,
)


class CommandRecorder:
    def __init__(self) -> None:
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class LaunchdSchedulerServiceTest(unittest.TestCase):
    def test_install_writes_private_background_launch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = AppPaths.resolve(str(root / "data"))
            recorder = CommandRecorder()
            service = LaunchdSchedulerService(
                paths, home=root / "home", uid=501, run_command=recorder
            )

            result = service.install()

            self.assertTrue(result["installed"])
            self.assertEqual(0o600, service.plist_path.stat().st_mode & 0o777)
            with service.plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            self.assertEqual(LAUNCHD_LABEL, payload["Label"])
            self.assertEqual(60, payload["StartInterval"])
            self.assertEqual("Background", payload["ProcessType"])
            self.assertIn("schedule", payload["ProgramArguments"])
            self.assertIn("worker", payload["ProgramArguments"])
            runtime = paths.data_dir / "scheduler-runtime"
            self.assertTrue((runtime / "xiaoe_core" / "scheduler.py").is_file())
            self.assertEqual(
                str(runtime), payload["EnvironmentVariables"]["PYTHONPATH"]
            )
            self.assertEqual(
                "launchctl", recorder.commands[-2][0][0]
            )
            self.assertEqual("bootstrap", recorder.commands[-2][0][1])

    def test_stop_preserves_configuration_and_uninstall_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = AppPaths.resolve(str(root / "data"))
            recorder = CommandRecorder()
            service = LaunchdSchedulerService(
                paths, home=root / "home", uid=501, run_command=recorder
            )
            service.install()
            stopped = service.stop()
            self.assertTrue(stopped["installed"])
            self.assertTrue(service.plist_path.is_file())
            removed = service.uninstall()
            self.assertFalse(removed["installed"])
            self.assertFalse(service.plist_path.exists())


class WindowsTaskSchedulerServiceTest(unittest.TestCase):
    def test_install_uses_one_minute_background_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = AppPaths.resolve(temporary)
            recorder = CommandRecorder()
            service = WindowsTaskSchedulerService(paths, run_command=recorder)
            result = service.install()
            command = recorder.commands[0][0]
            self.assertTrue(result["installed"])
            self.assertIn("schtasks", command)
            self.assertIn("MINUTE", command)
            self.assertIn("1", command)
            self.assertIn("schedule worker", command[command.index("/TR") + 1])

    def test_factory_rejects_unsupported_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = AppPaths.resolve(temporary)
            with self.assertRaises(RuntimeError):
                build_scheduler_service(paths, platform="plan9")


if __name__ == "__main__":
    unittest.main()
