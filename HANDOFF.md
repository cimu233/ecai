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
5. 整理输出 Markdown 讲义

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
│   │   ├── config.py        # 配置文件管理（TOML），支持 5 种浏览器后端
│   │   ├── database.py      # SQLite 数据库（课程/任务状态持久化）
│   │   ├── download_service.py  # 下载任务编排
│   │   ├── downloader.py    # 音频下载 + ffmpeg 转码
│   │   ├── ego_browser.py   # Ego 浏览器后端（macOS 专用，Task Space 隔离）
│   │   ├── local_asr_worker.py  # 本地 Qwen ASR 子进程 worker
│   │   ├── media.py         # 音频处理工具
│   │   ├── models.py        # Pydantic 数据模型
│   │   ├── pipeline.py      # 完整流水线编排
│   │   ├── playwright_browser.py  # Playwright 跨平台浏览器后端
│   │   ├── secrets.py       # 跨平台凭据存储（Keychain / Windows Credential / .env）
│   │   ├── services.py      # 业务逻辑层
│   │   ├── slider_captcha.py    # OpenCV 滑块验证码自动识别
│   │   ├── structure_service.py # 课程结构扫描
│   │   ├── structurer.py    # Markdown 讲义生成
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
│   └── select_course.py
├── tests/                   # 16 个测试文件，72 个测试用例全部通过
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
分支: main (领先 origin/main 1 个提交)
远程: https://github.com/cimu233/ecai.git

本地提交（从新到旧）：
  701825c fix: remove NSIS, use portable zip packaging     ← 未推送！
  72634c8 feat: add transcribe-file + transcribe prompt     ← 已推送
  1933589 feat: dual-version build workflow                 ← 已推送
  88d1694 chore: add ecai-build.command                    ← 已推送
  c9959ce feat: add ecai setup command                      ← 已推送
  00a6abf feat: add GitHub Actions Windows installer        ← 已推送

标签:
  v0.1.0 — 本地存在，指向 72634c8（含 NSIS 的版本，构建已失败）
          GitHub 上 v0.1.0 同样指向含 NSIS 的失败构建
```

**核心问题**：提交 `701825c` (fix: remove NSIS, use portable zip packaging) 在本地但未推送。v0.1.0 标签指向的是含 NSIS 的旧提交，GitHub Actions 构建已失败。

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

### 5.3 上次失败原因

`v0.1.0` 构建失败是因为 workflow 中使用了 NSIS 制作 `.exe` 安装程序，但 `makensis.exe` 不在 GitHub Actions 的 `windows-latest` 镜像中。

**已在本地修复**（提交 `701825c`）：移除 NSIS，改用 PowerShell 内置的 `Compress-Archive` 打包为 zip。

---

## 六、你需要做的事情

### 步骤 1：推送修复提交

```bash
cd "/Users/cimu_lumi/Desktop/【項目】小工具/xiaoe-tools/xiaoe-audio-pipeline"
git -c http.proxy= -c https.proxy= push origin main
```

（`-c http.proxy=` 是为了绕过可能存在的本地代理）

### 步骤 2：处理标签

**方案 A（推荐）— 删除旧标签重建**：
```bash
# 删除本地标签
git tag -d v0.1.0
# 删除远程标签
git -c http.proxy= -c https.proxy= push origin :refs/tags/v0.1.0
# 在当前最新提交上重建标签
git tag -a v0.1.0 -m "Release v0.1.0"
# 推送新标签（触发构建）
git -c http.proxy= -c https.proxy= push origin v0.1.0
```

**方案 B — 新建 v0.1.1**：
```bash
git tag -a v0.1.1 -m "Release v0.1.1"
git -c http.proxy= -c https.proxy= push origin v0.1.1
```

### 步骤 3：监控构建

推送标签后，GitHub Actions 自动触发。监控方式：

```bash
# 方式 1：CLI 监控
gh run list --workflow=build.yml --limit 1
gh run watch <RUN_ID>

# 方式 2：使用构建脚本
cd ~/Desktop/小工具/xiaoe-tools
./ecai-build.command v0.1.0
```

### 步骤 4：验证产物

构建成功后，在 [Releases 页面](https://github.com/cimu233/ecai/releases) 应该能看到：
- `ecai-lite.zip` — 包含 `ecai.exe`
- `ecai-full.zip` — 包含 `ecai.exe` + `playwright-browsers/`

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
| Shell 启动菜单 | `Xiaoe Audio Pipeline.command` |
| 构建脚本 | `scripts/ecai-build.command` |

---

*文档生成于 2026-07-25，写给下一个接手的 AI。祝构建顺利！*
