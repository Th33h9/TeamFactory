from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from teamfactory.stages.oracle_repair.contracts import (
    looks_transient,
    oracle_passed,
    validate_repair_result,
)
from teamfactory.stages.oracle_repair.evidence import build_oracle_evidence
from teamfactory.stages.oracle_repair.stage import result_for_repair_round
from teamfactory.stages.oracle_repair.validation import validate_oracle_candidate


START_MD = """## Demo Project Introduction and Goals

## Natural Language Instructions (Prompt)

## Environment Configuration

## Demo Project Architecture

## API Usage Guide

### Core API

#### 1. Demo - behavior

## Usage Example

## Detailed Function Implementation Nodes

### Node 1: Demo behavior
**Function Description**:
Demo behavior.

**Handling Strategy**:
Handle it.

**Input and Output Examples**:
Input maps to output.
"""


class OracleRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="teamfactory_oracle_repair_test_"))
        self.original = self.temp / "original"
        files = {
            "environment/start.md": START_MD,
            "environment/Dockerfile": "FROM python:3.11\n",
            "environment/api_manifest.json": "{}\n",
            "instruction.md": "implement the project\n",
            "task.toml": 'docker_image_archive = "/shared/task.tar"\n',
            "solution/solve.sh": "#!/bin/bash\ncp -a /solution/oracle/. /workspace/\n",
            "solution/oracle/demo.py": "def value(): return 1\n",
            "tests/config.json": json.dumps(
                {
                    "test_case_count": 1,
                    "test_files": ["tests/test_demo.py"],
                    "test_commands": ["pytest -q tests/test_demo.py"],
                }
            )
            + "\n",
            "tests/test.sh": "/tests/reference /workspace /logs/verifier reward.txt\n",
            "tests/reference/tests/test_demo.py": "def test_demo(): assert value() == 1\n",
        }
        for relative, content in files.items():
            path = self.original / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def candidate(self) -> Path:
        candidate = self.temp / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)
        shutil.copytree(self.original, candidate)
        return candidate

    def test_oracle_requires_full_reward(self) -> None:
        self.assertTrue(oracle_passed({"reward": {"reward": 1.0}}))
        self.assertFalse(oracle_passed({"reward": {"reward": 0.99}}))
        self.assertFalse(oracle_passed({"reward": None}))

    def test_connection_refused_is_transient(self) -> None:
        self.assertTrue(looks_transient({"error": "ConnectionRefused"}))
        self.assertFalse(looks_transient({"error": "pytest assertion failed"}))

    def test_contract_accepts_repair(self) -> None:
        result = validate_repair_result(
            {
                "schema_version": "teamfactory.oracle_repair.v1",
                "status": "repaired",
                "root_cause": "fixture packaging",
                "diagnosis": "a required fixture was absent",
                "evidence": ["FileNotFoundError"],
                "changed_files": ["solution/oracle/demo.py"],
                "image_commands": [],
                "validation_notes": ["rerun oracle"],
            }
        )
        self.assertTrue(result.repaired)

    def test_start_md_is_immutable(self) -> None:
        candidate = self.candidate()
        (candidate / "environment/start.md").write_text(
            START_MD + "\nextra\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "may not modify environment/start.md"):
            validate_oracle_candidate(
                self.original,
                candidate,
                ["environment/start.md"],
                [],
            )

    def test_existing_test_source_is_immutable(self) -> None:
        candidate = self.candidate()
        test_path = candidate / "tests/reference/tests/test_demo.py"
        test_path.write_text("def test_demo(): assert True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "existing oracle test source may not change"):
            validate_oracle_candidate(
                self.original,
                candidate,
                ["tests/reference/tests/test_demo.py"],
                [],
            )

    def test_oracle_implementation_can_be_repaired(self) -> None:
        candidate = self.candidate()
        source = candidate / "solution/oracle/demo.py"
        source.write_text("def value(): return 2\n", encoding="utf-8")
        changed = validate_oracle_candidate(
            self.original,
            candidate,
            ["solution/oracle/demo.py"],
            [],
        )
        self.assertEqual(changed, {"solution/oracle/demo.py"})

    def test_test_command_may_only_gain_tokens(self) -> None:
        candidate = self.candidate()
        config_path = candidate / "tests/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["test_commands"] = [
            "pytest --disable-warnings -q tests/test_demo.py"
        ]
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        changed = validate_oracle_candidate(
            self.original,
            candidate,
            ["tests/config.json"],
            [],
        )
        self.assertEqual(changed, {"tests/config.json"})

    def test_test_command_target_cannot_be_replaced(self) -> None:
        candidate = self.candidate()
        config_path = candidate / "tests/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["test_commands"] = ["pytest -q tests/test_other.py"]
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "may only add tokens"):
            validate_oracle_candidate(
                self.original,
                candidate,
                ["tests/config.json"],
                [],
            )

    def test_count_can_match_all_passing_observed_total(self) -> None:
        original_config_path = self.original / "tests/config.json"
        original_config = json.loads(original_config_path.read_text(encoding="utf-8"))
        original_config["test_case_count"] = 2
        original_config_path.write_text(
            json.dumps(original_config) + "\n", encoding="utf-8"
        )
        candidate = self.candidate()
        candidate_config_path = candidate / "tests/config.json"
        candidate_config = json.loads(candidate_config_path.read_text(encoding="utf-8"))
        candidate_config["test_case_count"] = 1
        candidate_config_path.write_text(
            json.dumps(candidate_config) + "\n", encoding="utf-8"
        )
        trial = self.temp / "trial"
        report = trial / "verifier/report.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "observed_total": 1,
                }
            ),
            encoding="utf-8",
        )
        changed = validate_oracle_candidate(
            self.original,
            candidate,
            ["tests/config.json"],
            [],
            {"trial_dir": str(trial)},
        )
        self.assertEqual(changed, {"tests/config.json"})

    def test_count_cannot_hide_observed_failure(self) -> None:
        original_config_path = self.original / "tests/config.json"
        original_config = json.loads(original_config_path.read_text(encoding="utf-8"))
        original_config["test_case_count"] = 2
        original_config_path.write_text(
            json.dumps(original_config) + "\n", encoding="utf-8"
        )
        candidate = self.candidate()
        candidate_config_path = candidate / "tests/config.json"
        candidate_config = json.loads(candidate_config_path.read_text(encoding="utf-8"))
        candidate_config["test_case_count"] = 1
        candidate_config_path.write_text(
            json.dumps(candidate_config) + "\n", encoding="utf-8"
        )
        trial = self.temp / "failing-trial"
        report = trial / "verifier/report.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "passed": 0,
                    "failed": 1,
                    "errors": 0,
                    "observed_total": 1,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "every observed test already passes"):
            validate_oracle_candidate(
                self.original,
                candidate,
                ["tests/config.json"],
                [],
                {"trial_dir": str(trial)},
            )

    def test_count_can_use_all_passing_historical_oracle_report(self) -> None:
        original_config_path = self.original / "tests/config.json"
        original_config = json.loads(original_config_path.read_text(encoding="utf-8"))
        original_config["test_case_count"] = 2
        original_config_path.write_text(
            json.dumps(original_config) + "\n", encoding="utf-8"
        )
        candidate = self.candidate()
        candidate_config_path = candidate / "tests/config.json"
        candidate_config = json.loads(candidate_config_path.read_text(encoding="utf-8"))
        candidate_config["test_case_count"] = 1
        candidate_config_path.write_text(
            json.dumps(candidate_config) + "\n", encoding="utf-8"
        )

        current = self.temp / "current-failing-trial"
        historical = self.temp / "historical-passing-trial"
        for trial, report in (
            (
                current,
                {"passed": 0, "failed": 0, "errors": 2, "observed_total": 2},
            ),
            (
                historical,
                {"passed": 1, "failed": 0, "errors": 0, "observed_total": 1},
            ),
        ):
            report_path = trial / "verifier/report.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")

        changed = validate_oracle_candidate(
            self.original,
            candidate,
            ["tests/config.json"],
            [],
            {"trial_dir": str(current)},
            [{"trial_dir": str(historical)}],
        )

        self.assertEqual(changed, {"tests/config.json"})

    def test_count_cannot_use_failing_historical_oracle_report(self) -> None:
        original_config_path = self.original / "tests/config.json"
        original_config = json.loads(original_config_path.read_text(encoding="utf-8"))
        original_config["test_case_count"] = 2
        original_config_path.write_text(
            json.dumps(original_config) + "\n", encoding="utf-8"
        )
        candidate = self.candidate()
        candidate_config_path = candidate / "tests/config.json"
        candidate_config = json.loads(candidate_config_path.read_text(encoding="utf-8"))
        candidate_config["test_case_count"] = 1
        candidate_config_path.write_text(
            json.dumps(candidate_config) + "\n", encoding="utf-8"
        )

        historical = self.temp / "historical-failing-trial"
        report_path = historical / "verifier/report.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(
            json.dumps(
                {"passed": 0, "failed": 1, "errors": 0, "observed_total": 1}
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "every observed test already passes"):
            validate_oracle_candidate(
                self.original,
                candidate,
                ["tests/config.json"],
                [],
                {},
                [{"trial_dir": str(historical)}],
            )

    def test_preexisting_start_md_format_error_does_not_block_repair(self) -> None:
        start_path = self.original / "environment/start.md"
        start_path.write_text(START_MD + "\n```python\n", encoding="utf-8")
        candidate = self.candidate()
        source = candidate / "solution/oracle/demo.py"
        source.write_text("def value(): return 2\n", encoding="utf-8")

        changed = validate_oracle_candidate(
            self.original,
            candidate,
            ["solution/oracle/demo.py"],
            [],
        )

        self.assertEqual(changed, {"solution/oracle/demo.py"})
        self.assertEqual(
            start_path.read_bytes(),
            (candidate / "environment/start.md").read_bytes(),
        )

    def test_retry_evidence_includes_prior_repair_results(self) -> None:
        prior = self.temp / "repair_result.round-1.json"
        prior.write_text(
            json.dumps({"root_cause": "broken editable install path"}) + "\n",
            encoding="utf-8",
        )
        destination = self.temp / "evidence" / "round-2"

        build_oracle_evidence(
            {"reward": {"reward": 0.5}},
            destination,
            feedback="the first candidate only partially repaired the instance",
            prior_repair_results=[prior],
        )

        copied = destination / "prior_attempts" / prior.name
        self.assertEqual(copied.read_bytes(), prior.read_bytes())
        self.assertIn(
            "partially repaired",
            (destination / "previous_attempt_feedback.txt").read_text(
                encoding="utf-8"
            ),
        )

    def test_resume_uses_previous_recheck_for_next_round(self) -> None:
        item_dir = self.temp / "item"
        result_path = item_dir / "oracle" / "recheck-2" / "result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps({"reward": {"reward": 0.25}, "attempt": "recheck-2"}),
            encoding="utf-8",
        )

        result = result_for_repair_round(item_dir, 3)

        self.assertIsNotNone(result)
        self.assertEqual(result["attempt"], "recheck-2")

    def test_resume_prefers_materialized_round_evidence(self) -> None:
        item_dir = self.temp / "item"
        recheck = item_dir / "oracle" / "recheck-1" / "result.json"
        evidence = item_dir / "evidence" / "round-2" / "oracle_result.json"
        recheck.parent.mkdir(parents=True)
        evidence.parent.mkdir(parents=True)
        recheck.write_text(json.dumps({"source": "recheck"}), encoding="utf-8")
        evidence.write_text(json.dumps({"source": "evidence"}), encoding="utf-8")

        result = result_for_repair_round(item_dir, 2)

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "evidence")

    def test_resume_after_validation_rejection_uses_prior_round_evidence(self) -> None:
        item_dir = self.temp / "item"
        evidence = item_dir / "evidence" / "round-3" / "oracle_result.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            json.dumps({"source": "round-3-validation-input"}),
            encoding="utf-8",
        )

        result = result_for_repair_round(item_dir, 4)

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "round-3-validation-input")


if __name__ == "__main__":
    unittest.main()
