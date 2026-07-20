from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from teamfactory.artifacts import ItemRef
from teamfactory.stages.agent2_stage3.stage import next_stage_after_materialization
from teamfactory.stages.oracle_gate.stage import (
    OracleRepairGateStage,
    build_repair_args,
    safe_run_name,
)
from teamfactory.stages.oracle_repair.contracts import validate_repair_result


class FakeOracleRepairPipeline:
    terminal_status = "oracle_pass"
    responsible_stage = ""

    def __init__(self, args: Namespace, instances: list[Path]) -> None:
        self.args = args
        self.instances = instances
        self.run_dir = Path(args.hyperdistill_root) / "runs" / args.run_name

    def state_path(self, task_id: str) -> Path:
        return self.run_dir / "items" / task_id / "state.json"

    async def run(self) -> None:
        task_id = self.instances[0].name
        state = {
            "task_id": task_id,
            "status": self.terminal_status,
            "responsible_stage": self.responsible_stage,
        }
        path = self.state_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        if self.responsible_stage:
            repair = {
                "schema_version": "teamfactory.oracle_repair.v1",
                "status": "repaired",
                "responsible_stage": self.responsible_stage,
                "root_cause": "fixture inventory",
                "diagnosis": "a required fixture was not copied",
                "evidence": ["verifier log"],
                "changed_files": ["tests/reference/data.json"],
                "image_commands": [],
                "validation_notes": ["canonical recheck"],
            }
            (path.parent / "repair_result.round-1.json").write_text(
                json.dumps(repair) + "\n", encoding="utf-8"
            )


class OracleRepairGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="teamfactory_oracle_gate_"))
        self.task_id = "github-owner-repo__1234567"
        self.dataset_root = self.temp / "dataset"
        (self.dataset_root / self.task_id).mkdir(parents=True)
        self.work_dir = self.temp / "work"
        stage3 = self.work_dir / "items" / self.task_id / "agent2_stage3.json"
        stage3.parent.mkdir(parents=True)
        stage3.write_text(json.dumps({"status": "stage3_passed"}) + "\n")
        self.args = Namespace(
            dataset_root=str(self.dataset_root),
            work_dir=str(self.work_dir),
            run_dir=str(self.temp / "main-runs" / "20260720_120000"),
            hyperdistill_root=str(self.temp / "hyperdistill"),
            oracle_max_repair_rounds=3,
            oracle_infra_retries=2,
            oracle_repair_model="claude-sonnet-4-6-ppio",
            api_key="test-token",
        )
        Path(self.args.run_dir).mkdir(parents=True)
        self.ref = ItemRef(index=0, url="https://github.com/owner/repo", task_id=self.task_id)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def run_gate(self, terminal_status: str, responsible_stage: str = "") -> dict:
        FakeOracleRepairPipeline.terminal_status = terminal_status
        FakeOracleRepairPipeline.responsible_stage = responsible_stage
        with patch(
            "teamfactory.stages.oracle_gate.stage.OracleRepairPipeline",
            FakeOracleRepairPipeline,
        ):
            self.assertEqual(OracleRepairGateStage().run(self.args, self.ref), "")
        output = self.work_dir / "items" / self.task_id / "oracle_repair.json"
        return json.loads(output.read_text(encoding="utf-8"))

    def test_clean_oracle_passes(self) -> None:
        result = self.run_gate("oracle_pass")
        self.assertEqual(result["status"], "oracle_passed")

    def test_repair_records_routed_stage(self) -> None:
        result = self.run_gate("repaired", "stage2_ast")
        self.assertEqual(result["status"], "oracle_repaired")
        self.assertEqual(result["responsible_stage"], "stage2_ast")

    def test_terminal_failure_fails_the_gate(self) -> None:
        result = self.run_gate("repair_rejected", "agent2_stage3")
        self.assertEqual(result["status"], "oracle_failed")
        self.assertEqual(result["oracle_terminal_status"], "repair_rejected")

    def test_repair_args_are_bounded_to_one_instance(self) -> None:
        result = build_repair_args(self.args, self.ref)
        self.assertEqual(result.instance, [self.task_id])
        self.assertEqual(result.oracle_workers, 1)
        self.assertEqual(result.max_repair_rounds, 3)
        self.assertEqual(result.agent2_model, "claude-sonnet-4-6-ppio")

    def test_long_run_name_keeps_a_hash_suffix(self) -> None:
        result = safe_run_name("x" * 500)
        self.assertLessEqual(len(result), 180)
        self.assertRegex(result, r"-[0-9a-f]{10}$")

    def test_stage3_can_disable_oracle_gate_for_debugging(self) -> None:
        self.assertEqual(
            next_stage_after_materialization(Namespace(oracle_repair=True)),
            "oracle_repair",
        )
        self.assertEqual(
            next_stage_after_materialization(Namespace(oracle_repair=False)),
            "",
        )

    def test_legacy_repair_result_infers_the_owning_stage(self) -> None:
        result = validate_repair_result(
            {
                "schema_version": "teamfactory.oracle_repair.v1",
                "status": "repaired",
                "root_cause": "missing fixture inventory",
                "diagnosis": "fixture was absent from the reference bundle",
                "evidence": [],
                "changed_files": ["tests/reference/data.json"],
                "image_commands": [],
                "validation_notes": [],
            }
        )
        self.assertEqual(result.responsible_stage, "stage2_ast")


if __name__ == "__main__":
    unittest.main()
