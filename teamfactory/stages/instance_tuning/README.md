# Instance Tuning Stage

This stage is a streaming two-agent-pool quality pipeline:

1. Agent1 has 16 independent Hyperdistill Harbor workers. Each worker uses
   Claude Code with `claude-sonnet-4-6-ppio` to solve one instance for real.
2. Positive-score cases finish immediately. Zero/no-reward cases enter the
   Agent2 queue without blocking Agent1 from taking another instance.
3. Agent2 has 16 independent remote workers. Each worker uses Claude Code with
   `claude-opus-4-8-ppio` to diagnose the evidence and, in the same session,
   repair only a proven high-confidence `instance_error`.
4. A separate four-worker mechanical finalizer builds repaired images, runs the
   leak gate, commits transactions, and requests a score recheck. Rechecks share
   Agent1's 16-evaluation capacity so Harbor concurrency stays bounded.

`agent_capability`, transient infrastructure failures, inconclusive cases, and
low-confidence diagnoses never modify the dataset.

Agent2 can edit any instance path when evidence supports the change. Acceptance
is result-based instead of path-whitelist-based: the candidate is rejected if it
changes the NL2Repo structure of `environment/start.md`, decreases reference
test files/functions/assertions, removes configured tests or commands, changes
the image archive identity, introduces agent-visible oracle/test content, or
causes the rebuilt image leak check to fail. `start.md` content may be corrected,
but its section order, heading conventions, Core API numbering style, Node style,
and Node fields must remain intact.

Repairs are transactional. The candidate instance and image tar are committed,
then evaluated again. The transaction is retained only when the score improves,
or when an initial no-reward infrastructure failure becomes a valid scored run.
Otherwise both the instance directory and image tar are rolled back.

Run all instances with the default 16 Agent1 and 16 Agent2 workers:

```bash
cd /volume/pt-coder/users/kka/TeamFactory
ANTHROPIC_AUTH_TOKEN='...' ./scripts/run_instance_tuning.sh
```

Plan or smoke-test selection:

```bash
./scripts/run_instance_tuning.sh --plan-only
./scripts/run_instance_tuning.sh --limit 2 --run-name tuning-smoke2
./scripts/run_instance_tuning.sh --instance github-owner-repo__abcdef0
./scripts/run_instance_tuning.sh --agent1-workers 16 --agent2-workers 16 --finalize-workers 4
```

All Harbor jobs, ATIF trajectories, Agent2 events/results, repair reports,
diffs, scores, queue states, and resumable item state are written below:

```text
/volume/pt-coder/users/kka/Hyperdistill/runs/<run-name>/
```
