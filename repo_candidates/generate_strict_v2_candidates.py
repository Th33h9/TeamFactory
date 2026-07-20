#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
USER_AGENT = "TeamFactoryCandidateStrictV2"
TARGET_CATEGORIES = [
    "web_development",
    "testing",
    "utility_libraries",
    "machine_learning",
    "data_analysis_processing",
    "database_interaction",
    "networking_tools",
    "batch_file_processing",
    "system_tools",
]

PACKAGE_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "tox.ini",
}

SKIP_DIR_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
    "htmlcov",
    "site-packages",
    ".eggs",
    "docs/_build",
}

EXTERNAL_SERVICE_PATTERNS = [
    r"\bselenium\b",
    r"\bplaywright\b",
    r"\bwebdriver\b",
    r"\bchromedriver\b",
    r"\bgeckodriver\b",
    r"\bchromium\b",
    r"\bxvfb\b",
    r"\bpyqt\b",
    r"\bpyside\b",
    r"\btkinter\b",
    r"\bpostgres(?:ql)?\b",
    r"\bmysql\b",
    r"\bmariadb\b",
    r"\bmongodb\b",
    r"\bredis\b",
    r"\belasticsearch\b",
    r"\bminio\b",
    r"\bkafka\b",
    r"\brabbitmq\b",
    r"\bcelery\b",
    r"\bdocker[-_ ]compose\b",
    r"\bboto3\b",
    r"\bgoogle\.cloud\b",
    r"\bopenai_api_key\b",
    r"\banthropic_api_key\b",
    r"\bapi[_ -]?key\b",
]
EXTERNAL_SERVICE_RE = re.compile("|".join(EXTERNAL_SERVICE_PATTERNS), re.I)

CATEGORY_TERMS = {
    "web_development": [
        "flask",
        "django",
        "fastapi",
        "starlette",
        "wsgi",
        "asgi",
        "http",
        "web",
        "jinja",
        "openapi",
    ],
    "testing": [
        "pytest",
        "testing",
        "test",
        "fixture",
        "mock",
        "snapshot",
        "coverage",
        "assert",
        "plugin",
    ],
    "utility_libraries": [
        "utility",
        "utils",
        "helpers",
        "toolkit",
        "cli",
        "config",
        "validator",
        "parser",
        "formatter",
    ],
    "machine_learning": [
        "machine-learning",
        "ml",
        "sklearn",
        "scikit",
        "numpy",
        "torch",
        "tensorflow",
        "model",
        "dataset",
        "feature",
    ],
    "data_analysis_processing": [
        "data",
        "pandas",
        "csv",
        "excel",
        "json",
        "xml",
        "yaml",
        "etl",
        "transform",
        "analysis",
    ],
    "database_interaction": [
        "sqlite",
        "sql",
        "database",
        "db",
        "orm",
        "query",
        "migration",
        "schema",
    ],
    "networking_tools": [
        "network",
        "protocol",
        "http",
        "dns",
        "socket",
        "tcp",
        "udp",
        "packet",
        "url",
        "ip",
    ],
    "batch_file_processing": [
        "batch",
        "file",
        "filesystem",
        "path",
        "rename",
        "convert",
        "archive",
        "markdown",
        "pdf",
        "image",
    ],
    "system_tools": [
        "system",
        "cli",
        "shell",
        "process",
        "logging",
        "config",
        "path",
        "terminal",
        "monitor",
        "command",
    ],
}

LABEL_TERMS = {
    "parser": ["parser", "parse", "lexer", "grammar", "syntax"],
    "validator": ["validator", "validate", "schema", "check"],
    "cli": ["cli", "command", "terminal", "__main__.py"],
    "config": ["config", "toml", "yaml", "ini", "settings"],
    "format": ["json", "yaml", "xml", "toml", "csv", "markdown", "html", "pdf"],
    "pytest": ["pytest", "fixture", "test_"],
    "database": ["sqlite", "sql", "orm", "database", "query"],
    "network": ["http", "url", "dns", "socket", "protocol"],
    "filesystem": ["file", "path", "directory", "archive"],
    "ml": ["sklearn", "numpy", "torch", "tensorflow", "model", "dataset"],
    "web": ["flask", "django", "fastapi", "wsgi", "asgi", "jinja", "openapi"],
}


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def canonical_key(url_or_full_name: str) -> str:
    value = (url_or_full_name or "").strip().removesuffix(".git")
    if not value:
        return ""
    if value.startswith("http"):
        parsed = urllib.parse.urlparse(value)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}".lower()
    if "/" in value:
        owner, repo = value.split("/", 1)
        return f"{owner}/{repo}".lower()
    return value.lower()


def repo_name_from_key(key: str) -> str:
    return key.split("/", 1)[1].lower() if "/" in key else key.lower()


def task_id_for_key(key: str) -> str:
    owner, repo = key.split("/", 1)
    slug_owner = re.sub(r"[^A-Za-z0-9_.-]+", "-", owner).strip("-_.")
    slug_repo = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo).strip("-_.")
    digest = hashlib.sha1(f"https://github.com/{key}".encode()).hexdigest()[:7]
    return f"github-{slug_owner}-{slug_repo}__{digest}"


def nl2repo_names() -> set[str]:
    names: set[str] = set()
    roots = [
        Path("/volume/pt-coder/users/kka/harbor/adapters/nl2repobench/source_data"),
        Path("/volume/pt-coder/users/kka/harbor-eval/datasets/nl2repobench-oracle-to-1"),
        Path("/volume/pt-coder/users/kka/harbor/datasets/nl2repobench"),
    ]
    for root in roots:
        if root.exists():
            for p in root.iterdir():
                if p.is_dir():
                    names.add(p.name.lower())
                    names.add(p.name.lower().replace("_", "-"))
                    names.add(p.name.lower().replace("-", "_"))
    return names


def read_existing_strict(seed_paths: list[Path], blocked_names: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in seed_paths:
        for row in load_jsonl(path):
            url = row.get("url") or row.get("html_url")
            key = canonical_key(str(url or row.get("full_name") or ""))
            if not key or repo_name_from_key(key) in blocked_names:
                continue
            inv = row.get("inventory") or {}
            loc = inv.get("python_loc_estimate")
            py_files = int(inv.get("python_files") or 0)
            test_files = row.get("test_files") or []
            source_files = inv.get("source_python_files")
            if source_files is None:
                source_files = max(0, py_files - len(test_files))
            tc = int(row.get("test_case_count") or 0)
            if not (isinstance(loc, int) and 2000 <= loc <= 8000):
                continue
            if int(source_files or 0) < 5 or tc <= 0 or not test_files:
                continue
            if row.get("static_inferred_buildable") is not True:
                continue
            if row.get("static_inferred_no_network_api_gui_service") is not True:
                continue
            normalized = normalize_record(
                key=key,
                source_candidate=row.get("source_candidate") or {},
                inventory={
                    **inv,
                    "source_python_files": int(source_files or 0),
                    "agent_target_py_files": int(source_files or 0),
                },
                test_files=list(test_files),
                test_case_count=tc,
                package_files=list(row.get("package_files") or []),
                category_hint=None,
                final_source="existing_candidate_refiltered_strict_v2",
                verification_level=str(row.get("verification_level") or "static_refiltered_no_build_no_oracle"),
            )
            out[key] = normalized
    return out


def github_request_json(url: str, token: str = "", timeout: int = 30) -> tuple[dict[str, Any], dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
        return payload, {k: v for k, v in response.headers.items()}


def sleep_for_search_rate(headers: dict[str, str]) -> None:
    remaining = int(headers.get("X-RateLimit-Remaining") or 1)
    reset = int(headers.get("X-RateLimit-Reset") or 0)
    if remaining > 1:
        return
    wait = max(5, reset - int(time.time()) + 2)
    print(f"[rate] GitHub search remaining={remaining}; sleeping {wait}s", flush=True)
    time.sleep(wait)


def search_queries() -> list[tuple[str, str]]:
    rng = random.Random(20260713)
    date_buckets = [
        "pushed:>=2026-01-01",
        "pushed:2025-01-01..2025-12-31",
        "pushed:2024-01-01..2024-12-31",
        "pushed:2023-07-13..2023-12-31",
    ]
    size_buckets = ["size:80..1200", "size:1201..3500", "size:3501..9000"]
    star_buckets = ["stars:10..80", "stars:81..500", "stars:>500"]
    queries: list[tuple[str, str]] = []
    for category, terms in CATEGORY_TERMS.items():
        for term in terms:
            for date in date_buckets:
                for size in size_buckets:
                    stars = rng.choice(star_buckets)
                    q = f"language:Python fork:false archived:false {stars} {date} {size} pytest {term} in:readme"
                    queries.append((category, q))
    rng.shuffle(queries)
    return queries


def discover_metadata(
    target_pool: int,
    token: str,
    blocked_keys: set[str],
    log_path: Path,
    checkpoint_path: Path,
    search_enabled: bool = True,
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if checkpoint_path.exists():
        with checkpoint_path.open(encoding="utf-8") as checkpoint:
            for line in checkpoint:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                key = canonical_key(item.get("full_name") or item.get("html_url") or "")
                if key and key not in blocked_keys:
                    rows[key] = item

    queries = search_queries()
    completed_pages: set[tuple[str, str, int]] = set()
    terminal_queries: set[tuple[str, str]] = set()
    if log_path.exists():
        with log_path.open(encoding="utf-8") as previous_log:
            for line in previous_log:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                category = event.get("category")
                query = event.get("query")
                page = event.get("page")
                if not isinstance(category, str) or not isinstance(query, str) or not isinstance(page, int):
                    continue
                completed_pages.add((category, query, page))
                if event.get("terminal") is True:
                    terminal_queries.add((category, query))

    if rows:
        print(f"[search] resumed metadata checkpoint={len(rows)}", flush=True)
    if not search_enabled:
        print(f"[search] discovery disabled; using checkpoint metadata={len(rows)}", flush=True)
        return list(rows.values())

    log_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log, checkpoint_path.open("a", encoding="utf-8") as checkpoint:
        for qi, (category, query) in enumerate(queries, 1):
            if len(rows) >= target_pool:
                break
            if (category, query) in terminal_queries:
                continue
            for page in range(1, 11):
                if len(rows) >= target_pool:
                    break
                if (category, query, page) in completed_pages:
                    continue
                params = {
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": "100",
                    "page": str(page),
                }
                url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(params)
                try:
                    payload, headers = github_request_json(url, token=token, timeout=35)
                except urllib.error.HTTPError as exc:
                    if exc.code in {403, 429}:
                        reset = int(exc.headers.get("X-RateLimit-Reset") or 0)
                        wait = max(30, reset - int(time.time()) + 3)
                        print(f"[search] rate/forbidden {exc.code}; sleeping {wait}s", flush=True)
                        time.sleep(wait)
                        continue
                    print(f"[search] HTTP {exc.code} query={query}", flush=True)
                    break
                except Exception as exc:
                    print(f"[search] error {type(exc).__name__}: {exc}", flush=True)
                    break
                items = payload.get("items") if isinstance(payload, dict) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    key = canonical_key(item.get("full_name") or item.get("html_url") or "")
                    if not key or key in blocked_keys or key in rows:
                        continue
                    rows[key] = {
                        "category_hint": category,
                        "full_name": item.get("full_name"),
                        "html_url": item.get("html_url"),
                        "description": item.get("description"),
                        "stars": item.get("stargazers_count"),
                        "forks": item.get("forks_count"),
                        "open_issues": item.get("open_issues_count"),
                        "language": item.get("language"),
                        "license": (item.get("license") or {}).get("spdx_id") if isinstance(item.get("license"), dict) else None,
                        "default_branch": item.get("default_branch") or "main",
                        "size_kb": item.get("size"),
                        "updated_at": item.get("updated_at"),
                        "pushed_at": item.get("pushed_at"),
                        "topics": item.get("topics") or [],
                        "query": query,
                    }
                    checkpoint.write(json.dumps(rows[key], ensure_ascii=False, sort_keys=True) + "\n")
                checkpoint.flush()
                terminal = len(items) < 100
                log.write(json.dumps({"query_index": qi, "page": page, "category": category, "query": query, "items": len(items), "pool": len(rows), "terminal": terminal}, sort_keys=True) + "\n")
                log.flush()
                print(f"[search] {len(rows)}/{target_pool} pool after q{qi}/{len(queries)} p{page}", flush=True)
                sleep_for_search_rate(headers)
                if terminal:
                    break
    return list(rows.values())


def safe_relpath(name: str) -> str:
    parts = name.split("/", 1)
    return parts[1] if len(parts) == 2 else name


def should_skip_path(path: str) -> bool:
    parts = path.split("/")
    joined = "/".join(parts)
    return any(part in SKIP_DIR_PARTS for part in parts) or any(skip in joined for skip in SKIP_DIR_PARTS if "/" in skip)


def is_test_file(path: str) -> bool:
    parts = path.lower().split("/")
    base = parts[-1]
    return base.startswith("test_") and base.endswith(".py") or base.endswith("_test.py") or "tests" in parts or "test" in parts


def count_source_loc(text: str) -> int:
    count = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def inspect_archive(meta: dict[str, Any], timeout: int = 45, max_bytes: int = 35_000_000) -> dict[str, Any] | None:
    full_name = str(meta.get("full_name") or "")
    key = canonical_key(full_name)
    if not key:
        return None
    owner, repo = key.split("/", 1)
    branch = str(meta.get("default_branch") or "main")
    branch_enc = urllib.parse.quote(branch, safe="")
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{branch_enc}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            clen = response.headers.get("Content-Length")
            if clen and int(clen) > max_bytes:
                return None
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
    except Exception:
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None

    paths: list[str] = []
    py_files: list[str] = []
    source_py_files: list[str] = []
    test_files: list[str] = []
    package_files: list[str] = []
    loc = 0
    test_case_count = 0
    scan_text_parts: list[str] = []
    total_uncompressed = 0
    py_total_bytes = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = safe_relpath(info.filename)
        if not rel or should_skip_path(rel):
            continue
        paths.append(rel)
        total_uncompressed += int(info.file_size or 0)
        base = rel.rsplit("/", 1)[-1]
        if base in PACKAGE_FILES:
            package_files.append(rel)
            if info.file_size <= 300_000:
                try:
                    scan_text_parts.append(zf.read(info).decode("utf-8", "ignore")[:120_000])
                except Exception:
                    pass
        lower = rel.lower()
        if lower.endswith((".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml")) and info.file_size <= 250_000 and len(scan_text_parts) < 40:
            try:
                scan_text_parts.append(zf.read(info).decode("utf-8", "ignore")[:80_000])
            except Exception:
                pass
        if not lower.endswith(".py"):
            continue
        py_files.append(rel)
        py_total_bytes += int(info.file_size or 0)
        if info.file_size > 1_000_000:
            continue
        try:
            text = zf.read(info).decode("utf-8", "ignore")
        except Exception:
            continue
        if is_test_file(rel):
            test_files.append(rel)
            test_case_count += len(re.findall(r"(?m)^\s*(?:async\s+def|def)\s+test_[A-Za-z0-9_]*\s*\(", text))
            test_case_count += len(re.findall(r"(?m)^\s*class\s+Test[A-Za-z0-9_]*\s*[:(]", text))
        else:
            if not lower.startswith(("docs/", "doc/", "examples/", "example/", "scripts/")) and base not in {"setup.py", "conftest.py"}:
                source_py_files.append(rel)
                loc += count_source_loc(text)
    if not package_files:
        return None
    if not test_files or test_case_count <= 0:
        return None
    if len(source_py_files) < 5:
        return None
    if not (2000 <= loc <= 8000):
        return None
    scan_blob = "\n".join(scan_text_parts + paths[:500]).lower()
    if EXTERNAL_SERVICE_RE.search(scan_blob):
        # Allow sqlite-only database projects, but reject service DBs.
        if not ("sqlite" in scan_blob and not re.search(r"\b(postgres|mysql|mongodb|redis|elasticsearch|minio)\b", scan_blob)):
            return None

    inventory = {
        "file_count": len(paths),
        "total_size": total_uncompressed,
        "metadata_size_kb": meta.get("size_kb"),
        "python_files": len(py_files),
        "source_python_files": len(source_py_files),
        "agent_target_py_files": len(source_py_files),
        "test_python_files": len(test_files),
        "python_loc_estimate": loc,
        "python_bytes": py_total_bytes,
        "tree_sample": paths[:100],
    }
    return normalize_record(
        key=key,
        source_candidate=meta,
        inventory=inventory,
        test_files=test_files[:120],
        test_case_count=test_case_count,
        package_files=package_files[:40],
        category_hint=meta.get("category_hint"),
        final_source="github_search_archive_static_strict_v2",
        verification_level="github_archive_static_inspection_no_clone_no_build_no_oracle",
    )


def classify_category(text: str, category_hint: str | None) -> tuple[str, dict[str, int]]:
    lower = text.lower()
    scores: dict[str, int] = {}
    for category, terms in CATEGORY_TERMS.items():
        score = 0
        for term in terms:
            if term.lower() in lower:
                score += 1
        if category_hint == category:
            score += 3
        scores[category] = score
    best = max(TARGET_CATEGORIES, key=lambda c: (scores.get(c, 0), -TARGET_CATEGORIES.index(c)))
    if scores.get(best, 0) <= 0:
        best = "utility_libraries"
    return best, scores


def labels_for_text(text: str) -> list[str]:
    lower = text.lower()
    labels = ["python-library"]
    for label, terms in LABEL_TERMS.items():
        if any(term in lower for term in terms):
            labels.append(label)
    return sorted(set(labels))


def normalize_record(
    *,
    key: str,
    source_candidate: dict[str, Any],
    inventory: dict[str, Any],
    test_files: list[str],
    test_case_count: int,
    package_files: list[str],
    category_hint: str | None,
    final_source: str,
    verification_level: str,
) -> dict[str, Any]:
    url = f"https://github.com/{key}"
    text = " ".join(
        [
            key,
            str(source_candidate.get("description") or ""),
            " ".join(source_candidate.get("topics") or []),
            " ".join(inventory.get("tree_sample") or []),
            " ".join(test_files[:40]),
            " ".join(package_files),
        ]
    )
    category, category_scores = classify_category(text, category_hint)
    labels = labels_for_text(text)
    return {
        "url": url,
        "html_url": url,
        "full_name": key,
        "task_id": task_id_for_key(key),
        "status": "passed_static_prefilter",
        "final_source": final_source,
        "verification_level": verification_level,
        "rule_version": "strict_v2_2000loc_5py_9categories",
        "primary_category": category,
        "category_scores": category_scores,
        "labels": labels,
        "source_candidate": source_candidate,
        "inventory": inventory,
        "package_files": package_files,
        "pytest_signal": True,
        "test_files": test_files,
        "test_case_count": int(test_case_count),
        "test_case_count_estimation": "static_count_def_test_and_class_Test_from_archive",
        "static_inferred_cloneable": True,
        "clone_checked": False,
        "static_inferred_buildable": True,
        "build_install_checked": False,
        "static_inferred_no_network_api_gui_service": True,
        "dryrun_oracle_checked": False,
        "oracle_network_none_checked": False,
        "hard_rules_static": {
            "is_python_project": True,
            "test_files_non_empty": bool(test_files),
            "test_case_count_gt_0": int(test_case_count) > 0,
            "agent_target_py_files_gte_5": int(inventory.get("agent_target_py_files") or 0) >= 5,
            "python_loc_2000_8000": 2000 <= int(inventory.get("python_loc_estimate") or 0) <= 8000,
            "has_package_manifest": bool(package_files),
            "not_nl2repobench_repo_name": True,
        },
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=2000)
    parser.add_argument("--pool-target", type=int, default=10000)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--inspect-all", action="store_true")
    args = parser.parse_args()

    stamp = now_stamp()
    output = args.output or ROOT / f"github_nl2repo_like_strict_v2_2000_{stamp}.jsonl"
    summary_path = output.with_suffix(".summary.json")
    progress_path = output.with_suffix(".progress.jsonl")
    search_log_path = output.with_suffix(".search.jsonl")
    metadata_checkpoint_path = output.with_suffix(".metadata.jsonl")

    blocked_names = nl2repo_names()
    seed_paths = sorted(ROOT.glob("*.jsonl")) + sorted(ROOT.glob(".*.jsonl"))
    accepted: dict[str, dict[str, Any]] = {}
    if not args.no_seed:
        accepted.update(read_existing_strict(seed_paths, blocked_names))
    preserved_output_keys: list[str] = []
    if args.inspect_all and output.exists():
        with output.open(encoding="utf-8") as current_output:
            for line in current_output:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                key = canonical_key(row.get("url") or row.get("html_url") or row.get("full_name") or "")
                if key in accepted and key not in preserved_output_keys:
                    accepted[key] = row
                    preserved_output_keys.append(key)
    print(f"[seed] accepted={len(accepted)} from existing files", flush=True)
    if preserved_output_keys:
        print(f"[seed] preserved exact output rows={len(preserved_output_keys)}", flush=True)

    blocked_keys = set(accepted)
    metadata = discover_metadata(
        max(args.pool_target, args.target * 4),
        args.token,
        blocked_keys,
        search_log_path,
        metadata_checkpoint_path,
        search_enabled=not args.skip_discovery,
    )
    print(f"[discover] metadata={len(metadata)}", flush=True)

    random.Random(20260713).shuffle(metadata)
    checked = 0
    rejected = 0
    max_workers = max(1, args.workers)
    inflight_limit = max_workers * 4
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        metadata_iter = iter(metadata)
        futures: set[concurrent.futures.Future[dict[str, Any] | None]] = set()

        def fill_queue() -> None:
            while len(futures) < inflight_limit:
                try:
                    meta = next(metadata_iter)
                except StopIteration:
                    return
                futures.add(executor.submit(inspect_archive, meta))

        fill_queue()
        with progress_path.open("a", encoding="utf-8") as progress:
            while futures and (args.inspect_all or len(accepted) < args.target):
                done, futures = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    checked += 1
                    rec = None
                    try:
                        rec = fut.result()
                    except Exception as exc:
                        progress.write(json.dumps({"event": "inspect_error", "error": repr(exc)}, sort_keys=True) + "\n")
                    if rec:
                        key = canonical_key(rec["url"])
                        if repo_name_from_key(key) in blocked_names:
                            rejected += 1
                        elif key not in accepted:
                            accepted[key] = rec
                    else:
                        rejected += 1
                    if checked % 25 == 0 or (not args.inspect_all and len(accepted) >= args.target):
                        counts = Counter(r["primary_category"] for r in accepted.values())
                        event = {"checked": checked, "accepted": len(accepted), "rejected": rejected, "category_counts": counts}
                        progress.write(json.dumps(event, sort_keys=True) + "\n")
                        progress.flush()
                        print(f"[inspect] checked={checked} accepted={len(accepted)} rejected={rejected}", flush=True)
                    if not args.inspect_all and len(accepted) >= args.target:
                        break
                if args.inspect_all or len(accepted) < args.target:
                    fill_queue()
            for fut in futures:
                fut.cancel()

    def row_sort_key(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            TARGET_CATEGORIES.index(r["primary_category"]) if r["primary_category"] in TARGET_CATEGORIES else 99,
            -(r.get("source_candidate") or {}).get("stars", 0) if isinstance((r.get("source_candidate") or {}).get("stars", 0), int) else 0,
            r["full_name"],
        )

    if args.inspect_all and preserved_output_keys:
        preserved_set = set(preserved_output_keys)
        appended_rows = [row for key, row in accepted.items() if key not in preserved_set]
        appended_rows.sort(key=row_sort_key)
        rows = [accepted[key] for key in preserved_output_keys] + appended_rows
    else:
        rows = list(accepted.values())
        rows.sort(key=row_sort_key)
        rows = rows[: args.target]
    write_jsonl(output, rows)
    category_counts = Counter(r["primary_category"] for r in rows)
    summary = {
        "output": str(output),
        "target": args.target,
        "count": len(rows),
        "inspect_all": args.inspect_all,
        "candidate_pool_count": len(metadata),
        "generated_at": stamp,
        "rule_version": "strict_v2_2000loc_5py_9categories",
        "category_counts": dict(category_counts),
        "all_target_categories_present": all(category_counts.get(c, 0) > 0 for c in TARGET_CATEGORIES),
        "static_rules": {
            "python_loc_min": 2000,
            "python_loc_max": 8000,
            "agent_target_py_files_min": 5,
            "pytest_required": True,
            "test_case_count_min": 1,
            "package_manifest_required": True,
            "nl2repobench_repo_name_excluded": True,
        },
        "deferred_runtime_rules": [
            "git clone/build/install must be verified by TeamFactory Agent1",
            "dryrun/oracle collected/passed/failed/errors must be verified by TeamFactory Agent1",
            "oracle --network none must be verified in Harbor/TeamFactory runtime",
        ],
        "search_log": str(search_log_path),
        "progress_log": str(progress_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0 if len(rows) >= args.target and summary["all_target_categories_present"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
