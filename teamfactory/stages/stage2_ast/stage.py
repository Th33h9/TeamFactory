from __future__ import annotations

import json
from typing import Any

from teamfactory.artifacts import ItemRef, item_dir, read_stage, write_json, write_stage
from teamfactory.remote import q, run_remote, scp_from_remote


STAGE2_SCHEMA = "teamfactory.stage2_ast.v1"
MIN_IMPLEMENTATION_PY_FILES = 5


SCANNER_CODE = r'''
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


IGNORED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".venv", "venv", "env", "build", "dist", "node_modules",
}


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_test_file(path: Path, root: Path) -> bool:
    r = path.relative_to(root)
    parts = set(r.parts)
    name = path.name
    return "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py") or name == "tests.py"


def is_test_artifact_path(path: Path, root: Path) -> bool:
    r = path.relative_to(root)
    for part in r.parts:
        lowered = part.lower()
        stem = lowered.rsplit(".", 1)[0]
        tokens = [token for token in stem.replace("_", "-").split("-") if token]
        if lowered in {"tests", "test", "testing", "conftest.py", "tests.py"}:
            return True
        if lowered.startswith("test_") or lowered.endswith("_test.py"):
            return True
        if any(token in {"test", "tests", "testing"} for token in tokens):
            return True
    return False


def module_name_for(path: Path, root: Path) -> str:
    r = path.relative_to(root)
    if r.name == "__init__.py":
        return r.parent.as_posix().replace("/", ".")
    return r.with_suffix("").as_posix().replace("/", ".")


def project_module_inventory(root: Path) -> dict[str, Any]:
    modules: set[str] = set()
    roots: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        r = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in r.parts):
            continue
        if is_test_artifact_path(path, root):
            continue
        module = module_name_for(path, root)
        if not module or module == "__init__":
            continue
        modules.add(module)
        roots.add(module.split(".", 1)[0])
    return {
        "modules": sorted(modules),
        "roots": sorted(roots),
    }


def is_repo_module(module: str, module_names: set[str], module_roots: set[str]) -> bool:
    if not module:
        return False
    root = module.split(".", 1)[0]
    return module in module_names or root in module_roots


def imported_repo_symbols(tree: ast.AST, test_path: str, module_names: set[str], module_roots: set[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if not is_repo_module(module, module_names, module_roots):
                    continue
                symbol = module
                key = ("import", symbol, test_path)
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "symbol": symbol,
                    "module": module,
                    "name": "",
                    "import_type": "import",
                    "test_path": test_path,
                    "lineno": node.lineno,
                    "statement": f"import {module}",
                })
        elif isinstance(node, ast.ImportFrom):
            module = "." * int(node.level or 0) + (node.module or "")
            if node.level:
                # Relative imports inside tests are often test helpers. Keep only
                # absolute project imports so the gate checks public repo surface.
                continue
            if not is_repo_module(module, module_names, module_roots):
                continue
            for alias in node.names:
                name = alias.name
                symbol = f"{module}.{name}" if name != "*" else f"{module}.*"
                key = ("from", symbol, test_path)
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "symbol": symbol,
                    "module": module,
                    "name": name,
                    "import_type": "from",
                    "test_path": test_path,
                    "lineno": node.lineno,
                    "statement": f"from {module} import {name}",
                })
    return found


def unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def dotted_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted_call_name(node.func)
    return unparse(node)


def format_arg(arg: ast.arg, default: ast.AST | None = None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {unparse(arg.annotation)}"
    if default is not None:
        text += f" = {unparse(default)}"
    return text


def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    parts: list[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, arg in enumerate(positional):
        if index == len(args.posonlyargs) and args.posonlyargs:
            parts.append("/")
        parts.append(format_arg(arg, defaults[index]))
    if args.vararg:
        parts.append("*" + format_arg(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(format_arg(arg, default))
    if args.kwarg:
        parts.append("**" + format_arg(args.kwarg))
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({', '.join(parts)}){ret}"


def return_hints(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    samples: list[str] = []
    has_value = False
    has_bare = False
    for child in ast.walk(node):
        if isinstance(child, ast.Return):
            if child.value is None:
                has_bare = True
            else:
                has_value = True
                expr = unparse(child.value)
                if expr and expr not in samples:
                    samples.append(expr[:200])
    return {
        "annotation": unparse(node.returns),
        "has_value_return": has_value,
        "has_bare_return": has_bare,
        "samples": samples[:12],
    }


def raises(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            value = unparse(child.exc)
            if value and value not in found:
                found.append(value[:200])
    return found[:30]


def calls(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = dotted_call_name(child.func)
            if name and name not in found:
                found.append(name[:200])
    return found[:80]


def function_record(node: ast.FunctionDef | ast.AsyncFunctionDef, module: str, owner: str = "") -> dict[str, Any]:
    return {
        "name": node.name,
        "qualname": f"{owner}.{node.name}" if owner else node.name,
        "module": module,
        "lineno": node.lineno,
        "end_lineno": getattr(node, "end_lineno", None),
        "signature": signature(node),
        "docstring": ast.get_docstring(node) or "",
        "return_hints": return_hints(node),
        "raises": raises(node),
        "calls": calls(node),
        "decorators": [unparse(item) for item in node.decorator_list],
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def class_record(node: ast.ClassDef, module: str) -> dict[str, Any]:
    methods: list[dict[str, Any]] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
            methods.append(function_record(child, module, owner=node.name))
    return {
        "name": node.name,
        "qualname": node.name,
        "module": module,
        "lineno": node.lineno,
        "end_lineno": getattr(node, "end_lineno", None),
        "bases": [unparse(base) for base in node.bases],
        "decorators": [unparse(item) for item in node.decorator_list],
        "docstring": ast.get_docstring(node) or "",
        "methods": methods,
        "calls": calls(node),
    }


def project_tree(root: Path, include_tests: bool = True) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(root.rglob("*")):
        r = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in r.parts):
            continue
        if not include_tests and is_test_artifact_path(path, root):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({
            "path": r.as_posix(),
            "type": "dir" if path.is_dir() else "file",
            "bytes": 0 if path.is_dir() else stat.st_size,
        })
        if len(entries) >= 5000:
            truncated = True
            break
    return {"entries": entries, "entry_count": len(entries), "truncated": truncated}


def scan(root: Path) -> dict[str, Any]:
    public_classes: list[dict[str, Any]] = []
    public_functions: list[dict[str, Any]] = []
    python_files: list[str] = []
    test_imported_symbols: list[dict[str, Any]] = []
    test_case_count = 0
    parse_errors: list[dict[str, str]] = []
    module_inventory = project_module_inventory(root)
    module_names = set(module_inventory["modules"])
    module_roots = set(module_inventory["roots"])
    for path in sorted(root.rglob("*.py")):
        r = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in r.parts):
            continue
        module = module_name_for(path, root)
        python_files.append(r.as_posix())
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError as exc:
            parse_errors.append({"path": r.as_posix(), "error": str(exc)})
            continue
        if is_test_file(path, root):
            test_imported_symbols.extend(
                imported_repo_symbols(tree, r.as_posix(), module_names, module_roots)
            )
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if not node.name.startswith("_"):
                    public_classes.append(class_record(node, module))
                if is_test_file(path, root):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                            test_case_count += 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    public_functions.append(function_record(node, module))
                if is_test_file(path, root) and node.name.startswith("test"):
                    test_case_count += 1
    impl_tree = project_tree(root, include_tests=False)
    implementation_python_files = [
        entry["path"]
        for entry in impl_tree["entries"]
        if entry.get("type") == "file" and str(entry.get("path", "")).endswith(".py")
    ]
    required_api_symbols = sorted({item["symbol"] for item in test_imported_symbols})
    return {
        "schema_version": "teamfactory.stage2_ast_payload.v1",
        "repo_root": str(root),
        "project_import_roots": module_inventory["roots"],
        "project_modules": module_inventory["modules"],
        "project_tree": project_tree(root),
        "implementation_tree": impl_tree,
        "implementation_python_files": implementation_python_files,
        "python_files": python_files,
        "public_classes": public_classes,
        "public_functions": public_functions,
        "test_imported_repo_symbols": test_imported_symbols,
        "required_api_symbols": required_api_symbols,
        "test_case_count": test_case_count,
        "parse_errors": parse_errors,
        "summary": {
            "python_file_count": len(python_files),
            "public_class_count": len(public_classes),
            "public_function_count": len(public_functions),
            "public_method_count": sum(len(item.get("methods", [])) for item in public_classes),
            "test_imported_repo_symbol_count": len(required_api_symbols),
            "test_case_count": test_case_count,
            "implementation_python_file_count": len(implementation_python_files),
            "parse_error_count": len(parse_errors),
        },
    }


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    output.write_text(json.dumps(scan(root), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


class Stage2AstStage:
    name = "stage2_ast"

    def run(self, args: Any, ref: ItemRef) -> str:
        try:
            agent1 = read_stage(args, ref.task_id, "agent1", {})
            if agent1.get("status") != "agent1_passed":
                raise ValueError(f"Agent1 is not passed: {agent1.get('status')!r}")
            remote_task_dir = str(agent1.get("remote_task_dir") or "").rstrip("/")
            if not remote_task_dir:
                raise ValueError("Agent1 output missing remote_task_dir")
            remote_repo = f"{remote_task_dir}/repo"
            scanner_path = f"{remote_task_dir}/stage2_ast_scan.py"
            remote_output = f"{remote_task_dir}/stage2_ast.json"
            local_payload = item_dir(args, ref.task_id) / "stage2_ast_payload.json"
            script = f"""
set -euo pipefail
cat > {q(scanner_path)} <<'PYSCAN'
{SCANNER_CODE}
PYSCAN
docker run --rm \
  -v {q(remote_repo)}:/repo:ro \
  -v {q(remote_task_dir)}:/out \
  python:3.11-slim \
  python /out/stage2_ast_scan.py /repo /out/stage2_ast.json
test -s {q(remote_output)}
"""
            result = run_remote(args, script, timeout=int(args.stage2_timeout))
            if result.returncode != 0:
                raise RuntimeError(f"stage2 docker ast scan failed: {result.stdout[-4000:]}")
            scp = scp_from_remote(args, remote_output, local_payload)
            if scp.returncode != 0:
                raise RuntimeError(f"copy stage2 output failed: {scp.stdout[-4000:]}")
            payload = json.loads(local_payload.read_text(encoding="utf-8"))
            implementation_py_count = int((payload.get("summary") or {}).get("implementation_python_file_count") or 0)
            if implementation_py_count < MIN_IMPLEMENTATION_PY_FILES:
                row = {
                    "schema_version": STAGE2_SCHEMA,
                    "status": "stage2_filtered",
                    "filter_reason": "implementation_python_file_count_lt_5",
                    "implementation_python_file_count": implementation_py_count,
                    "min_implementation_python_files": MIN_IMPLEMENTATION_PY_FILES,
                    "input": {
                        "agent1_stage_path": str(item_dir(args, ref.task_id) / "agent1.json"),
                        "agent1_status": agent1.get("status"),
                        "remote_task_dir": remote_task_dir,
                        "remote_repo": remote_repo,
                        "repo_url": ref.url,
                        "docker_image": (agent1.get("docker") or {}).get("image"),
                    },
                    "summary": payload.get("summary", {}),
                    "artifact": payload,
                }
                write_json(item_dir(args, ref.task_id) / "stage2_ast.json", row)
                write_stage(args, ref, self.name, row)
                return ""
            row = {
                "schema_version": STAGE2_SCHEMA,
                "status": "stage2_passed",
                "input": {
                    "agent1_stage_path": str(item_dir(args, ref.task_id) / "agent1.json"),
                    "agent1_status": agent1.get("status"),
                    "remote_task_dir": remote_task_dir,
                    "remote_repo": remote_repo,
                    "repo_url": ref.url,
                    "docker_image": (agent1.get("docker") or {}).get("image"),
                },
                "scanner": {
                    "docker_image": "python:3.11-slim",
                    "remote_scanner_path": scanner_path,
                    "remote_output_path": remote_output,
                    "local_payload_path": str(local_payload),
                },
                "annotations": {
                    "public_classes": "Public top-level Python classes; private names beginning with '_' are excluded.",
                    "public_functions": "Public module-level Python functions; private names beginning with '_' are excluded.",
                    "methods": "Public methods attached to each public class, with signatures and metadata.",
                    "return_hints": "Return annotation and sampled return expressions from AST.",
                    "raises": "Raised exception expressions collected from AST Raise nodes.",
                    "calls": "Unique call targets observed in the AST subtree.",
                    "project_tree": "Repository file tree excluding common generated/cache directories; retained as evidence and may include tests.",
                    "implementation_tree": "Repository file tree excluding common generated/cache directories and test files/directories. Use this tree for start.md Project Directory Structure.",
                    "implementation_python_files": "Non-test Python files in implementation_tree. Items with fewer than five are filtered before Agent2.",
                    "test_imported_repo_symbols": "Repo modules/classes/functions imported by test files; Agent2 must cover these in start.md/API manifest.",
                    "required_api_symbols": "Unique symbol strings derived from test_imported_repo_symbols for the hard API coverage gate.",
                    "test_case_count": "Approximate pytest/unittest test case count from test files and test_* functions/methods.",
                },
                "summary": payload.get("summary", {}),
                "artifact": payload,
            }
            write_json(item_dir(args, ref.task_id) / "stage2_ast.json", row)
            write_stage(args, ref, self.name, row)
            return "agent2_stage3"
        except Exception as exc:
            row = {
                "schema_version": STAGE2_SCHEMA,
                "status": "stage2_error",
                "error": repr(exc),
            }
            write_stage(args, ref, self.name, row)
            return ""
