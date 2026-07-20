from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "teamfactory.oracle_repair.v1"
RESPONSIBLE_STAGES = {"agent1", "stage2_ast", "agent2_stage3"}


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return [str(item).strip() for item in value if str(item).strip()]


def infer_responsible_stage(root_cause: str, changed_files: list[str]) -> str:
    explicit_paths = [str(path).lstrip("./") for path in changed_files]
    if any(path == "environment/Dockerfile" for path in explicit_paths):
        return "agent1"
    if any(
        path == "tests/config.json" or path.startswith("tests/reference/")
        for path in explicit_paths
    ):
        return "stage2_ast"
    lowered = root_cause.lower()
    if any(
        marker in lowered
        for marker in ("dependency", "docker", "image", "runtime", "system package")
    ):
        return "agent1"
    if any(
        marker in lowered
        for marker in ("test count", "test discovery", "fixture", "inventory", "ast")
    ):
        return "stage2_ast"
    return "agent2_stage3"


@dataclass(frozen=True)
class OracleRepairResult:
    status: str
    responsible_stage: str
    root_cause: str
    diagnosis: str
    evidence: list[str]
    changed_files: list[str]
    image_commands: list[str]
    validation_notes: list[str]
    raw: dict[str, Any]

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"


def validate_repair_result(value: dict[str, Any]) -> OracleRepairResult:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid oracle repair schema: {value.get('schema_version')!r}")
    status = str(value.get("status") or "").strip()
    if status not in {"repaired", "unrepairable"}:
        raise ValueError(f"invalid oracle repair status: {status!r}")
    root_cause = str(value.get("root_cause") or "").strip()
    diagnosis = str(value.get("diagnosis") or "").strip()
    if not root_cause or not diagnosis:
        raise ValueError("root_cause and diagnosis are required")
    changed_files = _strings(value.get("changed_files", []), "changed_files")
    image_commands = _strings(value.get("image_commands", []), "image_commands")
    if status == "repaired" and not changed_files:
        raise ValueError("a repaired result must declare changed_files")
    if status == "unrepairable" and (changed_files or image_commands):
        raise ValueError("an unrepairable result must not leave changes")
    responsible_stage = str(value.get("responsible_stage") or "").strip()
    if not responsible_stage:
        # Keep old on-disk repair results resumable. New repair prompts always
        # request an explicit owner.
        responsible_stage = infer_responsible_stage(root_cause, changed_files)
    if responsible_stage not in RESPONSIBLE_STAGES:
        raise ValueError(
            "responsible_stage must be one of agent1, stage2_ast, agent2_stage3"
        )
    return OracleRepairResult(
        status=status,
        responsible_stage=responsible_stage,
        root_cause=root_cause,
        diagnosis=diagnosis,
        evidence=_strings(value.get("evidence", []), "evidence"),
        changed_files=changed_files,
        image_commands=image_commands,
        validation_notes=_strings(value.get("validation_notes", []), "validation_notes"),
        raw=value,
    )


def oracle_score(result: dict[str, Any]) -> float | None:
    from teamfactory.stages.instance_tuning.contracts import reward_value

    return reward_value(result.get("reward"))


def oracle_passed(result: dict[str, Any]) -> bool:
    score = oracle_score(result)
    return score is not None and score >= 0.999999


def looks_transient(result: dict[str, Any]) -> bool:
    text = str(result).lower()
    markers = (
        "connection refused",
        "connectionrefused",
        "connection reset",
        "temporarily unavailable",
        "ssh connection",
        "no space left on device",
        "docker daemon is unavailable",
        "context deadline exceeded",
    )
    return any(marker in text for marker in markers)
