from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any

from teamfactory.artifacts import ItemRef, write_stage
from teamfactory.providers.remote_claude import RemoteClaudeCodeProvider


AGENT1_SCHEMA = "teamfactory.agent1.v1"
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt.md")


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty agent response")
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    try:
        value, _end = json.JSONDecoder().raw_decode(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fenced:
        value = json.loads(fenced.group(1))
        if isinstance(value, dict):
            return value
    first = stripped.find("{")
    if first >= 0:
        value, _end = json.JSONDecoder().raw_decode(stripped[first:])
        if isinstance(value, dict):
            return value
    raise ValueError("could not parse JSON object from agent response")


def require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def validate_agent1_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload = require_dict(payload, "agent1_payload")
    status = str(payload.get("status") or "").strip()
    if status not in {"agent1_passed", "agent1_failed"}:
        raise ValueError(f"invalid Agent1 status: {status!r}")
    env_spec = require_dict(payload.get("env_spec"), "env_spec")
    oracle_report = require_dict(payload.get("oracle_report"), "oracle_report")
    docker = require_dict(payload.get("docker"), "docker")
    commands = require_dict(payload.get("commands"), "commands")
    install_commands = [str(item).strip() for item in require_list(env_spec.get("install_commands"), "env_spec.install_commands") if str(item).strip()]
    test_commands = [str(item).strip() for item in require_list(env_spec.get("test_commands"), "env_spec.test_commands") if str(item).strip()]
    if not test_commands:
        raise ValueError("env_spec.test_commands must be non-empty")
    if not isinstance(oracle_report.get("ok"), bool):
        raise ValueError("oracle_report.ok must be boolean")
    env_spec["install_commands"] = install_commands
    env_spec["test_commands"] = test_commands
    for key in ("package_files", "test_files", "fixture_files", "env_notes"):
        value = env_spec.get(key, [])
        env_spec[key] = [str(item).strip() for item in value] if isinstance(value, list) else []
    return {
        "schema_version": AGENT1_SCHEMA,
        "status": status,
        "env_spec": env_spec,
        "oracle_report": oracle_report,
        "docker": docker,
        "commands": commands,
        "notes": str(payload.get("notes") or ""),
    }


class Agent1Stage:
    name = "agent1"

    def run(self, args: Any, ref: ItemRef) -> str:
        remote_task_dir = f"{str(args.remote_work_root).rstrip('/')}/{ref.task_id}"
        provider = RemoteClaudeCodeProvider(args)
        try:
            prompt = self.build_prompt(args, ref, remote_task_dir)
            turn = provider.run(prompt, task_id=ref.task_id, phase=self.name, cwd=remote_task_dir)
            payload = extract_json(str(turn.get("final_response") or ""))
            row = validate_agent1_payload(payload)
            row["remote_task_dir"] = remote_task_dir
            row["agent_turn"] = {
                "record_type": turn.get("record_type"),
                "duration_ms": turn.get("duration_ms"),
                "returncode": turn.get("returncode"),
                "model": turn.get("model"),
            }
            write_stage(args, ref, self.name, row)
            return "stage2_ast" if row["status"] == "agent1_passed" else ""
        except Exception as exc:
            row = {
                "schema_version": AGENT1_SCHEMA,
                "status": "agent1_error",
                "remote_task_dir": remote_task_dir,
                "error": repr(exc),
            }
            write_stage(args, ref, self.name, row)
            return ""

    def build_prompt(self, args: Any, ref: ItemRef, remote_task_dir: str) -> str:
        image_name = f"teamfactory-agent1-{ref.task_id}".lower().replace("_", "-")
        container_name = f"{image_name}-ctr"
        return Template(PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")).safe_substitute(
            repo_url=ref.url,
            remote_task_dir=remote_task_dir,
            image_name=image_name,
            container_name=container_name,
        )
