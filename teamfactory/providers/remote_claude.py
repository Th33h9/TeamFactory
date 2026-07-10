from __future__ import annotations

import json
import time
from typing import Any

from teamfactory.artifacts import append_jsonl
from teamfactory.remote import prepare_sshpass, q, ssh_prefix

import subprocess


def strip_v1(api_base: str) -> str:
    base = api_base.rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def final_text_from_stdout(stdout: str) -> tuple[str, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    final_parts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        value = event.get("result") or event.get("content") or event.get("message")
        if event.get("type") in {"result", "assistant"}:
            if isinstance(value, str):
                final_parts.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        final_parts.append(item["text"])
            elif isinstance(value, dict):
                content = value.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            final_parts.append(item["text"])
    return ("\n".join(final_parts).strip() if final_parts else stdout.strip()), events


class RemoteClaudeCodeProvider:
    def __init__(self, args: Any) -> None:
        self.args = args

    def sidecar_token(self) -> str:
        token = str(self.args.api_key or "").strip()
        if not token:
            raise RuntimeError("missing --api-key for remote Claude Code sidecar")
        return token

    def run(self, prompt: str, *, task_id: str, phase: str, cwd: str) -> dict[str, Any]:
        api_base = strip_v1(str(self.args.api_base))
        token = self.sidecar_token()
        claude_bin = str(self.args.claude_bin)
        cmd = [
            q(claude_bin),
            "--print",
            "--bare",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
            "--add-dir",
            q(cwd),
            "--tools",
            q("Bash,Read,Write"),
            "--model",
            q(self.args.model),
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        for extra in self.args.claude_extra_arg or []:
            cmd.append(q(str(extra)))
        script = f"""
set -euo pipefail
mkdir -p {q(cwd)}
cd {q(cwd)}
export ANTHROPIC_BASE_URL={q(api_base)}
export ANTHROPIC_API_KEY={q(token)}
export ANTHROPIC_AUTH_TOKEN={q(token)}
export ANTHROPIC_DEFAULT_OPUS_MODEL={q(self.args.model)}
export ANTHROPIC_DEFAULT_SONNET_MODEL={q(self.args.model)}
export ANTHROPIC_DEFAULT_HAIKU_MODEL={q(self.args.model)}
export ANTHROPIC_CUSTOM_MODEL_OPTION={q(self.args.model)}
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME={q(f"{self.args.model} via sidecar")}
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export API_TIMEOUT_MS={q(str(self.args.claude_api_timeout_ms))}
export CLAUDE_CODE_MAX_RETRIES={q(str(self.args.claude_max_retries))}
export CLAUDE_STREAM_IDLE_TIMEOUT_MS={q(str(self.args.claude_stream_idle_timeout_ms))}
export CLAUDE_ENABLE_STREAM_WATCHDOG=1
export IS_SANDBOX=1
{" ".join(cmd)}
"""
        started = time.time()
        append_jsonl(
            self.args.trajectory_output,
            {
                "record_type": "remote_claude_started",
                "phase": phase,
                "task_id": task_id,
                "model": self.args.model,
                "cwd": cwd,
                "remote_host": self.args.remote_host,
                "claude_bin": claude_bin,
                "timestamp": int(started),
            },
        )
        prepare_sshpass(self.args.ssh_pass_file)
        proc = subprocess.run(
            ssh_prefix(self.args) + [script],
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(self.args.agent_timeout),
        )
        final_response, events = final_text_from_stdout(proc.stdout)
        for index, event in enumerate(events):
            append_jsonl(
                self.args.trajectory_output,
                {
                    "record_type": "remote_claude_event",
                    "phase": phase,
                    "task_id": task_id,
                    "item_index": index,
                    "item": event,
                    "timestamp": int(time.time()),
                },
            )
        row = {
            "record_type": "remote_claude_summary",
            "phase": phase,
            "task_id": task_id,
            "model": self.args.model,
            "cwd": cwd,
            "status": "completed" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "duration_ms": int((time.time() - started) * 1000),
            "stdout_tail": proc.stdout[-8000:],
            "stderr_tail": proc.stderr[-4000:],
            "final_response": final_response,
            "timestamp": int(time.time()),
        }
        append_jsonl(self.args.trajectory_output, row)
        if proc.returncode != 0:
            raise RuntimeError(f"remote Claude failed rc={proc.returncode}: {proc.stderr[-1000:] or proc.stdout[-1000:]}")
        return row
