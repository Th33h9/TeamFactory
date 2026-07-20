from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from teamfactory.stages.instance_tuning.contracts import (
    reward_value,
    score_improved,
    validate_agent2,
)
from teamfactory.stages.instance_tuning.remote_agent import (
    bundle_file_index,
    normalize_maintenance_tools,
)
from teamfactory.stages.instance_tuning.stage import discover_instances
from teamfactory.stages.instance_tuning.validation import validate_candidate


class InstanceTuningValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="teamfactory_tuning_test_"))
        self.original = self.temp / "original"
        for rel, content in {
            "environment/start.md": """## Demo Project Introduction and Goals

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
""",
            "environment/Dockerfile": "FROM alpine\n",
            "environment/api_manifest.json": "{}\n",
            "instruction.md": "build it\n",
            "task.toml": 'docker_image_archive = "/shared/task.tar"\n',
            "tests/config.json": json.dumps({
                "test_case_count": 1,
                "test_files": ["tests/test_one.py"],
                "test_commands": ["pytest -q tests/test_one.py"],
            }) + "\n",
            "tests/test.sh": "/tests/reference /workspace /logs/verifier reward.txt\n",
            "tests/reference/tests/test_one.py": "def test_one(): assert 1 == 1\n",
            "solution/oracle/tests/test_one.py": "def test_one(): assert 1 == 1\n",
            "solution/solve.sh": "#!/bin/bash\n",
        }.items():
            path = self.original / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def candidate(self) -> Path:
        path = self.temp / "candidate"
        if path.exists():
            shutil.rmtree(path)
        shutil.copytree(self.original, path)
        return path

    def test_reward_shapes(self) -> None:
        self.assertEqual(reward_value({"reward": 0.5}), 0.5)
        self.assertEqual(reward_value({"score": 0}), 0.0)
        self.assertIsNone(reward_value(None))

    def test_score_improvement_requires_a_positive_score_after_no_reward(self) -> None:
        self.assertFalse(score_improved(None, None))
        self.assertFalse(score_improved(None, 0.0))
        self.assertTrue(score_improved(None, 0.1))
        self.assertFalse(score_improved(0.5, 0.5))
        self.assertTrue(score_improved(0.5, 0.6))

    def test_maintenance_tools_match_remote_binary(self) -> None:
        self.assertEqual(normalize_maintenance_tools("Read,Glob,Grep"), "Read,Bash")
        self.assertEqual(
            normalize_maintenance_tools("Read,Write,Edit,Glob,Grep"),
            "Read,Edit,Bash",
        )

    def test_bundle_index_exposes_exact_paths(self) -> None:
        evidence = self.temp / "evidence"
        evidence.mkdir()
        (evidence / "evaluation_result.json").write_text("{}\n", encoding="utf-8")
        index = bundle_file_index(self.original, evidence)
        self.assertIn("instance/environment/start.md", index)
        self.assertIn("evidence/evaluation_result.json", index)
        self.assertNotIn("/testbed", index)

    def test_start_md_structure_may_not_change(self) -> None:
        candidate = self.candidate()
        path = candidate / "environment/start.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "## API Usage Guide", "## Renamed API Guide"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "top-level section order"):
            validate_candidate(
                self.original, candidate, ["environment/start.md"], []
            )

    def test_start_md_content_can_change_when_format_is_preserved(self) -> None:
        candidate = self.candidate()
        path = candidate / "environment/start.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("Demo behavior.", "Corrected behavior."),
            encoding="utf-8",
        )
        changed = validate_candidate(
            self.original, candidate, ["environment/start.md"], []
        )
        self.assertEqual(changed, {"environment/start.md"})

    def test_metadata_only_repair_is_allowed(self) -> None:
        candidate = self.candidate()
        (candidate / "instruction.md").write_text("build it carefully\n", encoding="utf-8")
        changed = validate_candidate(
            self.original, candidate, ["instruction.md"], []
        )
        self.assertEqual(changed, {"instruction.md"})

    def test_remote_instance_prefix_is_normalized(self) -> None:
        candidate = self.candidate()
        instruction = candidate / "instruction.md"
        instruction.write_text("build it carefully\n", encoding="utf-8")
        changed = validate_candidate(
            self.original,
            candidate,
            ["instance/instruction.md"],
            [],
        )
        self.assertEqual(changed, {"instruction.md"})

    def test_agent2_may_edit_non_whitelisted_metadata(self) -> None:
        candidate = self.candidate()
        manifest = candidate / "environment/api_manifest.json"
        manifest.write_text('{"covered": true}\n', encoding="utf-8")
        changed = validate_candidate(
            self.original,
            candidate,
            ["environment/api_manifest.json"],
            [],
        )
        self.assertEqual(changed, {"environment/api_manifest.json"})

    def test_discovery_ignores_transaction_directories(self) -> None:
        dataset = self.temp / "dataset"
        valid = dataset / "github-example-project__1234567"
        backup = dataset / ".github-example-project__1234567.tuning-backup-run"
        shutil.copytree(self.original, valid)
        shutil.copytree(self.original, backup)
        args = Namespace(
            dataset_root=str(dataset),
            instance=[],
            start_index=0,
            limit=0,
        )
        self.assertEqual(discover_instances(args), [valid])

    def test_reference_test_coverage_cannot_decrease(self) -> None:
        candidate = self.candidate()
        path = candidate / "tests/reference/tests/test_one.py"
        path.write_text("def helper(): return True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "coverage may not decrease"):
            validate_candidate(
                self.original, candidate,
                ["tests/reference/tests/test_one.py"], [],
            )

    def test_existing_test_command_cannot_be_replaced(self) -> None:
        candidate = self.candidate()
        config_path = candidate / "tests/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["test_commands"] = ["python -c 'print(1)'"]
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "test_commands may not be removed"):
            validate_candidate(
                self.original, candidate, ["tests/config.json"], []
            )

    def test_unconditional_passing_assertion_is_rejected(self) -> None:
        candidate = self.candidate()
        path = candidate / "tests/reference/tests/test_one.py"
        path.write_text("def test_one(): assert True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unconditional passing assertions"):
            validate_candidate(
                self.original, candidate,
                ["tests/reference/tests/test_one.py"], [],
            )

    def test_agent_visible_oracle_path_leak_is_rejected(self) -> None:
        candidate = self.candidate()
        instruction = candidate / "instruction.md"
        instruction.write_text("read /solution/oracle for the answer\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "leak marker"):
            validate_candidate(
                self.original, candidate, ["instruction.md"], []
            )

    def test_agent2_contract_rejects_non_instance_repairs(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-instance failures"):
            validate_agent2({
                "schema_version": "teamfactory.instance_tuning.agent2.v2",
                "decision": "agent_capability",
                "confidence": 0.9,
                "root_cause": "Agent1 missed a documented branch.",
                "evidence": ["trajectory"],
                "repair": {
                    "status": "repaired",
                    "summary": "changed task",
                    "changed_files": ["instruction.md"],
                    "image_commands": [],
                    "validations_requested": [],
                },
            })

    def test_image_command_must_be_durable_in_dockerfile(self) -> None:
        candidate = self.candidate()
        (candidate / "environment/Dockerfile").write_text(
            "FROM alpine\nRUN apk add --no-cache bash\n", encoding="utf-8"
        )
        changed = validate_candidate(
            self.original,
            candidate,
            ["environment/Dockerfile"],
            ["apk add --no-cache bash"],
        )
        self.assertEqual(changed, {"environment/Dockerfile"})


if __name__ == "__main__":
    unittest.main()
