from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


TRIAGE_SCHEMA = "teamfactory.instance_tuning.triage.v1"
REPAIR_SCHEMA = "teamfactory.instance_tuning.repair.v1"
AGENT2_SCHEMA = "teamfactory.instance_tuning.agent2.v2"


class RepairDecision(str, Enum):
    INSTANCE_ERROR = "instance_error"
    AGENT_CAPABILITY = "agent_capability"
    INFRASTRUCTURE_TRANSIENT = "infrastructure_transient"
    INCONCLUSIVE = "inconclusive"


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty model response")
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fenced:
        value = json.loads(fenced.group(1))
        if isinstance(value, dict):
            return value
    start = stripped.find("{")
    if start >= 0:
        value, _ = json.JSONDecoder().raw_decode(stripped[start:])
        if isinstance(value, dict):
            return value
    raise ValueError("model response does not contain a JSON object")


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass(frozen=True)
class TriageResult:
    decision: RepairDecision
    confidence: float
    root_cause: str
    evidence: list[str]
    proposed_changes: list[str]
    image_commands: list[str]
    raw: dict[str, Any]

    @property
    def should_repair(self) -> bool:
        return self.decision is RepairDecision.INSTANCE_ERROR and self.confidence >= 0.70


def validate_triage(value: dict[str, Any]) -> TriageResult:
    if value.get("schema_version") != TRIAGE_SCHEMA:
        raise ValueError(f"invalid triage schema: {value.get('schema_version')!r}")
    try:
        decision = RepairDecision(str(value.get("decision") or ""))
    except ValueError as exc:
        raise ValueError(f"invalid triage decision: {value.get('decision')!r}") from exc
    confidence = float(value.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("triage confidence must be between 0 and 1")
    root_cause = str(value.get("root_cause") or "").strip()
    if not root_cause:
        raise ValueError("triage root_cause is required")
    return TriageResult(
        decision=decision,
        confidence=confidence,
        root_cause=root_cause,
        evidence=_string_list(value.get("evidence", []), "evidence"),
        proposed_changes=_string_list(value.get("proposed_changes", []), "proposed_changes"),
        image_commands=_string_list(value.get("image_commands", []), "image_commands"),
        raw=value,
    )


@dataclass(frozen=True)
class RepairResult:
    status: str
    summary: str
    changed_files: list[str]
    image_commands: list[str]
    validations_requested: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Agent2Result:
    decision: RepairDecision
    confidence: float
    root_cause: str
    evidence: list[str]
    repair: RepairResult
    raw: dict[str, Any]


def validate_repair(value: dict[str, Any]) -> RepairResult:
    if value.get("schema_version") != REPAIR_SCHEMA:
        raise ValueError(f"invalid repair schema: {value.get('schema_version')!r}")
    status = str(value.get("status") or "").strip()
    if status not in {"repaired", "no_safe_repair"}:
        raise ValueError(f"invalid repair status: {status!r}")
    summary = str(value.get("summary") or "").strip()
    if not summary:
        raise ValueError("repair summary is required")
    return RepairResult(
        status=status,
        summary=summary,
        changed_files=_string_list(value.get("changed_files", []), "changed_files"),
        image_commands=_string_list(value.get("image_commands", []), "image_commands"),
        validations_requested=_string_list(
            value.get("validations_requested", []), "validations_requested"
        ),
        raw=value,
    )


def validate_agent2(value: dict[str, Any]) -> Agent2Result:
    if value.get("schema_version") != AGENT2_SCHEMA:
        raise ValueError(f"invalid agent2 schema: {value.get('schema_version')!r}")
    try:
        decision = RepairDecision(str(value.get("decision") or ""))
    except ValueError as exc:
        raise ValueError(f"invalid agent2 decision: {value.get('decision')!r}") from exc
    confidence = float(value.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("agent2 confidence must be between 0 and 1")
    root_cause = str(value.get("root_cause") or "").strip()
    if not root_cause:
        raise ValueError("agent2 root_cause is required")

    repair_value = value.get("repair")
    if not isinstance(repair_value, dict):
        raise ValueError("agent2 repair must be an object")
    repair_payload = {
        "schema_version": REPAIR_SCHEMA,
        **repair_value,
    }
    repair = validate_repair(repair_payload)
    if decision is not RepairDecision.INSTANCE_ERROR:
        if repair.status != "no_safe_repair":
            raise ValueError("non-instance failures may not be repaired")
        if repair.changed_files or repair.image_commands:
            raise ValueError("non-instance failures must not change files or images")
    if confidence < 0.70 and repair.status == "repaired":
        raise ValueError("low-confidence instance failures may not be repaired")
    if repair.status == "repaired" and not repair.changed_files:
        raise ValueError("repaired agent2 result must declare changed_files")
    return Agent2Result(
        decision=decision,
        confidence=confidence,
        root_cause=root_cause,
        evidence=_string_list(value.get("evidence", []), "evidence"),
        repair=repair,
        raw=value,
    )


def reward_value(reward: Any) -> float | None:
    """Extract one scalar reward from Harbor/Hyperdistill reward shapes."""
    if reward is None or isinstance(reward, bool):
        return None
    if isinstance(reward, (int, float)):
        return float(reward)
    if isinstance(reward, dict):
        for key in ("reward", "score", "mean"):
            if key in reward:
                found = reward_value(reward[key])
                if found is not None:
                    return found
        values = [reward_value(item) for item in reward.values()]
        values = [item for item in values if item is not None]
        if len(values) == 1:
            return values[0]
    return None


def is_repair_candidate(result: dict[str, Any]) -> bool:
    score = reward_value(result.get("reward"))
    return score is None or score <= 0.0


def score_improved(initial_score: float | None, final_score: float | None) -> bool:
    if final_score is None:
        return False
    if initial_score is None:
        return final_score > 0.0
    return final_score > initial_score
