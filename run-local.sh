#!/usr/bin/env bash
# 本地（非容器）运行入口：需已安装 app/requirements.txt 中的依赖。
set -euo pipefail
cd "$(dirname "$0")/app"

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
  else
    PYTHON_BIN=python3
  fi
fi
exec "$PYTHON_BIN" server.py
