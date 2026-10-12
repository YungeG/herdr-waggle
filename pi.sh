#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if command -v uv >/dev/null 2>&1; then
    exec uv run --no-project --python python3 "$ROOT/runtime.py" pi "$@"
fi
if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    printf '%s\n' '需要 Python >=3.11 和已安装的 Pi；请先阅读 DEPLOY.md。' >&2
    exit 127
fi
exec "$PYTHON" "$ROOT/runtime.py" pi "$@"
