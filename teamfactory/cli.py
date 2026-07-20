from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from teamfactory import config
from teamfactory.remote import prepare_sshpass
from teamfactory.scheduler import StreamingPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TeamFactory Agent1 Docker oracle pipeline.")
    parser.add_argument("--repo-jsonl", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--end-index", type=int, default=0)
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--task-id-suffix", default="")

    parser.add_argument("--work-dir", default=config.DEFAULT_WORK_DIR)
    parser.add_argument("--run-dir", default=config.DEFAULT_RUN_DIR)
    parser.add_argument("--output", default=config.DEFAULT_OUTPUT)
    parser.add_argument("--error-output", default=config.DEFAULT_ERRORS)
    parser.add_argument("--trajectory-output", default=config.DEFAULT_TRAJECTORY)
    parser.add_argument("--dataset-root", default=config.DEFAULT_DATASET_ROOT)

    parser.add_argument("--remote-host", default=config.DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-user", default=config.DEFAULT_REMOTE_USER)
    parser.add_argument("--ssh-port", type=int, default=config.DEFAULT_SSH_PORT)
    parser.add_argument("--ssh-pass-file", default=config.DEFAULT_SSH_PASS_FILE)
    parser.add_argument("--ssh-connect-timeout", type=int, default=15)
    parser.add_argument("--remote-work-root", default=config.DEFAULT_REMOTE_WORK_ROOT)
    parser.add_argument("--remote-image-root", default=config.DEFAULT_REMOTE_IMAGE_ROOT)

    parser.add_argument("--claude-bin", default=config.DEFAULT_CLAUDE_BIN)
    parser.add_argument(
        "--model",
        default="",
        help="legacy alias: override both --agent1-model and --agent2-model",
    )
    parser.add_argument("--agent1-model", default=config.DEFAULT_MODEL)
    parser.add_argument("--agent2-model", default=config.DEFAULT_MODEL)
    parser.add_argument("--api-base", default=config.DEFAULT_SIDECAR_API_BASE)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--claude-extra-arg", action="append", default=[])
    parser.add_argument("--claude-api-timeout-ms", type=int, default=1200000)
    parser.add_argument("--claude-max-retries", type=int, default=20)
    parser.add_argument("--claude-stream-idle-timeout-ms", type=int, default=600000)

    parser.add_argument("--tp", type=int, default=1)
    parser.add_argument("--pp", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--agent1-concurrency", type=int, default=0)
    parser.add_argument("--agent2-concurrency", type=int, default=0)
    parser.add_argument("--item-timeout", type=int, default=1800)
    parser.add_argument("--agent-timeout", type=int, default=3600)
    parser.add_argument("--stage2-timeout", type=int, default=900)
    parser.add_argument("--stage3-timeout", type=int, default=1800)
    parser.add_argument("--agent2-coverage-retries", type=int, default=2)
    parser.add_argument("--validate-start-md", action="store_true")

    parser.add_argument(
        "--skip-oracle-repair",
        action="store_false",
        dest="oracle_repair",
        help="stop after Stage3 instead of running the canonical oracle repair gate",
    )
    parser.set_defaults(oracle_repair=True)
    parser.add_argument("--oracle-concurrency", type=int, default=4)
    parser.add_argument("--oracle-max-repair-rounds", type=int, default=3)
    parser.add_argument("--oracle-infra-retries", type=int, default=2)
    parser.add_argument("--oracle-repair-model", default=config.DEFAULT_ORACLE_REPAIR_MODEL)
    parser.add_argument("--hyperdistill-root", default=config.DEFAULT_HYPERDISTILL_ROOT)
    parser.add_argument("--remote-docker-host", default=config.DEFAULT_REMOTE_DOCKER_HOST)
    parser.add_argument("--harbour-python", default=config.DEFAULT_HARBOUR_PYTHON)
    parser.add_argument("--harbour-src-dir", default=config.DEFAULT_HARBOUR_SRC_DIR)
    parser.add_argument("--harbour-timeout", type=int, default=7200)
    parser.add_argument("--harbour-timeout-multiplier", type=float, default=3.0)
    parser.add_argument("--maintenance-timeout", type=int, default=7200)
    parser.add_argument("--transfer-timeout", type=int, default=3600)
    parser.add_argument("--image-rebuild-timeout", type=int, default=3600)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.tp = max(1, int(args.tp))
    args.pp = max(1, int(args.pp))
    args.concurrency = max(1, int(args.concurrency))
    args.agent1_concurrency = max(0, int(args.agent1_concurrency or args.concurrency))
    args.agent2_concurrency = max(0, int(args.agent2_concurrency or args.concurrency))
    args.limit = max(0, int(args.limit))
    args.start_index = max(0, int(args.start_index))
    args.end_index = max(0, int(args.end_index))
    args.task_id_suffix = str(args.task_id_suffix or "").strip()
    if str(args.model or "").strip():
        args.agent1_model = str(args.model).strip()
        args.agent2_model = str(args.model).strip()
    else:
        args.agent1_model = str(args.agent1_model or config.DEFAULT_MODEL).strip()
        args.agent2_model = str(args.agent2_model or config.DEFAULT_MODEL).strip()
    args.item_timeout = max(1, int(args.item_timeout))
    args.agent_timeout = max(1, int(args.agent_timeout))
    args.stage2_timeout = max(1, int(args.stage2_timeout))
    args.stage3_timeout = max(1, int(args.stage3_timeout))
    args.agent2_coverage_retries = max(0, int(args.agent2_coverage_retries))
    args.oracle_concurrency = max(1, int(args.oracle_concurrency))
    args.oracle_max_repair_rounds = max(1, int(args.oracle_max_repair_rounds))
    args.oracle_infra_retries = max(0, int(args.oracle_infra_retries))
    args.harbour_timeout = max(1, int(args.harbour_timeout))
    args.harbour_timeout_multiplier = max(0.1, float(args.harbour_timeout_multiplier))
    args.maintenance_timeout = max(1, int(args.maintenance_timeout))
    args.transfer_timeout = max(1, int(args.transfer_timeout))
    args.image_rebuild_timeout = max(1, int(args.image_rebuild_timeout))
    args.api_key = args.api_key or os.environ.get("TEAMFACTORY_API_KEY", "") or os.environ.get("SIDECAR_API_KEY", "")
    for attr in ("work_dir", "run_dir", "dataset_root"):
        Path(getattr(args, attr)).mkdir(parents=True, exist_ok=True)
    return args


def main(argv: list[str] | None = None) -> int:
    args = normalize_args(build_parser().parse_args(argv))
    prepare_sshpass(args.ssh_pass_file)
    return StreamingPipeline(args).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
