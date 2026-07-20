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

exec "${PYTHON_BIN}" -m teamfactory.stages.instance_tuning.stage \
  --dataset-root /volume/pt-coder/users/kka/harbor/datasets/TeamFactory0713 \
  --agent1-workers 16 \
  --agent1-model claude-sonnet-4-6-ppio \
  --agent2-workers 16 \
  --agent2-model claude-opus-4-8-ppio \
  --finalize-workers 4 \
  "$@"
