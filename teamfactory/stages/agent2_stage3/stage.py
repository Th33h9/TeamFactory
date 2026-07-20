from __future__ import annotations

import json
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from string import Template
from typing import Any

from teamfactory.artifacts import ItemRef, item_dir, read_stage, write_json, write_stage
from teamfactory.providers.remote_claude import RemoteClaudeCodeProvider
from teamfactory.remote import q, run_remote, scp_from_remote
from teamfactory.stages.agent1.stage import extract_json


STAGE3_SCHEMA = "teamfactory.agent2_stage3.v1"
PROMPT_TEMPLATE_PATH = Path(__file__).with_name("prompt.md")


class FinalImageLeakError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        hits = report.get("fatal_hits", [])
        super().__init__(
            "final_image_oracle_leak: "
            + json.dumps(hits[:5], ensure_ascii=False, sort_keys=True)
        )


class ApiCoverageError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        missing = report.get("missing_symbols", [])
        super().__init__(
            "api_manifest_coverage_failed: "
            + json.dumps(missing[:20], ensure_ascii=False, sort_keys=True)
        )


INSTRUCTION = """According to the start.md in the workspace, implement the entire project as per the requirements specified in the document, ensuring that the final product can be directly run in the current directory. The running requirements should comply with the <API Usage Guide> section of the document.
Note that all required dependencies have already been pre-configured in the local environment. You are strictly prohibited from fetching external information or dependencies. Do not use commands such as git clone, pip install, curl, wget, or similar tools. Please complete this task step by step.
"""

TEST_SH = r'''#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
python - <<'PY'
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

LOG_DIR = Path('/logs/verifier')
LOG_FILE = LOG_DIR / 'test-stdout.txt'
CONFIG = json.loads(Path('/tests/config.json').read_text())
WORKSPACE = Path('/workspace')
REFERENCE = Path('/tests/reference')

def test_root_for(path_str: str) -> str:
    p = PurePosixPath(path_str)
    parts = p.parts
    if not parts:
        return path_str
    for idx, part in enumerate(parts):
        if part in {'tests', 'test'}:
            return str(PurePosixPath(*parts[: idx + 1]))
    if p.name.startswith('test') or p.name == 'tests.py':
        return str(p)
    if len(parts) > 1:
        return str(PurePosixPath(*parts[:-1]))
    return str(p)

def copy_any(src: Path, dest: Path):
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    elif src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

test_roots = []
seen_roots = set()
for entry in CONFIG.get('test_files', []):
    root = test_root_for(entry)
    if root not in seen_roots:
        seen_roots.add(root)
        test_roots.append(root)

for root in test_roots:
    target = WORKSPACE / root
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()

restored_roots = []
for root in test_roots:
    src = REFERENCE / root
    dest = WORKSPACE / root
    if src.exists():
        copy_any(src, dest)
        restored_roots.append(root)

for rel in CONFIG.get('package_files', []):
    src = REFERENCE / rel
    dest = WORKSPACE / rel
    if src.exists():
        copy_any(src, dest)

for rel in CONFIG.get('test_files', []):
    src = REFERENCE / rel
    dest = WORKSPACE / rel
    if src.exists():
        copy_any(src, dest)

for rel in CONFIG.get('fixture_files', []):
    src = REFERENCE / rel
    dest = WORKSPACE / rel
    if dest.exists() or dest.is_symlink():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    if src.exists():
        copy_any(src, dest)

needs_testbed = any('/testbed' in cmd for cmd in CONFIG.get('test_commands', []))
if needs_testbed and not Path('/testbed').exists():
    os.symlink('/workspace', '/testbed')

env = os.environ.copy()
env['PYTHONUNBUFFERED'] = '1'
with LOG_FILE.open('w', encoding='utf-8') as fh:
    for cmd in CONFIG.get('test_commands', []):
        fh.write(f"$ {cmd}\n")
        fh.flush()
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(WORKSPACE),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            universal_newlines=True,
        )
        fh.write(f"\n[command-exit-code] {proc.returncode}\n")
        fh.flush()

text = LOG_FILE.read_text(encoding='utf-8', errors='replace')
whitelist = set()
for rel in CONFIG.get('test_files', []):
    ref_file = REFERENCE / rel
    if ref_file.is_file():
        whitelist.add(rel)
for root in restored_roots:
    ref_root = REFERENCE / root
    if ref_root.is_dir():
        for path in ref_root.rglob('*.py'):
            whitelist.add(path.relative_to(REFERENCE).as_posix())
    elif ref_root.is_file():
        whitelist.add(ref_root.relative_to(REFERENCE).as_posix())

node_pattern = re.compile(r'^(?P<nodeid>[^\s].*?)\s+(?P<status>PASSED|FAILED|ERROR)\s*$', re.MULTILINE)
passed = failed = errors = 0
counted = 0
for match in node_pattern.finditer(text):
    nodeid = match.group('nodeid').strip()
    status = match.group('status')
    file_part = nodeid.split('::', 1)[0]
    if file_part in whitelist:
        counted += 1
        if status == 'PASSED':
            passed += 1
        elif status == 'FAILED':
            failed += 1
        else:
            errors += 1

summary_counts = {'passed': 0, 'failed': 0, 'errors': 0}
pytest_collected = None
for line in text.splitlines():
    collect_match = re.search(r'collected\s+(\d+)\s+items?', line)
    if collect_match:
        pytest_collected = int(collect_match.group(1))
    for count, kind in re.findall(r"(\d+)\s+(passed|failed|error|errors)", line):
        value = int(count)
        if kind == 'passed':
            summary_counts['passed'] += value
        elif kind == 'failed':
            summary_counts['failed'] += value
        else:
            summary_counts['errors'] += value

observed_total = passed + failed + errors
summary_total = summary_counts['passed'] + summary_counts['failed'] + summary_counts['errors']
if counted == 0 and summary_total > 0:
    passed = summary_counts['passed']
    failed = summary_counts['failed']
    errors = summary_counts['errors']
    observed_total = summary_total

total = max(int(CONFIG.get('test_case_count', 0)), observed_total)
reward = min((passed / total) if total else 0.0, 1.0)
(LOG_DIR / 'reward.txt').write_text(f"{reward:.10g}\n", encoding='utf-8')
report = {
    'passed': passed,
    'failed': failed,
    'errors': errors,
    'total': total,
    'configured_total': int(CONFIG.get('test_case_count', 0)),
    'observed_total': observed_total,
    'pytest_collected': pytest_collected,
    'reward': reward,
}
(LOG_DIR / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
PY
'''


def toml_string(value: str) -> str:
    return json.dumps(value)


def toml_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(item) for item in values) + "]"


def safe_name(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "teamfactory-instance"


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    parts = set(p.parts)
    name = p.name
    return "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py") or name == "tests.py"


def test_root_for(path_str: str) -> str:
    p = PurePosixPath(path_str)
    parts = p.parts
    for idx, part in enumerate(parts):
        if part in {"tests", "test"}:
            return str(PurePosixPath(*parts[: idx + 1]))
    if p.name.startswith("test") or p.name == "tests.py":
        return str(p)
    if len(parts) > 1:
        return str(PurePosixPath(*parts[:-1]))
    return str(p)


def unique_existing(paths: list[str], root: Path) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in paths:
        rel = str(item).strip().lstrip("/")
        if not rel or rel in seen:
            continue
        if (root / rel).exists():
            seen.add(rel)
            out.append(rel)
    return out


def infer_package_files(root: Path, env_spec: dict[str, Any]) -> list[str]:
    candidates = [str(item) for item in env_spec.get("package_files", []) if str(item).strip()]
    candidates.extend(
        [
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "tox.ini",
            "pytest.ini",
            "requirements.txt",
            "requirements-dev.txt",
        ]
    )
    return unique_existing(candidates, root)


def infer_test_files(root: Path, env_spec: dict[str, Any], stage2_payload: dict[str, Any]) -> list[str]:
    candidates = [str(item) for item in env_spec.get("test_files", []) if str(item).strip()]
    candidates.extend([path for path in stage2_payload.get("python_files", []) if is_test_path(str(path))])
    if not candidates:
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if is_test_path(rel):
                candidates.append(rel)
    return unique_existing(candidates, root)


def infer_fixture_files(root: Path, env_spec: dict[str, Any]) -> list[str]:
    return unique_existing([str(item) for item in env_spec.get("fixture_files", []) if str(item).strip()], root)


def copy_any(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    elif src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def extract_tar_to(tar_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError(f"unsafe tar member: {member.name}")
        tf.extractall(dest)


def write_task_toml(path: Path, instance_name: str, project_name: str, image_archive: str) -> None:
    text = f"""version = "1.0"

[metadata]
author_name = "TeamFactory"
author_email = "unknown"
difficulty = "medium"
category = "code-generation"
tags = {toml_array(["code-generation", "0-to-1", "teamfactory", "python", project_name])}

[verifier]
timeout_sec = 1200.0

[agent]
timeout_sec = 3600.0

[environment]
build_timeout_sec = 1800.0
cpus = 2
memory_mb = 8192
storage_mb = 20480
docker_image_archive = {toml_string(image_archive)}
"""
    path.write_text(text, encoding="utf-8")


def dockerfile_text(base_image: str) -> str:
    return f"""FROM {base_image}
USER root
RUN rm -rf /workspace /testbed /repo /app /src && mkdir -p /workspace /logs
COPY start.md /workspace/start.md
WORKDIR /workspace
"""


def validate_nlfactory_start_md(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    required_patterns = [
        r"^## .+ Project Introduction and Goals\s*$",
        r"^## Natural Language Instructions \(Prompt\)\s*$",
        r"^## Environment Configuration\s*$",
        r"^### Python Version\s*$",
        r"^### Core Dependency Library Versions\s*$",
        r"^## .+ Project Architecture\s*$",
        r"^### Project Directory Structure\s*$",
        r"^## API Usage Guide\s*$",
        r"^### Core API\s*$",
        r"^#### 1\. Module Import\s*$",
        r"^## Usage Example\s*$",
        r"^## Detailed Function Implementation Nodes\s*$",
        r"^### Node 1: .+",
        r"\*\*Function Description\*\*:",
        r"\*\*Handling Strategy\*\*:",
        r"\*\*Input and Output Examples\*\*:",
        r"\*\*Function Signature\*\*:",
        r"\*\*Parameter Description\*\*:",
        r"\*\*Returns\*\*:",
        r"\*\*Input and Output Example\*\*:",
    ]
    missing = [pattern for pattern in required_patterns if not re.search(pattern, text, re.M)]
    if missing:
        raise ValueError(f"start.md does not match NLFactory style; missing patterns: {missing[:5]}")
    forbidden_headings = [
        "## Scope and path rules",
        "## Directory tree",
        "## Natural-language requirements",
        "## Important data and behavior that must match",
        "## Core API reference",
        "## Reconstruction notes",
    ]
    present_forbidden = [heading for heading in forbidden_headings if heading in text]
    if present_forbidden:
        raise ValueError(f"start.md contains non-NLFactory freeform headings: {present_forbidden}")
    forbidden_node_fields = [
        "**Implementation Path**:",
        "**Public Import Path**:",
        "**Required imports**:",
        "**Processing flow**:",
        "**CLI Usage Example**:",
        "**Features**:",
        "**No Integration",
    ]
    present_forbidden_fields = [field for field in forbidden_node_fields if field in text]
    if present_forbidden_fields:
        raise ValueError(f"start.md contains forbidden node fields: {present_forbidden_fields}")
    if re.search(r"^###\s+\d+\.\s+", text, re.M):
        raise ValueError("start.md contains numbered node heading like '### 1.'; use '### Node N: ...'")


def validate_project_tree_excludes_tests(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    heading = re.search(r"^### Project Directory Structure\s*$", text, re.M)
    if not heading:
        return
    after_heading = text[heading.end():]
    next_heading = re.search(r"^##+ ", after_heading, re.M)
    section = after_heading[: next_heading.start()] if next_heading else after_heading
    fence = re.search(r"```(?:text|Plain|plain)?\s*\n(?P<body>.*?)```", section, re.S)
    if not fence:
        return
    body = fence.group("body")
    forbidden: list[str] = []
    patterns = [
        r"(^|[ /│├└─])tests?(/|\b)",
        r"(^|[ /│├└─])testing(/|\b)",
        r"(^|[ /│├└─])test(/|\b)",
        r"(^|[ /│├└─])test[^/\n]*\b",
        r"(^|[ /│├└─])[^/\n]*(?:^|[-_.])tests?(?:[-_.]|/|\b)",
        r"(^|[ /│├└─])[^/\n]*testing(?:[-_.]|/|\b)",
        r"(^|[ /│├└─])conftest\.py\b",
    ]
    for line in body.splitlines():
        normalized = line.strip()
        for pattern in patterns:
            if re.search(pattern, normalized):
                forbidden.append(normalized)
                break
    if forbidden:
        raise ValueError(
            "start.md Project Directory Structure leaks test paths; "
            f"remove these entries from the tree: {forbidden[:10]}"
        )


PROJECT_TREE_TEST_PATTERNS = [
    r"(^|[ /│├└─])tests?(/|\b)",
    r"(^|[ /│├└─])testing(/|\b)",
    r"(^|[ /│├└─])test(/|\b)",
    r"(^|[ /│├└─])test[^/\n]*\b",
    r"(^|[ /│├└─])[^/\n]*(?:^|[-_.])tests?(?:[-_.]|/|\b)",
    r"(^|[ /│├└─])[^/\n]*testing(?:[-_.]|/|\b)",
    r"(^|[ /│├└─])conftest\.py\b",
]
MIN_PROJECT_TREE_PY_FILES = 5


def project_tree_line_depth(line: str) -> int:
    pos = max(line.rfind("├── "), line.rfind("└── "))
    return -1 if pos < 0 else pos // 4


def line_is_test_project_tree_entry(line: str) -> bool:
    normalized = line.strip()
    return any(re.search(pattern, normalized, re.I) for pattern in PROJECT_TREE_TEST_PATTERNS)


def scrub_project_tree_test_entries(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    heading = re.search(r"^### Project Directory Structure\s*$", text, re.M)
    if not heading:
        return 0
    after_heading = text[heading.end():]
    next_heading = re.search(r"^##+ ", after_heading, re.M)
    section_start = heading.end()
    section_end = heading.end() + next_heading.start() if next_heading else len(text)
    section = text[section_start:section_end]
    fence = re.search(r"```(?:text|Plain|plain)?\s*\n(?P<body>.*?)```", section, re.S)
    if not fence:
        return 0

    removed = 0
    skip_depth: int | None = None
    kept: list[str] = []
    body = fence.group("body")
    for line in body.splitlines():
        depth = project_tree_line_depth(line)
        if skip_depth is not None:
            if depth > skip_depth or depth == -1:
                removed += 1
                continue
            skip_depth = None
        if line_is_test_project_tree_entry(line):
            removed += 1
            if line.strip().endswith("/"):
                skip_depth = depth
            continue
        kept.append(line)
    if not removed:
        return 0

    new_body = "\n".join(kept) + ("\n" if body.endswith("\n") else "")
    new_section = section[: fence.start("body")] + new_body + section[fence.end("body"):]
    path.write_text(text[:section_start] + new_section + text[section_end:], encoding="utf-8")
    return removed


def project_tree_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    heading = re.search(r"^### Project Directory Structure\s*$", text, re.M)
    if not heading:
        return ""
    after_heading = text[heading.end():]
    next_heading = re.search(r"^##+ ", after_heading, re.M)
    section = after_heading[: next_heading.start()] if next_heading else after_heading
    fence = re.search(r"```(?:text|Plain|plain)?\s*\n(?P<body>.*?)```", section, re.S)
    return fence.group("body") if fence else section


def count_project_tree_python_files(path: Path) -> int:
    return sum(1 for line in project_tree_body(path).splitlines() if re.search(r"\.py\b", line))


def validate_project_tree_python_file_count(path: Path) -> None:
    count = count_project_tree_python_files(path)
    if count < MIN_PROJECT_TREE_PY_FILES:
        raise ValueError(
            "start.md Project Directory Structure has too few Python files; "
            f"found {count}, expected at least {MIN_PROJECT_TREE_PY_FILES}"
        )


def next_stage_after_materialization(args: Any) -> str:
    return "oracle_repair" if getattr(args, "oracle_repair", True) else ""


class Agent2Stage3:
    name = "agent2_stage3"

    def run(self, args: Any, ref: ItemRef) -> str:
        previous = read_stage(args, ref.task_id, self.name, {}) or {}
        try:
            agent1 = read_stage(args, ref.task_id, "agent1", {})
            stage2 = read_stage(args, ref.task_id, "stage2_ast", {})
            if agent1.get("status") != "agent1_passed":
                raise ValueError(f"Agent1 is not passed: {agent1.get('status')!r}")
            if stage2.get("status") != "stage2_passed":
                raise ValueError(f"Stage2 is not passed: {stage2.get('status')!r}")
            remote_task_dir = str(agent1.get("remote_task_dir") or "").rstrip("/")
            if not remote_task_dir:
                raise ValueError("Agent1 output missing remote_task_dir")
            base_image = str((agent1.get("docker") or {}).get("image") or "").strip()
            if not base_image:
                raise ValueError("Agent1 output missing docker.image")

            provider = RemoteClaudeCodeProvider(args)
            prompt = self.build_prompt(args, ref, agent1, stage2, remote_task_dir)
            turn = provider.run(prompt, task_id=ref.task_id, phase=self.name, cwd=remote_task_dir)
            payload = self.payload_from_agent2_turn(args, ref, turn, remote_task_dir)
            if payload["status"] != "stage3_passed":
                raise RuntimeError(f"agent2 failed: {payload.get('notes', '')}")

            instance_name = ref.task_id
            project_name = safe_name(str(payload.get("project_name") or ref.task_id.split("__", 1)[0]))
            coverage_report = self.validate_remote_api_coverage(args, ref, agent1, stage2, payload, remote_task_dir)
            paths = self.materialize_instance(args, ref, agent1, stage2, payload, base_image, instance_name, project_name)
            row = {
                "schema_version": STAGE3_SCHEMA,
                "status": "stage3_passed",
                "input": {
                    "agent1_stage_path": str(item_dir(args, ref.task_id) / "agent1.json"),
                    "stage2_stage_path": str(item_dir(args, ref.task_id) / "stage2_ast.json"),
                    "remote_task_dir": remote_task_dir,
                    "repo_url": ref.url,
                    "base_docker_image": base_image,
                },
                "agent2": {
                    "project_name": project_name,
                    "remote_start_md": payload["start_md_path"],
                    "remote_api_manifest": payload["api_manifest_path"],
                    "notes": payload.get("notes", ""),
                    "turn": {
                        "record_type": turn.get("record_type"),
                        "duration_ms": turn.get("duration_ms"),
                        "returncode": turn.get("returncode"),
                        "model": turn.get("model"),
                    },
                },
                "api_coverage": coverage_report,
                "outputs": paths,
            }
            write_json(item_dir(args, ref.task_id) / "agent2_stage3.json", row)
            write_stage(args, ref, self.name, row)
            return next_stage_after_materialization(args)
        except ApiCoverageError as exc:
            retry_count = int(previous.get("coverage_retry_count") or 0) + 1
            max_retries = int(getattr(args, "agent2_coverage_retries", 2))
            will_retry = retry_count <= max_retries
            row = {
                "schema_version": STAGE3_SCHEMA,
                "status": "stage3_retry_api_coverage" if will_retry else "stage3_error",
                "error": repr(exc),
                "coverage_retry_count": retry_count,
                "max_coverage_retries": max_retries,
                "api_coverage": exc.report,
            }
            write_stage(args, ref, self.name, row)
            return self.name if will_retry else ""
        except Exception as exc:
            retry_count = int(previous.get("agent2_retry_count") or 0) + 1
            max_retries = int(getattr(args, "agent2_coverage_retries", 2))
            retryable = not isinstance(exc, FinalImageLeakError)
            will_retry = retryable and retry_count <= max_retries
            row = {
                "schema_version": STAGE3_SCHEMA,
                "status": "stage3_retry_agent2" if will_retry else "stage3_error",
                "error": repr(exc),
                "agent2_retry_count": retry_count,
                "max_agent2_retries": max_retries,
            }
            if isinstance(exc, FinalImageLeakError):
                row["leak_check"] = exc.report
            write_stage(args, ref, self.name, row)
            return self.name if will_retry else ""

    def payload_from_agent2_turn(
        self,
        args: Any,
        ref: ItemRef,
        turn: dict[str, Any],
        remote_task_dir: str,
    ) -> dict[str, Any]:
        final_response = str(turn.get("final_response") or "")
        try:
            return self.validate_agent2_payload(extract_json(final_response))
        except Exception as exc:
            fallback = {
                "status": "stage3_passed",
                "project_name": safe_name(ref.task_id.split("__", 1)[0]),
                "start_md_path": "stage3/start.md",
                "api_manifest_path": "stage3/api_manifest.json",
                "core_api_count": 0,
                "node_count": 0,
                "notes": f"fallback payload from default stage3 paths after final JSON parse failed: {exc!r}",
            }
            remote_start = self.resolve_remote_stage3_path(remote_task_dir, fallback["start_md_path"])
            remote_manifest = self.resolve_remote_stage3_path(remote_task_dir, fallback["api_manifest_path"])
            check = run_remote(
                args,
                f"test -s {q(remote_start)} && test -s {q(remote_manifest)}",
                timeout=30,
            )
            if check.returncode == 0:
                return fallback
            raise ValueError(
                "Agent2 did not return valid final JSON and default stage3 files are missing: "
                f"{exc!r}; remote check tail={check.stdout[-1000:]!r}"
            ) from exc

    def validate_agent2_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "").strip()
        if status not in {"stage3_passed", "stage3_failed"}:
            raise ValueError(f"invalid Agent2 status: {status!r}")
        start_md_path = str(payload.get("start_md_path") or "stage3/start.md").strip()
        if not start_md_path:
            raise ValueError("Agent2 output missing start_md_path")
        return {
            "status": status,
            "project_name": str(payload.get("project_name") or "").strip(),
            "start_md_path": start_md_path,
            "api_manifest_path": str(payload.get("api_manifest_path") or "stage3/api_manifest.json").strip(),
            "core_api_count": int(payload.get("core_api_count") or 0),
            "node_count": int(payload.get("node_count") or 0),
            "notes": str(payload.get("notes") or ""),
        }

    def resolve_remote_stage3_path(self, remote_task_dir: str, path: str) -> str:
        path = str(path or "").strip()
        if not path:
            raise ValueError("empty remote stage3 path")
        if path.startswith("/"):
            return path
        return f"{remote_task_dir.rstrip('/')}/{path}"

    def validate_remote_api_coverage(
        self,
        args: Any,
        ref: ItemRef,
        agent1: dict[str, Any],
        stage2: dict[str, Any],
        payload: dict[str, Any],
        remote_task_dir: str,
    ) -> dict[str, Any]:
        remote_start_md = self.resolve_remote_stage3_path(remote_task_dir, payload["start_md_path"])
        remote_manifest = self.resolve_remote_stage3_path(remote_task_dir, payload["api_manifest_path"])
        local_item = item_dir(args, ref.task_id)
        local_start = local_item / "stage3_coverage_start.md"
        local_manifest = local_item / "stage3_api_manifest.json"
        scp_start = scp_from_remote(args, remote_start_md, local_start)
        if scp_start.returncode != 0:
            raise RuntimeError(f"copy start.md for API coverage failed: {scp_start.stdout[-4000:]}")
        scp_manifest = scp_from_remote(args, remote_manifest, local_manifest)
        if scp_manifest.returncode != 0:
            raise ApiCoverageError({
                "schema_version": "teamfactory.api_coverage_report.v1",
                "status": "failed",
                "reason": "missing_or_unreadable_api_manifest",
                "remote_api_manifest": remote_manifest,
                "stdout_tail": scp_manifest.stdout[-4000:],
            })
        try:
            manifest = json.loads(local_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiCoverageError({
                "schema_version": "teamfactory.api_coverage_report.v1",
                "status": "failed",
                "reason": "invalid_api_manifest_json",
                "remote_api_manifest": remote_manifest,
                "error": str(exc),
            }) from exc
        report = self.check_api_coverage(stage2, local_start, manifest, remote_manifest)
        write_json(local_item / "api_coverage_report.json", report)
        if report.get("missing_symbols"):
            raise ApiCoverageError(report)
        return report

    def check_api_coverage(
        self,
        stage2: dict[str, Any],
        start_md_path: Path,
        manifest: dict[str, Any],
        remote_manifest: str,
    ) -> dict[str, Any]:
        artifact = stage2.get("artifact") or {}
        required_symbols = [
            str(item).strip()
            for item in artifact.get("required_api_symbols", [])
            if str(item).strip()
        ]
        import_records = artifact.get("test_imported_repo_symbols", [])
        start_text = start_md_path.read_text(encoding="utf-8", errors="replace")
        covered_symbols = self.extract_manifest_symbols(manifest)
        missing: list[dict[str, Any]] = []
        covered: list[str] = []
        for symbol in sorted(set(required_symbols)):
            if self.symbol_is_covered(symbol, covered_symbols, start_text):
                covered.append(symbol)
            else:
                records = [
                    item for item in import_records
                    if str(item.get("symbol") or "") == symbol
                ][:10]
                missing.append({
                    "symbol": symbol,
                    "import_records": records,
                })
        return {
            "schema_version": "teamfactory.api_coverage_report.v1",
            "status": "passed" if not missing else "failed",
            "required_symbol_count": len(set(required_symbols)),
            "covered_symbol_count": len(covered),
            "missing_symbol_count": len(missing),
            "covered_symbols": covered,
            "missing_symbols": missing,
            "api_manifest_path": remote_manifest,
        }

    def extract_manifest_symbols(self, manifest: dict[str, Any]) -> set[str]:
        symbols: set[str] = set()
        for value in manifest.get("covered_symbols", []) or []:
            if isinstance(value, str):
                symbols.add(value.strip())
            elif isinstance(value, dict):
                for key in ("symbol", "qualified_name", "name"):
                    if value.get(key):
                        symbols.add(str(value[key]).strip())
        for item in manifest.get("core_apis", []) or []:
            if not isinstance(item, dict):
                continue
            for key in ("qualified_name", "name"):
                if item.get(key):
                    symbols.add(str(item[key]).strip())
            for symbol in item.get("covered_import_symbols", []) or []:
                if str(symbol).strip():
                    symbols.add(str(symbol).strip())
        return {symbol for symbol in symbols if symbol}

    def symbol_is_covered(self, symbol: str, covered_symbols: set[str], start_text: str) -> bool:
        aliases = self.symbol_aliases(symbol)
        normalized_manifest = {item for value in covered_symbols for item in self.symbol_aliases(value)}
        if aliases & normalized_manifest:
            return True
        return any(self.text_mentions_symbol(start_text, alias) for alias in aliases)

    def symbol_aliases(self, symbol: str) -> set[str]:
        symbol = str(symbol or "").strip()
        if not symbol:
            return set()
        parts = [part for part in symbol.split(".") if part]
        aliases = {symbol}
        if parts:
            aliases.add(parts[-1])
        if len(parts) >= 2:
            aliases.add(".".join(parts[-2:]))
        return aliases

    def text_mentions_symbol(self, text: str, symbol: str) -> bool:
        if not symbol:
            return False
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])"
        return re.search(pattern, text) is not None

    def materialize_instance(
        self,
        args: Any,
        ref: ItemRef,
        agent1: dict[str, Any],
        stage2: dict[str, Any],
        payload: dict[str, Any],
        base_image: str,
        instance_name: str,
        project_name: str,
    ) -> dict[str, Any]:
        remote_task_dir = str(agent1["remote_task_dir"]).rstrip("/")
        remote_stage3 = f"{remote_task_dir}/stage3"
        remote_repo = f"{remote_task_dir}/repo"
        remote_start_md = self.resolve_remote_stage3_path(remote_task_dir, payload["start_md_path"])
        remote_api_manifest = self.resolve_remote_stage3_path(remote_task_dir, payload["api_manifest_path"])
        final_image = safe_name(f"teamfactory-instance-{instance_name}")
        remote_image_root = str(args.remote_image_root).rstrip("/")
        remote_image_archive = f"{remote_image_root}/{instance_name}.tar"
        remote_repo_archive = f"{remote_stage3}/repo.tgz"
        remote_context = f"{remote_stage3}/image_context"
        docker_build_command = str((agent1.get("commands") or {}).get("docker_build") or "").strip()
        if not docker_build_command:
            docker_build_command = ""

        dockerfile = dockerfile_text(base_image)
        rebuild_base = ""
        if docker_build_command:
            rebuild_base = f"""
if ! docker image inspect {q(base_image)} >/dev/null 2>&1; then
  echo "Agent1 base image missing; rebuilding {base_image}" >&2
  cd {q(remote_task_dir)}
  {docker_build_command}
fi
"""
        else:
            rebuild_base = f"""
docker image inspect {q(base_image)} >/dev/null 2>&1 || {{
  echo "Agent1 base image missing and agent1.commands.docker_build is empty: {base_image}" >&2
  exit 41
}}
"""
        script = f"""
set -euo pipefail
test -s {q(remote_start_md)}
test -s {q(remote_api_manifest)}
mkdir -p {q(remote_stage3)} {q(remote_context)} {q(remote_image_root)}
{rebuild_base}
tar --exclude=.git --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache -C {q(remote_repo)} -czf {q(remote_repo_archive)} .
cp {q(remote_start_md)} {q(remote_context)}/start.md
cat > {q(remote_context)}/Dockerfile <<'DOCKERFILE'
{dockerfile}
DOCKERFILE
docker build -t {q(final_image)} {q(remote_context)}
docker save -o {q(remote_image_archive)} {q(final_image)}
test -s {q(remote_image_archive)}
"""
        result = run_remote(args, script, timeout=int(args.stage3_timeout))
        if result.returncode != 0:
            raise RuntimeError(f"stage3 materialize remote failed: {result.stdout[-6000:]}")

        leak_report = self.run_remote_image_leak_check(args, final_image, remote_image_archive, remote_stage3)
        if leak_report.get("fatal"):
            raise FinalImageLeakError(leak_report)

        local_item = item_dir(args, ref.task_id)
        local_repo_archive = local_item / "stage3_repo.tgz"
        scp_repo = scp_from_remote(args, remote_repo_archive, local_repo_archive)
        if scp_repo.returncode != 0:
            raise RuntimeError(f"copy repo archive failed: {scp_repo.stdout[-4000:]}")
        local_start = local_item / "stage3_start.md"
        scp_start = scp_from_remote(args, remote_start_md, local_start)
        if scp_start.returncode != 0:
            raise RuntimeError(f"copy start.md failed: {scp_start.stdout[-4000:]}")
        local_manifest = local_item / "stage3_api_manifest.json"
        scp_manifest = scp_from_remote(args, remote_api_manifest, local_manifest)
        if scp_manifest.returncode != 0:
            raise RuntimeError(f"copy api_manifest.json failed: {scp_manifest.stdout[-4000:]}")
        scrub_project_tree_test_entries(local_start)
        validate_project_tree_excludes_tests(local_start)
        validate_project_tree_python_file_count(local_start)
        if getattr(args, "validate_start_md", False):
            validate_nlfactory_start_md(local_start)

        instance_dir = Path(args.dataset_root) / instance_name
        if instance_dir.exists():
            shutil.rmtree(instance_dir)
        env_dir = instance_dir / "environment"
        solution_dir = instance_dir / "solution"
        oracle_dir = solution_dir / "oracle"
        tests_dir = instance_dir / "tests"
        reference_dir = tests_dir / "reference"
        env_dir.mkdir(parents=True, exist_ok=True)
        solution_dir.mkdir(parents=True, exist_ok=True)
        tests_dir.mkdir(parents=True, exist_ok=True)
        reference_dir.mkdir(parents=True, exist_ok=True)

        extract_tar_to(local_repo_archive, oracle_dir)
        shutil.copy2(local_start, env_dir / "start.md")
        shutil.copy2(local_manifest, env_dir / "api_manifest.json")
        (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (instance_dir / "instruction.md").write_text(INSTRUCTION, encoding="utf-8")
        solve = solution_dir / "solve.sh"
        solve.write_text("#!/bin/bash\nset -euo pipefail\ncp -a /solution/oracle/. /workspace/\n", encoding="utf-8")
        solve.chmod(0o755)

        env_spec = agent1.get("env_spec") or {}
        stage2_payload = stage2.get("artifact") or {}
        package_files = infer_package_files(oracle_dir, env_spec)
        test_files = infer_test_files(oracle_dir, env_spec, stage2_payload)
        fixture_files = infer_fixture_files(oracle_dir, env_spec)
        for rel in package_files:
            copy_any(oracle_dir / rel, reference_dir / rel)
        seen_roots: set[str] = set()
        for rel in test_files:
            root = test_root_for(rel)
            if root not in seen_roots:
                seen_roots.add(root)
                copy_any(oracle_dir / root, reference_dir / root)
            copy_any(oracle_dir / rel, reference_dir / rel)
        for rel in fixture_files:
            copy_any(oracle_dir / rel, reference_dir / rel)

        test_case_count = int((stage2.get("summary") or {}).get("test_case_count") or 0)
        if test_case_count <= 0:
            test_case_count = int((agent1.get("oracle_report") or {}).get("collected") or 0)
        config = {
            "pro_name": project_name,
            "test_case_count": test_case_count,
            "test_commands": [str(item) for item in env_spec.get("test_commands", []) if str(item).strip()],
            "test_files": test_files,
            "package_files": package_files,
            "fixture_files": fixture_files,
        }
        write_json(tests_dir / "config.json", config)
        test_sh = tests_dir / "test.sh"
        test_sh.write_text(TEST_SH, encoding="utf-8")
        test_sh.chmod(0o755)
        write_task_toml(instance_dir / "task.toml", instance_name, project_name, remote_image_archive)

        return {
            "dataset_instance_dir": str(instance_dir),
            "docker_image_archive": remote_image_archive,
            "final_docker_image": final_image,
            "environment_start_md": str(env_dir / "start.md"),
            "environment_api_manifest": str(env_dir / "api_manifest.json"),
            "environment_dockerfile": str(env_dir / "Dockerfile"),
            "instruction_md": str(instance_dir / "instruction.md"),
            "task_toml": str(instance_dir / "task.toml"),
            "tests_config": str(tests_dir / "config.json"),
            "tests_test_sh": str(test_sh),
            "solution_oracle": str(oracle_dir),
            "test_file_count": len(test_files),
            "package_file_count": len(package_files),
            "fixture_file_count": len(fixture_files),
            "test_case_count": test_case_count,
            "leak_check": leak_report,
        }

    def run_remote_image_leak_check(
        self,
        args: Any,
        final_image: str,
        remote_image_archive: str,
        remote_stage3: str,
    ) -> dict[str, Any]:
        remote_check = f"{remote_stage3}/leak_check"
        remote_script = f"{remote_check}/check_final_image_leak.py"
        remote_report = f"{remote_check}/leak_report.json"
        remote_fs_tar = f"{remote_check}/final_fs.tar"
        scanner = r'''
from __future__ import annotations

import json
import sys
import tarfile


BAD_PTH_TOKENS = ("/app", "/repo", "/src", "/workspace", "/testbed")
FORBIDDEN_SOURCE_PREFIXES = ("app/", "repo/", "src/", "testbed/")


def read_text(tar: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    try:
        fh = tar.extractfile(member)
        if fh is None:
            return ""
        return fh.read(200000).decode("utf-8", "replace")
    except Exception:
        return ""


def normalized_path(path: str) -> str:
    path = path.lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path


def main() -> int:
    fs_tar = sys.argv[1]
    out_path = sys.argv[2]
    fatal_hits = []
    warnings = []
    with tarfile.open(fs_tar, "r") as tar:
        for member in tar:
            path = normalized_path(member.name)
            if not member.isfile():
                continue

            if path.endswith(".py") and path.startswith(FORBIDDEN_SOURCE_PREFIXES):
                fatal_hits.append({
                    "type": "visible_forbidden_source_path",
                    "path": path,
                })
                continue

            if "/site-packages/" not in path:
                continue

            if path.endswith("/direct_url.json") and ".dist-info/" in path:
                raw = read_text(tar, member)
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {}
                url = str(data.get("url") or "")
                editable = bool((data.get("dir_info") or {}).get("editable"))
                if url.startswith("file:") and not editable:
                    fatal_hits.append({
                        "type": "noneditable_file_direct_url",
                        "path": path,
                        "url": url,
                        "editable": editable,
                    })
                elif url.startswith("file:"):
                    warnings.append({
                        "type": "editable_file_direct_url",
                        "path": path,
                        "url": url,
                        "editable": editable,
                    })
            elif path.endswith(".pth"):
                text = read_text(tar, member)
                if any(token in text for token in BAD_PTH_TOKENS):
                    warnings.append({
                        "type": "repo_like_pth",
                        "path": path,
                        "content": text[:500],
                    })

    report = {
        "schema_version": "teamfactory.final_image_leak_check.v1",
        "fatal": bool(fatal_hits),
        "fatal_hit_count": len(fatal_hits),
        "warning_count": len(warnings),
        "fatal_hits": fatal_hits[:50],
        "warnings": warnings[:50],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 42 if fatal_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
        script = f"""
set -euo pipefail
mkdir -p {q(remote_check)}
cat > {q(remote_script)} <<'PYLEAK'
{scanner}
PYLEAK
cid=""
cleanup() {{
  if [ -n "$cid" ]; then
    docker rm "$cid" >/dev/null 2>&1 || true
  fi
  rm -f {q(remote_fs_tar)}
}}
trap cleanup EXIT
docker image inspect {q(final_image)} >/dev/null
cid=$(docker create {q(final_image)} /bin/sh -c true)
docker export "$cid" -o {q(remote_fs_tar)}
set +e
python3 {q(remote_script)} {q(remote_fs_tar)} {q(remote_report)}
rc=$?
set -e
if [ "$rc" -eq 42 ]; then
  rm -f {q(remote_image_archive)}
fi
exit "$rc"
"""
        result = run_remote(args, script, timeout=max(300, int(args.stage3_timeout)))
        report = self.parse_leak_check_report(result.stdout)
        if result.returncode == 42:
            if not report:
                report = {
                    "schema_version": "teamfactory.final_image_leak_check.v1",
                    "fatal": True,
                    "fatal_hits": [{"type": "unknown", "stdout_tail": result.stdout[-4000:]}],
                }
            return report
        if result.returncode != 0:
            raise RuntimeError(f"final image leak check failed: {result.stdout[-6000:]}")
        if not report:
            raise RuntimeError(f"final image leak check did not emit JSON: {result.stdout[-4000:]}")
        return report

    @staticmethod
    def parse_leak_check_report(stdout: str) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("schema_version") == "teamfactory.final_image_leak_check.v1":
                return data
        return {}

    def build_prompt(self, args: Any, ref: ItemRef, agent1: dict[str, Any], stage2: dict[str, Any], remote_task_dir: str) -> str:
        summary = stage2.get("summary") or {}
        artifact = stage2.get("artifact") or {}
        previous_agent2 = read_stage(args, ref.task_id, self.name, {}) or {}
        class_count = int(summary.get("public_class_count") or 0)
        function_count = int(summary.get("public_function_count") or 0)
        api_budget = min(60, max(20, class_count + function_count))
        context = {
            "repo_url": ref.url,
            "remote_task_dir": remote_task_dir,
            "repo_path": f"{remote_task_dir}/repo",
            "stage2_payload_path": f"{remote_task_dir}/stage2_ast.json",
            "agent1_env_spec": agent1.get("env_spec", {}),
            "agent1_oracle_report": agent1.get("oracle_report", {}),
            "stage2_summary": summary,
            "required_api_symbols": artifact.get("required_api_symbols", []),
            "test_imported_repo_symbols": artifact.get("test_imported_repo_symbols", [])[:120],
            "previous_agent2_coverage_failure": (previous_agent2 or {}).get("api_coverage", {}),
            "previous_agent2_coverage_retry_count": int((previous_agent2 or {}).get("coverage_retry_count") or 0),
            "sample_public_classes": artifact.get("public_classes", [])[:12],
            "sample_public_functions": artifact.get("public_functions", [])[:20],
            "implementation_tree": artifact.get("implementation_tree", {}),
            "api_budget": api_budget,
        }
        return Template(PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")).safe_substitute(
            repo_url=ref.url,
            remote_task_dir=remote_task_dir,
            context_json=json.dumps(context, ensure_ascii=False, indent=2)[:24000],
        )
