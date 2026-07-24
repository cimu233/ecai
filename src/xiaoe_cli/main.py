"""CLI entry point."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from xiaoe_core.config import AppPaths, AppSettings
from xiaoe_core.auth import XIAOE_LOGIN_URL, XiaoeCredentialStore, XiaoePasswordLogin
from xiaoe_core.asr import AsrProvider
from xiaoe_core.asr_registry import PROVIDER_SPECS, build_asr_provider as create_asr_provider, list_provider_specs
from xiaoe_core.api import create_server
from xiaoe_core.browser import BrowserError, ChromeManager, EdgeManager, inspect_page
from xiaoe_core.database import Database
from xiaoe_core.download_service import DownloadService
from xiaoe_core.downloader import AudioDownloader
from xiaoe_core.ego_browser import EgoBrowserManager
from xiaoe_core.media import MediaSelector, StoredUrlResolver
from xiaoe_core.pipeline import PipelineRunner
from xiaoe_core.services import CourseService, LessonService
from xiaoe_core.structure_service import StructureService
from xiaoe_core.structurer import CodexCliStructurer
from xiaoe_core.transcription_service import TranscriptionService
from xiaoe_core.xiaoe import (
    HybridMediaResolver,
    XiaoeAccountCatalogService,
    XiaoeBrowserMediaResolver,
    XiaoeCatalogService,
)
from xiaoe_cli.course_picker import course_status_label


DEFAULT_AUTH_URL = "https://study.xiaoe-tech.com"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaoe", description="Local Xiaoe audio pipeline")
    parser.add_argument("--data-dir", help="Runtime data directory; defaults to ~/.xiaoe-audio-pipeline")
    parser.add_argument("--browser", choices=["chrome", "edge", "ego"], dest="browser_backend")
    subcommands = parser.add_subparsers(dest="command", required=True)

    course_parser = subcommands.add_parser("course", help="Manage courses")
    course_commands = course_parser.add_subparsers(dest="course_command", required=True)

    add_parser = course_commands.add_parser("add", help="Add a course URL")
    add_parser.add_argument("url")
    add_parser.add_argument("--title")
    add_parser.add_argument("--json", action="store_true", dest="as_json")

    list_parser = course_commands.add_parser("list", help="List known courses")
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    scan_account_parser = course_commands.add_parser("scan-account", help="Import course containers from My Courses")
    scan_account_parser.add_argument("--url", help="A Xiaoe course or store URL; saved course origins are used by default")
    scan_account_parser.add_argument("--json", action="store_true", dest="as_json")

    refresh_parser = course_commands.add_parser("refresh", help="Discover lessons from the Xiaoe course page")
    refresh_parser.add_argument("course_id")
    refresh_parser.add_argument("--json", action="store_true", dest="as_json")

    status_parser = subcommands.add_parser("status", help="Show pipeline status")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    download_parser = subcommands.add_parser("download", help="Download lesson audio for a course")
    download_parser.add_argument("course_id")
    download_parser.add_argument("--lesson", dest="lesson_id")
    download_parser.add_argument("--limit", type=int)
    download_parser.add_argument("--json", action="store_true", dest="as_json")

    transcribe_parser = subcommands.add_parser("transcribe", help="Transcribe downloaded lesson audio")
    transcribe_parser.add_argument("course_id")
    transcribe_parser.add_argument("--lesson", dest="lesson_id")
    transcribe_parser.add_argument("--limit", type=int)
    transcribe_parser.add_argument("--provider", choices=list(PROVIDER_SPECS))
    transcribe_parser.add_argument("--model")
    transcribe_parser.add_argument("--language", help="Known language, for example zh or en")
    transcribe_parser.add_argument("--local-model-dir")
    transcribe_parser.add_argument("--local-runtime-python")
    transcribe_parser.add_argument("--local-device", choices=["auto", "mps", "cpu"], default="auto")
    transcribe_parser.add_argument("--force", action="store_true")
    transcribe_parser.add_argument("--json", action="store_true", dest="as_json")

    structure_parser = subcommands.add_parser("structure", help="Turn transcripts into faithful Markdown notes")
    structure_parser.add_argument("course_id")
    structure_parser.add_argument("--lesson", dest="lesson_id")
    structure_parser.add_argument("--limit", type=int)
    structure_parser.add_argument("--model")
    structure_parser.add_argument("--force", action="store_true")
    structure_parser.add_argument("--json", action="store_true", dest="as_json")

    run_parser = subcommands.add_parser("run", help="Run download, transcription, and structuring")
    run_parser.add_argument("course_id")
    run_parser.add_argument("--lesson", dest="lesson_id")
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument("--language")
    run_parser.add_argument("--asr-provider", choices=list(PROVIDER_SPECS))
    run_parser.add_argument("--asr-model")
    run_parser.add_argument("--local-model-dir")
    run_parser.add_argument("--local-runtime-python")
    run_parser.add_argument("--local-device", choices=["auto", "mps", "cpu"], default="auto")
    run_parser.add_argument("--codex-model")
    run_parser.add_argument("--force-transcription", action="store_true")
    run_parser.add_argument("--force-structure", action="store_true")
    run_parser.add_argument("--json", action="store_true", dest="as_json")

    auth_parser = subcommands.add_parser("auth", help="Manage the selected browser session")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_start = auth_commands.add_parser("start", help="Open the selected browser for Xiaoe login")
    auth_start.add_argument("--url", default="https://study.xiaoe-tech.com")
    auth_start.add_argument("--json", action="store_true", dest="as_json")
    auth_login = auth_commands.add_parser("login", help="Log the selected browser in with saved credentials")
    auth_login.add_argument("--json", action="store_true", dest="as_json")
    auth_check = auth_commands.add_parser("check", help="Verify the saved Xiaoe session")
    auth_check.add_argument("--url", help="Page used for verification; defaults to the latest saved course")
    auth_check.add_argument("--recover", action="store_true", help="Open visible Chrome when login is required")
    auth_check.add_argument("--json", action="store_true", dest="as_json")
    auth_stop = auth_commands.add_parser("stop", help="Stop the managed browser session")
    auth_stop.add_argument("--json", action="store_true", dest="as_json")
    auth_credentials = auth_commands.add_parser("credentials", help="Inspect or create Xiaoe login credentials")
    auth_credentials.add_argument("--template", action="store_true", help="Create the private local credential file")
    auth_credentials.add_argument("--json", action="store_true", dest="as_json")

    browser_parser = subcommands.add_parser("browser", help="Select Chrome, Edge, or Ego Browser")
    browser_commands = browser_parser.add_subparsers(dest="browser_command", required=True)
    browser_current = browser_commands.add_parser("current", help="Show the selected browser")
    browser_current.add_argument("--json", action="store_true", dest="as_json")
    browser_use = browser_commands.add_parser("use", help="Persist the selected browser")
    browser_use.add_argument("backend", choices=["chrome", "edge", "ego"])
    browser_use.add_argument("--json", action="store_true", dest="as_json")

    asr_parser = subcommands.add_parser("asr", help="Inspect or select ASR providers")
    asr_commands = asr_parser.add_subparsers(dest="asr_command", required=True)
    asr_providers = asr_commands.add_parser("providers", help="List supported ASR providers")
    asr_providers.add_argument("--json", action="store_true", dest="as_json")
    asr_current = asr_commands.add_parser("current", help="Show the selected ASR provider")
    asr_current.add_argument("--json", action="store_true", dest="as_json")
    asr_use = asr_commands.add_parser("use", help="Select an ASR provider without saving a secret")
    asr_use.add_argument("provider", choices=list(PROVIDER_SPECS))
    asr_use.add_argument("--model")
    asr_use.add_argument("--base-url")
    asr_use.add_argument("--json", action="store_true", dest="as_json")

    serve_parser = subcommands.add_parser("serve", help="Serve the loopback read API")
    serve_parser.add_argument("--port", type=int, default=8765)
    return parser


def emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def browser_label(browser: str) -> str:
    return {"chrome": "Chrome", "edge": "Edge", "ego": "Ego"}.get(browser, browser)


def emit_auth(payload: dict, as_json: bool) -> None:
    if as_json:
        emit(payload, True)
        return
    browser = browser_label(str(payload.get("browser") or ""))
    status = str(payload.get("status") or "")
    action = str(payload.get("login") or payload.get("recovery") or "")
    if status == "credentials_required" or action == "credentials_required":
        print("尚未配置完整的小鹅通登录凭据。")
        print("凭据文件：{}".format(payload.get("credential_file") or ""))
    elif status == "authenticated" or action in {"completed", "automatic_login_completed"}:
        suffix = "，浏览器：{}".format(browser) if browser else ""
        print("小鹅通登录有效{}。".format(suffix))
        if payload.get("title"):
            print("验证页面：{}".format(payload["title"]))
    elif action == "manual_verification_required":
        if payload.get("challenge") == "slider":
            print("账号密码已经提交，请在浏览器中手动完成滑块验证。")
            if payload.get("handed_off"):
                print("Ego 已保留当前验证页面并交给你操作，完成后重新检查登录状态。")
        else:
            print("账号密码已经提交，需要在可见浏览器中完成验证码或安全验证。")
    elif status == "access_denied":
        print("当前账号无权访问检查页面。")
    elif status == "running":
        print("{} 登录窗口已打开。".format(browser or "浏览器"))
    elif status == "stopped":
        print("浏览器自动化会话已停止。")
    else:
        print("小鹅通当前未登录。")


def emit_catalog(course_title: str, lessons: list, as_json: bool, course_id: str) -> None:
    if as_json:
        emit({"course_id": course_id, "lessons": [lesson.to_dict() for lesson in lessons]}, True)
        return
    print("目录扫描完成：{}，共 {} 条内容。".format(course_title, len(lessons)))
    print()
    for lesson in lessons:
        print("{:>3}. {}".format(lesson.position, lesson.title))


def emit_pipeline_result(result: Any, as_json: bool) -> None:
    payload = result.to_dict()
    if as_json:
        emit(payload, True)
        return
    print("课程处理{}。".format("完成" if result.status == "completed" else "部分失败"))
    print()
    for key, label in (("download", "音频下载"), ("transcription", "语音转文字"), ("structure", "文字整理")):
        stage = payload[key]
        print(
            "{}：处理 {}，成功 {}，跳过 {}，失败 {}".format(
                label,
                stage.get("processed", 0),
                stage.get("succeeded", 0),
                stage.get("skipped", 0),
                stage.get("failed", 0),
            )
        )
        for item in stage.get("items", []):
            if item.get("error"):
                print("  失败：{} - {}".format(item.get("lesson_id") or "未知条目", item["error"]))


def resolve_auth_check_url(service: CourseService, requested_url: Optional[str]) -> str:
    if requested_url:
        return requested_url
    courses = service.list_courses()
    return courses[0].source_url if courses else DEFAULT_AUTH_URL


def inspect_saved_session(chrome: Any, url: str) -> dict:
    if hasattr(chrome, "check_auth"):
        return chrome.check_auth(url)
    for attempt in range(2):
        endpoint = chrome.ensure_running(visible=False)
        page = chrome.open_page(endpoint, url)
        try:
            return inspect_page(page)
        except BrowserError as error:
            if error.code != "cdp_disconnected" or attempt == 1:
                raise
        finally:
            page.close()
    raise RuntimeError("Saved Chrome session could not be inspected.")


def build_browser_manager(arguments: argparse.Namespace, paths: AppPaths) -> Any:
    selected = arguments.browser_backend or AppSettings(paths.settings_file).browser()
    if selected == "ego":
        return EgoBrowserManager()
    if selected == "edge":
        return EdgeManager(paths.edge_profile_dir)
    return ChromeManager(paths.browser_profile_dir)


def build_asr_provider(arguments: argparse.Namespace, paths: AppPaths, provider_name: Optional[str]) -> AsrProvider:
    settings = AppSettings(paths.settings_file).asr()
    selected = provider_name or str(settings.get("provider") or "local")
    model = arguments.model if hasattr(arguments, "model") else arguments.asr_model
    return create_asr_provider(
        selected,
        settings=settings,
        language=arguments.language,
        model_override=model,
        local_model_dir=Path(arguments.local_model_dir) if arguments.local_model_dir else None,
        local_runtime_python=Path(arguments.local_runtime_python) if arguments.local_runtime_python else None,
        local_device=arguments.local_device,
    )


def run(arguments: argparse.Namespace) -> int:
    paths = AppPaths.resolve(arguments.data_dir)
    paths.create()
    service = CourseService(Database(paths.database_file))

    if arguments.command == "browser":
        settings = AppSettings(paths.settings_file)
        if arguments.browser_command == "use":
            settings.save_browser(arguments.backend)
        payload = {"browser": settings.browser()}
        if arguments.as_json:
            emit(payload, True)
        else:
            print("当前浏览器：{}".format(browser_label(payload["browser"])))
        return 0

    if arguments.command == "asr":
        settings = AppSettings(paths.settings_file)
        if arguments.asr_command == "providers":
            from xiaoe_core.secrets import MacKeychainSecretStore
            secrets = MacKeychainSecretStore()
            payload = {"providers": [spec.to_dict(secrets) for spec in list_provider_specs()]}
        elif arguments.asr_command == "current":
            payload = settings.asr()
        else:
            if arguments.provider == "custom" and not arguments.base_url:
                raise ValueError("Custom ASR requires --base-url.")
            settings.save_asr(arguments.provider, arguments.model, arguments.base_url)
            payload = settings.asr()
        emit(payload, arguments.as_json)
        return 0

    if arguments.command == "auth":
        credential_store = XiaoeCredentialStore(paths)
        if arguments.auth_command == "credentials":
            if arguments.template:
                credential_store.ensure_file_template()
            payload = credential_store.status()
            if arguments.as_json:
                emit(payload, True)
            elif payload["configured"]:
                print("登录凭据已配置，来源：{}。".format("macOS 钥匙圈" if payload["source"] == "keychain" else "本地文件"))
            else:
                print("登录凭据尚未配置。")
                print("凭据文件：{}".format(payload["file"]))
            return 0
        chrome = build_browser_manager(arguments, paths)
        if arguments.auth_command == "start":
            endpoint = chrome.ensure_running(visible=True, initial_url=arguments.url)
            emit_auth(
                {"status": "running", "browser": chrome.browser_name, "mode": "visible", "endpoint": endpoint},
                arguments.as_json,
            )
            return 0
        if arguments.auth_command == "login":
            credentials, credential_source = credential_store.load()
            if credentials is None:
                credential_file = credential_store.ensure_file_template()
                emit_auth(
                    {
                        "status": "credentials_required",
                        "browser": chrome.browser_name,
                        "credential_file": str(credential_file),
                    },
                    arguments.as_json,
                )
                return 3
            attempt = XiaoePasswordLogin(chrome).attempt(credentials)
            check_url = resolve_auth_check_url(service, None)
            if attempt.get("challenge") and attempt.get("handed_off"):
                emit_auth(
                    {
                        "status": "login_required",
                        "browser": chrome.browser_name,
                        "login": "manual_verification_required",
                        "challenge": attempt["challenge"],
                        "handed_off": True,
                    },
                    arguments.as_json,
                )
                return 1
            verified = inspect_saved_session(chrome, check_url)
            payload = {
                "status": verified["status"],
                "browser": chrome.browser_name,
                "credential_source": credential_source,
                "login_submitted": bool(attempt.get("submitted")),
                "checked_url": check_url,
                "url": verified.get("url"),
                "title": verified.get("title"),
            }
            if verified["status"] == "authenticated":
                payload["login"] = "completed"
                emit_auth(payload, arguments.as_json)
                return 0
            chrome.stop()
            chrome.ensure_running(visible=True, initial_url=XIAOE_LOGIN_URL)
            payload["login"] = "manual_verification_required"
            emit_auth(payload, arguments.as_json)
            return 1
        if arguments.auth_command == "stop":
            chrome.stop()
            emit_auth({"status": "stopped"}, arguments.as_json)
            return 0
        check_url = resolve_auth_check_url(service, arguments.url)
        state = inspect_saved_session(chrome, check_url)
        payload = {
            "status": state["status"],
            "checked_url": check_url,
            "url": state.get("url"),
            "title": state.get("title"),
        }
        if state["status"] == "login_required" and arguments.recover:
            credentials, credential_source = credential_store.load()
            if credentials is None:
                credential_file = credential_store.ensure_file_template()
                payload["recovery"] = "credentials_required"
                payload["credential_file"] = str(credential_file)
                emit_auth(payload, arguments.as_json)
                return 3
            attempt = XiaoePasswordLogin(chrome).attempt(credentials)
            payload["credential_source"] = credential_source
            payload["login_submitted"] = bool(attempt.get("submitted"))
            if attempt.get("challenge") and attempt.get("handed_off"):
                payload["recovery"] = "manual_verification_required"
                payload["challenge"] = attempt["challenge"]
                payload["handed_off"] = True
                emit_auth(payload, arguments.as_json)
                return 1
            verified = inspect_saved_session(chrome, check_url)
            payload.update(
                {
                    "status": verified["status"],
                    "url": verified.get("url"),
                    "title": verified.get("title"),
                }
            )
            if verified["status"] == "authenticated":
                payload["recovery"] = "automatic_login_completed"
                emit_auth(payload, arguments.as_json)
                return 0
            chrome.stop()
            chrome.ensure_running(visible=True, initial_url=XIAOE_LOGIN_URL)
            payload["recovery"] = "manual_verification_required"
        emit_auth(payload, arguments.as_json)
        return 0 if state["status"] == "authenticated" else 1

    if arguments.command == "serve":
        server = create_server(service.database, port=arguments.port)
        print("Local API: http://127.0.0.1:{}".format(server.server_port))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if arguments.command == "course" and arguments.course_command == "add":
        result = service.add_course(arguments.url, arguments.title)
        if arguments.as_json:
            emit(result.to_dict(), True)
        else:
            action = "课程已添加" if result.created else "课程已经存在"
            print("{}：{}".format(action, result.course.title))
            print("课程 ID：{}".format(result.course.id))
        return 0

    if arguments.command == "course" and arguments.course_command == "scan-account":
        browser = build_browser_manager(arguments, paths)
        result = XiaoeAccountCatalogService(paths, service, browser).scan(arguments.url)
        if arguments.as_json:
            emit(result, True)
        else:
            print(
                "扫描完成：账号资源 {scanned_resources} 条，课程 {course_candidates} 门，"
                "新增 {imported} 门，已有 {existing} 门。".format(**result)
            )
            print()
            for index, course in enumerate(result["courses"], 1):
                print("{:>2}. {}  [{}]".format(index, course["title"], course["id"]))
            print()
            print("原始账号清单：{}".format(result["raw_file"]))
        return 0

    if arguments.command == "course" and arguments.course_command == "list":
        courses = [course.to_dict() for course in service.list_courses()]
        if arguments.as_json:
            emit({"courses": courses}, True)
        elif not courses:
            emit("No courses found.", False)
        else:
            for course in courses:
                print("课程名称：{title}".format(**course))
                print("课程 ID：{id}".format(**course))
                print("状态：{}".format(course_status_label(course["status"])))
                print("课程链接：{source_url}".format(**course))
                print()
        return 0

    if arguments.command == "course" and arguments.course_command == "refresh":
        lessons = LessonService(service.database)
        chrome = build_browser_manager(arguments, paths)
        discovered = XiaoeCatalogService(paths, service, lessons, chrome).refresh(arguments.course_id)
        course = service.get(arguments.course_id)
        emit_catalog(course.title if course else arguments.course_id, discovered, arguments.as_json, arguments.course_id)
        return 0

    if arguments.command == "status":
        status = service.status().to_dict()
        if arguments.as_json:
            emit(status, True)
        else:
            print("课程总数：{}".format(status["total_courses"]))
            print("内容总数：{}".format(status["total_lessons"]))
            active = service.get(status["active_course_id"]) if status["active_course_id"] else None
            print("当前任务：{}".format(active.title if active else "无"))
        return 0

    if arguments.command == "download":
        lessons = LessonService(service.database)
        download_service = DownloadService(
            paths=paths,
            courses=service,
            lessons=lessons,
            resolver=HybridMediaResolver(XiaoeBrowserMediaResolver(build_browser_manager(arguments, paths))),
            selector=MediaSelector(),
            downloader=AudioDownloader(),
        )
        result = download_service.download_course(
            arguments.course_id,
            lesson_id=arguments.lesson_id,
            limit=arguments.limit,
        )
        if arguments.as_json:
            emit(result.to_dict(), True)
        elif result.processed == 0:
            print("该课程尚未扫描内容目录，请先执行「扫描课程目录」。")
        else:
            print(
                "处理 {} 节，成功 {} 节，跳过 {} 节，失败 {} 节".format(
                    result.processed,
                    result.succeeded,
                    result.skipped,
                    result.failed,
                )
            )
            for item in result.items:
                label = {"audio_ready": "已下载", "skipped": "已跳过", "source_unavailable": "无音频源", "download_failed": "下载失败"}.get(
                    item.status, item.status
                )
                print("  {} — {}".format(item.lesson_id, label))
        return 1 if result.failed else 0

    if arguments.command == "transcribe":
        lessons = LessonService(service.database)
        provider = build_asr_provider(arguments, paths, arguments.provider)
        transcription = TranscriptionService(paths, service, lessons, provider)
        result = transcription.transcribe_course(
            arguments.course_id,
            lesson_id=arguments.lesson_id,
            limit=arguments.limit,
            force=arguments.force,
        )
        if arguments.as_json:
            emit(result.to_dict(), True)
        else:
            emit(
                "Processed: {} | Succeeded: {} | Skipped: {} | Failed: {}".format(
                    result.processed, result.succeeded, result.skipped, result.failed
                ),
                False,
            )
            for item in result.items:
                print("{}\t{}\t{}".format(item.lesson_id, item.status, item.text_file or item.error or ""))
        return 1 if result.failed else 0

    if arguments.command == "structure":
        lessons = LessonService(service.database)
        structuring = StructureService(service, lessons, CodexCliStructurer(model=arguments.model))
        result = structuring.structure_course(
            arguments.course_id,
            lesson_id=arguments.lesson_id,
            limit=arguments.limit,
            force=arguments.force,
        )
        if arguments.as_json:
            emit(result.to_dict(), True)
        else:
            emit(
                "Processed: {} | Succeeded: {} | Skipped: {} | Failed: {}".format(
                    result.processed, result.succeeded, result.skipped, result.failed
                ),
                False,
            )
            for item in result.items:
                print("{}\t{}\t{}".format(item.lesson_id, item.status, item.markdown_file or item.error or ""))
        return 1 if result.failed else 0

    if arguments.command == "run":
        lessons = LessonService(service.database)
        provider = build_asr_provider(arguments, paths, arguments.asr_provider)
        chrome = build_browser_manager(arguments, paths)
        XiaoeCatalogService(paths, service, lessons, chrome).refresh(arguments.course_id)
        downloads = DownloadService(
            paths=paths,
            courses=service,
            lessons=lessons,
            resolver=HybridMediaResolver(XiaoeBrowserMediaResolver(chrome)),
            selector=MediaSelector(),
            downloader=AudioDownloader(),
        )
        transcriptions = TranscriptionService(
            paths,
            service,
            lessons,
            provider,
        )
        structures = StructureService(service, lessons, CodexCliStructurer(model=arguments.codex_model))
        result = PipelineRunner(downloads, transcriptions, structures).run(
            arguments.course_id,
            lesson_id=arguments.lesson_id,
            limit=arguments.limit,
            force_transcription=arguments.force_transcription,
            force_structure=arguments.force_structure,
        )
        emit_pipeline_result(result, arguments.as_json)
        return 0 if result.status == "completed" else 1

    raise RuntimeError("Unhandled command.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        return run(parser.parse_args(argv))
    except (ValueError, RuntimeError, BrowserError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2
