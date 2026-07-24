"""Runtime path configuration."""

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    database_file: Path
    courses_dir: Path
    browser_profile_dir: Path
    edge_profile_dir: Path
    settings_file: Path

    @classmethod
    def resolve(cls, override: Optional[str] = None) -> "AppPaths":
        configured = override or os.environ.get("XIAOE_DATA_DIR")
        data_dir = Path(configured).expanduser() if configured else Path.home() / ".xiaoe-audio-pipeline"
        return cls(
            data_dir=data_dir,
            database_file=data_dir / "pipeline.sqlite3",
            courses_dir=data_dir / "courses",
            browser_profile_dir=data_dir / "chrome-profile",
            edge_profile_dir=data_dir / "edge-profile",
            settings_file=data_dir / "settings.json",
        )

    def create(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.courses_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.edge_profile_dir.mkdir(parents=True, exist_ok=True)


class AppSettings:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        if not self.path.is_file():
            return {"asr": {"provider": "local"}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"asr": {"provider": "local"}}
        return payload if isinstance(payload, dict) else {"asr": {"provider": "local"}}

    def asr(self) -> dict:
        value = self.load().get("asr")
        return value if isinstance(value, dict) else {"provider": "local"}

    VALID_BROWSERS = {"chrome", "edge", "ego", "playwright-chrome", "playwright-edge"}

    def browser(self) -> str:
        value = str(self.load().get("browser") or "chrome")
        return value if value in self.VALID_BROWSERS else "chrome"

    def save_browser(self, browser: str) -> None:
        if browser not in self.VALID_BROWSERS:
            raise ValueError("Unsupported browser backend: {}".format(browser))
        payload = self.load()
        payload["browser"] = browser
        self._save(payload)

    def save_asr(self, provider: str, model: Optional[str] = None, base_url: Optional[str] = None) -> None:
        payload = self.load()
        settings = {"provider": provider}
        if model:
            settings["model"] = model
        if base_url:
            settings["base_url"] = base_url
        payload["asr"] = settings
        self._save(payload)

    def _save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
