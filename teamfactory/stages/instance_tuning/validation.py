from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_INSTANCE_FILES = {
    "environment/Dockerfile",
    "environment/start.md",
    "instruction.md",
    "task.toml",
    "tests/config.json",
    "tests/test.sh",
}
LEAK_MARKERS = (
    "/solution",
    "/tests/reference",
    "solution/oracle",
    "/testbed",
    "FILE_INDEX.txt",
    "Hyperdistill",
    "maintenance_trajectories",
)


def file_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            result[path.relative_to(root).as_posix()] = "symlink:" + str(path.readlink())
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.relative_to(root).as_posix()] = digest.hexdigest()
    return result


def changed_paths(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def normalize_declared_path(value: Any) -> str:
    path = str(value).strip()
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("instance/"):
        path = path.removeprefix("instance/")
    return path


def archive_path(task_toml: Path) -> str:
    match = re.search(
        r'^\s*docker_image_archive\s*=\s*["\']([^"\']+)',
        task_toml.read_text(encoding="utf-8", errors="replace"),
        re.M,
    )
    if not match:
        raise ValueError("task.toml has no docker_image_archive")
    return match.group(1)


def _load_test_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("tests/config.json must be an object")
    return value


def validate_image_commands(commands: list[str]) -> None:
    forbidden = ["docker ", "podman ", "/solution", "/tests", "--privileged", "mount "]
    for command in commands:
        if "\n" in command or "\r" in command:
            raise ValueError("image_commands entries must be single-line commands")
        lowered = command.lower()
        if any(token in lowered for token in forbidden):
            raise ValueError(f"unsafe image command: {command}")


def _headings(text: str, level: int) -> list[str]:
    prefix = "#" * level
    return [
        match.group(1).strip()
        for match in re.finditer(rf"^{re.escape(prefix)}\s+(.+?)\s*$", text, re.M)
        if not match.group(1).startswith("#")
    ]


def _fixed_h3(headings: list[str]) -> list[str]:
    return [heading for heading in headings if not re.match(r"^Node\s+\d+\s*:", heading)]


def validate_start_md_format(original: Path, candidate: Path) -> None:
    before = original.read_text(encoding="utf-8", errors="replace")
    after = candidate.read_text(encoding="utf-8", errors="replace")
    if _headings(before, 2) != _headings(after, 2):
        raise ValueError("environment/start.md top-level section order changed")
    if _fixed_h3(_headings(before, 3)) != _fixed_h3(_headings(after, 3)):
        raise ValueError("environment/start.md subsection format changed")
    if after.count("```") % 2:
        raise ValueError("environment/start.md has an unclosed fenced block")

    core_block = after.split("### Core API", 1)
    if len(core_block) == 2:
        core_text = core_block[1].split("## Usage Example", 1)[0]
        numbers = [
            int(value)
            for value in re.findall(r"^####\s+(\d+)\.\s+", core_text, re.M)
        ]
        if numbers and numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("environment/start.md Core API numbering is not sequential")

    nodes = list(
        re.finditer(
            r"^###\s+Node\s+(\d+)\s*:\s*(.+?)\s*$",
            after,
            re.M,
        )
    )
    if nodes:
        numbers = [int(match.group(1)) for match in nodes]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("environment/start.md Node numbering is not sequential")
        for index, match in enumerate(nodes):
            end = nodes[index + 1].start() if index + 1 < len(nodes) else len(after)
            body = after[match.end():end]
            fields = [
                body.find("**Function Description**:"),
                body.find("**Handling Strategy**:"),
                body.find("**Input and Output Examples**:"),
            ]
            if any(position < 0 for position in fields) or fields != sorted(fields):
                raise ValueError(
                    f"environment/start.md Node {match.group(1)} field format changed"
                )


def _test_metrics(root: Path) -> tuple[int, int, int]:
    files = functions = assertions = 0
    if not root.is_dir():
        return files, functions, assertions
    for path in root.rglob("*.py"):
        files += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        functions += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        )
        assertions += sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))
        assertions += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"assertEqual", "assertTrue", "assertFalse", "assertRaises", "raises"}
        )
    return files, functions, assertions


def _validate_no_test_bypass(original: Path, candidate: Path) -> None:
    old_reference = original / "tests" / "reference"
    new_reference = candidate / "tests" / "reference"
    for old_path in old_reference.rglob("*"):
        if old_path.is_file():
            relative = old_path.relative_to(old_reference)
            if not (new_reference / relative).is_file():
                raise ValueError(f"reference test file may not be removed: {relative}")

    bypass_count_before = bypass_count_after = 0
    for root, target in ((old_reference, "before"), (new_reference, "after")):
        count = 0
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and bool(node.test.value):
                    count += 1
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"skip", "skipif", "xfail"}:
                        count += 1
        if target == "before":
            bypass_count_before = count
        else:
            bypass_count_after = count
    if bypass_count_after > bypass_count_before:
        raise ValueError("repair introduced skip/xfail or unconditional passing assertions")

    before_sh = (original / "tests" / "test.sh").read_text(encoding="utf-8", errors="replace")
    after_sh = (candidate / "tests" / "test.sh").read_text(encoding="utf-8", errors="replace")
    bypass_patterns = (r"\|\|\s*true\b", r"\bexit\s+0\b", r"\bpytest\b[^\n]*--collect-only")
    for pattern in bypass_patterns:
        if len(re.findall(pattern, after_sh)) > len(re.findall(pattern, before_sh)):
            raise ValueError(f"tests/test.sh introduced verifier bypass pattern: {pattern}")


def _visible_text(root: Path) -> str:
    values: list[str] = []
    for relative in ("environment/start.md", "instruction.md"):
        path = root / relative
        if path.is_file():
            values.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(values)


def _oracle_sensitive_lines(root: Path) -> set[str]:
    lines: set[str] = set()
    for base in (root / "solution" / "oracle", root / "tests" / "reference"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                normalized = line.strip()
                if len(normalized) >= 40 and not normalized.startswith(("#", "import ", "from ")):
                    lines.add(normalized)
    return lines


def validate_no_answer_leak(original: Path, candidate: Path) -> None:
    before = _visible_text(original)
    after = _visible_text(candidate)
    for marker in LEAK_MARKERS:
        if after.count(marker) > before.count(marker):
            raise ValueError(f"agent-visible text introduced forbidden leak marker: {marker}")

    added_lines = set(after.splitlines()) - set(before.splitlines())
    copied = {
        line.strip()
        for line in added_lines
        if line.strip() in _oracle_sensitive_lines(original)
    }
    if copied:
        raise ValueError("agent-visible text copied oracle/test implementation lines")

    dockerfile = (candidate / "environment" / "Dockerfile").read_text(
        encoding="utf-8", errors="replace"
    ).lower()
    dangerous = (
        "copy solution",
        "add solution",
        "copy tests",
        "add tests",
        "pip install /solution",
        "pip install /tests",
    )
    if any(value in dockerfile for value in dangerous):
        raise ValueError("environment/Dockerfile exposes oracle or tests to the image")


def validate_candidate(
    original: Path,
    candidate: Path,
    declared_changes: list[str],
    image_commands: list[str],
    *,
    allow_test_case_count_decrease: bool = False,
    allow_test_command_replacement: bool = False,
    validate_start_md: bool = True,
) -> set[str]:
    before = file_manifest(original)
    after = file_manifest(candidate)
    changed = changed_paths(before, after)
    if not changed:
        raise ValueError("repair agent did not change the instance")

    missing_required = [path for path in REQUIRED_INSTANCE_FILES if not (candidate / path).is_file()]
    if missing_required:
        raise ValueError(f"repair removed required instance files: {missing_required}")

    declared = {
        normalize_declared_path(path)
        for path in declared_changes
        if str(path).strip()
    }
    if declared != changed:
        raise ValueError(
            f"repair changed_files mismatch: declared={sorted(declared)}, actual={sorted(changed)}"
        )

    if archive_path(original / "task.toml") != archive_path(candidate / "task.toml"):
        raise ValueError("docker_image_archive may not change")

    if validate_start_md:
        validate_start_md_format(
            original / "environment" / "start.md",
            candidate / "environment" / "start.md",
        )
    validate_no_answer_leak(original, candidate)

    old_config = _load_test_config(original / "tests" / "config.json")
    new_config = _load_test_config(candidate / "tests" / "config.json")
    if (
        int(new_config.get("test_case_count") or 0)
        < int(old_config.get("test_case_count") or 0)
        and not allow_test_case_count_decrease
    ):
        raise ValueError("test_case_count may not decrease")
    old_tests = list(old_config.get("test_files") or [])
    new_tests = list(new_config.get("test_files") or [])
    if not set(old_tests).issubset(set(new_tests)):
        raise ValueError("test_files may not be removed")
    old_commands = list(old_config.get("test_commands") or [])
    new_commands = list(new_config.get("test_commands") or [])
    if not new_commands:
        raise ValueError("test_commands may not be empty")
    if (
        not allow_test_command_replacement
        and not set(old_commands).issubset(set(new_commands))
    ):
        raise ValueError("test_commands may not be removed")

    old_metrics = _test_metrics(original / "tests" / "reference")
    new_metrics = _test_metrics(candidate / "tests" / "reference")
    if any(new < old for old, new in zip(old_metrics, new_metrics)):
        raise ValueError(
            f"reference test coverage may not decrease: before={old_metrics}, after={new_metrics}"
        )
    _validate_no_test_bypass(original, candidate)

    test_sh = (candidate / "tests" / "test.sh").read_text(encoding="utf-8", errors="replace")
    for marker in ("/logs/verifier", "reward.txt", "/tests/reference", "/workspace"):
        if marker not in test_sh:
            raise ValueError(f"tests/test.sh lost required marker: {marker}")

    dockerfile_changed = "environment/Dockerfile" in changed
    if dockerfile_changed != bool(image_commands):
        raise ValueError(
            "environment/Dockerfile and image_commands must change together"
        )
    if image_commands:
        dockerfile = (candidate / "environment" / "Dockerfile").read_text(
            encoding="utf-8", errors="replace"
        )
        missing_commands = [command for command in image_commands if command not in dockerfile]
        if missing_commands:
            raise ValueError(
                f"image_commands are not durable in environment/Dockerfile: {missing_commands}"
            )

    for path in candidate.rglob("*"):
        if path.is_symlink():
            target = path.resolve()
            if candidate.resolve() not in target.parents and target != candidate.resolve():
                raise ValueError(f"candidate contains escaping symlink: {path}")

    validate_image_commands(image_commands)
    return changed
