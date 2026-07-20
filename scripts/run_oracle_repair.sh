#!/usr/bin/env bash
set -euo pipefail

ROOT="/volume/pt-coder/users/kka/TeamFactory"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi
if [[ -z "${SSHPASS:-}" && -f /volume/pt-coder/users/kka/instancehelper/.sshpass ]]; then
  export SSHPASS="$(tr -d '\n' < /volume/pt-coder/users/kka/instancehelper/.sshpass)"
fi

export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

exec "${PYTHON_BIN}" -m teamfactory.stages.oracle_repair.stage \
  --dataset-root /volume/pt-coder/users/kka/harbor/datasets/TeamFactory0713 \
  --oracle-workers 10 \
  --repair-workers 10 \
  --model claude-sonnet-4-6-ppio \
  --finalize-workers 4 \
  "$@"
