#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if command -v uv >/dev/null 2>&1; then
    exec uv run --no-project --python python3 "$ROOT/app.py" "$@"
fi
if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    printf '%s\n' '需要 Python >=3.11；请先阅读 DEPLOY.md。' >&2
    exit 127
fi
if ! "$PYTHON" -c 'import textual' >/dev/null 2>&1; then
    printf '%s\n' '缺少 Textual。请在解包目录创建 .venv 并安装包内 requirements.txt，步骤见 DEPLOY.md。' >&2
    exit 1
fi
exec "$PYTHON" "$ROOT/app.py" "$@"
