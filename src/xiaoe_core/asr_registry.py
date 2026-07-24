"""ASR provider catalog, defaults, and factory."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .asr import (
    AlibabaQwenAsrProvider,
    AsrProvider,
    BaiduAsrProvider,
    DeepgramAsrProvider,
    LocalQwenAsrProvider,
    OpenAICompatibleAsrProvider,
    TencentFlashAsrProvider,
    VolcengineAsrProvider,
)
from .secrets import MacKeychainSecretStore


@dataclass(frozen=True)
class AsrProviderSpec:
    id: str
    label: str
    region: str
    default_model: str
    secret_fields: Tuple[Tuple[str, str], ...] = ()
    needs_base_url: bool = False

    def to_dict(self, secrets: MacKeychainSecretStore) -> dict:
        configured = all(secrets.get(account) for account, _ in self.secret_fields)
        return {
            "id": self.id,
            "label": self.label,
            "region": self.region,
            "default_model": self.default_model,
            "configured": configured if self.secret_fields else True,
            "secret_fields": [{"account": account, "label": label} for account, label in self.secret_fields],
            "needs_base_url": self.needs_base_url,
        }


PROVIDER_SPECS: Dict[str, AsrProviderSpec] = {
    "local": AsrProviderSpec("local", "本地 Qwen3-ASR 1.7B", "local", "qwen3-asr-1.7b"),
    "alibaba": AsrProviderSpec("alibaba", "阿里云百炼 Qwen ASR", "CN", "qwen3-asr-flash", (("dashscope", "DashScope API Key"),)),
    "volcengine": AsrProviderSpec("volcengine", "火山引擎豆包录音识别极速版", "CN", "bigmodel", (("volcengine.api_key", "X-Api-Key"),)),
    "tencent": AsrProviderSpec("tencent", "腾讯云录音文件识别极速版", "CN", "16k_zh_en", (("tencent.app_id", "AppID"), ("tencent.secret_id", "SecretID"), ("tencent.secret_key", "SecretKey"))),
    "baidu": AsrProviderSpec("baidu", "百度智能云短语音识别", "CN", "short-speech", (("baidu.api_key", "API Key / 短期 API Key"),)),
    "openai": AsrProviderSpec("openai", "OpenAI Speech-to-Text", "global", "gpt-4o-mini-transcribe", (("openai.api_key", "OpenAI API Key"),)),
    "groq": AsrProviderSpec("groq", "Groq Whisper", "global", "whisper-large-v3-turbo", (("groq.api_key", "Groq API Key"),)),
    "deepgram": AsrProviderSpec("deepgram", "Deepgram Nova", "global", "nova-3", (("deepgram.api_key", "Deepgram API Key"),)),
    "custom": AsrProviderSpec("custom", "自定义 OpenAI-compatible", "custom", "whisper-1", (("custom.api_key", "API Key"),), True),
}


def list_provider_specs() -> List[AsrProviderSpec]:
    return list(PROVIDER_SPECS.values())


def build_asr_provider(
    provider_id: str,
    settings: Optional[dict] = None,
    language: Optional[str] = None,
    model_override: Optional[str] = None,
    local_model_dir: Optional[Path] = None,
    local_runtime_python: Optional[Path] = None,
    local_device: str = "auto",
    secrets: Optional[MacKeychainSecretStore] = None,
) -> AsrProvider:
    if provider_id not in PROVIDER_SPECS:
        raise ValueError("Unknown ASR provider: {}".format(provider_id))
    settings = settings or {}
    secret_store = secrets or MacKeychainSecretStore()
    model = model_override or settings.get("model") or PROVIDER_SPECS[provider_id].default_model

    if provider_id == "local":
        return LocalQwenAsrProvider(local_model_dir, local_runtime_python, language, local_device)
    if provider_id == "alibaba":
        return AlibabaQwenAsrProvider(
            api_key=secret_store.get("dashscope"),
            model=model,
            base_url=settings.get("base_url"),
            language=language,
        )
    if provider_id in ("openai", "groq", "custom"):
        defaults = {
            "openai": "https://api.openai.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "custom": settings.get("base_url"),
        }
        base_url = settings.get("base_url") or defaults[provider_id]
        if not base_url:
            raise ValueError("Custom ASR requires a base URL.")
        response_format = "verbose_json" if provider_id == "groq" else "json"
        return OpenAICompatibleAsrProvider(
            provider_id,
            secret_store.get("{}.api_key".format(provider_id)),
            base_url,
            model,
            language,
            response_format,
        )
    if provider_id == "deepgram":
        return DeepgramAsrProvider(secret_store.get("deepgram.api_key"), model, language)
    if provider_id == "volcengine":
        return VolcengineAsrProvider(
            secret_store.get("volcengine.api_key"), secret_store.get("volcengine.app_key")
        )
    if provider_id == "tencent":
        provider = TencentFlashAsrProvider(
            secret_store.get("tencent.app_id"),
            secret_store.get("tencent.secret_id"),
            secret_store.get("tencent.secret_key"),
            language,
        )
        provider.model = model
        return provider
    if provider_id == "baidu":
        return BaiduAsrProvider(secret_store.get("baidu.api_key"), language)
    raise RuntimeError("ASR provider factory is incomplete.")
