"""Online-first model discovery with a resilient local cache."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .secrets import MacKeychainSecretStore
from .structure_registry import STRUCTURE_PROVIDER_SPECS, StructureProviderSpec


@dataclass(frozen=True)
class CatalogResult:
    provider: str
    models: List[str]
    source: str
    updated: bool
    checked_at: Optional[int]
    effort_by_model: Dict[str, List[str]]
    warning: Optional[str] = None

    def effort_levels(self, model: str, fallback: Tuple[str, ...]) -> List[str]:
        return self.effort_by_model.get(model) or list(fallback)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "models": self.models,
            "source": self.source,
            "updated": self.updated,
            "checked_at": self.checked_at,
            "effort_by_model": self.effort_by_model,
            "warning": self.warning,
        }


class CatalogHttpClient:
    def get(
        self,
        url: str,
        headers: Dict[str, str],
        timeout: float = 3.0,
    ) -> Tuple[int, Optional[Dict[str, Any]], Dict[str, str]]:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return response.status, payload, dict(response.headers.items())
        except HTTPError as error:
            if error.code == 304:
                return 304, None, dict(error.headers.items())
            raise RuntimeError("Model catalog returned HTTP {}.".format(error.code)) from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError("Model catalog network request failed.") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Model catalog returned invalid JSON.") from error


class StructureModelCatalog:
    CACHE_VERSION = 1

    def __init__(
        self,
        cache_path: Path,
        http: Optional[CatalogHttpClient] = None,
        now: Callable[[], float] = time.time,
        ttl_seconds: int = 6 * 60 * 60,
        retry_seconds: int = 30 * 60,
    ) -> None:
        self.cache_path = cache_path
        self.http = http or CatalogHttpClient()
        self.now = now
        self.ttl_seconds = ttl_seconds
        self.retry_seconds = retry_seconds

    def get(
        self,
        provider_id: str,
        settings: Optional[dict] = None,
        secrets: Optional[MacKeychainSecretStore] = None,
        force_refresh: bool = False,
    ) -> CatalogResult:
        if provider_id not in STRUCTURE_PROVIDER_SPECS:
            raise ValueError("Unknown structure provider: {}".format(provider_id))
        spec = STRUCTURE_PROVIDER_SPECS[provider_id]
        settings = settings or {}
        cache = self._load()
        entry = cache["providers"].get(provider_id, {})
        current_time = int(self.now())

        if spec.mode == "local_agent":
            models = self._cached_or_fallback(entry, spec, provider_id)
            self._store_entry(cache, provider_id, {
                "models": models,
                "effort_by_model": {},
                "checked_at": current_time,
                "fetched_at": current_time,
            })
            return CatalogResult(
                provider_id, models, "built_in", False, current_time, {}
            )

        fetched_at = int(entry.get("fetched_at") or 0)
        retry_after = int(entry.get("retry_after") or 0)
        if not force_refresh and fetched_at and current_time - fetched_at < self.ttl_seconds:
            return self._result(provider_id, entry, spec, "cache", False)
        if not force_refresh and retry_after > current_time:
            return self._result(
                provider_id, entry, spec, "cache", False,
                "网络刷新暂时不可用，已使用本地缓存。"
            )

        secret_store = secrets or MacKeychainSecretStore()
        api_key = self._api_key(spec, secret_store)
        if not api_key:
            return self._result(
                provider_id, entry, spec, "cache", False,
                "配置 API Key 后才能联网更新模型清单。"
            )

        headers = self._headers(provider_id, api_key, entry)
        try:
            status, payload, response_headers = self.http.get(
                self._models_url(provider_id, settings), headers
            )
            if status == 304:
                entry["checked_at"] = current_time
                entry["fetched_at"] = current_time
                entry.pop("retry_after", None)
                self._store_entry(cache, provider_id, entry)
                return self._result(provider_id, entry, spec, "cache", False)
            models, efforts = self._parse_models(provider_id, payload or {})
            if not models:
                raise RuntimeError("Model catalog did not return usable text models.")
            previous = entry.get("models") or []
            entry = {
                "models": models,
                "effort_by_model": efforts,
                "checked_at": current_time,
                "fetched_at": current_time,
                "etag": self._header(response_headers, "etag"),
                "last_modified": self._header(response_headers, "last-modified"),
            }
            self._store_entry(cache, provider_id, entry)
            return self._result(
                provider_id, entry, spec, "network", models != previous
            )
        except RuntimeError as error:
            entry["checked_at"] = current_time
            entry["retry_after"] = current_time + self.retry_seconds
            self._store_entry(cache, provider_id, entry)
            return self._result(
                provider_id, entry, spec, "cache", False, str(error)
            )

    def _load(self) -> dict:
        if self.cache_path.is_file():
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if payload.get("version") == self.CACHE_VERSION:
                    providers = payload.get("providers")
                    if isinstance(providers, dict):
                        return payload
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        return {"version": self.CACHE_VERSION, "providers": {}}

    def _store_entry(self, cache: dict, provider_id: str, entry: dict) -> None:
        cache["providers"][provider_id] = {
            key: value for key, value in entry.items() if value is not None
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.cache_path.with_suffix(self.cache_path.suffix + ".partial")
        partial.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        partial.replace(self.cache_path)

    def _result(
        self,
        provider_id: str,
        entry: dict,
        spec: StructureProviderSpec,
        source: str,
        updated: bool,
        warning: Optional[str] = None,
    ) -> CatalogResult:
        models = self._cached_or_fallback(entry, spec, provider_id)
        effort_by_model = entry.get("effort_by_model")
        if not isinstance(effort_by_model, dict):
            effort_by_model = {}
        return CatalogResult(
            provider_id,
            models,
            source if entry.get("models") else "built_in",
            updated,
            entry.get("checked_at"),
            effort_by_model,
            warning,
        )

    @staticmethod
    def _cached_or_fallback(
        entry: dict, spec: StructureProviderSpec, provider_id: str
    ) -> List[str]:
        models = entry.get("models")
        if isinstance(models, list) and models:
            values = [str(model) for model in models if str(model).strip()]
            if provider_id == "alibaba":
                values = [model for model in values if _is_alibaba_text_model(model)]
            if values:
                return values
        fallback = list(spec.fallback_models)
        if spec.default_model and spec.default_model not in fallback:
            fallback.append(spec.default_model)
        return fallback

    @staticmethod
    def _api_key(
        spec: StructureProviderSpec, secrets: MacKeychainSecretStore
    ) -> Optional[str]:
        if not spec.secret_fields:
            return None
        return secrets.get(spec.secret_fields[0][0])

    @staticmethod
    def _headers(provider_id: str, api_key: str, entry: dict) -> Dict[str, str]:
        if provider_id == "anthropic":
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            headers = {"Authorization": "Bearer {}".format(api_key)}
        if entry.get("etag"):
            headers["If-None-Match"] = str(entry["etag"])
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = str(entry["last_modified"])
        return headers

    @staticmethod
    def _models_url(provider_id: str, settings: dict) -> str:
        defaults = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "alibaba": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
        base_url = settings.get("base_url") or defaults.get(provider_id)
        if not base_url:
            raise RuntimeError("Custom structure provider requires a Base URL.")
        suffix = "/models"
        return str(base_url) if str(base_url).endswith(suffix) else str(base_url).rstrip("/") + suffix

    @staticmethod
    def _parse_models(
        provider_id: str, payload: Dict[str, Any]
    ) -> Tuple[List[str], Dict[str, List[str]]]:
        items = payload.get("data")
        if not isinstance(items, list):
            return [], {}
        models: List[str] = []
        efforts: Dict[str, List[str]] = {}
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            model_id = str(item["id"])
            if provider_id == "openai" and not _is_openai_text_model(model_id):
                continue
            if provider_id == "alibaba" and not _is_alibaba_text_model(model_id):
                continue
            if model_id not in models:
                models.append(model_id)
            if provider_id == "anthropic":
                capability = item.get("capabilities", {}).get("effort", {})
                supported = [
                    level for level in ("low", "medium", "high", "xhigh", "max")
                    if capability.get(level, {}).get("supported")
                ]
                if supported:
                    efforts[model_id] = supported
        return models, efforts

    @staticmethod
    def _header(headers: Dict[str, str], name: str) -> Optional[str]:
        lowered = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered:
                return value
        return None


def _is_openai_text_model(model_id: str) -> bool:
    lowered = model_id.lower()
    excluded = (
        "audio", "transcribe", "tts", "realtime", "embedding",
        "moderation", "image", "dall-e", "whisper",
    )
    if any(part in lowered for part in excluded):
        return False
    return lowered.startswith(("gpt-", "o1", "o3", "o4", "codex"))


def _is_alibaba_text_model(model_id: str) -> bool:
    lowered = model_id.lower()
    excluded = (
        "audio", "embedding", "image", "video", "tts", "realtime",
        "asr", "speech", "rerank", "moderation", "ocr", "wan",
    )
    return not any(part in lowered for part in excluded)
