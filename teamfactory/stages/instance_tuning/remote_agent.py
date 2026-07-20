from __future__ import annotations

import json
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

from teamfactory.remote import prepare_sshpass, q, run_remote, ssh_prefix

from .contracts import extract_json_object


_JSONL_LOCK = threading.Lock()
_SUPPORTED_MAINTENANCE_TOOLS = ("Read", "Edit", "Bash")


def normalize_maintenance_tools(tools: str) -> str:
    """Map the requested tool set to the tools shipped by Claude Code 2.1.169."""
    requested = {
        token
        for chunk in tools.replace(",", " ").split()
        if (token := chunk.strip())
    }
    effective = requested & set(_SUPPORTED_MAINTENANCE_TOOLS)
    if requested & {"Glob", "Grep"}:
        effective.add("Bash")
    if "Write" in requested:
        effective.update({"Edit", "Bash"})
    if not effective:
        raise ValueError(f"maintenance tool request has no supported tools: {tools!r}")
    return ",".join(tool for tool in _SUPPORTED_MAINTENANCE_TOOLS if tool in effective)


def bundle_file_index(instance: Path, evidence: Path) -> str:
    """Describe every uploaded file so a Read-only agent never has to guess paths."""
    lines = [
        "# Uploaded Bundle File Index",
        "",
        "All paths below are relative to the bundle directory containing this file.",
        "Read exact files from this list; do not try to Read a directory.",
        "",
    ]
    for heading, root, prefix in (
        ("Instance files", instance, "instance"),
        ("Evaluation evidence files", evidence, "evidence"),
    ):
        lines.extend((f"## {heading}", ""))
        files = sorted(path for path in root.rglob("*") if path.is_file())
        if not files:
            lines.append("(none)")
        else:
            for path in files:
                relative = path.relative_to(root).as_posix()
                lines.append(f"- {prefix}/{relative} ({path.stat().st_size} bytes)")
        lines.append("")
    return "\n".join(lines)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with _JSONL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)


def _final_text(stdout: str) -> tuple[str, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    final: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            final.append(event["result"])
    return ("\n".join(final).strip() if final else stdout.strip()), events


class RemoteMaintenanceAgent:
    """Run maintenance Claude Code turns on the configured remote host."""

    def __init__(self, args: Any, trajectory_path: Path) -> None:
        self.args = args
        self.trajectory_path = trajectory_path

    def upload_bundle(self, source: Path, remote_dir: str) -> None:
        archive = source.parent / f"{source.name}.upload.tar.gz"
        with tarfile.open(archive, "w:gz", dereference=False) as tar:
            tar.add(source, arcname="bundle", recursive=True)
        prepare_sshpass(self.args.ssh_pass_file)
        remote_archive = f"{remote_dir}.upload.tar.gz"
        try:
            prep = run_remote(
                self.args,
                f"rm -rf {q(remote_dir)} {q(remote_archive)}; mkdir -p {q(Path(remote_dir).parent)}",
                timeout=self.args.transfer_timeout,
            )
            if prep.returncode != 0:
                raise RuntimeError(f"remote bundle cleanup failed: {prep.stdout[-2000:]}")
            proc = subprocess.run(
                [
                    "sshpass", "-e", "scp", "-P", str(self.args.ssh_port),
                    "-o", "StrictHostKeyChecking=no", str(archive),
                    f"{self.args.remote_user}@{self.args.remote_host}:{remote_archive}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.args.transfer_timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"bundle upload failed: {proc.stdout[-2000:]}")
            unpack = run_remote(
                self.args,
                f"mkdir -p {q(remote_dir)}; tar -xzf {q(remote_archive)} -C {q(Path(remote_dir).parent)}; rm -f {q(remote_archive)}",
                timeout=self.args.transfer_timeout,
            )
            if unpack.returncode != 0:
                raise RuntimeError(f"remote bundle extract failed: {unpack.stdout[-2000:]}")
        finally:
            archive.unlink(missing_ok=True)

    def upload_instance_bundle(
        self,
        instance: Path,
        evidence: Path,
        remote_dir: str,
        scratch_dir: Path,
    ) -> None:
        """Stream an instance and evidence without first copying the instance locally."""
        scratch_dir.mkdir(parents=True, exist_ok=True)
        archive = scratch_dir / "bundle.upload.tar.gz"
        index = scratch_dir / "bundle_file_index.txt"
        index.write_text(bundle_file_index(instance, evidence), encoding="utf-8")
        with tarfile.open(archive, "w:gz", dereference=False) as tar:
            tar.add(instance, arcname="bundle/instance", recursive=True)
            tar.add(evidence, arcname="bundle/evidence", recursive=True)
            tar.add(index, arcname="bundle/FILE_INDEX.txt", recursive=False)
        prepare_sshpass(self.args.ssh_pass_file)
        remote_archive = f"{remote_dir}.upload.tar.gz"
        try:
            prep = run_remote(
                self.args,
                f"rm -rf {q(remote_dir)} {q(remote_archive)}; mkdir -p {q(Path(remote_dir).parent)}",
                timeout=self.args.transfer_timeout,
            )
            if prep.returncode != 0:
                raise RuntimeError(f"remote bundle cleanup failed: {prep.stdout[-2000:]}")
            proc = subprocess.run(
                [
                    "sshpass", "-e", "scp", "-P", str(self.args.ssh_port),
                    "-o", "StrictHostKeyChecking=no", str(archive),
                    f"{self.args.remote_user}@{self.args.remote_host}:{remote_archive}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.args.transfer_timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"bundle upload failed: {proc.stdout[-2000:]}")
            unpack = run_remote(
                self.args,
                f"mkdir -p {q(Path(remote_dir).parent)}; tar -xzf {q(remote_archive)} -C {q(Path(remote_dir).parent)}; rm -f {q(remote_archive)}",
                timeout=self.args.transfer_timeout,
            )
            if unpack.returncode != 0:
                raise RuntimeError(f"remote bundle extract failed: {unpack.stdout[-2000:]}")
        finally:
            archive.unlink(missing_ok=True)
            index.unlink(missing_ok=True)

    def download_bundle(self, remote_dir: str, destination: Path) -> None:
        prepare_sshpass(self.args.ssh_pass_file)
        remote_archive = f"{remote_dir}.download.tar.gz"
        pack = run_remote(
            self.args,
            f"rm -f {q(remote_archive)}; tar -C {q(Path(remote_dir).parent)} -czf {q(remote_archive)} {q(Path(remote_dir).name)}",
            timeout=self.args.transfer_timeout,
        )
        if pack.returncode != 0:
            raise RuntimeError(f"remote bundle pack failed: {pack.stdout[-2000:]}")
        local_archive = destination.parent / f"{destination.name}.download.tar.gz"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [
                    "sshpass", "-e", "scp", "-P", str(self.args.ssh_port),
                    "-o", "StrictHostKeyChecking=no",
                    f"{self.args.remote_user}@{self.args.remote_host}:{remote_archive}",
                    str(local_archive),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.args.transfer_timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"bundle download failed: {proc.stdout[-2000:]}")
            if destination.exists():
                import shutil
                shutil.rmtree(destination)
            destination.mkdir(parents=True)
            with tarfile.open(local_archive, "r:gz") as tar:
                root = destination.resolve()
                for member in tar.getmembers():
                    target = (destination / member.name).resolve()
                    if root not in target.parents and target != root:
                        raise RuntimeError(f"unsafe downloaded member: {member.name}")
                tar.extractall(destination)
        finally:
            local_archive.unlink(missing_ok=True)
            run_remote(self.args, f"rm -f {q(remote_archive)}", timeout=60)

    def run_json_turn(
        self,
        prompt: str,
        *,
        task_id: str,
        phase: str,
        remote_cwd: str,
        tools: str,
    ) -> dict[str, Any]:
        token = str(self.args.agent2_api_key).strip()
        if not token:
            raise RuntimeError("missing sidecar API key")
        effective_tools = normalize_maintenance_tools(tools)
        command = [
            q(self.args.claude_bin),
            "--print", "--bare", "--no-session-persistence",
            "--permission-mode", "bypassPermissions",
            "--add-dir", q(remote_cwd),
            "--disable-slash-commands",
            "--tools", q(effective_tools),
            "--model", q(self.args.agent2_model),
            "--input-format", "text",
            "--output-format", "stream-json",
            "--verbose",
        ]
        script = f"""
set -euo pipefail
cd {q(remote_cwd)}
export ANTHROPIC_BASE_URL={q(self.args.api_base.rstrip('/'))}
export ANTHROPIC_API_KEY={q(token)}
export ANTHROPIC_AUTH_TOKEN={q(token)}
export ANTHROPIC_DEFAULT_OPUS_MODEL={q(self.args.agent2_model)}
export ANTHROPIC_DEFAULT_SONNET_MODEL={q(self.args.agent2_model)}
export ANTHROPIC_DEFAULT_HAIKU_MODEL={q(self.args.agent2_model)}
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export API_TIMEOUT_MS={q(str(self.args.claude_api_timeout_ms))}
export CLAUDE_CODE_MAX_RETRIES={q(str(self.args.claude_max_retries))}
export CLAUDE_STREAM_IDLE_TIMEOUT_MS={q(str(self.args.claude_stream_idle_timeout_ms))}
export CLAUDE_ENABLE_STREAM_WATCHDOG=1
export IS_SANDBOX=1
{" ".join(command)}
"""
        started = time.time()
        _append_jsonl(self.trajectory_path, {
            "record_type": "maintenance_agent_started",
            "task_id": task_id,
            "phase": phase,
            "model": self.args.agent2_model,
            "remote_cwd": remote_cwd,
            "requested_tools": tools,
            "effective_tools": effective_tools,
            "started_at": started,
        })
        prepare_sshpass(self.args.ssh_pass_file)
        proc = subprocess.run(
            ssh_prefix(self.args) + [script],
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.args.maintenance_timeout,
        )
        final, events = _final_text(proc.stdout)
        for index, event in enumerate(events):
            _append_jsonl(self.trajectory_path, {
                "record_type": "maintenance_agent_event",
                "task_id": task_id,
                "phase": phase,
                "event_index": index,
                "event": event,
            })
        summary = {
            "record_type": "maintenance_agent_summary",
            "task_id": task_id,
            "phase": phase,
            "model": self.args.agent2_model,
            "requested_tools": tools,
            "effective_tools": effective_tools,
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - started, 3),
            "stderr_tail": proc.stderr[-4000:],
            "final_response": final,
        }
        _append_jsonl(self.trajectory_path, summary)
        if proc.returncode != 0:
            raise RuntimeError(
                f"remote maintenance Claude failed rc={proc.returncode}: "
                f"{proc.stderr[-1200:] or proc.stdout[-1200:]}"
            )
        return extract_json_object(final)

    def cleanup_remote(self, remote_dir: str) -> None:
        run_remote(
            self.args,
            f"rm -rf -- {q(remote_dir)} {q(remote_dir + '.upload.tar.gz')} {q(remote_dir + '.download.tar.gz')}",
            timeout=120,
        )
