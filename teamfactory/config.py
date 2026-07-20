from __future__ import annotations

from pathlib import Path


ROOT = Path("/volume/pt-coder/users/kka/TeamFactory")

DEFAULT_REMOTE_HOST = "10.161.41.53"
DEFAULT_REMOTE_USER = "root"
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_PASS_FILE = "/volume/pt-coder/users/kka/instancehelper/.sshpass"

DEFAULT_WORK_DIR = str(ROOT / ".work")
DEFAULT_RUN_DIR = str(ROOT / ".runs")
DEFAULT_OUTPUT = str(ROOT / "teamfactory_agent1_results.jsonl")
DEFAULT_ERRORS = str(ROOT / "teamfactory_agent1_errors.jsonl")
DEFAULT_TRAJECTORY = str(ROOT / "teamfactory_agent1_trajectory.jsonl")
DEFAULT_DATASET_ROOT = "/volume/pt-coder/users/kka/harbor/datasets/TeamFactory"

DEFAULT_REMOTE_WORK_ROOT = "/tmp/kka_TeamFactory_agent1"
DEFAULT_REMOTE_IMAGE_ROOT = "/shared/users/kka/TeamFactory_images"
DEFAULT_CLAUDE_BIN = "/shared/users/kka/human-intelligence/tb-harbor-taskgen/cc-binary/claude-2.1.169-linux-x64"
DEFAULT_SIDECAR_API_BASE = "http://llm-sidecar.iquest-inner.com:8000"
DEFAULT_MODEL = "gpt-5.4-ppio"

DEFAULT_HYPERDISTILL_ROOT = "/volume/pt-coder/users/kka/Hyperdistill"
DEFAULT_REMOTE_DOCKER_HOST = "tcp://10.161.41.53:60001"
DEFAULT_HARBOUR_PYTHON = "/volume/pt-coder/users/kka/harbor/.venv/bin/python"
DEFAULT_HARBOUR_SRC_DIR = "/volume/pt-coder/users/kka/harbor/src"
DEFAULT_ORACLE_REPAIR_MODEL = "claude-sonnet-4-6-ppio"
