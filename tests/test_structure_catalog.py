import tempfile
import unittest
from pathlib import Path

from xiaoe_core.structure_catalog import StructureModelCatalog


class FakeSecrets:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, account):
        return self.values.get(account)


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers, timeout=3.0):
        self.calls.append((url, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StructureModelCatalogTest(unittest.TestCase):
    def test_network_result_is_cached_and_revalidated_with_etag(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = [1000]
            http = FakeHttp([
                (
                    200,
                    {
                        "data": [
                            {"id": "gpt-5.2"},
                            {"id": "text-embedding-3-small"},
                        ]
                    },
                    {"ETag": '"catalog-v1"'},
                ),
                (304, None, {}),
            ])
            catalog = StructureModelCatalog(
                Path(temporary) / "models.json",
                http=http,
                now=lambda: now[0],
                ttl_seconds=100,
            )
            secrets = FakeSecrets({"openai.api_key": "secret"})

            first = catalog.get("openai", secrets=secrets)
            self.assertEqual("network", first.source)
            self.assertEqual(["gpt-5.2"], first.models)
            self.assertTrue(first.updated)

            now[0] = 1050
            second = catalog.get("openai", secrets=secrets)
            self.assertEqual("cache", second.source)
            self.assertEqual(1, len(http.calls))

            now[0] = 1200
            third = catalog.get("openai", secrets=secrets)
            self.assertEqual("cache", third.source)
            self.assertEqual('"catalog-v1"', http.calls[1][1]["If-None-Match"])

    def test_offline_failure_uses_cache_and_avoids_repeated_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = [1000]
            http = FakeHttp([
                (200, {"data": [{"id": "deepseek-v4-flash"}]}, {}),
                RuntimeError("offline"),
            ])
            catalog = StructureModelCatalog(
                Path(temporary) / "models.json",
                http=http,
                now=lambda: now[0],
                ttl_seconds=10,
                retry_seconds=100,
            )
            secrets = FakeSecrets({"deepseek.api_key": "secret"})
            catalog.get("deepseek", secrets=secrets)
            now[0] = 1020
            fallback = catalog.get("deepseek", secrets=secrets)
            self.assertEqual(["deepseek-v4-flash"], fallback.models)
            self.assertEqual("cache", fallback.source)
            self.assertIn("offline", fallback.warning)
            now[0] = 1030
            catalog.get("deepseek", secrets=secrets)
            self.assertEqual(2, len(http.calls))

    def test_anthropic_model_capabilities_drive_effort_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            http = FakeHttp([(
                200,
                {
                    "data": [{
                        "id": "claude-sonnet-5",
                        "capabilities": {
                            "effort": {
                                "supported": True,
                                "low": {"supported": True},
                                "medium": {"supported": True},
                                "high": {"supported": True},
                                "xhigh": {"supported": False},
                                "max": {"supported": True},
                            }
                        },
                    }]
                },
                {},
            )])
            catalog = StructureModelCatalog(
                Path(temporary) / "models.json", http=http, now=lambda: 1000
            )
            result = catalog.get(
                "anthropic",
                secrets=FakeSecrets({"anthropic.api_key": "secret"}),
            )
            self.assertEqual(
                ["low", "medium", "high", "max"],
                result.effort_levels("claude-sonnet-5", ()),
            )

    def test_alibaba_catalog_filters_non_text_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            http = FakeHttp([(
                200,
                {
                    "data": [
                        {"id": "qwen3.7-max"},
                        {"id": "qwen3.7-text-embedding"},
                        {"id": "qwen-audio-3.0-realtime-flash"},
                    ]
                },
                {},
            )])
            catalog = StructureModelCatalog(
                Path(temporary) / "models.json", http=http, now=lambda: 1000
            )
            result = catalog.get(
                "alibaba", secrets=FakeSecrets({"dashscope": "secret"})
            )
            self.assertEqual(["qwen3.7-max"], result.models)


if __name__ == "__main__":
    unittest.main()
