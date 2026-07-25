#!/bin/zsh
#
# 鹅采 ecai — 一键构建打包脚本
# 用法：双击运行，或终端运行 ./ecai-build.command [版本号]
#

set -e

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="$SCRIPT_DIR/xiaoe-audio-pipeline"

# ---- helpers ----

red()    { printf "\033[31m%s\033[0m" "$*"; }
green()  { printf "\033[32m%s\033[0m" "$*"; }
yellow() { printf "\033[33m%s\033[0m" "$*"; }

pause() {
  printf "\n按回车键退出..."
  read -r
}

# ---- check prerequisites ----

if ! command -v gh &>/dev/null; then
  echo "$(red "✗") 未找到 GitHub CLI (gh)。请先安装：brew install gh"
  pause
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "$(red "✗") gh 未登录。请先运行：gh auth login"
  pause
  exit 1
fi

# ---- project check ----

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "$(red "✗") 找不到项目目录：$PROJECT_DIR"
  pause
  exit 1
fi

cd "$PROJECT_DIR"

if ! git remote get-url origin &>/dev/null; then
  echo "$(red "✗") 项目没有配置 git remote。"
  pause
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "cimu233/ecai")

# ---- version ----

CURRENT_VERSION=$(git tag --sort=-v:refname | grep "^v" | head -1 | sed 's/^v//')
if [[ -z "$CURRENT_VERSION" ]]; then
  CURRENT_VERSION="0.0.0"
fi

if [[ -n "$1" ]]; then
  VERSION="$1"
else
  # auto-bump patch version
  IFS="." read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
  PATCH=$((PATCH + 1))
  VERSION="${MAJOR}.${MINOR}.${PATCH}"
  echo "当前版本: v$CURRENT_VERSION → 新版本: $(green "v$VERSION")"
  printf "确认？(回车确认 / 输入其他版本号): "
  read -r input
  if [[ -n "$input" ]]; then
    VERSION="$input"
  fi
fi

TAG="v${VERSION#v}"

echo ""
echo "========================================"
echo "  鹅采 ecai 构建脚本"
echo "========================================"
echo "  仓库:   $REPO"
echo "  版本:   $(green "$TAG")"
echo "  项目:   $PROJECT_DIR"
echo "========================================"
echo ""

# ---- ensure clean ----

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "$(yellow "!") 有未提交的更改，正在提交..."
  git add -A
  git commit -m "chore: pre-release changes for $TAG" || true
fi

# ---- push current branch ----

BRANCH=$(git branch --show-current)
echo "→ 推送 $BRANCH 分支..."
git -c http.proxy= -c https.proxy= push origin "$BRANCH" 2>/dev/null || true

# ---- tag ----

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "$(yellow "!") 标签 $TAG 已存在，跳过创建。"
else
  echo "→ 创建标签 $TAG..."
  git tag -a "$TAG" -m "Release $TAG"
  echo "→ 推送标签 $TAG..."
  git -c http.proxy= -c https.proxy= push origin "$TAG"
  echo "$(green "✓") 标签 $TAG 已推送，GitHub Actions 开始构建。"
fi

# ---- monitor workflow ----

echo ""
echo "→ 等待 GitHub Actions 启动..."
sleep 5

RUN_ID=$(gh run list --workflow=build.yml --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")

if [[ -z "$RUN_ID" ]]; then
  echo "$(yellow "!") 未找到工作流运行记录，请手动检查："
  echo "   https://github.com/$REPO/actions"
  pause
  exit 0
fi

echo "→ 监控工作流运行: #$RUN_ID"
echo "   https://github.com/$REPO/actions/runs/$RUN_ID"
echo ""

# watch with timeout (30 min)
TIMEOUT=1800
ELAPSED=0
INTERVAL=15

while [[ $ELAPSED -lt $TIMEOUT ]]; do
  STATUS=$(gh run view "$RUN_ID" --json status,conclusion -q '[.status, .conclusion] | @tsv' 2>/dev/null || echo "")
  RUN_STATUS=$(echo "$STATUS" | cut -f1)
  CONCLUSION=$(echo "$STATUS" | cut -f2)

  if [[ "$RUN_STATUS" == "completed" ]]; then
    if [[ "$CONCLUSION" == "success" ]]; then
      echo ""
      echo "$(green "✓") 构建成功！"
      break
    else
      echo ""
      echo "$(red "✗") 构建失败 (conclusion: $CONCLUSION)。"
      echo "   https://github.com/$REPO/actions/runs/$RUN_ID"
      pause
      exit 1
    fi
  fi

  printf "\r  状态: %-12s  已等待: %ds" "$RUN_STATUS" "$ELAPSED"
  sleep "$INTERVAL"
  ELAPSED=$((ELAPSED + INTERVAL))
done

if [[ $ELAPSED -ge $TIMEOUT ]]; then
  echo ""
  echo "$(yellow "!") 等待超时，请手动下载："
  echo "   https://github.com/$REPO/actions/runs/$RUN_ID"
  pause
  exit 0
fi

# ---- download artifact ----

echo ""
echo "→ 下载构建产物..."
DOWNLOAD_DIR="$HOME/Downloads/ecai-builds"
mkdir -p "$DOWNLOAD_DIR"

gh run download "$RUN_ID" --dir "$DOWNLOAD_DIR/$TAG" 2>/dev/null || {
  echo "$(yellow "!") 自动下载失败，请手动下载："
  echo "   https://github.com/$REPO/actions/runs/$RUN_ID"
  pause
  exit 0
}

echo "$(green "✓") 构建产物已下载到: $DOWNLOAD_DIR/$TAG"
ls -lh "$DOWNLOAD_DIR/$TAG"/

echo ""
echo "$(green "✓") 全部完成！"
pause
