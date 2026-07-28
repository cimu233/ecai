#!/usr/bin/python3
"""Interactive structure provider configuration for the desktop launcher."""

import getpass
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xiaoe_core.config import AppPaths, AppSettings
from xiaoe_core.file_opener import open_with_default_app
from xiaoe_core.secrets import MacKeychainSecretStore
from xiaoe_core.structure_catalog import StructureModelCatalog
from xiaoe_core.structure_registry import list_structure_provider_specs
from xiaoe_core.structurer import (
    DEFAULT_PROMPT_TEMPLATE,
    TITLE_PLACEHOLDER,
    TRANSCRIPT_PLACEHOLDER,
    build_structure_prompt,
)


def choose_model(models, default_model):
    models = list(dict.fromkeys(model for model in models if model))
    page = 0
    page_size = 10
    while True:
        total_pages = max(1, (len(models) + page_size - 1) // page_size)
        page = min(page, total_pages - 1)
        start = page * page_size
        visible = models[start:start + page_size]
        print("\n可选模型（第 {}/{} 页）：".format(page + 1, total_pages))
        for offset, model in enumerate(visible, 1):
            marker = "（默认）" if model == default_model else ""
            print("{}. {} {}".format(offset, model, marker))
        print("直接回车：使用默认模型 {}".format(default_model or "Agent 默认值"))
        print("c. 手动输入模型名称")
        if page > 0:
            print("p. 上一页")
        if page + 1 < total_pages:
            print("n. 下一页")
        choice = input("请选择模型：").strip().lower()
        if not choice:
            return default_model
        if choice == "c":
            return input("模型名称：").strip() or default_model
        if choice == "n" and page + 1 < total_pages:
            page += 1
            continue
        if choice == "p" and page > 0:
            page -= 1
            continue
        try:
            return visible[int(choice) - 1]
        except (ValueError, IndexError):
            print("选择无效。")


def choose_effort(levels, default_effort):
    if not levels:
        print("该服务通过模型选择控制推理能力，不提供独立思考强度参数。")
        return None
    default_effort = default_effort if default_effort in levels else levels[0]
    print("\n可选思考强度：")
    for index, level in enumerate(levels, 1):
        marker = "（默认）" if level == default_effort else ""
        print("{}. {} {}".format(index, level, marker))
    choice = input("请选择思考强度（直接回车使用默认值）：").strip()
    if not choice:
        return default_effort
    try:
        return levels[int(choice) - 1]
    except (ValueError, IndexError):
        print("选择无效，已使用默认值。")
        return default_effort


def configure_prompt(paths, settings):
    current = settings.structure().get("prompt_file")
    print("\n结构化提示词：")
    print("当前：{}".format(current or "程序内置忠实整理模板"))
    print("1. 保持当前设置")
    print("2. 启用默认的本地自定义模板文件")
    print("3. 使用其他模板文件")
    print("4. 恢复程序内置模板")
    print("5. 用系统默认程序打开自定义模板")
    choice = input("请选择（直接回车保持当前设置）：").strip()
    if not choice or choice == "1":
        return
    if choice == "4":
        settings.save_structure_prompt(None)
        print("已恢复程序内置提示词。")
        return
    if choice == "5":
        path = Path(current).expanduser() if current else paths.data_dir / "structure-prompt.txt"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_PROMPT_TEMPLATE, encoding="utf-8")
            settings.save_structure_prompt(str(path.resolve()))
        try:
            open_with_default_app(path)
        except RuntimeError as error:
            print("无法打开模板：{}".format(error))
            return
        print("已用系统默认程序打开：{}".format(path.resolve()))
        return
    if choice == "2":
        path = paths.data_dir / "structure-prompt.txt"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_PROMPT_TEMPLATE, encoding="utf-8")
    elif choice == "3":
        value = input("模板文件路径：").strip()
        if not value:
            print("路径为空，已保持当前设置。")
            return
        path = Path(value).expanduser().resolve()
    else:
        print("选择无效，已保持当前设置。")
        return
    try:
        template = path.read_text(encoding="utf-8")
        build_structure_prompt("课程标题", "转写全文", template)
    except (OSError, RuntimeError) as error:
        print("模板无法使用：{}".format(error))
        return
    settings.save_structure_prompt(str(path))
    print("已启用自定义提示词：{}".format(path))
    print("可选占位符：{} 和 {}。".format(
        TITLE_PLACEHOLDER, TRANSCRIPT_PLACEHOLDER
    ))
    print("未写占位符时，程序会自动附加课程标题和转写全文。")


def main() -> int:
    specs = list_structure_provider_specs()
    paths = AppPaths.resolve()
    settings = AppSettings(paths.settings_file)
    secrets = MacKeychainSecretStore()
    saved = settings.structure()
    current = saved.get("provider", "codex")
    print("\n可用结构化生成服务：")
    for index, spec in enumerate(specs, 1):
        marker = "（当前）" if spec.id == current else ""
        mode = "本机 Agent" if spec.mode == "local_agent" else "API"
        status = spec.to_dict(secrets)["configured"]
        status_text = "可用" if status else "待配置"
        print("{}. {} [{} / {}] {} {}".format(
            index, spec.label, mode, status_text, spec.region, marker
        ))
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
            secrets.set(account, value, "Xiaoe Structure - {}".format(label))
        elif not existing:
            print("缺少 {}，本次没有切换默认服务。".format(label))
            return 2

    base_url = None
    if spec.needs_base_url:
        base_url = input(
            "OpenAI-compatible Base URL（例如 https://host/v1）："
        ).strip()
        if not base_url:
            print("Base URL 不能为空。")
            return 2

    catalog = StructureModelCatalog(paths.data_dir / "structure-models.json")
    catalog_settings = {"base_url": base_url} if base_url else {}
    result = catalog.get(spec.id, catalog_settings, secrets)
    source_labels = {
        "network": "联网获取",
        "cache": "本地缓存",
        "built_in": "内置备选清单",
    }
    print("\n模型清单来源：{}".format(source_labels.get(result.source, result.source)))
    if result.updated:
        print("检测到模型清单更新，本地缓存已刷新。")
    if result.warning:
        print("提示：{}".format(result.warning))

    default_model = (
        saved.get("model") if saved.get("provider") == spec.id
        else spec.default_model
    )
    model = choose_model(result.models, default_model)
    effort_levels = result.effort_levels(model or "", spec.effort_levels)
    default_effort = (
        saved.get("effort") if saved.get("provider") == spec.id
        else spec.default_effort
    )
    effort = choose_effort(effort_levels, default_effort)
    settings.save_structure(spec.id, model, base_url, effort)
    configure_prompt(paths, settings)
    print("已将 {} 设为默认结构化生成服务。".format(spec.label))
    print("模型：{}".format(model or "Agent 默认值"))
    print("思考强度：{}".format(effort or "由模型决定"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
