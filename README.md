# TeamFactory

TeamFactory is an independent GitHub-URL-to-Harbor-instance pipeline. It does
not import or depend on NLFactory, NLFactory2, or NLFactory3 code.

Current scope:

- Agent1: clone a GitHub repository on the remote physical machine, create or
  reuse a Docker environment, install dependencies, and run the repository's
  own oracle tests inside Docker.
- Stage2 AST: deterministically scan the repository for public APIs, signatures,
  docstrings, calls, raises, project tree, and test count. This stage does not
  use a model.
- Agent2/Stage3: generate an NLFactory/NL2RepoBench-style `start.md`, then
  materialize a Harbor-format instance and save the final Docker image tar.
- Oracle repair gate: run the canonical Harbor oracle after materialization;
  diagnose failures, route them to the owning generation stage, apply a
  transactional instance repair, and require a passing recheck.

## Pipeline

```text
repo jsonl
  -> router assigns each URL to the lowest-load lane
  -> Agent1 starts one remote Claude Code agent
  -> Agent1 clones the repo, solves Docker/env, and runs oracle tests
  -> Stage2 scans source/tests with deterministic AST extraction
  -> Agent2 reads repo + AST inventory and writes environment/start.md
  -> materializer writes Harbor files locally
  -> materializer builds the final remote Docker image and saves <task_id>.tar
  -> canonical Harbor oracle probe
  -> failing instances are repaired for agent1, stage2_ast, or agent2_stage3
  -> transactional oracle recheck
```

The model-backed generation and repair stages keep their prompts in stage folders:

```text
teamfactory/stages/agent1/prompt.md
teamfactory/stages/agent2_stage3/prompt.md
teamfactory/stages/oracle_repair/prompt.md
```

`Agent2` is prompted to produce `start.md` in NLFactory/NL2RepoBench style:

- Core API entries expose signatures, parameters, return behavior, examples,
  and class variables/method names without copying implementation bodies.
- Detailed nodes use `### Node N: ...` headings.
- Each node contains only `Function Description`, `Handling Strategy`, and
  `Input and Output Examples`.
- Node examples must be mini test-specs: concrete normal cases, edge/error
  cases when visible, and observable output/state/file/CLI behavior.

## Concurrency Model

`tp` is the number of lanes. Each URL item is assigned dynamically to the
lowest-load lane; input is not pre-sharded.

`pp` controls how many queued/active items a lane may hold beyond the currently
running stage slot. In code, lane capacity is `pp + 1`.

Within one lane, the same stage runs at most one item at a time, while different
stages can process different items in the same lane. Stage-level global limits
are controlled by:

- `--agent1-concurrency`
- `--agent2-concurrency`
- `--oracle-concurrency`

Stage2 has no model call and currently has no explicit global concurrency cap.
The oracle repair gate is enabled by default. `--skip-oracle-repair` is intended
only for generation debugging; `--oracle-max-repair-rounds` bounds the repair
loop.

## Quick Run

```bash
cd /volume/pt-coder/users/kka/TeamFactory

TEAMFACTORY_API_KEY='YOUR_KEY' \
./scripts/run_teamfactory_agent1.sh \
  --repo-jsonl /volume/pt-coder/users/kka/repo_candidates/github_nl2repo_like_candidates_500.jsonl \
  --limit 16 \
  --tp 4 \
  --pp 4 \
  --concurrency 4 \
  --agent1-concurrency 4 \
  --agent2-concurrency 4 \
  --oracle-concurrency 4 \
  --dataset-root /volume/pt-coder/users/kka/harbor/datasets/TeamFactory \
  --remote-work-root /tmp/kka_TeamFactory_smoke_stage3_tp4pp4 \
  --remote-image-root /shared/users/kka/TeamFactory_images \
  --agent1-model gpt-5.4-ppio \
  --agent2-model gpt-5.4-ppio \
  --oracle-repair-model claude-sonnet-4-6-ppio
```

All three stages share `TEAMFACTORY_API_KEY`, while their models are selected
independently. The legacy `--model NAME` option remains available and overrides
both `--agent1-model` and `--agent2-model`; it does not override the oracle
repair model.

Useful defaults:

```text
remote host: 10.161.41.53
remote user: root
remote work root: /tmp/kka_TeamFactory_agent1
remote image root: /shared/users/kka/TeamFactory_images
claude binary: /shared/users/kka/human-intelligence/tb-harbor-taskgen/cc-binary/claude-2.1.169-linux-x64
sidecar base: http://127.0.0.1:5005
default model: gpt-5.4-ppio
```

`--validate-start-md` is optional and disabled by default. When enabled, Stage3
will reject generated `start.md` files that violate the NLFactory-style heading
and node-field constraints.

## Inputs

`--repo-jsonl` is a JSONL file. Each line must contain a repository URL:

```json
{"url": "https://github.com/owner/repo"}
```

Use `--start-index` to resume from a later line and `--limit` to cap how many
records are read.

## Outputs

Per item local work files:

```text
<work-dir>/items/<task_id>/agent1.json
<work-dir>/items/<task_id>/stage2_ast.json
<work-dir>/items/<task_id>/stage2_ast_payload.json
<work-dir>/items/<task_id>/agent2_stage3.json
```

Run-level files:

```text
<run-dir>/<run_id>/checkpoint.json
<run-dir>/<run_id>/pipeline_events.jsonl
<run-dir>/<run_id>/stage_events.jsonl
<output>
<error-output>
<trajectory-output>
```

Final Harbor instance:

```text
<dataset-root>/<task_id>/
├── environment/
│   ├── Dockerfile
│   └── start.md
├── instruction.md
├── solution/
│   ├── oracle/
│   └── solve.sh
├── task.toml
└── tests/
    ├── config.json
    ├── reference/
    └── test.sh
```

Final remote image archive:

```text
<remote-image-root>/<task_id>.tar
```

## Stage Contracts

Agent1 output is `teamfactory.agent1.v1`. It records the Docker image,
environment commands, test commands, package/test/fixture files, and oracle test
result.

Stage2 output is `teamfactory.stage2_ast.v1`. It records public classes,
methods, functions, signatures, docstrings, return hints, raises, calls, project
tree, approximate test count, and parse errors.

Agent2/Stage3 output is `agent2_stage3.json`. It records whether `start.md` was
generated and whether Harbor materialization plus image archive creation
succeeded.

The post-Stage3 oracle gate writes `oracle_repair.json`. A clean canonical run
records `oracle_passed`; an accepted transactional repair records
`oracle_repaired` together with the responsible generation stage and the
standalone oracle-repair run directory. Other terminal states fail the item.

See `流程原理.md` for the Chinese explanation of the pipeline.

## Instance Tuning Pipeline

`teamfactory/stages/instance_tuning/` uses two independent streaming pools. The
16-worker Agent1 pool runs real Harbor solutions with Claude Code and
`claude-sonnet-4-6-ppio`; zero/no-reward results flow into a separate
16-worker Agent2 pool using Claude Code and `claude-opus-4-8-ppio` for combined
diagnosis and evidence-backed repair. Mechanical image build/recheck work is
separated from both model pools. Repairs are transactional, preserve the
existing `start.md` document format, reject test weakening and answer leakage,
and are retained only after a score-improving recheck. See
`teamfactory/stages/instance_tuning/README.md` and run with:

```bash
ANTHROPIC_AUTH_TOKEN='...' ./scripts/run_instance_tuning.sh
```
