import json
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.config import AppSettings


class AppSettingsTest(unittest.TestCase):
    def test_asr_selection_is_saved_without_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            settings = AppSettings(path)
            settings.save_asr("custom", "whisper-model", "https://asr.example/v1")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("custom", payload["asr"]["provider"])
            self.assertEqual("whisper-model", payload["asr"]["model"])
            self.assertNotIn("api_key", json.dumps(payload))

    def test_browser_selection_defaults_to_chrome_and_supports_all_backends(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            settings = AppSettings(path)
            self.assertEqual("chrome", settings.browser())
            for backend in ("edge", "ego", "playwright-chrome", "playwright-edge"):
                settings.save_browser(backend)
                self.assertEqual(backend, settings.browser())
                self.assertEqual(backend, json.loads(path.read_text(encoding="utf-8"))["browser"])
            with self.assertRaises(ValueError):
                settings.save_browser("firefox")

    def test_structure_selection_is_saved_without_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            settings = AppSettings(path)
            self.assertEqual("codex", settings.structure()["provider"])
            settings.save_structure(
                "custom", "notes-model", "https://llm.example/v1", "high"
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("custom", payload["structure"]["provider"])
            self.assertEqual("notes-model", payload["structure"]["model"])
            self.assertEqual("high", payload["structure"]["effort"])
            self.assertNotIn("api_key", json.dumps(payload))

    def test_structure_prompt_survives_provider_changes_and_can_be_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            prompt = Path(temporary) / "prompt.txt"
            settings = AppSettings(path)
            settings.save_structure_prompt(str(prompt))
            settings.save_structure("claude-code", "sonnet", effort="medium")
            self.assertEqual(str(prompt), settings.structure()["prompt_file"])
            settings.save_structure_prompt(None)
            self.assertNotIn("prompt_file", settings.structure())
