from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

from .contracts import oracle_score


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class OracleHarborRunner:
    """Run the canonical solution and verifier without modifying the task copy."""

    def __init__(self, args: Any, run_dir: Path) -> None:
        self.args = args
        self.run_dir = run_dir
        self.capacity = asyncio.Semaphore(args.oracle_workers)
        self.results_lock = asyncio.Lock()
        sys.path.insert(0, str(Path(args.hyperdistill_root)))
        from hyperdistill.backends.harbour_backend import HarbourBackend
        from hyperdistill.tasks.harbour_eval import HarbourEvalTask

        class CleanOracleBackend(HarbourBackend):
            def _prepare_case(self, case_path: str) -> str:
                target = Path(tempfile.gettempdir()) / f"harbouroracle{uuid.uuid4().hex[:12]}"
                shutil.copytree(case_path, target, symlinks=True, ignore_dangling_symlinks=True)
                return str(target)

        ssh_pass = Path(args.ssh_pass_file).read_text(encoding="utf-8").strip()
        extra_env = {
            "REMOTE_DOCKER_HOST": args.remote_docker_host,
            "DOCKER_HOST": args.remote_docker_host,
            "ROOT_NAME": f"{args.remote_user}@{args.remote_host}",
            "SSH_PASS": ssh_pass,
        }
        self.task = HarbourEvalTask()
        self.backend = CleanOracleBackend(
            harbour_python=args.harbour_python,
            harbour_src_dir=args.harbour_src_dir,
            agent_name="oracle",
            model_name=None,
            timeout=args.harbour_timeout,
            jobs_dir=str(run_dir / "oracle_jobs"),
            output_base_dir=None,
            extra_env=extra_env,
            force_build=False,
            timeout_multiplier=args.harbour_timeout_multiplier,
            harbour_extra_args=[
                "--yes",
                "--quiet",
                "--force-archive",
                "--cleanup-archive-source-image",
            ],
        )

    async def evaluate(self, instance: Path, *, attempt: str) -> dict[str, Any]:
        item = {"case_path": str(instance), "id": instance.name}
        async with self.capacity:
            try:
                content, thinking = await self.backend.call(dict(item), self.task)
                result = self.task.process_result(dict(item), content, thinking)
                if result is None:
                    raise RuntimeError("Harbor returned an unparsable oracle result")
            except Exception as exc:
                result = {
                    **item,
                    "task_name": instance.name,
                    "reward": None,
                    "trial_dir": None,
                    "exception_info": {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "exception_traceback": traceback.format_exc(),
                    },
                }
        result["attempt"] = attempt
        result["score"] = oracle_score(result)
        safe_attempt = attempt.replace("/", "-")
        output = self.run_dir / "items" / instance.name / "oracle" / safe_attempt / "result.json"
        _atomic_json(output, result)
        async with self.results_lock:
            with (self.run_dir / "oracle_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
        return result
