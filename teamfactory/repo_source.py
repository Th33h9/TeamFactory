from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def normalize_repo_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if re.match(r"^[\w.-]+/[\w.-]+$", raw):
        return f"https://github.com/{raw}"
    if raw.startswith("git@github.com:"):
        owner_repo = raw.split(":", 1)[1]
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[:-4]
        return f"https://github.com/{owner_repo}"
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw


def url_from_record(row: dict[str, Any]) -> str:
    for key in ("url", "html_url", "clone_url"):
        if row.get(key):
            return normalize_repo_url(str(row[key]))
    if row.get("full_name"):
        return normalize_repo_url(str(row["full_name"]))
    return ""


def read_repo_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        url = url_from_record(row)
        if url:
            row["url"] = url
            records.append(row)
    return records


def task_id_for_url(url: str) -> str:
    clean = normalize_repo_url(url).rstrip("/")
    match = re.search(r"github\.com/([^/]+)/([^/#?]+)", clean)
    if match:
        owner, repo = match.groups()
        base = f"github-{owner}-{repo}"
    else:
        base = re.sub(r"[^a-zA-Z0-9]+", "-", clean).strip("-")[:80] or "repo"
    base = re.sub(r"[^a-zA-Z0-9._-]+", "-", base).strip("-").lower()
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:7]
    return f"{base}__{digest}"
