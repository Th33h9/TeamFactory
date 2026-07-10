from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any


def prepare_sshpass(pass_file: str) -> None:
    if os.environ.get("SSHPASS"):
        return
    path = Path(pass_file)
    if path.exists():
        os.environ["SSHPASS"] = path.read_text(encoding="utf-8").strip()


def ssh_prefix(args: Any) -> list[str]:
    return [
        "sshpass",
        "-e",
        "ssh",
        "-p",
        str(args.ssh_port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"ConnectTimeout={args.ssh_connect_timeout}",
        f"{args.remote_user}@{args.remote_host}",
    ]


def run_remote(args: Any, script: str, *, timeout: int | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    prepare_sshpass(args.ssh_pass_file)
    return subprocess.run(
        ssh_prefix(args) + [script],
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout if timeout is not None else int(args.item_timeout),
    )


def scp_from_remote(args: Any, remote: str, local: str | Path) -> subprocess.CompletedProcess[str]:
    prepare_sshpass(args.ssh_pass_file)
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            "sshpass",
            "-e",
            "scp",
            "-P",
            str(args.ssh_port),
            "-o",
            "StrictHostKeyChecking=no",
            f"{args.remote_user}@{args.remote_host}:{remote}",
            str(local),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=int(args.item_timeout),
    )


def q(value: str | Path) -> str:
    return shlex.quote(str(value))
