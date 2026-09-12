#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

info() { printf '[Ripple] %s\n' "$*"; }
fail() { printf 'Ripple setup: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail '需要 Python 3.10+。'
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' || fail '需要 Python 3.10+。'
command -v node >/dev/null 2>&1 || fail '需要 Node.js 22.19+。'
command -v npm >/dev/null 2>&1 || fail '需要 npm。'
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=19)?0:1)' || fail '需要 Node.js 22.19+。'

if [ ! -x "$PY" ]; then
  info '创建项目虚拟环境 .venv...'
  python3 -m venv "$VENV"
fi

info '安装 Python 依赖...'
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -e "$ROOT"

info '构建 Web 前端...'
(
  cd "$ROOT/web/frontend"
  npm ci --no-audit --no-fund
  npm run build
)

if [ "${RIPPLE_SKIP_BROWSER_RUNTIME:-0}" != "1" ]; then
  info '安装 Playwright Chromium...'
  "$PY" -m playwright install chromium
fi

if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  info '已创建本地 .env（Git 会忽略该文件）'
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  printf '提示：未检测到 FFmpeg；视频/音频处理功能会受限。\n' >&2
fi

cat <<'EOF'
安装完成。
启动：.venv/bin/python -X utf8 web/app.py

Linux/macOS 私密连接使用 Ripple 的机器本地 AES-256-GCM Secret Store；默认密钥文件为 ~/.config/ripple/secret.key（0600）。
迁移到另一台机器时不要复制密文假定可解密，请在新机器重新连接平台账号。
Linux/macOS 已提供核心安装路径；真实浏览器登录、桌面 Agent、媒体工具和平台连接仍需在目标主机逐项验证。
Ripple 安装器不会全局安装或修改 OpenCode / Claude Code / Codex / Hermes。
EOF
