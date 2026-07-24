"""Local secret lookup backed by the macOS login keychain."""

import subprocess
from typing import Callable, Optional


class MacKeychainSecretStore:
    SERVICE = "xiaoe-audio-pipeline"
    DASHSCOPE_ACCOUNT = "dashscope"

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.runner = runner

    def get_dashscope_api_key(self) -> Optional[str]:
        return self.get(self.DASHSCOPE_ACCOUNT)

    def get(self, account: str) -> Optional[str]:
        process = self.runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                self.SERVICE,
                "-a",
                account,
                "-w",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if process.returncode != 0:
            return None
        value = process.stdout.strip()
        return value or None

    def set(self, account: str, value: str, label: Optional[str] = None) -> None:
        if not value:
            raise ValueError("Secret value cannot be empty.")
        process = self.runner(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                self.SERVICE,
                "-a",
                account,
                "-l",
                label or "Xiaoe Audio Pipeline - {}".format(account),
                "-w",
                value,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if process.returncode != 0:
            raise RuntimeError("Could not save secret in macOS Keychain.")
