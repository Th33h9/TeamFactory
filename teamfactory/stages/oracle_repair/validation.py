from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from teamfactory.stages.instance_tuning.validation import (
    file_manifest,
    validate_candidate,
)


IMMUTABLE_VISIBLE_FILES = (
    "environment/start.md",
    "environment/api_manifest.json",
    "instruction.md",
)


def _assert_same_file(original: Path, candidate: Path, relative: str) -> None:
    before = original / relative
    after = candidate / relative
    if before.exists() != after.exists():
        raise ValueError(f"oracle repair may not add or remove {relative}")
    if before.is_file() and before.read_bytes() != after.read_bytes():
        raise ValueError(f"oracle repair may not modify {relative}")


def _existing_test_sources(original: Path) -> set[str]:
    config_path = original / "tests/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    paths = {
        str(value).strip().lstrip("./")
        for value in config.get("test_files", [])
        if str(value).strip()
    }
    reference = original / "tests/reference"
    if reference.is_dir():
        for path in reference.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if name.startswith("test") or name.endswith("_test.py") or name == "conftest.py":
                paths.add(path.relative_to(reference).as_posix())
    return paths


def _is_token_subsequence(before: str, after: str) -> bool:
    try:
        old_tokens = shlex.split(before)
        new_tokens = iter(shlex.split(after))
    except ValueError as exc:
        raise ValueError(f"invalid test command shell syntax: {exc}") from exc
    return all(any(candidate == expected for candidate in new_tokens) for expected in old_tokens)


def _validate_command_extensions(old_commands: list[str], new_commands: list[str]) -> None:
    forbidden = ("reward.txt", "/logs/verifier", "echo ", "printf ", "exit 0", "|| true")
    for command in new_commands:
        lowered = command.lower()
        if any(marker in lowered for marker in forbidden):
            raise ValueError(f"unsafe replacement test command: {command}")
    for old_command in old_commands:
        if not any(
            _is_token_subsequence(old_command, new_command)
            for new_command in new_commands
        ):
            raise ValueError(
                "replacement test_commands may only add tokens to existing commands: "
                f"{old_command}"
            )


def _load_oracle_report(result: dict[str, Any]) -> dict[str, Any]:
    trial_value = result.get("trial_dir")
    if not trial_value:
        raise ValueError("cannot decrease test_case_count without an oracle trial report")
    report_path = Path(str(trial_value)) / "verifier" / "report.json"
    if not report_path.is_file():
        raise ValueError("cannot decrease test_case_count without verifier/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("oracle verifier report must be an object")
    return report


def _validate_count_correction(
    original: Path,
    candidate: Path,
    oracle_result: dict[str, Any],
    trusted_oracle_results: list[dict[str, Any]],
) -> None:
    before = json.loads((original / "tests/config.json").read_text(encoding="utf-8"))
    after = json.loads((candidate / "tests/config.json").read_text(encoding="utf-8"))
    old_count = int(before.get("test_case_count") or 0)
    new_count = int(after.get("test_case_count") or 0)
    if new_count >= old_count:
        return

    reports: list[dict[str, Any]] = []
    for result in [oracle_result, *trusted_oracle_results]:
        try:
            reports.append(_load_oracle_report(result))
        except ValueError:
            continue
    if not reports:
        raise ValueError("cannot decrease test_case_count without an oracle trial report")

    matching = [
        report
        for report in reports
        if int(report.get("observed_total") or 0) == new_count and new_count > 0
    ]
    if not matching:
        raise ValueError(
            "test_case_count may only be corrected to the verifier observed_total"
        )
    if not any(
        int(report.get("passed") or 0) == new_count
        and int(report.get("failed") or 0) == 0
        and int(report.get("errors") or 0) == 0
        for report in matching
    ):
        raise ValueError(
            "test_case_count may decrease only when every observed test already passes"
        )


def validate_oracle_candidate(
    original: Path,
    candidate: Path,
    declared_changes: list[str],
    image_commands: list[str],
    oracle_result: dict[str, Any] | None = None,
    trusted_oracle_results: list[dict[str, Any]] | None = None,
) -> set[str]:
    for relative in IMMUTABLE_VISIBLE_FILES:
        _assert_same_file(original, candidate, relative)

    before_reference = file_manifest(original / "tests/reference")
    after_reference = file_manifest(candidate / "tests/reference")
    for relative in _existing_test_sources(original):
        if relative in before_reference and before_reference.get(relative) != after_reference.get(relative):
            raise ValueError(f"existing oracle test source may not change: {relative}")

    old_config = json.loads((original / "tests/config.json").read_text(encoding="utf-8"))
    new_config = json.loads((candidate / "tests/config.json").read_text(encoding="utf-8"))
    _validate_command_extensions(
        [str(value) for value in old_config.get("test_commands", [])],
        [str(value) for value in new_config.get("test_commands", [])],
    )
    _validate_count_correction(
        original,
        candidate,
        oracle_result or {},
        trusted_oracle_results or [],
    )

    return validate_candidate(
        original,
        candidate,
        declared_changes,
        image_commands,
        allow_test_case_count_decrease=True,
        allow_test_command_replacement=True,
        validate_start_md=False,
    )
