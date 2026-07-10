from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ItemRef:
    index: int
    url: str
    task_id: str
    lane: int = -1


def now_ts() -> int:
    return int(time.time())


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def write_json(path: str | Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def item_dir(args: Any, task_id: str) -> Path:
    return ensure_dir(Path(args.work_dir) / "items" / task_id)


def stage_path(args: Any, task_id: str, stage: str) -> Path:
    return item_dir(args, task_id) / f"{stage}.json"


def write_stage(args: Any, ref: ItemRef, stage: str, row: dict[str, Any]) -> dict[str, Any]:
    enriched = {
        "stage": stage,
        "index": ref.index,
        "lane": ref.lane,
        "url": ref.url,
        "task_id": ref.task_id,
        "updated_at": now_ts(),
        **row,
    }
    write_json(stage_path(args, ref.task_id, stage), enriched)
    append_jsonl(Path(args.run_dir) / "stage_events.jsonl", enriched)
    return enriched


def read_stage(args: Any, task_id: str, stage: str, default: Any = None) -> Any:
    return read_json(stage_path(args, task_id, stage), default)
