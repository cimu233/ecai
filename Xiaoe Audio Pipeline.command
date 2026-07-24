#!/bin/zsh

project_dir="${0:A:h}"
python_bin="/usr/bin/python3"

run_xiaoe() {
  "$python_bin" - "$project_dir" "$@" <<'PY'
import sys

project_dir = sys.argv[1]
sys.path.insert(0, project_dir + "/src")
from xiaoe_cli.main import main

raise SystemExit(main(sys.argv[2:]))
PY
}

pause_screen() {
  printf "\n按回车键返回主菜单..."
  read -r
}

select_course() {
  "$python_bin" "$project_dir/scripts/select_course.py"
}

if [[ ! -d "$project_dir/src/xiaoe_cli" ]]; then
  echo "找不到项目：$project_dir"
  pause_screen
  exit 1
fi

cd "$project_dir" || exit 1

while true; do
  clear
  cat <<'MENU'
========================================
       Xiaoe Audio Pipeline
========================================
—— 账户 ——
 1. 登录当前浏览器
 2. 检查登录状态
 3. 配置登录凭据
 4. 切换浏览器（Chrome / Edge / Ego）

—— 课程 ——
 5. 导入账号全部课程
 6. 手动添加课程
 7. 查看课程列表
 8. 扫描课程目录

—— 下载与处理 ——
 9. 下载课程音频
10. 转写课程音频
11. 试跑一节课（下载+转写+整理）
12. 运行整门课程（下载+转写+整理）

—— 工具 ——
13. 查看任务状态
14. 配置语音转文字服务
15. 检查本地 Qwen ASR

16. 退出
========================================
MENU
  printf "请选择："
  read -r choice

  case "$choice" in
    1)
      echo "正在使用保存的账号密码登录当前选择的浏览器..."
      run_xiaoe auth login
      auth_status=$?
      if [[ "$auth_status" -eq 3 ]]; then
        credential_file="$HOME/.xiaoe-audio-pipeline/xiaoe-login.json"
        echo "未找到完整登录凭据，正在打开本地私密配置文件..."
        /usr/bin/open -a TextEdit "$credential_file"
      fi
      pause_screen
      ;;
    2)
      echo "正在使用本地保存的课程和当前浏览器登录会话自动检查..."
      run_xiaoe auth check --recover
      auth_status=$?
      if [[ "$auth_status" -eq 3 ]]; then
        credential_file="$HOME/.xiaoe-audio-pipeline/xiaoe-login.json"
        echo "未找到完整登录凭据，正在打开本地私密配置文件..."
        /usr/bin/open -a TextEdit "$credential_file"
      fi
      pause_screen
      ;;
    3)
      "$python_bin" "$project_dir/scripts/configure_xiaoe_login.py"
      pause_screen
      ;;
    4)
      echo "当前配置："
      run_xiaoe browser current
      echo
      echo "1. Chrome（独立本地 profile）"
      echo "2. Edge（独立本地 profile）"
      echo "3. Ego（隔离 Task Space，共享 Ego 登录状态）"
      printf "请选择浏览器："
      read -r browser_choice
      case "$browser_choice" in
        1) run_xiaoe browser use chrome ;;
        2) run_xiaoe browser use edge ;;
        3) run_xiaoe browser use ego ;;
        *) echo "无效选项，配置未改变。" ;;
      esac
      pause_screen
      ;;
    5)
      echo "正在扫描已保存店铺账号下的全部课程..."
      run_xiaoe course scan-account
      pause_screen
      ;;
    6)
      printf "课程地址："
      read -r course_url
      printf "课程名称："
      read -r course_title
      run_xiaoe course add "$course_url" --title "$course_title"
      pause_screen
      ;;
    7)
      run_xiaoe course list
      pause_screen
      ;;
    8)
      course_id="$(select_course)"
      if [[ -n "$course_id" ]]; then
        run_xiaoe course refresh "$course_id"
        scan_status=$?
        if [[ "$scan_status" -eq 0 ]]; then
          echo ""
          printf "是否下载该课程音频？(y/n)："
          read -r download_choice
          if [[ "$download_choice" = "y" || "$download_choice" = "Y" ]]; then
            echo ""
            echo "下载范围（默认全部）："
            echo "  all          — 全部下载"
            echo "  88           — 仅第 88 节"
            echo "  88-93        — 第 88 至 93 节"
            echo "  1,5,10       — 第 1、5、10 节"
            echo "  88-93,1,5   — 混合"
            printf "请输入："
            read -r positions
            echo ""
            if [[ -z "$positions" || "$positions" = "all" ]]; then
              run_xiaoe download "$course_id"
            else
              run_xiaoe download "$course_id" --positions "$positions"
            fi
          fi
        fi
      fi
      pause_screen
      ;;
    9)
      course_id="$(select_course)"
      if [[ -n "$course_id" ]]; then
        echo ""
        echo "下载范围（默认全部）："
        echo "  all / 回车   — 全部下载"
        echo "  88           — 仅第 88 节"
        echo "  88-93        — 第 88 至 93 节"
        echo "  1,5,10       — 第 1、5、10 节"
        echo "  88-93,1,5   — 混合"
        printf "请输入："
        read -r positions
        echo ""
        if [[ -z "$positions" || "$positions" = "all" ]]; then
          run_xiaoe download "$course_id"
        else
          run_xiaoe download "$course_id" --positions "$positions"
        fi
      fi
      pause_screen
      ;;
    10)
      course_id="$(select_course)"
      if [[ -n "$course_id" ]]; then
        echo ""
        echo "转写范围（默认全部）："
        echo "  all / 回车   — 全部转写"
        echo "  88           — 仅第 88 节"
        echo "  88-93        — 第 88 至 93 节"
        echo "  1,5,10       — 第 1、5、10 节"
        echo "  88-93,1,5   — 混合"
        printf "请输入："
        read -r positions
        echo ""
        if [[ -z "$positions" || "$positions" = "all" ]]; then
          run_xiaoe transcribe "$course_id" --language zh
        else
          run_xiaoe transcribe "$course_id" --positions "$positions" --language zh
        fi
      fi
      pause_screen
      ;;
    11)
      course_id="$(select_course)"
      if [[ -n "$course_id" ]]; then
        run_xiaoe run "$course_id" --limit 1 --language zh
      fi
      pause_screen
      ;;
    12)
      course_id="$(select_course)"
      if [[ -n "$course_id" ]]; then
        run_xiaoe run "$course_id" --language zh
      fi
      pause_screen
      ;;
    13)
      run_xiaoe status
      pause_screen
      ;;
    14)
      "$python_bin" "$project_dir/scripts/configure_asr.py"
      pause_screen
      ;;
    15)
      "$project_dir/.local-asr-venv/bin/python" - <<'PY'
import torch
from qwen_asr import Qwen3ASRModel
from pathlib import Path

model = Path.home() / "Library/Application Support/OpenLess/models/qwen3-asr/qwen3-asr-1.7b"
print("模型目录：", model)
print("模型完整：", (model / "model.safetensors.index.json").is_file())
print("PyTorch：", torch.__version__)
print("Apple MPS：", torch.backends.mps.is_available())
print("Qwen 运行时：", Qwen3ASRModel.__name__)
PY
      pause_screen
      ;;
    16)
      exit 0
      ;;
    *)
      echo "无效选项。"
      sleep 1
      ;;
  esac
done
