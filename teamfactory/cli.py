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
    parser.add_argument("--model", default=config.DEFAULT_MODEL)
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
    parser.add_argument("--validate-start-md", action="store_true")
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.tp = max(1, int(args.tp))
    args.pp = max(1, int(args.pp))
    args.concurrency = max(1, int(args.concurrency))
    args.agent1_concurrency = max(0, int(args.agent1_concurrency or args.concurrency))
    args.agent2_concurrency = max(0, int(args.agent2_concurrency or args.concurrency))
    args.limit = max(0, int(args.limit))
    args.start_index = max(0, int(args.start_index))
    args.item_timeout = max(1, int(args.item_timeout))
    args.agent_timeout = max(1, int(args.agent_timeout))
    args.stage2_timeout = max(1, int(args.stage2_timeout))
    args.stage3_timeout = max(1, int(args.stage3_timeout))
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
