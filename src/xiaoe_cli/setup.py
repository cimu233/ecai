"""First-run setup wizard: detect location, install optional dependencies on demand."""

import os
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import Optional, Tuple


PIP_REPOS = {
    "cn": [
        "https://mirrors.aliyun.com/pypi/simple/",
        "https://pypi.tuna.tsinghua.edu.cn/simple/",
    ],
    "global": [
        "https://pypi.org/simple/",
    ],
}

MIRROR_INDEX_CHINA = "https://mirrors.aliyun.com/pypi/simple/"
MIRROR_INDEX_GLOBAL = "https://pypi.org/simple/"


def detect_region(timeout: float = 3.0) -> str:
    """Return 'cn' or 'global' based on which mirrors are reachable."""
    for label, urls in [("cn", PIP_REPOS["cn"]), ("global", PIP_REPOS["global"])]:
        for url in urls:
            try:
                req = urllib.request.Request(url, method="HEAD")
                urllib.request.urlopen(req, timeout=timeout)
                return label
            except Exception:
                continue
    return "global"


def pip_index_url() -> str:
    """Return the best pip --index-url for the detected region."""
    return MIRROR_INDEX_CHINA if detect_region() == "cn" else MIRROR_INDEX_GLOBAL


def _run_pip(args: list, index_url: str) -> bool:
    """Run pip install. Uses internal API if running as PyInstaller exe."""
    print("  → {}".format(" ".join(args)))
    try:
        if getattr(sys, "frozen", False):
            _run_pip_internal(args, index_url)
        else:
            _run_pip_subprocess(args, index_url)
        return True
    except Exception as exc:
        print("  pip 安装失败: {}".format(exc), file=sys.stderr)
        return False


def _run_pip_subprocess(args: list, index_url: str) -> None:
    hostname = urllib.parse.urlparse(index_url).hostname or ""
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install",
         "--index-url", index_url, "--trusted-host", hostname] + args,
        stdout=sys.stderr, stderr=sys.stderr,
    )


def _run_pip_internal(args: list, index_url: str) -> None:
    hostname = urllib.parse.urlparse(index_url).hostname or ""
    sys.argv = ["pip", "install", "--index-url", index_url,
                "--trusted-host", hostname] + args
    from pip._internal.cli.main import main as pip_main
    raise SystemExit(pip_main())


def ensure_pip() -> bool:
    """Make sure pip is usable (subprocess or PyInstaller bundle)."""
    if getattr(sys, "frozen", False):
        try:
            import pip._internal.cli.main  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def install_core(index_url: str) -> bool:
    """Install the minimal required packages for basic audio download."""
    print("\n▸ 安装核心依赖 (websocket-client, imageio-ffmpeg)...")
    return _run_pip(["websocket-client", "imageio-ffmpeg"], index_url)


def install_playwright(index_url: str) -> bool:
    """Install Playwright Python package and download Chromium browser."""
    print("\n▸ 安装 Playwright (浏览器自动化)...")
    if not _run_pip(["playwright"], index_url):
        return False
    try:
        import playwright.sync_api
    except ImportError:
        print("  Playwright 安装失败，跳过 Chromium 下载。", file=sys.stderr)
        return False
    print("  → 下载 Chromium 浏览器 (~150MB)...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=sys.stderr, stderr=sys.stderr,
        )
        return True
    except subprocess.CalledProcessError:
        print("  Chromium 下载失败，可稍后手动运行: playwright install chromium", file=sys.stderr)
        return False


def install_opencv(index_url: str) -> bool:
    """Install OpenCV for slider captcha auto-solving."""
    print("\n▸ 安装 OpenCV (滑块验证码自动识别)...")
    return _run_pip(["opencv-python", "numpy"], index_url)


def install_keyring(index_url: str) -> bool:
    """Install keyring for Windows credential storage."""
    print("\n▸ 安装 keyring (系统凭据存储)...")
    return _run_pip(["keyring"], index_url)


def run_setup(interactive: bool = True) -> int:
    """Run the first-time setup wizard. Returns 0 on success."""
    print("=" * 50)
    print("  鹅采 ecai — 首次运行设置")
    print("=" * 50)

    region = detect_region()
    index_url = MIRROR_INDEX_CHINA if region == "cn" else MIRROR_INDEX_GLOBAL
    if region == "cn":
        print("📍 检测到中国大陆网络，使用阿里云镜像加速下载。")
    else:
        print("📍 使用国际 PyPI 源。")

    if not ensure_pip():
        print("\n❌ 当前环境中 pip 不可用。请手动安装 pip 后重试。", file=sys.stderr)
        return 1

    all_ok = True

    # Core deps: always needed
    all_ok &= install_core(index_url)

    # Playwright
    if interactive:
        print()
        choice = input("是否安装 Playwright 浏览器驱动？(y/n，默认 y): ").strip().lower()
    else:
        choice = "y"
    if choice != "n":
        all_ok &= install_playwright(index_url)

    # OpenCV
    if interactive:
        choice = input("是否安装 OpenCV 滑块验证码识别？(y/n，默认 y): ").strip().lower()
    else:
        choice = "y"
    if choice != "n":
        all_ok &= install_opencv(index_url)

    # Keyring
    if sys.platform == "win32":
        if interactive:
            choice = input("是否安装 keyring 凭据存储？(y/n，默认 y): ").strip().lower()
        else:
            choice = "y"
        if choice != "n":
            all_ok &= install_keyring(index_url)

    print()
    if all_ok:
        print("✅ 设置完成！运行 ecai --help 开始使用。")
    else:
        print("⚠️  部分组件安装失败。核心功能可用，可选功能请稍后重试 ecai setup。")
    return 0 if all_ok else 1
