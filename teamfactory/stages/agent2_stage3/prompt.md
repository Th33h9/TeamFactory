You are Agent2 in TeamFactory.

Goal:
Generate a high-quality `environment/start.md` for a Harbor NL2Repo instance. The target style must match NLFactory/NL2RepoBench-style curated tasks: behavior-oriented, implementation-complete, concise enough to be usable, and detailed enough for an agent to reconstruct the project from an empty `/workspace`.

Repository URL:
$repo_url

Remote paths:
- task directory: $remote_task_dir
- source repository: $remote_task_dir/repo
- Stage2 AST payload: $remote_task_dir/stage2_ast.json
- output directory: $remote_task_dir/stage3
- required start.md path: $remote_task_dir/stage3/start.md

Context JSON:
```json
$context_json
```

Required work:
1. Read the repository, README/docs if present, tests, and `$remote_task_dir/stage2_ast.json`.
2. Write `$remote_task_dir/stage3/start.md`.
3. You may inspect the repository and tests to infer behavior, but the final `start.md` must not reveal or copy oracle source code, hidden tests, exact test assertions, physical pipeline paths, generation pipeline details, verifier internals, or private implementation bodies. Translate evidence into public behavior contracts.
4. Evidence priority:
   - Tests and fixtures, for observable behavior.
   - README/docs and examples, for intended public usage.
   - `__init__.py` exports and package metadata, for the public import surface.
   - Stage2 AST inventory, for signatures, classes, functions, docstrings, calls, raises, and returns.
   - Source code, only to infer public behavior.
5. The document must follow this exact NLFactory-style section order and heading vocabulary:
   - `## <ProjectName> Project Introduction and Goals`
   - `## Natural Language Instructions (Prompt)`
   - `## Environment Configuration`
   - literal sentence: `All implementation paths must be relative to /workspace. Never create, write, or use /testbed.`
   - `### Python Version`
   - `### Core Dependency Library Versions`
   - `## <ProjectName> Project Architecture`
   - `### Project Directory Structure`
   - a fenced `text` code block rooted at `workspace/` and using `├──`, `│`, and `└──`
   - This directory tree must describe only the implementation workspace the agent should reconstruct. It must not include any tests, verifier files, hidden test fixtures, or test-only support files.
   - `## API Usage Guide`
   - `### Core API`
   - `#### 1. Module Import`
   - `## Usage Example`
   - usage subsections grounded in README/docs/tests. You may use `### Actual Usage Modes`, `#### Basic Usage`, `#### File Processing Example`, `#### Complex Parameter Example`, `#### Enumeration Support Example`, `#### Asynchronous Function Support`, `### Supported Parameter Types`, `### Error Handling`, and `### Important Notes` only when the repository evidence supports them. If they are not supported, replace them with project-specific usage modes that are supported by evidence.
   - `## Detailed Function Implementation Nodes`

6. Do not add freeform headings such as `Scope and path rules`, `Directory tree`, `Natural-language requirements`, `Important data and behavior that must match`, `Core API reference`, or `Reconstruction notes`. Any important behavior must be folded into `Natural Language Instructions`, `Project Architecture`, `Usage Example`, `Core API`, or `Detailed Function Implementation Nodes`.
7. Core API selection:
   - Always include `Module Import` as API 1.
   - Include all important public APIs if there are 60 or fewer.
   - If there are more than 60 candidates, select the most important 60 by tests, README/docs usage, public exports, class centrality, and call centrality.
   - Merge a class and its key public methods into one class entry.
   - Standalone public functions should be separate entries.
   - Do not promote private helpers or incidental internal utilities unless tests/docs make them part of the public contract.
8. The API section must look like NLFactory output, not a narrative design document. Every API entry must use one of these forms:
   - Function entry:
     - `#### N. <name> - <one sentence summary>`
     - `**Function**: ...`
     - `**Function Signature**:` followed by a Python code block with the public signature and a docstring-style behavior contract
     - `**Parameter Description**:`
     - `**Returns**:`
     - `**Input and Output Example**:`
   - Class entry:
     - `#### N. <ClassName> - <one sentence summary>`
     - `**Function**: ...`
     - `**Class Definition**:` followed by a Python code block that exposes public class variables/constants, documented instance variables, and all important public method names
     - `**Parameter Description**:`
     - `**Returns**:`
     - `**Input and Output Example**:`
9. Core API code blocks must be informative enough for reconstruction:
   - Function code blocks must not be just `def name(...): ...`. Include the full public signature plus a concise docstring explaining behavior, args, returns, and raises when known.
   - Class code blocks must not expose only `class Name: ...`. Include public class variables/constants, constructor parameters, documented/observable instance variables, and public method names. Add inline comments or docstrings that explain what each variable or method is for. Do not copy method bodies.
   - For classes, list all public methods visible from Stage2 AST unless there are many trivial aliases. When there are many, include all important methods and group obvious aliases with comments.
10. For a class, include the class and its public variables/methods in the same `**Class Definition**` code block, similar to:

```python
class Example:
    DEFAULT_MODE: str
    value: str

    def __init__(self, value: str): ...
    def run(self, text: str) -> str: ...
    def reset(self) -> None: ...
```

11. For a standalone function, expose only the function interface and a docstring-style behavior summary, not the implementation body, similar to:

```python
def required() -> mods.RequiredModifier:
    '''
    Marks a field as required.

    Returns:
        A value that can be used by the public API.
    '''
```

12. For every Core API entry, include:
   - Purpose / behavior.
   - Public signature or class definition.
   - Parameters.
   - Return value.
   - Error behavior if known.
   - A non-trivial example.
13. Do not use weak filler phrases such as:
   - `Returns the documented result`
   - `according to its signature and examples`
   - `public API object is importable`
   - `Value accepted by the public signature`
   - `Public method used by callers`
14. Do not write examples that only print the function object or only prove importability. Every example must demonstrate actual behavior: input, output, state change, exception, parsing result, formatting result, or side effect.
15. Use tests to infer behavior nodes. Do not copy test code verbatim. Convert tests into natural-language implementation contracts. Tests are evidence only; they are not part of the project structure shown to the reconstruction agent.
16. Detailed Function Implementation Nodes must be behavior-oriented, not one-to-one API listings. Each node should correspond to a coherent behavior tested by the project, such as parsing and normalization, validation and error reporting, serialization/deserialization, CLI invocation, file I/O, async behavior, decorators or plugin registration, path handling, formatting/rendering, or configuration loading.
17. Every behavior node heading must use exactly this form:
   - `### Node 1: <behavior name>`
   - `### Node 2: <behavior name>`
   - Never use `### 1. <behavior name>` or `#### Node ...`.
18. Each behavior node must include exactly these fields:
   - `**Function Description**:`
   - `**Handling Strategy**:`
   - `**Input and Output Examples**:`
19. The `**Input and Output Examples**:` field in every behavior node must be written as a mini test-spec, not as a short prose note. It must be detailed enough that a developer can reconstruct the behavior without seeing the original tests:
   - Include 2-4 concrete examples for the node whenever evidence exists.
   - Prefer fenced Python snippets for library behavior, shell snippets for CLI behavior, and short before/after file trees or JSON snippets for file/config behavior.
   - At least one example should show the normal successful path with realistic input values and the expected returned value, object attributes, formatted text, parsed data, JSON keys, file side effect, or state transition.
   - Include edge or error examples when visible from tests/source/docs, such as invalid input, missing fields, malformed files, unsupported types, empty input, duplicate names, missing dependencies, path errors, or platform-specific cases.
   - For stateful APIs, show `before -> operation -> after`.
   - For async behavior, show an `async def` / `await` example and the expected resolved value or exception.
   - For CLI behavior, show the command, representative stdout/stderr or generated file, exit behavior if known, and any relevant input file content.
   - For file I/O behavior, show the input file content/path, the call or command, and the expected output file content/path or exception.
   - Do not copy test code verbatim, exact assertion text, hidden paths, verifier paths, or private fixture names. Rewrite the evidence into public examples.
   - Do not use one-line examples like `api(x) -> result` unless the behavior is genuinely trivial and at least one other example in the same node is detailed.
   - Do not say only `returns expected result`, `raises an error`, or `handles invalid input`; name the concrete result type/value shape, exception type/message theme, or observable side effect.
20. Do not include these fields in behavior nodes:
   - `Implementation Path`
   - `Public Import Path`
   - `Required imports`
   - `Processing flow`
   - `CLI Usage Example`
   - `Features`
   - `No Integration`
21. Include edge cases and error cases when visible from tests or source: invalid input, empty input, missing files, malformed config, unsupported types, duplicate keys, encoding issues, async cancellation/timeouts, and platform-specific behavior.
22. If behavior is uncertain, be conservative. Do not invent behavior unsupported by README, AST, source, or tests.
23. Project directory tree rules:
   - The tree must use box-drawing vertical lines like `├──`, `│`, and `└──`, and must be rooted at `workspace/`, not `/workspace`.
   - Use Stage2 `implementation_tree` as the source of truth for this tree.
   - Do not include any path under `tests/` or `test/`.
   - Do not include root-level test modules or test helpers such as `test.py`, `test_*.py`, `*_test.py`, `tests.py`, `conftest.py`, `test.sh`, `run-tests.sh`, or fixture/support files whose purpose is only testing.
   - Do not include test asset names such as `requirements-test.txt`, `test-requirements.txt`, `test-data/`, `manual-tests/`, `JSON-Schema-Test-Suite/`, `python-tests.yml`, `tests.rst`, or any directory/file whose path component clearly means test, tests, or testing.
   - Do not include `/tests`, `/testbed`, verifier files, reference tests, hidden tests, or any generated evaluation harness content.
   - It is acceptable to mention test-derived behavior in `Detailed Function Implementation Nodes`, but never as files in `Project Directory Structure`.
24. Keep the tone concrete and implementation-oriented. Avoid invented product claims, broad speculation, or behavior not grounded in repo files, tests, docs, Agent1 env_spec, or Stage2 AST.
25. The document must be self-contained enough for reconstructing the whole project in `/workspace`, but it must not copy full source implementation bodies.
26. Mention dependency/runtime facts only when they are visible from project files, Agent1 env_spec, or test/oracle commands.

After writing the file, output JSON only:

```json
{
  "status": "stage3_passed",
  "project_name": "short-python-project-name",
  "start_md_path": "stage3/start.md",
  "core_api_count": 25,
  "node_count": 8,
  "notes": "short summary"
}
```

If you cannot produce a usable start.md, still write the best diagnostic JSON with `"status": "stage3_failed"` and a clear `notes` value.
