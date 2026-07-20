from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def _copy_if_file(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def build_oracle_evidence(
    result: dict[str, Any],
    destination: Path,
    *,
    feedback: str = "",
    prior_repair_results: list[Path] | None = None,
) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    (destination / "oracle_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    if feedback:
        (destination / "previous_attempt_feedback.txt").write_text(
            feedback.rstrip() + "\n", encoding="utf-8"
        )
    for source in prior_repair_results or []:
        _copy_if_file(source, destination / "prior_attempts" / source.name)

    trial_value = result.get("trial_dir")
    if trial_value:
        trial = Path(str(trial_value))
        for relative in (
            "result.json",
            "agent/oracle.txt",
            "agent/exit-code.txt",
            "verifier/test-stdout.txt",
            "verifier/report.json",
            "verifier/reward.txt",
            "verifier/stdout.txt",
            "verifier/stderr.txt",
        ):
            _copy_if_file(trial / relative, destination / "trial" / relative)
    return destination
