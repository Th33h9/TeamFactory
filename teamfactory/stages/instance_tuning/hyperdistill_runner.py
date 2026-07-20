from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .contracts import reward_value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


class HyperdistillHarborRunner:
    """Thin async adapter around Hyperdistill's native HarbourBackend."""

    def __init__(self, args: Any, run_dir: Path) -> None:
        self.args = args
        self.run_dir = run_dir
        self.results_lock = asyncio.Lock()
        self.capacity = asyncio.Semaphore(args.agent1_workers)
        sys.path.insert(0, str(Path(args.hyperdistill_root)))
        from hyperdistill.backends.harbour_backend import HarbourBackend
        from hyperdistill.tasks.harbour_eval import HarbourEvalTask

        self.task = HarbourEvalTask()
        extra_hosts = run_dir / "extra-hosts.yaml"
        extra_hosts.write_text(
            'services:\n  main:\n    extra_hosts:\n      - "llm-sidecar.iquest-inner.com:10.148.194.100"\n',
            encoding="utf-8",
        )
        ssh_pass = Path(args.ssh_pass_file).read_text(encoding="utf-8").strip()
        extra_env = {
            "ANTHROPIC_BASE_URL": args.api_base.rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": args.agent1_api_key,
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": args.agent1_model,
            "CLAUDE_CODE_OFFLINE_DIR": args.offline_agent_dir,
            "HARBOR_OFFLINE_AGENT_DIR": args.offline_agent_dir,
            "HARBOR_REQUIRE_OFFLINE_AGENT": "1",
            "CLAUDE_CODE_REQUIRE_OFFLINE": "1",
            "REMOTE_DOCKER_HOST": args.remote_docker_host,
            "DOCKER_HOST": args.remote_docker_host,
            "ROOT_NAME": f"{args.remote_user}@{args.remote_host}",
            "SSH_PASS": ssh_pass,
        }
        extra_args = [
            "--yes", "--quiet", "--force-archive", "--cleanup-archive-source-image",
            "--artifact", "/workspace", "--extra-docker-compose", str(extra_hosts),
            "--ae", f"API_TIMEOUT_MS={args.claude_api_timeout_ms}",
            "--ae", f"CLAUDE_CODE_MAX_RETRIES={args.claude_max_retries}",
            "--ae", f"CLAUDE_STREAM_IDLE_TIMEOUT_MS={args.claude_stream_idle_timeout_ms}",
            "--ae", "CLAUDE_ENABLE_STREAM_WATCHDOG=1",
            "--ae", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1",
            "--ak", f"settings_path={args.claude_settings}",
            "--ak", f"max_turns={args.max_turns}",
            "--ak", f"version={args.claude_version}",
            "--ak", "disallowed_tools=EnterPlanMode,ExitPlanMode",
        ]
        self.backend = HarbourBackend(
            harbour_python=args.harbour_python,
            harbour_src_dir=args.harbour_src_dir,
            agent_name="claude-code",
            model_name=args.agent1_model,
            timeout=args.harbour_timeout,
            jobs_dir=str(run_dir / "jobs"),
            output_base_dir=str(run_dir / "diffs"),
            extra_env=extra_env,
            force_build=False,
            timeout_multiplier=args.harbour_timeout_multiplier,
            harbour_extra_args=extra_args,
        )

    async def evaluate(self, instance: Path, *, attempt: str) -> dict[str, Any]:
        task_id = instance.name
        item = {"case_path": str(instance), "id": task_id}
        async with self.capacity:
            try:
                content, thinking = await self.backend.call(dict(item), self.task)
                result = self.task.process_result(dict(item), content, thinking)
                if result is None:
                    raise RuntimeError("Hyperdistill returned an unparsable result")
            except Exception as exc:
                result = {
                    **item,
                    "task_name": task_id,
                    "trajectory": None,
                    "reward": None,
                    "diff": "",
                    "diff_stat": "",
                    "changed_files": [],
                    "diff_dir": None,
                    "agent_result": None,
                    "verifier_result": None,
                    "timing": {},
                    "trial_dir": None,
                    "exception_info": {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "exception_traceback": traceback.format_exc(),
                    },
                }
        result["attempt"] = attempt
        result["score"] = reward_value(result.get("reward"))
        per_item = self.run_dir / "items" / task_id / "evaluations" / attempt / "result.json"
        _atomic_json(per_item, result)
        async with self.results_lock:
            output = self.run_dir / f"results.{attempt}.jsonl"
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
        return result
