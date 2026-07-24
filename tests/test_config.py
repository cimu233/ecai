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

    def test_browser_selection_defaults_to_chrome_and_supports_edge_and_ego(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            settings = AppSettings(path)
            self.assertEqual("chrome", settings.browser())
            settings.save_browser("edge")
            self.assertEqual("edge", settings.browser())
            settings.save_browser("ego")
            self.assertEqual("ego", settings.browser())
            self.assertEqual("ego", json.loads(path.read_text(encoding="utf-8"))["browser"])
