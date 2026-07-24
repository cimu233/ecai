#!/usr/bin/python3
"""Configure Xiaoe login credentials without exposing passwords in shell history."""

import getpass
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from xiaoe_core.auth import XiaoeCredentialStore
from xiaoe_core.config import AppPaths


def main() -> int:
    paths = AppPaths.resolve()
    paths.create()
    store = XiaoeCredentialStore(paths)
    print("\n小鹅通登录凭据：")
    print("1. 保存到 macOS 钥匙圈（推荐）")
    print("2. 打开本地私密配置文件")
    print("q. 返回")
    choice = input("请选择：").strip().lower()
    if choice == "q":
        return 0
    if choice == "1":
        username = input("手机号或帐号：").strip()
        password = getpass.getpass("密码：")
        store.save_keychain(username, password)
        print("登录凭据已保存到 macOS 钥匙圈。")
        return 0
    if choice == "2":
        path = store.ensure_file_template()
        subprocess.run(["/usr/bin/open", "-a", "TextEdit", str(path)], check=False)
        print("请填写 username 和 password，然后保存文件：{}".format(path))
        return 0
    print("选择无效。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
