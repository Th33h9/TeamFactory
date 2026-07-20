from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


MAX_TEXT_BYTES = 2_000_000
MAX_TRAJECTORY_STEPS = 120


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _copy_tail(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    data = source.read_bytes()
    if len(data) > MAX_TEXT_BYTES:
        data = b"[...truncated to final 2 MB...]\n" + data[-MAX_TEXT_BYTES:]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def trajectory_excerpt(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = {key: item for key, item in value.items() if key != "steps"}
    steps = value.get("steps")
    if isinstance(steps, list) and len(steps) > MAX_TRAJECTORY_STEPS:
        result["steps"] = steps[:10] + [{"truncated": len(steps) - MAX_TRAJECTORY_STEPS}] + steps[-(MAX_TRAJECTORY_STEPS - 10):]
    else:
        result["steps"] = steps
    return result


def build_evidence(evaluation: dict[str, Any], destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    summary = dict(evaluation)
    summary["trajectory"] = trajectory_excerpt(summary.get("trajectory"))
    diff = str(summary.get("diff") or "")
    if len(diff.encode("utf-8")) > MAX_TEXT_BYTES:
        summary["diff"] = diff[-MAX_TEXT_BYTES:]
        summary["diff_truncated"] = True
    _write_json(destination / "evaluation_result.json", summary)

    trial_raw = str(evaluation.get("trial_dir") or "").strip()
    if not trial_raw:
        return
    trial = Path(trial_raw)
    selected = [
        "result.json", "exception.txt", "trial.log",
        "verifier/test-stdout.txt", "verifier/report.json",
        "verifier/reward.txt", "verifier/reward.json",
        "verifier/file_diff.patch", "verifier/file_diff_stat.txt",
        "verifier/changed_files.txt", "agent/trajectory.json",
    ]
    for rel in selected:
        _copy_tail(trial / rel, destination / "trial" / rel)
