"""Structure provider catalog, defaults, and factory."""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .secrets import MacKeychainSecretStore
from .structurer import (
    AnthropicStructurer,
    ClaudeCodeStructurer,
    CodexCliStructurer,
    OpenAICompatibleStructurer,
    OpenAIResponsesStructurer,
    StructureProvider,
    build_structure_prompt,
)


@dataclass(frozen=True)
class StructureProviderSpec:
    id: str
    label: str
    mode: str
    region: str
    default_model: str
    secret_fields: Tuple[Tuple[str, str], ...] = ()
    needs_base_url: bool = False
    executable: Optional[str] = None
    fallback_models: Tuple[str, ...] = ()
    effort_levels: Tuple[str, ...] = ()
    default_effort: Optional[str] = None

    def to_dict(self, secrets: MacKeychainSecretStore) -> dict:
        if self.executable:
            configured = shutil.which(self.executable) is not None
        else:
            configured = all(secrets.get(account) for account, _ in self.secret_fields)
        return {
            "id": self.id,
            "label": self.label,
            "mode": self.mode,
            "region": self.region,
            "default_model": self.default_model,
            "configured": configured,
            "secret_fields": [
                {"account": account, "label": label} for account, label in self.secret_fields
            ],
            "needs_base_url": self.needs_base_url,
            "fallback_models": list(self.fallback_models),
            "effort_levels": list(self.effort_levels),
            "default_effort": self.default_effort,
        }


STRUCTURE_PROVIDER_SPECS: Dict[str, StructureProviderSpec] = {
    "codex": StructureProviderSpec(
        "codex", "本机 Codex", "local_agent", "local", "", executable="codex",
        fallback_models=("gpt-5.3-codex", "gpt-5.2-codex", "gpt-5.1-codex-max"),
        effort_levels=("minimal", "low", "medium", "high", "xhigh"),
        default_effort="medium",
    ),
    "claude-code": StructureProviderSpec(
        "claude-code", "本机 Claude Code", "local_agent", "local", "sonnet", executable="claude",
        fallback_models=("sonnet", "opus", "haiku"),
        effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_effort="medium",
    ),
    "openai": StructureProviderSpec(
        "openai", "OpenAI API", "api", "global", "gpt-5-mini",
        (("openai.api_key", "OpenAI API Key"),),
        fallback_models=("gpt-5.2", "gpt-5.1", "gpt-5-mini"),
        effort_levels=("none", "minimal", "low", "medium", "high", "xhigh"),
        default_effort="medium",
    ),
    "anthropic": StructureProviderSpec(
        "anthropic", "Anthropic API", "api", "global", "claude-sonnet-4-6",
        (("anthropic.api_key", "Anthropic API Key"),),
        fallback_models=("claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-4-6"),
        effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_effort="medium",
    ),
    "deepseek": StructureProviderSpec(
        "deepseek", "DeepSeek API", "api", "CN", "deepseek-chat",
        (("deepseek.api_key", "DeepSeek API Key"),),
        fallback_models=("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-reasoner", "deepseek-chat"),
    ),
    "alibaba": StructureProviderSpec(
        "alibaba", "阿里云百炼 Qwen", "api", "CN", "qwen-plus",
        (("dashscope", "DashScope API Key"),),
        fallback_models=("qwen3.7-max", "qwen-plus", "qwen-flash"),
        effort_levels=("none", "low", "medium", "high", "xhigh", "max"),
        default_effort="medium",
    ),
    "custom": StructureProviderSpec(
        "custom", "自定义 OpenAI-compatible API", "api", "custom", "gpt-4o-mini",
        (("structure.custom.api_key", "API Key"),), True,
        fallback_models=("gpt-4o-mini",),
        effort_levels=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default_effort="medium",
    ),
}


def list_structure_provider_specs() -> List[StructureProviderSpec]:
    return list(STRUCTURE_PROVIDER_SPECS.values())


def build_structure_provider(
    provider_id: str,
    settings: Optional[dict] = None,
    model_override: Optional[str] = None,
    effort_override: Optional[str] = None,
    secrets: Optional[MacKeychainSecretStore] = None,
) -> StructureProvider:
    if provider_id not in STRUCTURE_PROVIDER_SPECS:
        raise ValueError("Unknown structure provider: {}".format(provider_id))
    settings = settings or {}
    secret_store = secrets or MacKeychainSecretStore()
    spec = STRUCTURE_PROVIDER_SPECS[provider_id]
    model = model_override or settings.get("model") or spec.default_model or None
    effort = effort_override or settings.get("effort")
    prompt_template = _load_prompt_template(settings)
    if effort and spec.effort_levels and effort not in spec.effort_levels:
        raise ValueError(
            "Unsupported effort '{}' for {}.".format(effort, provider_id)
        )
    if effort and not spec.effort_levels:
        raise ValueError("{} controls reasoning through model selection.".format(provider_id))

    if provider_id == "codex":
        return CodexCliStructurer(
            model=model, effort=effort, prompt_template=prompt_template
        )
    if provider_id == "claude-code":
        return ClaudeCodeStructurer(
            model=model, effort=effort, prompt_template=prompt_template
        )
    if provider_id == "openai":
        return OpenAIResponsesStructurer(
            secret_store.get("openai.api_key"),
            str(model),
            settings.get("base_url") or "https://api.openai.com/v1",
            effort=effort,
            prompt_template=prompt_template,
        )
    if provider_id == "anthropic":
        return AnthropicStructurer(
            secret_store.get("anthropic.api_key"),
            str(model),
            settings.get("base_url") or "https://api.anthropic.com/v1",
            effort=effort,
            prompt_template=prompt_template,
        )
    compatible = {
        "deepseek": (
            "deepseek_api", "deepseek.api_key", "https://api.deepseek.com/v1"
        ),
        "alibaba": (
            "alibaba_api", "dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "custom": (
            "custom_api", "structure.custom.api_key", settings.get("base_url")
        ),
    }
    name, secret_account, default_url = compatible[provider_id]
    base_url = settings.get("base_url") or default_url
    if not base_url:
        raise ValueError("Custom structure provider requires a base URL.")
    return OpenAICompatibleStructurer(
        name,
        secret_store.get(secret_account),
        str(model),
        str(base_url),
        effort=effort,
        effort_style=(
            "alibaba" if provider_id == "alibaba"
            else "standard" if provider_id == "custom"
            else None
        ),
        prompt_template=prompt_template,
    )


def _load_prompt_template(settings: dict) -> Optional[str]:
    prompt_file = settings.get("prompt_file")
    if not prompt_file:
        return None
    path = Path(str(prompt_file)).expanduser()
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(
            "Custom structure prompt cannot be read: {}".format(path)
        ) from error
    build_structure_prompt("Lesson", "Transcript", template)
    return template
