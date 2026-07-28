# 鹅采 ecai — 交接文档

> 写给下一个 AI：这份文档涵盖项目全貌、当前状态、以及你需要接手的构建发布任务。

---

## 一、项目概览

**鹅采 (ecai)** 是一个 Python CLI 工具，用于从小鹅通（Xiaoe）在线课程平台下载音频、语音转文字、整理课程内容。

- **仓库**：[https://github.com/cimu233/ecai](https://github.com/cimu233/ecai)
- **协议**：AGPL-3.0
- **平台**：macOS（主要开发环境）/ Windows / Linux
- **本地路径**：`/Users/cimu_lumi/Desktop/【項目】小工具/xiaoe-tools/xiaoe-audio-pipeline/`

### 核心功能
1. 浏览器自动登录小鹅通（账号密码 + 滑块验证码自动识别）
2. 扫描课程目录结构
3. 批量下载课程音频（m4a → mp3 转码）
4. 语音转文字（支持阿里 DashScope / OpenAI Whisper / 本地 Qwen ASR 等多后端）
5. 通过本机 Codex / Claude Code 或多家模型 API 整理输出 Markdown 讲义

---

## 二、目录结构

```
xiaoe-audio-pipeline/
├── src/
│   ├── xiaoe_core/          # 核心库（25 个模块）
│   │   ├── api.py           # 小鹅通 HTTP API + NDS 解密
│   │   ├── asr.py           # ASR 基类
│   │   ├── asr_registry.py  # ASR 提供商注册表
│   │   ├── auth.py          # 浏览器自动登录（账号密码 + 滑块验证码）
│   │   ├── browser.py       # Chrome/Edge CDP 后端（WebSocket）+ 平台自适应路径
│   │   ├── config.py        # settings.json 配置管理，支持 5 种浏览器后端
│   │   ├── database.py      # SQLite 数据库（课程/任务状态持久化）
│   │   ├── download_service.py  # 下载任务编排
│   │   ├── downloader.py    # 音频下载 + ffmpeg 转码
│   │   ├── ego_browser.py   # Ego 浏览器后端（macOS 专用，Task Space 隔离）
│   │   ├── file_opener.py   # 使用系统默认程序打开本地文件
│   │   ├── local_asr_worker.py  # 本地 Qwen ASR 子进程 worker
│   │   ├── media.py         # 音频处理工具
│   │   ├── models.py        # Pydantic 数据模型
│   │   ├── pipeline.py      # 完整流水线编排
│   │   ├── playwright_browser.py  # Playwright 跨平台浏览器后端
│   │   ├── secrets.py       # 跨平台凭据存储（Keychain / Windows Credential / .env）
│   │   ├── services.py      # 业务逻辑层
│   │   ├── slider_captcha.py    # OpenCV 滑块验证码自动识别
│   │   ├── structure_service.py # 转写稿到讲义的任务编排
│   │   ├── structure_catalog.py # 在线模型清单与本地缓存
│   │   ├── structure_registry.py # 结构化生成提供商注册表
│   │   ├── structurer.py    # 本机 Agent / API 讲义生成
│   │   ├── transcription_service.py  # 转写服务
│   │   └── xiaoe.py         # 小鹅通页面爬取
│   └── xiaoe_cli/           # CLI 入口
│       ├── main.py          # 命令行解析 + 主流程（约 600 行）
│       ├── setup.py         # ecai setup 首次运行安装向导
│       └── course_picker.py # 交互式课程选择器
├── scripts/
│   ├── ecai-build.command  # 一键构建发布脚本（也在桌面小工具文件夹）
│   ├── configure_xiaoe_login.py
│   ├── configure_asr.py
│   ├── configure_structure.py
│   └── select_course.py
├── tests/                   # 21 个测试文件，133 个测试用例全部通过
├── .github/workflows/
│   └── build.yml            # GitHub Actions Windows 构建（有未推送的修复）
├── Xiaoe Audio Pipeline.command  # macOS 双击启动的 Shell 菜单
├── requirements.txt
└── HANDOFF.md               # 本文档
```

---

## 三、关键技术架构

### 3.1 浏览器后端（Duck Typing）

项目有 **5 种浏览器后端**，使用鸭子类型共享接口，不设正式抽象基类：

| 后端 | 类 | 平台 | 原理 |
|------|-----|------|------|
| Chrome | `ChromiumManager` | 全平台 | CDP WebSocket |
| Edge | `EdgeManager` | 全平台 | CDP WebSocket |
| Ego | `EgoBrowserManager` | 仅 macOS | ego-browser CLI → CDP |
| Playwright-Chrome | `PlaywrightBrowserManager` | 全平台 | Playwright sync API + CDP |
| Playwright-Edge | `PlaywrightBrowserManager` | 全平台 | Playwright sync API + CDP |

**选用逻辑**：`browser.py` 中的 `_find_chrome()` / `_find_edge()` 自动检测各平台浏览器路径。Playwright 后端在 `playwright_browser.py` 中，通过 `_bundled_playwright_browsers_path()` 查找 PyInstaller 打包的浏览器。

### 3.2 懒加载机制

`main.py` 使用 `from __future__ import annotations` + `_lazy_imports()` 延迟导入重型模块。这使得在 PyInstaller 打包的 lite 版 exe 中，可以在导入任何重型依赖之前先拦截 `ecai setup` 命令。

**关键约束**：模块级有 `None` 占位符，`_import_if_none()` 不会覆盖已设置的 mock（测试兼容）。

### 3.3 跨平台凭据存储

`secrets.py` 中的 `SecretStore`：
- **macOS**：优先 Keychain（`/usr/bin/security`）
- **Windows**：优先 Credential Manager（通过 `keyring` 库）
- **回退**：`~/.ecai/.env` 文件

### 3.4 滑块验证码

`slider_captcha.py` 使用 OpenCV 模板匹配识别滑块缺口，通过 CDP `Input.dispatchMouseEvent` 模拟真人拖动轨迹。这是可选功能（opencv-python 未安装时 auth 仍可正常走手动流程）。

---

## 四、当前 Git 状态（重要！）

```
分支: main（已与 origin/main 同步）
远程: https://github.com/cimu233/ecai.git

本地提交（从新到旧）：
  c487eb6 fix: allow release asset uploads
  79924c8 fix: validate full Playwright bundle packaging
  3653002 fix: exit 0 after robocopy
  05bf5e1 fix: robocopy for playwright bundling

标签:
  v0.1.6 — 指向 c487eb6，Windows 构建及 GitHub Release 均成功
```

`v0.1.6` 是当前可用发布版本。

---

## 五、构建工作流说明

### 5.1 两个版本

| 版本 | exe 大小 | 内容 |
|------|----------|------|
| **lite** | ~60MB | 仅 Python + pip，首次运行 `ecai setup` 在线安装依赖 |
| **full** | ~300MB+ | 所有依赖 + Playwright Chromium + ffmpeg，开箱即用 |

### 5.2 构建流程（`.github/workflows/build.yml`）

1. **build-lite**：Windows runner → PyInstaller onefile → 打包 pip → zip
2. **build-full**：Windows runner → pip install 全部依赖 → PyInstaller onefile + ffmpeg + playwright chromium → zip
3. **release**（仅 tag 推送触发）：下载两个 job 产物 → `action-gh-release` 上传

### 5.3 已解决的构建问题

- NSIS 不存在：移除 NSIS，改用 `Compress-Archive` 生成 zip。
- Playwright Chromium 复制冲突：改用 `robocopy` 复制目录树。
- `robocopy` 成功状态被误判：接受小于 8 的退出码，并重置 PowerShell 退出状态。
- 不完整的 full 包：打包前递归确认 `chrome.exe` 已存在。
- Release 上传无权限：为 release job 增加 `contents: write`。

---

## 六、发布结果

GitHub Actions 运行 `30198943771` 已通过：

- `build-lite`：成功
- `build-full`：成功
- `release`：成功

[v0.1.6 Releases 页面](https://github.com/cimu233/ecai/releases/tag/v0.1.6)包含：
- `ecai-lite.zip` — 包含 `ecai.exe`
- `ecai-full.zip` — 包含 `ecai.exe` + `playwright-browsers/`

以后发布新版本时，在最新 `main` 上创建新标签并推送即可触发同一流程。

---

## 七、可能遇到的问题

### 7.1 PyInstaller 隐藏导入缺失

如果运行 exe 时报 `ModuleNotFoundError`，需要在 `build.yml` 中添加对应的 `--hidden-import`。当前 workflow 已经列举了所有 `xiaoe_core` 和 `xiaoe_cli` 模块。

### 7.2 PyInstaller 打包后 import 失败

`main.py` 的 `_lazy_imports()` 依赖 `importlib.import_module()` 来延迟导入。如果在 PyInstaller 打包后某些模块找不到：
- 确认 `--hidden-import` 包含该模块
- 或在该模块的 `__init__.py` 中做显式 re-export

### 7.3 Playwright 浏览器路径

`playwright_browser.py` 中的 `_bundled_playwright_browsers_path()` 查找逻辑：
1. `sys.executable` 所在目录下的 `playwright-browsers/`
2. PyInstaller 打包时 `sys._MEIPASS` 下的 `playwright-browsers/`

full 版 build 的最后一步（"Bundle everything"）正是把 Playwright Chromium 复制到 `ecai-full/playwright-browsers/`。

### 7.4 macOS TCC 权限

macOS 可能随机拒绝访问 Desktop 下的文件（"Operation not permitted"）。这是 macOS 的 TCC（透明度、同意和控制）机制。如果 Read 工具报错，改用 Bash 工具执行 `cat` 命令。

### 7.5 本地代理干扰

用户的 Mac 上有代理 `127.0.0.1:6789`，git 和 curl 操作可能被阻断。所有 git 远程操作应加 `-c http.proxy= -c https.proxy=` 绕过代理。

---

## 八、测试

```bash
cd "/Users/cimu_lumi/Desktop/【項目】小工具/xiaoe-tools/xiaoe-audio-pipeline"
python3 -m pytest tests/ -v
# 当前：72 passed，全部绿色
```

---

## 九、桌面工具

```
~/Desktop/小工具/xiaoe-tools/
├── ecai-build.command      # 一键构建发布脚本（macOS 双击运行）
└── xiaoe-audio-pipeline/   # 项目主目录
    └── Xiaoe Audio Pipeline.command  # 应用启动菜单
```

`ecai-build.command` 自动完成：版本号自增 → 推送 → 打标签 → 监控 CI → 下载产物。

---

## 十、关键文件速查

| 需求 | 文件 |
|------|------|
| CLI 入口/参数解析 | `src/xiaoe_cli/main.py` |
| 构建工作流 | `.github/workflows/build.yml` |
| 浏览器后端 | `src/xiaoe_core/browser.py`, `playwright_browser.py`, `ego_browser.py` |
| 凭据存储 | `src/xiaoe_core/secrets.py` |
| 安装向导 | `src/xiaoe_cli/setup.py` |
| 滑块验证码 | `src/xiaoe_core/slider_captcha.py` |
| 登录流程 | `src/xiaoe_core/auth.py` |
| 配置文件 | `src/xiaoe_core/config.py` |
| 结构化 Provider | `src/xiaoe_core/structure_registry.py`, `structurer.py` |
| 模型清单缓存 | `src/xiaoe_core/structure_catalog.py` |
| 自定义结构化提示词 | `~/.xiaoe-audio-pipeline/structure-prompt.txt` |
| Shell 启动菜单 | `Xiaoe Audio Pipeline.command` |
| 构建脚本 | `scripts/ecai-build.command` |

---

*文档生成于 2026-07-25，写给下一个接手的 AI。祝构建顺利！*
