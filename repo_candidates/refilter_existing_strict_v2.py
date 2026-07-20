#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
from collections import Counter
from pathlib import Path

import generate_strict_v2_candidates as gen


def main() -> int:
    base = Path(__file__).resolve().parent
    out = base / "github_nl2repo_like_strict_v2_existing_archive_refiltered_20260713.jsonl"
    summary = out.with_suffix(".summary.json")
    blocked = gen.nl2repo_names()
    old_files = [
        p
        for p in sorted(base.glob("*.jsonl")) + sorted(base.glob(".*.jsonl"))
        if "strict_v2" not in p.name
    ]
    metas: dict[str, dict] = {}
    for path in old_files:
        for row in gen.load_jsonl(path):
            key = gen.canonical_key(str(row.get("url") or row.get("html_url") or row.get("full_name") or ""))
            if not key or key in metas or gen.repo_name_from_key(key) in blocked:
                continue
            src = row.get("source_candidate") or {}
            metas[key] = {
                **src,
                "full_name": row.get("full_name") or src.get("full_name") or key,
                "html_url": f"https://github.com/{key}",
                "default_branch": src.get("default_branch") or "main",
                "category_hint": None,
            }

    print(f"[existing] metas={len(metas)}", flush=True)
    rows: dict[str, dict] = {}
    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(gen.inspect_archive, meta): key for key, meta in metas.items()}
        for future in concurrent.futures.as_completed(futures):
            checked += 1
            try:
                record = future.result()
            except Exception:
                record = None
            if record:
                rows[gen.canonical_key(record["url"])] = record
            if checked % 50 == 0:
                print(f"[existing] checked={checked} accepted={len(rows)}", flush=True)

    with out.open("w", encoding="utf-8") as f:
        for record in sorted(rows.values(), key=lambda r: r["full_name"]):
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    summary.write_text(
        json.dumps(
            {
                "count": len(rows),
                "output": str(out),
                "source_files": [str(p) for p in old_files],
                "category_counts": dict(Counter(r["primary_category"] for r in rows.values())),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[existing] done accepted={len(rows)} output={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
