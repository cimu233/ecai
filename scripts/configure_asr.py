#!/usr/bin/python3
"""Interactive ASR provider configuration for the desktop launcher."""

import getpass
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xiaoe_core.asr_registry import list_provider_specs
from xiaoe_core.config import AppPaths, AppSettings
from xiaoe_core.secrets import MacKeychainSecretStore


def main() -> int:
    specs = list_provider_specs()
    paths = AppPaths.resolve()
    settings = AppSettings(paths.settings_file)
    secrets = MacKeychainSecretStore()
    current = settings.asr().get("provider", "local")
    print("\n可用语音转文字服务：")
    for index, spec in enumerate(specs, 1):
        marker = "（当前）" if spec.id == current else ""
        print("{}. {} [{}] {}".format(index, spec.label, spec.region, marker))
    print("q. 返回")
    choice = input("请选择服务：").strip().lower()
    if choice == "q":
        return 0
    try:
        spec = specs[int(choice) - 1]
    except (ValueError, IndexError):
        print("选择无效。")
        return 2

    for account, label in spec.secret_fields:
        existing = secrets.get(account)
        suffix = "（直接回车保留现有值）" if existing else ""
        value = getpass.getpass("{}{}：".format(label, suffix))
        if value:
            secrets.set(account, value, "Xiaoe ASR - {}".format(label))
        elif not existing:
            print("缺少 {}，本次没有切换默认服务。".format(label))
            return 2

    default_model = spec.default_model
    model = input("模型名称（默认 {}）：".format(default_model)).strip() or default_model
    base_url = None
    if spec.needs_base_url:
        base_url = input("OpenAI-compatible Base URL（例如 https://host/v1）：").strip()
        if not base_url:
            print("Base URL 不能为空。")
            return 2
    settings.save_asr(spec.id, model, base_url)
    print("已将 {} 设为默认 ASR 服务。".format(spec.label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
