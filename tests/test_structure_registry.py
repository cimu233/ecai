import tempfile
import unittest
from pathlib import Path

from xiaoe_core.structure_registry import (
    STRUCTURE_PROVIDER_SPECS,
    build_structure_provider,
)
from xiaoe_core.structurer import (
    AnthropicStructurer,
    ClaudeCodeStructurer,
    CodexCliStructurer,
    OpenAICompatibleStructurer,
    OpenAIResponsesStructurer,
)


class FakeSecrets:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, account):
        return self.values.get(account)


class StructureRegistryTest(unittest.TestCase):
    def test_catalog_contains_local_and_api_options(self):
        self.assertEqual(
            {
                "codex", "claude-code", "openai", "anthropic",
                "deepseek", "alibaba", "custom",
            },
            set(STRUCTURE_PROVIDER_SPECS),
        )

    def test_factory_builds_each_provider_family(self):
        secrets = FakeSecrets({
            "openai.api_key": "openai-key",
            "anthropic.api_key": "anthropic-key",
            "deepseek.api_key": "deepseek-key",
            "dashscope": "dashscope-key",
            "structure.custom.api_key": "custom-key",
        })
        self.assertIsInstance(
            build_structure_provider("codex", secrets=secrets), CodexCliStructurer
        )
        self.assertIsInstance(
            build_structure_provider("claude-code", secrets=secrets),
            ClaudeCodeStructurer,
        )
        self.assertIsInstance(
            build_structure_provider("openai", secrets=secrets),
            OpenAIResponsesStructurer,
        )
        self.assertIsInstance(
            build_structure_provider("anthropic", secrets=secrets),
            AnthropicStructurer,
        )
        for provider in ("deepseek", "alibaba", "custom"):
            settings = {"base_url": "https://custom.example/v1"} if provider == "custom" else {}
            self.assertIsInstance(
                build_structure_provider(provider, settings=settings, secrets=secrets),
                OpenAICompatibleStructurer,
            )

    def test_factory_loads_and_validates_custom_prompt_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            prompt = Path(temporary) / "prompt.txt"
            prompt.write_text(
                "Custom {{LESSON_TITLE}}\n{{TRANSCRIPT}}", encoding="utf-8"
            )
            provider = build_structure_provider(
                "codex",
                settings={"prompt_file": str(prompt)},
                secrets=FakeSecrets(),
            )
            self.assertIn("Custom", provider.prompt_template)
            prompt.write_text("invalid", encoding="utf-8")
            provider = build_structure_provider(
                "codex",
                settings={"prompt_file": str(prompt)},
                secrets=FakeSecrets(),
            )
            self.assertEqual("invalid", provider.prompt_template)


if __name__ == "__main__":
    unittest.main()
