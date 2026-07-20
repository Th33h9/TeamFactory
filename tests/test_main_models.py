from __future__ import annotations

import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from teamfactory.cli import build_parser, normalize_args
from teamfactory.providers.remote_claude import model_for_phase


class MainPipelineModelSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="teamfactory_models_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def parse(self, *extra: str) -> Namespace:
        return normalize_args(
            build_parser().parse_args(
                [
                    "--repo-jsonl",
                    "repos.jsonl",
                    "--work-dir",
                    str(self.temp / "work"),
                    "--run-dir",
                    str(self.temp / "runs"),
                    "--dataset-root",
                    str(self.temp / "dataset"),
                    *extra,
                ]
            )
        )

    def test_three_stages_can_select_independent_models(self) -> None:
        args = self.parse(
            "--agent1-model",
            "model-a1",
            "--agent2-model",
            "model-a2",
            "--oracle-repair-model",
            "model-oracle",
        )
        self.assertEqual(model_for_phase(args, "agent1"), "model-a1")
        self.assertEqual(model_for_phase(args, "agent2_stage3"), "model-a2")
        self.assertEqual(args.oracle_repair_model, "model-oracle")

    def test_legacy_model_overrides_generation_models_only(self) -> None:
        args = self.parse(
            "--model",
            "shared-generation-model",
            "--agent1-model",
            "ignored-a1",
            "--agent2-model",
            "ignored-a2",
            "--oracle-repair-model",
            "oracle-model",
        )
        self.assertEqual(args.agent1_model, "shared-generation-model")
        self.assertEqual(args.agent2_model, "shared-generation-model")
        self.assertEqual(args.oracle_repair_model, "oracle-model")


if __name__ == "__main__":
    unittest.main()
