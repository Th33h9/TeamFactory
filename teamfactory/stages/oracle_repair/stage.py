from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from string import Template
from typing import Any

from teamfactory.remote import q, run_remote
from teamfactory.stages.instance_tuning.contracts import extract_json_object
from teamfactory.stages.instance_tuning.image_repair import (
    InstanceRepairCommitter,
    RepairTransaction,
)
from teamfactory.stages.instance_tuning.remote_agent import RemoteMaintenanceAgent

from .contracts import (
    OracleRepairResult,
    looks_transient,
    oracle_passed,
    oracle_score,
    validate_repair_result,
)
from .evidence import build_oracle_evidence
from .oracle_runner import OracleHarborRunner
from .validation import validate_oracle_candidate


ROOT = Path("/volume/pt-coder/users/kka/TeamFactory")
DEFAULT_DATASET = Path("/volume/pt-coder/users/kka/harbor/datasets/TeamFactory0713")
DEFAULT_HYPERDISTILL = Path("/volume/pt-coder/users/kka/Hyperdistill")
DEFAULT_TOKEN_SOURCE = Path(
    "/volume/pt-coder/users/kka/Hyperdistill/runs/"
    "NLFactory3-smoke10-validtar-cc-sidecar-gpt54ppio-w10-0708-0708-153410/launch.sh"
)
PROMPT_PATH = Path(__file__).with_name("prompt.md")
FINAL_STATES = {
    "oracle_pass",
    "repaired",
    "unrepairable",
    "repair_rejected",
    "infrastructure_error",
    "error",
}
REPAIR_RESUME_STATES = {
    "queued_repair",
    "repair_uploading",
    "repair_agent_running",
    "queued_finalize",
    "building_repaired_image",
    "prepared_repair",
    "committed_pending_recheck",
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def result_for_repair_round(item_dir: Path, round_number: int) -> dict[str, Any] | None:
    candidates = [
        item_dir / "evidence" / f"round-{round_number}" / "oracle_result.json"
    ]
    if round_number > 1:
        candidates.extend(
            [
                item_dir
                / "oracle"
                / f"recheck-{round_number - 1}"
                / "result.json",
                item_dir
                / "evidence"
                / f"round-{round_number - 1}"
                / "oracle_result.json",
            ]
        )
    else:
        candidates.extend(
            sorted(
                (item_dir / "oracle").glob("initial-*/result.json"),
                reverse=True,
            )
        )
    for path in candidates:
        value = read_json(path)
        if isinstance(value, dict):
            return value
    return None


def resolve_api_key(args: argparse.Namespace) -> str:
    direct = str(
        args.api_key
        or os.environ.get("TEAMFACTORY_ORACLE_REPAIR_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("TEAMFACTORY_API_KEY")
        or ""
    ).strip()
    if direct:
        return direct
    if args.api_key_file and Path(args.api_key_file).is_file():
        return Path(args.api_key_file).read_text(encoding="utf-8").strip()
    source = Path(args.token_source)
    if source.is_file():
        match = re.search(
            r'^export\s+ANTHROPIC_AUTH_TOKEN=["\']([^"\']+)',
            source.read_text(encoding="utf-8", errors="replace"),
            re.M,
        )
        if match:
            return match.group(1).strip()
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Harbor oracle checks and repair only failing TeamFactory instances."
    )
    parser.add_argument("--dataset-root", default=str(DEFAULT_DATASET))
    parser.add_argument("--hyperdistill-root", default=str(DEFAULT_HYPERDISTILL))
    parser.add_argument("--run-name", default="")
    parser.add_argument("--oracle-workers", type=int, default=10)
    parser.add_argument("--repair-workers", type=int, default=10)
    parser.add_argument("--finalize-workers", type=int, default=4)
    parser.add_argument("--max-repair-rounds", type=int, default=3)
    parser.add_argument("--oracle-infra-retries", type=int, default=2)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--instance", action="append", default=[])
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--plan-only", action="store_true")

    parser.add_argument("--remote-host", default="10.161.41.53")
    parser.add_argument("--remote-user", default="root")
    parser.add_argument("--remote-docker-host", default="tcp://10.161.41.53:60001")
    parser.add_argument("--remote-work-root", default="/tmp/kka_teamfactory_oracle_repair")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-connect-timeout", type=int, default=15)
    parser.add_argument(
        "--ssh-pass-file",
        default="/volume/pt-coder/users/kka/instancehelper/.sshpass",
    )
    parser.add_argument(
        "--claude-bin",
        default=(
            "/shared/users/kka/human-intelligence/tb-harbor-taskgen/"
            "cc-binary/claude-2.1.169-linux-x64"
        ),
    )
    parser.add_argument("--model", default="claude-sonnet-4-6-ppio")
    parser.add_argument("--api-base", default="http://llm-sidecar.iquest-inner.com:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-file", default="")
    parser.add_argument("--token-source", default=str(DEFAULT_TOKEN_SOURCE))

    parser.add_argument(
        "--harbour-python",
        default="/volume/pt-coder/users/kka/harbor/.venv/bin/python",
    )
    parser.add_argument(
        "--harbour-src-dir",
        default="/volume/pt-coder/users/kka/harbor/src",
    )
    parser.add_argument("--harbour-timeout", type=int, default=7200)
    parser.add_argument("--harbour-timeout-multiplier", type=float, default=3.0)
    parser.add_argument("--claude-api-timeout-ms", type=int, default=1200000)
    parser.add_argument("--claude-max-retries", type=int, default=20)
    parser.add_argument("--claude-stream-idle-timeout-ms", type=int, default=600000)
    parser.add_argument("--maintenance-timeout", type=int, default=7200)
    parser.add_argument("--transfer-timeout", type=int, default=3600)
    parser.add_argument("--image-rebuild-timeout", type=int, default=3600)
    parser.add_argument("--stage3-timeout", type=int, default=1800)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.dataset_root = str(Path(args.dataset_root).resolve())
    args.hyperdistill_root = str(Path(args.hyperdistill_root).resolve())
    for field in ("oracle_workers", "repair_workers", "finalize_workers"):
        setattr(args, field, max(1, int(getattr(args, field))))
    args.max_repair_rounds = max(1, int(args.max_repair_rounds))
    args.oracle_infra_retries = max(0, int(args.oracle_infra_retries))
    args.start_index = max(0, int(args.start_index))
    args.limit = max(0, int(args.limit))
    if not args.run_name:
        args.run_name = time.strftime("TeamFactory0713-oracle-repair-sonnet46-w10-%Y%m%d-%H%M%S")
    key = resolve_api_key(args)
    if not args.plan_only and not key:
        raise ValueError("missing Sidecar API key for oracle repair agents")
    # The shared maintenance/commit components use the Agent2-shaped attributes.
    args.agent2_api_key = key
    args.agent2_model = args.model
    return args


def discover_instances(args: argparse.Namespace) -> list[Path]:
    root = Path(args.dataset_root)
    if args.instance:
        values = [root / name for name in sorted(set(args.instance))]
    else:
        values = sorted(
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and path.name not in {"manifests", "reports"}
            and ".tuning-" not in path.name
        )
    values = values[args.start_index :]
    if args.limit:
        values = values[: args.limit]
    required = (
        "task.toml",
        "environment/Dockerfile",
        "environment/start.md",
        "instruction.md",
        "solution/solve.sh",
        "tests/config.json",
        "tests/test.sh",
    )
    invalid = [
        (path.name, [relative for relative in required if not (path / relative).is_file()])
        for path in values
    ]
    invalid = [row for row in invalid if row[1]]
    if invalid:
        raise ValueError(f"selected dataset contains incomplete instances: {invalid[:10]}")
    return values


def tx_to_json(tx: RepairTransaction) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in asdict(tx).items()
    }


def tx_from_json(value: dict[str, Any]) -> RepairTransaction:
    copied = dict(value)
    for key in ("instance_dir", "local_backup", "local_new"):
        copied[key] = Path(copied[key])
    return RepairTransaction(**copied)


class OracleRepairPipeline:
    def __init__(self, args: argparse.Namespace, instances: list[Path]) -> None:
        self.args = args
        self.instances = instances
        self.run_dir = Path(args.hyperdistill_root) / "runs" / args.run_name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.probe_queue: asyncio.Queue[Path | None] = asyncio.Queue()
        self.repair_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.finalize_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.runner = OracleHarborRunner(args, self.run_dir)
        self.remote = RemoteMaintenanceAgent(
            args, self.run_dir / "repair_trajectories.jsonl"
        )
        self.committer = InstanceRepairCommitter(args, args.run_name)
        self.state_lock = asyncio.Lock()
        self.done = asyncio.Event()
        self.terminal: set[str] = set()
        self.remaining = len(instances)
        self.counters: dict[str, int] = {"total": len(instances), "finished": 0}
        for instance in instances:
            status = str(read_json(self.state_path(instance.name), {}).get("status") or "")
            if status:
                self.counters[status] = self.counters.get(status, 0) + 1

    def item_dir(self, task_id: str) -> Path:
        path = self.run_dir / "items" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def state_path(self, task_id: str) -> Path:
        return self.item_dir(task_id) / "state.json"

    async def set_state(
        self,
        task_id: str,
        status: str,
        *,
        announce: bool = True,
        **fields: Any,
    ) -> None:
        row = {"task_id": task_id, "status": status, "updated_at": time.time(), **fields}
        async with self.state_lock:
            previous = str(read_json(self.state_path(task_id), {}).get("status") or "")
            atomic_json(self.state_path(task_id), row)
            if previous:
                self.counters[previous] = max(0, self.counters.get(previous, 0) - 1)
            self.counters[status] = self.counters.get(status, 0) + 1
            self.counters["finished"] = len(self.terminal)
            atomic_json(self.run_dir / "summary.json", self.counters)
        if announce:
            print(
                f"[{len(self.terminal)}/{len(self.instances)}] {task_id}: {status}",
                flush=True,
            )

    async def finish(self, task_id: str, status: str, **fields: Any) -> None:
        await self.set_state(task_id, status, **fields)
        async with self.state_lock:
            if task_id not in self.terminal:
                self.terminal.add(task_id)
                self.remaining -= 1
            self.counters["finished"] = len(self.terminal)
            atomic_json(self.run_dir / "summary.json", self.counters)
            if self.remaining == 0:
                self.done.set()

    async def process_probe(self, instance: Path) -> None:
        task_id = instance.name
        try:
            result: dict[str, Any] = {}
            for attempt in range(self.args.oracle_infra_retries + 1):
                await self.set_state(task_id, "oracle_probing", attempt=attempt + 1)
                result = await self.runner.evaluate(
                    instance, attempt=f"initial-{attempt + 1}"
                )
                if not looks_transient(result):
                    break
                if attempt < self.args.oracle_infra_retries:
                    await asyncio.sleep(min(30, 5 * (attempt + 1)))
            if looks_transient(result):
                await self.finish(
                    task_id,
                    "infrastructure_error",
                    score=oracle_score(result),
                    exception=result.get("exception_info"),
                )
            elif oracle_passed(result):
                await self.finish(task_id, "oracle_pass", score=oracle_score(result))
            else:
                await self.set_state(
                    task_id, "queued_repair", score=oracle_score(result), round=1
                )
                await self.repair_queue.put(
                    {"instance": instance, "result": result, "round": 1, "feedback": ""}
                )
        except Exception as exc:
            await self.finish(task_id, "error", phase="oracle_probe", error=repr(exc))

    async def _retry_or_finish(
        self,
        instance: Path,
        result: dict[str, Any],
        round_number: int,
        feedback: str,
    ) -> None:
        if round_number < self.args.max_repair_rounds:
            next_round = round_number + 1
            await self.set_state(
                instance.name,
                "queued_repair",
                score=oracle_score(result),
                round=next_round,
                feedback=feedback[-3000:],
            )
            await self.repair_queue.put(
                {
                    "instance": instance,
                    "result": result,
                    "round": next_round,
                    "feedback": feedback,
                }
            )
        else:
            await self.finish(
                instance.name,
                "repair_rejected",
                score=oracle_score(result),
                rounds=round_number,
                reason=feedback[-3000:],
            )

    async def process_repair(self, work: dict[str, Any]) -> None:
        instance: Path = work["instance"]
        result: dict[str, Any] = work["result"]
        round_number = int(work["round"])
        feedback = str(work.get("feedback") or "")
        task_id = instance.name
        item_dir = self.item_dir(task_id)
        evidence = build_oracle_evidence(
            result,
            item_dir / "evidence" / f"round-{round_number}",
            feedback=feedback,
            prior_repair_results=sorted(
                path
                for path in item_dir.glob("repair_result.round-*.json")
                if int(path.stem.rsplit("-", 1)[-1]) < round_number
            ),
        )
        remote_bundle = (
            f"{self.args.remote_work_root.rstrip('/')}/{self.args.run_name}/"
            f"{task_id}/round-{round_number}/bundle"
        )
        download_root = item_dir / "candidates" / f"round-{round_number}"
        handed_to_finalize = False
        try:
            await self.set_state(task_id, "repair_uploading", round=round_number)
            await asyncio.to_thread(
                self.remote.upload_instance_bundle,
                instance,
                evidence,
                remote_bundle,
                item_dir / "scratch" / f"round-{round_number}",
            )
            result_path = f"{remote_bundle}/oracle_repair_result.json"
            init = await asyncio.to_thread(
                run_remote,
                self.args,
                f"printf '{{}}\\n' > {q(result_path)}",
                timeout=120,
            )
            if init.returncode != 0:
                raise RuntimeError(f"failed to initialize repair result: {init.stdout[-1000:]}")
            prompt = Template(PROMPT_PATH.read_text(encoding="utf-8")).safe_substitute(
                instance_path=f"{remote_bundle}/instance",
                evidence_path=f"{remote_bundle}/evidence",
                result_path=result_path,
            )
            await self.set_state(task_id, "repair_agent_running", round=round_number)
            try:
                raw = await asyncio.to_thread(
                    self.remote.run_json_turn,
                    prompt,
                    task_id=task_id,
                    phase=f"oracle_repair_round_{round_number}",
                    remote_cwd=remote_bundle,
                    tools="Read,Write,Edit,Glob,Grep,Bash",
                )
            except ValueError as response_error:
                remote_result = await asyncio.to_thread(
                    run_remote, self.args, f"cat {q(result_path)}", timeout=120
                )
                if remote_result.returncode != 0:
                    raise response_error
                raw = extract_json_object(remote_result.stdout)
            repair = validate_repair_result(raw)
            atomic_json(item_dir / f"repair_result.round-{round_number}.json", repair.raw)
            if not repair.repaired:
                await self.finish(
                    task_id,
                    "unrepairable",
                    score=oracle_score(result),
                    rounds=round_number,
                    responsible_stage=repair.responsible_stage,
                    root_cause=repair.root_cause,
                    diagnosis=repair.diagnosis,
                )
                return

            await asyncio.to_thread(self.remote.download_bundle, remote_bundle, download_root)
            downloaded = download_root / "bundle"
            candidate = downloaded / "instance"
            on_disk = validate_repair_result(
                read_json(downloaded / "oracle_repair_result.json")
            )
            if on_disk.raw != repair.raw:
                raise ValueError("result file differs from the agent final response")
            trusted_oracle_results = [
                value
                for path in sorted((item_dir / "oracle").glob("*/result.json"))
                if isinstance((value := read_json(path)), dict)
            ]
            changed = validate_oracle_candidate(
                instance,
                candidate,
                repair.changed_files,
                repair.image_commands,
                result,
                trusted_oracle_results,
            )
            atomic_json(
                item_dir / f"validated_repair.round-{round_number}.json",
                {**repair.raw, "validated_changes": sorted(changed)},
            )
            await self.set_state(
                task_id,
                "queued_finalize",
                round=round_number,
                responsible_stage=repair.responsible_stage,
            )
            await self.finalize_queue.put(
                {
                    "instance": instance,
                    "initial_result": result,
                    "round": round_number,
                    "repair": repair,
                    "candidate": candidate,
                    "remote_bundle": remote_bundle,
                    "download_root": download_root,
                }
            )
            handed_to_finalize = True
        except Exception as exc:
            await self._retry_or_finish(
                instance,
                result,
                round_number,
                f"repair agent or validation failed: {exc!r}",
            )
        finally:
            if not handed_to_finalize:
                await asyncio.to_thread(self.remote.cleanup_remote, remote_bundle)
                if download_root.exists():
                    shutil.rmtree(download_root, ignore_errors=True)

    async def process_finalize(self, work: dict[str, Any]) -> None:
        instance: Path = work["instance"]
        initial_result: dict[str, Any] = work["initial_result"]
        round_number = int(work["round"])
        repair: OracleRepairResult = work["repair"]
        candidate: Path = work["candidate"]
        remote_bundle = str(work["remote_bundle"])
        download_root: Path = work["download_root"]
        task_id = instance.name
        tx: RepairTransaction | None = None
        try:
            await self.set_state(task_id, "building_repaired_image", round=round_number)
            tx = await asyncio.to_thread(
                self.committer.prepare_remote_image,
                instance,
                remote_bundle,
                repair.image_commands,
            )
            await self.set_state(
                task_id,
                "prepared_repair",
                round=round_number,
                transaction=tx_to_json(tx),
            )
            await asyncio.to_thread(self.committer.commit, tx, candidate)
            await self.set_state(
                task_id,
                "committed_pending_recheck",
                round=round_number,
                transaction=tx_to_json(tx),
            )
            recheck = await self.runner.evaluate(
                instance, attempt=f"recheck-{round_number}"
            )
            if oracle_passed(recheck):
                await asyncio.to_thread(self.committer.finalize, tx)
                tx = None
                await self.finish(
                    task_id,
                    "repaired",
                    score=oracle_score(recheck),
                    rounds=round_number,
                    responsible_stage=repair.responsible_stage,
                    root_cause=repair.root_cause,
                )
            else:
                await asyncio.to_thread(self.committer.rollback, tx)
                tx = None
                await self._retry_or_finish(
                    instance,
                    recheck,
                    round_number,
                    (
                        "The proposed repair was transactionally applied, but the canonical "
                        f"oracle recheck still failed with score={oracle_score(recheck)!r}. "
                        "Read the new verifier evidence and choose a different minimal repair."
                    ),
                )
        except Exception as exc:
            if tx is not None:
                try:
                    await asyncio.to_thread(self.committer.rollback, tx)
                except Exception:
                    pass
            await self._retry_or_finish(
                instance,
                initial_result,
                round_number,
                f"transactional finalize or oracle recheck failed: {exc!r}",
            )
        finally:
            await asyncio.to_thread(self.remote.cleanup_remote, remote_bundle)
            if download_root.exists():
                shutil.rmtree(download_root, ignore_errors=True)

    async def probe_worker(self) -> None:
        while True:
            instance = await self.probe_queue.get()
            try:
                if instance is None:
                    return
                await self.process_probe(instance)
            finally:
                self.probe_queue.task_done()

    async def repair_worker(self) -> None:
        while True:
            work = await self.repair_queue.get()
            try:
                if work is None:
                    return
                await self.process_repair(work)
            finally:
                self.repair_queue.task_done()

    async def finalize_worker(self) -> None:
        while True:
            work = await self.finalize_queue.get()
            try:
                if work is None:
                    return
                await self.process_finalize(work)
            finally:
                self.finalize_queue.task_done()

    async def run(self) -> None:
        atomic_json(
            self.run_dir / "run_config.json",
            {
                **{
                    key: value
                    for key, value in vars(self.args).items()
                    if "api_key" not in key
                },
                "api_key": "<redacted>",
                "instance_count": len(self.instances),
                "instances": [instance.name for instance in self.instances],
            },
        )
        (self.run_dir / "cases.jsonl").write_text(
            "".join(
                json.dumps({"case_path": str(instance)}, ensure_ascii=False) + "\n"
                for instance in self.instances
            ),
            encoding="utf-8",
        )

        probe_workers = [
            asyncio.create_task(self.probe_worker())
            for _ in range(self.args.oracle_workers)
        ]
        repair_workers = [
            asyncio.create_task(self.repair_worker())
            for _ in range(self.args.repair_workers)
        ]
        finalize_workers = [
            asyncio.create_task(self.finalize_worker())
            for _ in range(self.args.finalize_workers)
        ]

        for instance in self.instances:
            state = read_json(self.state_path(instance.name), {}) if not self.args.no_resume else {}
            status = str(state.get("status") or "")
            if status == "repair_rejected":
                completed_rounds = max(1, int(state.get("rounds") or 1))
                if completed_rounds < self.args.max_repair_rounds:
                    next_round = completed_rounds + 1
                    result = result_for_repair_round(
                        self.item_dir(instance.name), next_round
                    )
                    if result is not None:
                        feedback = str(state.get("reason") or "")
                        await self.set_state(
                            instance.name,
                            "queued_repair",
                            announce=False,
                            score=oracle_score(result),
                            round=next_round,
                            feedback=feedback[-3000:],
                        )
                        await self.repair_queue.put(
                            {
                                "instance": instance,
                                "result": result,
                                "round": next_round,
                                "feedback": feedback,
                            }
                        )
                        continue
            if status in FINAL_STATES:
                self.terminal.add(instance.name)
                self.remaining -= 1
                print(f"[resume] {instance.name}: {status}", flush=True)
                continue
            if status in {"prepared_repair", "committed_pending_recheck"} and state.get("transaction"):
                try:
                    await asyncio.to_thread(
                        self.committer.rollback, tx_from_json(state["transaction"])
                    )
                except Exception as exc:
                    await self.finish(
                        instance.name,
                        "error",
                        phase="resume_rollback",
                        error=repr(exc),
                    )
                    continue
            if status in REPAIR_RESUME_STATES:
                round_number = max(1, int(state.get("round") or 1))
                result = result_for_repair_round(
                    self.item_dir(instance.name), round_number
                )
                if result is not None:
                    feedback = str(state.get("feedback") or "")
                    await self.set_state(
                        instance.name,
                        "queued_repair",
                        announce=False,
                        score=oracle_score(result),
                        round=round_number,
                        feedback=feedback[-3000:],
                    )
                    await self.repair_queue.put(
                        {
                            "instance": instance,
                            "result": result,
                            "round": round_number,
                            "feedback": feedback,
                        }
                    )
                    continue
            await self.set_state(instance.name, "queued_probe", announce=False)
            await self.probe_queue.put(instance)

        if self.remaining == 0:
            self.done.set()
        self.counters["finished"] = len(self.terminal)
        atomic_json(self.run_dir / "summary.json", self.counters)
        await self.done.wait()
        await self.probe_queue.join()
        await self.repair_queue.join()
        await self.finalize_queue.join()

        for _ in probe_workers:
            await self.probe_queue.put(None)
        for _ in repair_workers:
            await self.repair_queue.put(None)
        for _ in finalize_workers:
            await self.finalize_queue.put(None)
        await self.probe_queue.join()
        await self.repair_queue.join()
        await self.finalize_queue.join()
        await asyncio.gather(*probe_workers, *repair_workers, *finalize_workers)


def main(argv: list[str] | None = None) -> int:
    args = normalize_args(build_parser().parse_args(argv))
    instances = discover_instances(args)
    print(f"dataset={args.dataset_root}")
    print(
        f"instances={len(instances)} oracle={args.oracle_workers} "
        f"repair={args.repair_workers}x{args.model} finalize={args.finalize_workers}",
        flush=True,
    )
    if args.plan_only:
        print("plan-only: no oracle checks or repair agents were started")
        return 0
    pipeline = OracleRepairPipeline(args, instances)
    print(f"run_dir={pipeline.run_dir}", flush=True)
    asyncio.run(pipeline.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
