from __future__ import annotations

import asyncio
import hashlib
import re
from argparse import Namespace
from pathlib import Path
from typing import Any

from teamfactory.artifacts import ItemRef, read_stage, write_stage
from teamfactory.stages.oracle_repair.stage import OracleRepairPipeline, read_json


ORACLE_GATE_SCHEMA = "teamfactory.oracle_repair_gate.v1"


def safe_run_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
    if len(cleaned) <= 180:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[:169]}-{digest}"


def build_repair_args(args: Any, ref: ItemRef) -> Namespace:
    values = dict(vars(args))
    main_run_id = Path(str(args.run_dir)).name
    values.update(
        {
            "run_name": safe_run_name(
                f"TeamFactory-main-{main_run_id}-oracle-{ref.task_id}"
            ),
            "oracle_workers": 1,
            "repair_workers": 1,
            "finalize_workers": 1,
            "max_repair_rounds": int(args.oracle_max_repair_rounds),
            "oracle_infra_retries": int(args.oracle_infra_retries),
            "instance": [ref.task_id],
            "start_index": 0,
            "limit": 1,
            "no_resume": False,
            "plan_only": False,
            "model": str(args.oracle_repair_model),
            "agent2_model": str(args.oracle_repair_model),
            "agent2_api_key": str(args.api_key),
            "api_key_file": "",
            "token_source": "",
        }
    )
    return Namespace(**values)


def latest_repair_result(run_dir: Path, task_id: str) -> dict[str, Any]:
    def round_number(path: Path) -> int:
        match = re.search(r"round-(\d+)\.json$", path.name)
        return int(match.group(1)) if match else -1

    paths = sorted(
        (run_dir / "items" / task_id).glob("repair_result.round-*.json"),
        key=round_number,
    )
    for path in reversed(paths):
        value = read_json(path, {})
        if isinstance(value, dict):
            return value
    return {}


class OracleRepairGateStage:
    name = "oracle_repair"

    def run(self, args: Any, ref: ItemRef) -> str:
        instance = Path(args.dataset_root) / ref.task_id
        try:
            stage3 = read_stage(args, ref.task_id, "agent2_stage3", {})
            if stage3.get("status") != "stage3_passed":
                raise ValueError(f"Stage3 is not passed: {stage3.get('status')!r}")
            if not instance.is_dir():
                raise ValueError(f"materialized instance is missing: {instance}")
            repair_args = build_repair_args(args, ref)
            pipeline = OracleRepairPipeline(repair_args, [instance])
            asyncio.run(pipeline.run())

            state = read_json(pipeline.state_path(ref.task_id), {}) or {}
            terminal_status = str(state.get("status") or "")
            repair_result = latest_repair_result(pipeline.run_dir, ref.task_id)
            responsible_stage = str(
                repair_result.get("responsible_stage")
                or state.get("responsible_stage")
                or ""
            )
            row = {
                "schema_version": ORACLE_GATE_SCHEMA,
                "status": (
                    "oracle_repaired"
                    if terminal_status == "repaired"
                    else "oracle_passed"
                    if terminal_status == "oracle_pass"
                    else "oracle_failed"
                ),
                "oracle_terminal_status": terminal_status,
                "responsible_stage": responsible_stage,
                "oracle_run_dir": str(pipeline.run_dir),
                "oracle_state": state,
                "repair_result": repair_result,
                "dataset_instance_dir": str(instance),
            }
            write_stage(args, ref, self.name, row)
            return ""
        except Exception as exc:
            write_stage(
                args,
                ref,
                self.name,
                {
                    "schema_version": ORACLE_GATE_SCHEMA,
                    "status": "oracle_error",
                    "dataset_instance_dir": str(instance),
                    "error": repr(exc),
                },
            )
            return ""
