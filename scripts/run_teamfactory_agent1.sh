#!/usr/bin/env bash
set -euo pipefail

ROOT="/volume/pt-coder/users/kka/TeamFactory"
UV_PY="/volume/pt-coder/users/kka/uv-python/cpython-3.10.12-linux-x86_64-gnu/bin/python3.10"

if [[ -z "${SSHPASS:-}" && -f "/volume/pt-coder/users/kka/instancehelper/.sshpass" ]]; then
  export SSHPASS="$(tr -d '\n' < /volume/pt-coder/users/kka/instancehelper/.sshpass)"
fi

if [[ -x "$UV_PY" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$UV_PY}"
else
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
fi

export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

exec "$PYTHON_BIN" -m teamfactory "$@"
