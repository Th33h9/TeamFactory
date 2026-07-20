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

from .contracts import (
    RepairDecision,
    extract_json_object,
    is_repair_candidate,
    reward_value,
    score_improved,
    validate_agent2,
)
from .evidence import build_evidence
from .hyperdistill_runner import HyperdistillHarborRunner
from .image_repair import InstanceRepairCommitter, RepairTransaction
from .remote_agent import RemoteMaintenanceAgent
from .validation import validate_candidate


ROOT = Path("/volume/pt-coder/users/kka/TeamFactory")
DEFAULT_DATASET = Path("/volume/pt-coder/users/kka/harbor/datasets/TeamFactory0713")
DEFAULT_HYPERDISTILL = Path("/volume/pt-coder/users/kka/Hyperdistill")
DEFAULT_TOKEN_SOURCE = Path(
    "/volume/pt-coder/users/kka/Hyperdistill/runs/"
    "NLFactory3-smoke10-validtar-cc-sidecar-gpt54ppio-w10-0708-0708-153410/launch.sh"
)
AGENT2_PROMPT = Path(__file__).with_name("agent2_prompt.md")
FINAL_STATES = {
    "positive_score",
    "not_instance_error",
    "low_confidence_instance_error",
    "no_safe_repair",
    "repair_rejected",
    "repair_accepted",
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_api_key(args: argparse.Namespace, stage: str) -> str:
    direct = str(
        getattr(args, f"{stage}_api_key", "")
        or args.api_key
        or os.environ.get(f"TEAMFACTORY_{stage.upper()}_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("TEAMFACTORY_API_KEY")
        or ""
    ).strip()
    if direct:
        return direct
    key_file = getattr(args, f"{stage}_api_key_file", "") or args.api_key_file
    if key_file:
        path = Path(key_file)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    source = Path(args.token_source)
    if source.is_file():
        text = source.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^export\s+ANTHROPIC_AUTH_TOKEN=["\']([^"\']+)', text, re.M)
        if match:
            return match.group(1).strip()
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate TeamFactory instances with Hyperdistill and repair only instance defects."
    )
    parser.add_argument("--dataset-root", default=str(DEFAULT_DATASET))
    parser.add_argument("--hyperdistill-root", default=str(DEFAULT_HYPERDISTILL))
    parser.add_argument("--run-name", default="")
    parser.add_argument("--agent1-workers", type=int, default=16)
    parser.add_argument("--agent2-workers", type=int, default=16)
    parser.add_argument("--finalize-workers", type=int, default=4)
    parser.add_argument("--workers", type=int, default=0, help="legacy alias: set both worker pools")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--instance", action="append", default=[])
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--no-resume", action="store_true")

    parser.add_argument("--remote-host", default="10.161.41.53")
    parser.add_argument("--remote-user", default="root")
    parser.add_argument("--remote-docker-host", default="tcp://10.161.41.53:60001")
    parser.add_argument("--remote-work-root", default="/tmp/kka_teamfactory_instance_tuning")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-connect-timeout", type=int, default=15)
    parser.add_argument("--ssh-pass-file", default="/volume/pt-coder/users/kka/instancehelper/.sshpass")
    parser.add_argument("--claude-bin", default="/shared/users/kka/human-intelligence/tb-harbor-taskgen/cc-binary/claude-2.1.169-linux-x64")
    parser.add_argument("--agent1-model", default="claude-sonnet-4-6-ppio")
    parser.add_argument("--agent2-model", default="claude-opus-4-8-ppio")
    parser.add_argument("--model", default="", help="legacy alias: set both models")
    parser.add_argument("--api-base", default="http://llm-sidecar.iquest-inner.com:8000")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-file", default="")
    parser.add_argument("--agent1-api-key", default="")
    parser.add_argument("--agent1-api-key-file", default="")
    parser.add_argument("--agent2-api-key", default="")
    parser.add_argument("--agent2-api-key-file", default="")
    parser.add_argument("--token-source", default=str(DEFAULT_TOKEN_SOURCE))

    parser.add_argument("--harbour-python", default="/volume/pt-coder/users/kka/harbor/.venv/bin/python")
    parser.add_argument("--harbour-src-dir", default="/volume/pt-coder/users/kka/harbor/src")
    parser.add_argument("--harbour-timeout", type=int, default=43200)
    parser.add_argument("--harbour-timeout-multiplier", type=float, default=5.0)
    parser.add_argument("--claude-settings", default="/volume/pt-coder/users/kka/harbor-eval/runs/claude-settings.json")
    parser.add_argument("--claude-version", default="2.1.145")
    parser.add_argument("--offline-agent-dir", default="/mnt/local/envs")
    parser.add_argument("--max-turns", type=int, default=800)
    parser.add_argument("--claude-api-timeout-ms", type=int, default=1200000)
    parser.add_argument("--claude-max-retries", type=int, default=20)
    parser.add_argument("--claude-stream-idle-timeout-ms", type=int, default=600000)
    parser.add_argument("--maintenance-timeout", type=int, default=7200)
    parser.add_argument("--transfer-timeout", type=int, default=3600)
    parser.add_argument("--image-rebuild-timeout", type=int, default=3600)
    parser.add_argument("--stage3-timeout", type=int, default=1800)
    parser.add_argument("--item-timeout", type=int, default=3600)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.dataset_root = str(Path(args.dataset_root).resolve())
    args.hyperdistill_root = str(Path(args.hyperdistill_root).resolve())
    if args.workers:
        args.agent1_workers = args.workers
        args.agent2_workers = args.workers
    args.agent1_workers = max(1, int(args.agent1_workers))
    args.agent2_workers = max(1, int(args.agent2_workers))
    args.finalize_workers = max(1, int(args.finalize_workers))
    args.start_index = max(0, int(args.start_index))
    args.limit = max(0, int(args.limit))
    if args.model:
        args.agent1_model = args.model
        args.agent2_model = args.model
    args.agent1_api_key = resolve_api_key(args, "agent1")
    args.agent2_api_key = resolve_api_key(args, "agent2")
    if not args.run_name:
        args.run_name = time.strftime("TeamFactory0713-tuning-a1sonnet46-a2opus48-w16x16-%Y%m%d-%H%M%S")
    if not args.plan_only and (not args.agent1_api_key or not args.agent2_api_key):
        raise ValueError(
            "missing sidecar API key; configure agent1/agent2 keys or the shared --api-key"
        )
    return args


def discover_instances(args: argparse.Namespace) -> list[Path]:
    root = Path(args.dataset_root)
    if args.instance:
        requested = set(args.instance)
        values = [root / name for name in sorted(requested)]
    else:
        values = sorted(
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and ".tuning-" not in path.name
        )
    values = values[args.start_index:]
    if args.limit:
        values = values[: args.limit]
    required = ("task.toml", "environment/start.md", "instruction.md", "tests/test.sh", "tests/config.json")
    invalid = [(path.name, [rel for rel in required if not (path / rel).exists()]) for path in values]
    invalid = [item for item in invalid if item[1]]
    if invalid:
        raise ValueError(f"selected dataset contains incomplete instances: {invalid[:10]}")
    return values


def tx_to_json(tx: RepairTransaction) -> dict[str, Any]:
    return {key: str(value) if isinstance(value, Path) else value for key, value in asdict(tx).items()}


def tx_from_json(value: dict[str, Any]) -> RepairTransaction:
    copied = dict(value)
    for key in ("instance_dir", "local_backup", "local_new"):
        copied[key] = Path(copied[key])
    return RepairTransaction(**copied)


class InstanceTuningPipeline:
    def __init__(self, args: argparse.Namespace, instances: list[Path]) -> None:
        self.args = args
        self.instances = instances
        self.run_dir = Path(args.hyperdistill_root) / "runs" / args.run_name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "jobs").mkdir(exist_ok=True)
        self.agent1_queue: asyncio.Queue[Path | None] = asyncio.Queue()
        self.agent2_queue: asyncio.Queue[tuple[Path, dict[str, Any]] | None] = asyncio.Queue()
        self.finalize_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.runner = HyperdistillHarborRunner(args, self.run_dir)
        self.remote = RemoteMaintenanceAgent(args, self.run_dir / "maintenance_trajectories.jsonl")
        self.committer = InstanceRepairCommitter(args, args.run_name)
        self.summary_lock = asyncio.Lock()
        self.counters: dict[str, int] = {"total": len(instances), "finished": 0}
        for instance in instances:
            state = read_json(
                self.run_dir / "items" / instance.name / "state.json", {}
            )
            status = str(state.get("status") or "")
            if not status:
                continue
            self.counters[status] = self.counters.get(status, 0) + 1
            if status in FINAL_STATES or status == "error":
                self.counters["finished"] += 1

    def item_dir(self, task_id: str) -> Path:
        path = self.run_dir / "items" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def state_path(self, task_id: str) -> Path:
        return self.item_dir(task_id) / "state.json"

    async def set_state(self, task_id: str, status: str, **fields: Any) -> None:
        row = {"task_id": task_id, "status": status, "updated_at": time.time(), **fields}
        async with self.summary_lock:
            state_path = self.state_path(task_id)
            previous = str(read_json(state_path, {}).get("status") or "")
            atomic_json(state_path, row)
            if previous:
                self.counters[previous] = max(
                    0, self.counters.get(previous, 0) - 1
                )
            self.counters[status] = self.counters.get(status, 0) + 1
            self.counters["finished"] = sum(
                self.counters.get(final_status, 0)
                for final_status in FINAL_STATES | {"error"}
            )
            atomic_json(self.run_dir / "summary.json", self.counters)
        print(f"[{self.counters['finished']}/{len(self.instances)}] {task_id}: {status}", flush=True)

    async def recover_pending(self, instance: Path, state: dict[str, Any]) -> None:
        task_id = instance.name
        tx = tx_from_json(state["transaction"])
        initial_score = state.get("initial_score")
        recheck = await self.runner.evaluate(instance, attempt="recheck")
        post_score = reward_value(recheck.get("reward"))
        improved = score_improved(initial_score, post_score)
        if improved:
            await asyncio.to_thread(self.committer.finalize, tx)
            await self.set_state(task_id, "repair_accepted", initial_score=initial_score, final_score=post_score)
        else:
            await asyncio.to_thread(self.committer.rollback, tx)
            await self.set_state(task_id, "repair_rejected", initial_score=initial_score, final_score=post_score)

    async def process_agent1(self, instance: Path) -> None:
        task_id = instance.name
        try:
            await self.set_state(task_id, "agent1_evaluating")
            initial = await self.runner.evaluate(instance, attempt="initial")
            initial_score = reward_value(initial.get("reward"))
            if not is_repair_candidate(initial):
                await self.set_state(task_id, "positive_score", score=initial_score)
                return
            evidence_dir = self.item_dir(task_id) / "evidence"
            build_evidence(initial, evidence_dir)
            await self.set_state(task_id, "queued_agent2", initial_score=initial_score)
            await self.agent2_queue.put((instance, initial))
        except Exception as exc:
            await self.set_state(task_id, "error", phase="agent1", error=repr(exc))

    async def process_agent2(self, instance: Path, initial: dict[str, Any]) -> None:
        from teamfactory.remote import q, run_remote

        task_id = instance.name
        item_dir = self.item_dir(task_id)
        evidence_dir = item_dir / "evidence"
        download_root = item_dir / "downloaded_repair"
        remote_bundle = f"{self.args.remote_work_root.rstrip('/')}/{self.args.run_name}/{task_id}/bundle"
        handed_to_finalize = False
        try:
            if not evidence_dir.is_dir():
                build_evidence(initial, evidence_dir)
            await self.set_state(
                task_id,
                "agent2_uploading",
                initial_score=reward_value(initial.get("reward")),
            )
            await asyncio.to_thread(
                self.remote.upload_instance_bundle,
                instance,
                evidence_dir,
                remote_bundle,
                item_dir,
            )
            result_path = f"{remote_bundle}/agent2_result.json"
            init_result = await asyncio.to_thread(
                run_remote,
                self.args,
                f"printf '{{}}\\n' > {q(result_path)}",
                timeout=120,
            )
            if init_result.returncode != 0:
                raise RuntimeError(f"failed to initialize Agent2 result: {init_result.stdout[-1000:]}")
            prompt = Template(AGENT2_PROMPT.read_text(encoding="utf-8")).safe_substitute(
                instance_path=f"{remote_bundle}/instance",
                evidence_path=f"{remote_bundle}/evidence",
                bundle_index_path=f"{remote_bundle}/FILE_INDEX.txt",
                agent2_result_path=result_path,
            )
            await self.set_state(
                task_id,
                "agent2_running",
                initial_score=reward_value(initial.get("reward")),
            )
            try:
                raw = await asyncio.to_thread(
                    self.remote.run_json_turn,
                    prompt,
                    task_id=task_id,
                    phase="agent2_triage_repair",
                    remote_cwd=remote_bundle,
                    tools="Read,Write,Edit,Glob,Grep",
                )
            except ValueError as response_error:
                remote_result = await asyncio.to_thread(
                    run_remote,
                    self.args,
                    f"cat {q(result_path)}",
                    timeout=120,
                )
                if remote_result.returncode != 0:
                    raise response_error
                raw = extract_json_object(remote_result.stdout)
            result = validate_agent2(raw)
            atomic_json(item_dir / "agent2_result.json", result.raw)
            initial_score = reward_value(initial.get("reward"))

            if result.decision is not RepairDecision.INSTANCE_ERROR:
                await self.set_state(
                    task_id,
                    "not_instance_error",
                    initial_score=initial_score,
                    decision=result.decision.value,
                    confidence=result.confidence,
                )
                return
            if result.repair.status == "no_safe_repair":
                status = "low_confidence_instance_error" if result.confidence < 0.70 else "no_safe_repair"
                await self.set_state(
                    task_id,
                    status,
                    initial_score=initial_score,
                    confidence=result.confidence,
                )
                return

            await asyncio.to_thread(self.remote.download_bundle, remote_bundle, download_root)
            downloaded = download_root / "bundle"
            candidate = downloaded / "instance"
            on_disk = validate_agent2(read_json(downloaded / "agent2_result.json"))
            if on_disk.raw != result.raw:
                raise ValueError("agent2_result.json differs from the Agent2 final response")
            try:
                changed = validate_candidate(
                    instance,
                    candidate,
                    result.repair.changed_files,
                    result.repair.image_commands,
                )
            except ValueError as rejection:
                atomic_json(
                    item_dir / "repair.json",
                    {**result.repair.raw, "rejected_reason": str(rejection)},
                )
                await self.set_state(
                    task_id,
                    "repair_rejected",
                    initial_score=initial_score,
                    reason=str(rejection),
                )
                return
            atomic_json(
                item_dir / "repair.json",
                {**result.repair.raw, "validated_changes": sorted(changed)},
            )
            await self.set_state(task_id, "queued_finalize", initial_score=initial_score)
            await self.finalize_queue.put({
                "instance": instance,
                "initial_score": initial_score,
                "repair": result.repair,
                "candidate": candidate,
                "remote_bundle": remote_bundle,
                "download_root": download_root,
            })
            handed_to_finalize = True
        except Exception as exc:
            await self.set_state(task_id, "error", phase="agent2", error=repr(exc))
        finally:
            if not handed_to_finalize:
                await asyncio.to_thread(self.remote.cleanup_remote, remote_bundle)
                if download_root.exists():
                    shutil.rmtree(download_root, ignore_errors=True)

    async def finalize_repair(self, work: dict[str, Any]) -> None:
        instance: Path = work["instance"]
        task_id = instance.name
        initial_score = work["initial_score"]
        repair = work["repair"]
        candidate: Path = work["candidate"]
        remote_bundle = str(work["remote_bundle"])
        download_root: Path = work["download_root"]
        tx: RepairTransaction | None = None
        try:
            await self.set_state(task_id, "building_repaired_image", initial_score=initial_score)
            tx = await asyncio.to_thread(
                self.committer.prepare_remote_image,
                instance,
                remote_bundle,
                repair.image_commands,
            )
            await self.set_state(
                task_id,
                "prepared_repair",
                initial_score=initial_score,
                transaction=tx_to_json(tx),
            )
            await asyncio.to_thread(self.committer.commit, tx, candidate)
            await self.set_state(
                task_id,
                "committed_pending_recheck",
                initial_score=initial_score,
                transaction=tx_to_json(tx),
            )
            recheck = await self.runner.evaluate(instance, attempt="recheck")
            final_score = reward_value(recheck.get("reward"))
            improved = score_improved(initial_score, final_score)
            if improved:
                await asyncio.to_thread(self.committer.finalize, tx)
                tx = None
                await self.set_state(
                    task_id,
                    "repair_accepted",
                    initial_score=initial_score,
                    final_score=final_score,
                )
            else:
                await asyncio.to_thread(self.committer.rollback, tx)
                tx = None
                await self.set_state(
                    task_id,
                    "repair_rejected",
                    initial_score=initial_score,
                    final_score=final_score,
                )
        except Exception as exc:
            if tx is not None:
                try:
                    await asyncio.to_thread(self.committer.rollback, tx)
                except Exception:
                    pass
            await self.set_state(task_id, "error", phase="finalize", error=repr(exc))
        finally:
            await asyncio.to_thread(self.remote.cleanup_remote, remote_bundle)
            if download_root.exists():
                shutil.rmtree(download_root, ignore_errors=True)

    async def agent1_worker(self) -> None:
        while True:
            instance = await self.agent1_queue.get()
            try:
                if instance is None:
                    return
                await self.process_agent1(instance)
            finally:
                self.agent1_queue.task_done()

    async def agent2_worker(self) -> None:
        while True:
            work = await self.agent2_queue.get()
            try:
                if work is None:
                    return
                await self.process_agent2(*work)
            finally:
                self.agent2_queue.task_done()

    async def finalize_worker(self) -> None:
        while True:
            work = await self.finalize_queue.get()
            try:
                if work is None:
                    return
                if work.get("recover"):
                    instance = work["instance"]
                    try:
                        await self.recover_pending(instance, work["state"])
                    except Exception as exc:
                        await self.set_state(instance.name, "error", phase="recover", error=repr(exc))
                else:
                    await self.finalize_repair(work)
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
                "agent1_api_key": "<redacted>",
                "agent2_api_key": "<redacted>",
                "instance_count": len(self.instances),
                "instances": [path.name for path in self.instances],
            },
        )
        cases = self.run_dir / "cases.jsonl"
        cases.write_text(
            "".join(json.dumps({"case_path": str(path)}, ensure_ascii=False) + "\n" for path in self.instances),
            encoding="utf-8",
        )
        agent1_workers = [
            asyncio.create_task(self.agent1_worker())
            for _ in range(self.args.agent1_workers)
        ]
        agent2_workers = [
            asyncio.create_task(self.agent2_worker())
            for _ in range(self.args.agent2_workers)
        ]
        finalize_workers = [
            asyncio.create_task(self.finalize_worker())
            for _ in range(self.args.finalize_workers)
        ]

        for instance in self.instances:
            state = read_json(self.state_path(instance.name), {}) if not self.args.no_resume else {}
            status = str(state.get("status") or "")
            if status in FINAL_STATES:
                print(f"[resume] {instance.name}: {status}", flush=True)
                continue
            if status == "committed_pending_recheck" and state.get("transaction"):
                await self.finalize_queue.put({"recover": True, "instance": instance, "state": state})
                continue
            if status == "prepared_repair" and state.get("transaction"):
                try:
                    await asyncio.to_thread(self.committer.rollback, tx_from_json(state["transaction"]))
                except Exception as exc:
                    await self.set_state(instance.name, "error", phase="recover_prepared", error=repr(exc))
                    continue
            initial_path = self.item_dir(instance.name) / "evaluations" / "initial" / "result.json"
            if not self.args.no_resume and initial_path.is_file() and is_repair_candidate(read_json(initial_path)):
                initial = read_json(initial_path)
                await self.agent2_queue.put((instance, initial))
            else:
                await self.agent1_queue.put(instance)

        for _ in agent1_workers:
            await self.agent1_queue.put(None)
        await self.agent1_queue.join()
        await asyncio.gather(*agent1_workers)

        for _ in agent2_workers:
            await self.agent2_queue.put(None)
        await self.agent2_queue.join()
        await asyncio.gather(*agent2_workers)

        for _ in finalize_workers:
            await self.finalize_queue.put(None)
        await self.finalize_queue.join()
        await asyncio.gather(*finalize_workers)


def main(argv: list[str] | None = None) -> int:
    args = normalize_args(build_parser().parse_args(argv))
    instances = discover_instances(args)
    print(f"dataset={args.dataset_root}")
    print(
        f"instances={len(instances)} "
        f"agent1={args.agent1_workers}x{args.agent1_model} "
        f"agent2={args.agent2_workers}x{args.agent2_model} "
        f"finalize={args.finalize_workers}",
        flush=True,
    )
    if args.plan_only:
        print("plan-only: no evaluation or repair was started")
        return 0
    pipeline = InstanceTuningPipeline(args, instances)
    print(f"run_dir={pipeline.run_dir}")
    asyncio.run(pipeline.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
