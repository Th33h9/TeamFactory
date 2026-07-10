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


class Agent2Stage3:
    name = "agent2_stage3"

    def run(self, args: Any, ref: ItemRef) -> str:
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
            prompt = self.build_prompt(ref, agent1, stage2, remote_task_dir)
            turn = provider.run(prompt, task_id=ref.task_id, phase=self.name, cwd=remote_task_dir)
            payload = self.validate_agent2_payload(extract_json(str(turn.get("final_response") or "")))
            if payload["status"] != "stage3_passed":
                raise RuntimeError(f"agent2 failed: {payload.get('notes', '')}")

            instance_name = ref.task_id
            project_name = safe_name(str(payload.get("project_name") or ref.task_id.split("__", 1)[0]))
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
                    "notes": payload.get("notes", ""),
                    "turn": {
                        "record_type": turn.get("record_type"),
                        "duration_ms": turn.get("duration_ms"),
                        "returncode": turn.get("returncode"),
                        "model": turn.get("model"),
                    },
                },
                "outputs": paths,
            }
            write_json(item_dir(args, ref.task_id) / "agent2_stage3.json", row)
            write_stage(args, ref, self.name, row)
            return ""
        except Exception as exc:
            row = {
                "schema_version": STAGE3_SCHEMA,
                "status": "stage3_error",
                "error": repr(exc),
            }
            write_stage(args, ref, self.name, row)
            return ""

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
            "core_api_count": int(payload.get("core_api_count") or 0),
            "node_count": int(payload.get("node_count") or 0),
            "notes": str(payload.get("notes") or ""),
        }

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
        remote_start_md = payload["start_md_path"]
        if not remote_start_md.startswith("/"):
            remote_start_md = f"{remote_task_dir}/{remote_start_md}"
        final_image = safe_name(f"teamfactory-instance-{instance_name}")
        remote_image_root = str(args.remote_image_root).rstrip("/")
        remote_image_archive = f"{remote_image_root}/{instance_name}.tar"
        remote_repo_archive = f"{remote_stage3}/repo.tgz"
        remote_context = f"{remote_stage3}/image_context"

        dockerfile = dockerfile_text(base_image)
        script = f"""
set -euo pipefail
test -s {q(remote_start_md)}
mkdir -p {q(remote_stage3)} {q(remote_context)} {q(remote_image_root)}
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

        local_item = item_dir(args, ref.task_id)
        local_repo_archive = local_item / "stage3_repo.tgz"
        scp_repo = scp_from_remote(args, remote_repo_archive, local_repo_archive)
        if scp_repo.returncode != 0:
            raise RuntimeError(f"copy repo archive failed: {scp_repo.stdout[-4000:]}")
        local_start = local_item / "stage3_start.md"
        scp_start = scp_from_remote(args, remote_start_md, local_start)
        if scp_start.returncode != 0:
            raise RuntimeError(f"copy start.md failed: {scp_start.stdout[-4000:]}")
        scrub_project_tree_test_entries(local_start)
        validate_project_tree_excludes_tests(local_start)
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
        }

    def build_prompt(self, ref: ItemRef, agent1: dict[str, Any], stage2: dict[str, Any], remote_task_dir: str) -> str:
        summary = stage2.get("summary") or {}
        artifact = stage2.get("artifact") or {}
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
