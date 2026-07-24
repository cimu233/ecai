"""Cross-platform secret store (macOS Keychain, Windows Credential Manager, .env fallback)."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


class SecretStore:
    """Unified secret store that auto-selects the best backend for the current platform."""

    SERVICE = "ecai"

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.runner = runner
        self._data_dir = data_dir or Path.home() / ".ecai"

    # -- public API -----------------------------------------------------------

    def get(self, account: str) -> Optional[str]:
        """Return the secret for *account*, or None."""
        if sys.platform == "darwin":
            value = self._get_keychain(account)
            if value is not None:
                return value
        if sys.platform == "win32":
            value = self._get_wincred(account)
            if value is not None:
                return value
        return self._get_env(account)

    def set(self, account: str, value: str, label: Optional[str] = None) -> None:
        """Persist *value* for *account*."""
        if not value:
            raise ValueError("Secret value cannot be empty.")
        if sys.platform == "darwin":
            self._set_keychain(account, value, label)
            return
        if sys.platform == "win32" and self._set_wincred(account, value, label):
            return
        self._set_env_file(account, value)

    def get_dashscope_api_key(self) -> Optional[str]:
        return self.get("dashscope")

    # -- macOS Keychain -------------------------------------------------------

    def _get_keychain(self, account: str) -> Optional[str]:
        process = self.runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s", self.SERVICE,
                "-a", account,
                "-w",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if process.returncode != 0:
            return None
        return process.stdout.strip() or None

    def _set_keychain(self, account: str, value: str, label: Optional[str]) -> None:
        process = self.runner(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s", self.SERVICE,
                "-a", account,
                "-l", label or "ecai - {}".format(account),
                "-w", value,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if process.returncode != 0:
            raise RuntimeError("Could not save secret in macOS Keychain.")

    # -- Windows Credential Manager -------------------------------------------

    @staticmethod
    def _get_wincred(account: str) -> Optional[str]:
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError:
            return None
        try:
            return keyring.get_password(SecretStore.SERVICE, account)
        except Exception:
            return None

    @staticmethod
    def _set_wincred(account: str, value: str, label: Optional[str]) -> bool:
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError:
            return False
        try:
            keyring.set_password(SecretStore.SERVICE, account, value)
            return True
        except Exception:
            return False

    # -- .env file fallback (all platforms) -----------------------------------

    def _env_path(self) -> Path:
        return self._data_dir / ".env"

    def _get_env(self, account: str) -> Optional[str]:
        env_var = "ECAI_" + account.upper().replace(".", "_")
        value = os.environ.get(env_var)
        if value:
            return value
        env_path = self._env_path()
        if not env_path.is_file():
            return None
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, val = stripped.partition("=")
                if key.strip() == env_var:
                    v = val.strip().strip('"').strip("'")
                    return v or None
        except OSError:
            return None
        return None

    def _set_env_file(self, account: str, value: str) -> None:
        env_var = "ECAI_" + account.upper().replace(".", "_")
        env_path = self._env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        new_line = '{}="{}"\n'.format(env_var, value)
        try:
            current = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
        except OSError:
            current = ""
        if env_var in current:
            lines = current.splitlines(keepends=True)
            lines = [new_line if env_var in line else line for line in lines]
            current = "".join(lines)
        else:
            current = current.rstrip("\n") + "\n" + new_line
        env_path.write_text(current, encoding="utf-8")


# Backward-compatible alias for existing callers.
MacKeychainSecretStore = SecretStore
