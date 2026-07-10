from __future__ import annotations

import os
import signal
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import Any

from teamfactory.artifacts import ItemRef, append_jsonl, ensure_dir, write_json
from teamfactory.repo_source import read_repo_jsonl, task_id_for_url
from teamfactory.remote import prepare_sshpass
from teamfactory.stages.agent1 import Agent1Stage
from teamfactory.stages.agent2_stage3 import Agent2Stage3
from teamfactory.stages.stage2_ast import Stage2AstStage


STAGE_OBJECTS = {
    "agent1": Agent1Stage(),
    "stage2_ast": Stage2AstStage(),
    "agent2_stage3": Agent2Stage3(),
}
FIRST_STAGE = "agent1"


class StreamingPipeline:
    def __init__(self, args: Any) -> None:
        self.args = args
        self.run_id = time.strftime("%Y%m%d_%H%M%S")
        args.run_dir = str(Path(args.run_dir) / self.run_id)
        ensure_dir(args.run_dir)
        ensure_dir(Path(args.work_dir) / "items")
        self.tp = max(1, int(args.tp))
        self.pp = max(1, int(args.pp))
        self.ingress: deque[ItemRef] = deque()
        self.queues: list[dict[str, deque[ItemRef]]] = [
            {stage: deque() for stage in STAGE_OBJECTS}
            for _ in range(self.tp)
        ]
        self.active: dict[Future[str], tuple[int, str, ItemRef]] = {}
        self.counts: Counter[str] = Counter()
        self.stop_requested = False

    def lane_capacity(self) -> int:
        return self.pp + 1

    def load_items(self) -> list[ItemRef]:
        records = read_repo_jsonl(self.args.repo_jsonl)
        if self.args.start_index:
            records = records[self.args.start_index :]
        if self.args.limit > 0:
            records = records[: self.args.limit]
        items: list[ItemRef] = []
        for offset, record in enumerate(records):
            url = str(record["url"])
            items.append(
                ItemRef(
                    index=self.args.start_index + offset,
                    url=url,
                    task_id=task_id_for_url(url),
                    lane=-1,
                )
            )
        return items

    def run(self) -> int:
        self.install_signal_handlers()
        prepare_sshpass(self.args.ssh_pass_file)
        items = self.load_items()
        for item in items:
            self.ingress.append(item)
        append_jsonl(
            Path(self.args.run_dir) / "pipeline_events.jsonl",
            {
                "event": "pipeline_start",
                "queue_size": len(items),
                "tp": self.tp,
                "pp": self.pp,
                "first_stage": FIRST_STAGE,
                "remote_host": self.args.remote_host,
                "remote_work_root": self.args.remote_work_root,
                "timestamp": int(time.time()),
            },
        )
        print(
            f"queue_size={len(items)} tp={self.tp} pp={self.pp} "
            f"first_stage={FIRST_STAGE} run_dir={self.args.run_dir}",
            flush=True,
        )
        max_workers = max(1, self.tp * self.lane_capacity())
        exit_code = 0
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                while (self.any_queued() or self.active) and not self.stop_requested:
                    self.launch_ready(pool)
                    self.write_checkpoint(complete=False)
                    if not self.active:
                        time.sleep(0.2)
                        continue
                    done, _ = wait(set(self.active), timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        lane, stage, ref = self.active.pop(future)
                        try:
                            next_stage = future.result()
                            self.counts[f"{stage}:done"] += 1
                            self.counts[f"lane{lane}:{stage}:done"] += 1
                            print(f"[{ref.index}] lane={lane} {stage} done task_id={ref.task_id}", flush=True)
                        except Exception as exc:
                            next_stage = ""
                            self.counts[f"{stage}:exception"] += 1
                            self.counts[f"lane{lane}:{stage}:exception"] += 1
                            append_jsonl(
                                Path(self.args.run_dir) / "pipeline_events.jsonl",
                                {
                                    "event": "stage_exception",
                                    "lane": lane,
                                    "stage": stage,
                                    "task_id": ref.task_id,
                                    "url": ref.url,
                                    "error": repr(exc),
                                    "timestamp": int(time.time()),
                                },
                            )
                        if next_stage and next_stage in STAGE_OBJECTS:
                            self.queues[lane][next_stage].append(ref)
        except BaseException as exc:
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            self.write_checkpoint(complete=False, error=repr(exc))
            if not isinstance(exc, KeyboardInterrupt):
                raise
        if self.stop_requested:
            exit_code = 130
        complete = exit_code == 0 and not self.any_queued() and not self.active
        self.write_checkpoint(complete=complete)
        print(f"complete counts={dict(self.counts)}", flush=True)
        return exit_code

    def any_queued(self) -> bool:
        return bool(self.ingress) or any(queue for lane in self.queues for queue in lane.values())

    def lane_load(self, lane: int) -> int:
        queued = sum(len(queue) for queue in self.queues[lane].values())
        active = sum(1 for active_lane, _stage, _ref in self.active.values() if active_lane == lane)
        return queued + active

    def assign_ingress_to_lanes(self, active_by_lane_stage: Counter[tuple[int, str]]) -> None:
        while self.ingress:
            candidates: list[tuple[int, int]] = []
            for lane in range(self.tp):
                if self.lane_load(lane) >= self.lane_capacity():
                    continue
                if len(self.queues[lane][FIRST_STAGE]) + active_by_lane_stage[(lane, FIRST_STAGE)] > 0:
                    continue
                candidates.append((self.lane_load(lane), lane))
            if not candidates:
                return
            load, lane = min(candidates)
            ref = replace(self.ingress.popleft(), lane=lane)
            self.queues[lane][FIRST_STAGE].append(ref)
            self.counts["assigned"] += 1
            self.counts[f"lane{lane}:assigned"] += 1
            append_jsonl(
                Path(self.args.run_dir) / "pipeline_events.jsonl",
                {
                    "event": "assigned",
                    "lane": lane,
                    "lane_load_before": load,
                    "index": ref.index,
                    "task_id": ref.task_id,
                    "url": ref.url,
                    "timestamp": int(time.time()),
                },
            )

    def launch_ready(self, pool: ThreadPoolExecutor) -> None:
        active_by_lane_stage = Counter((lane, stage) for lane, stage, _ref in self.active.values())
        active_by_stage = Counter(stage for _lane, stage, _ref in self.active.values())
        self.assign_ingress_to_lanes(active_by_lane_stage)
        for lane, lane_queues in enumerate(self.queues):
            active_in_lane = sum(1 for active_lane, _stage, _ref in self.active.values() if active_lane == lane)
            if active_in_lane >= self.lane_capacity():
                continue
            for stage, queue in lane_queues.items():
                if not queue:
                    continue
                if active_by_lane_stage[(lane, stage)] > 0:
                    continue
                stage_limit = self.stage_limit(stage)
                if stage_limit > 0 and active_by_stage[stage] >= stage_limit:
                    continue
                ref = queue.popleft()
                future = pool.submit(STAGE_OBJECTS[stage].run, self.args, ref)
                self.active[future] = (lane, stage, ref)
                active_by_lane_stage[(lane, stage)] += 1
                active_by_stage[stage] += 1
                self.counts[f"{stage}:launched"] += 1
                self.counts[f"lane{lane}:{stage}:launched"] += 1
                print(f"[{ref.index}] lane={lane} {stage} launched task_id={ref.task_id}", flush=True)

    def stage_limit(self, stage: str) -> int:
        if stage == "agent1":
            return int(self.args.agent1_concurrency)
        if stage == "agent2_stage3":
            return int(self.args.agent2_concurrency)
        return 0

    def queue_snapshot(self) -> dict[str, Any]:
        return {
            "ingress": len(self.ingress),
            "lanes": [
                {stage: len(queue) for stage, queue in lane.items() if queue}
                for lane in self.queues
            ],
        }

    def active_snapshot(self) -> list[dict[str, Any]]:
        return [
            {"lane": lane, "stage": stage, "task_id": ref.task_id, "index": ref.index}
            for lane, stage, ref in self.active.values()
        ]

    def write_checkpoint(self, *, complete: bool, error: str = "") -> None:
        write_json(
            Path(self.args.run_dir) / "checkpoint.json",
            {
                "complete": complete,
                "pid": os.getpid(),
                "tp": self.tp,
                "pp": self.pp,
                "lane_capacity": self.lane_capacity(),
                "counts": dict(self.counts),
                "active": self.active_snapshot(),
                "queued": self.queue_snapshot(),
                "error": error,
                "updated_at": int(time.time()),
            },
        )

    def install_signal_handlers(self) -> None:
        def mark_stop(signum: int, _frame: Any) -> None:
            self.stop_requested = True
            append_jsonl(
                Path(self.args.run_dir) / "pipeline_events.jsonl",
                {
                    "event": "signal_received",
                    "signal": signum,
                    "active": self.active_snapshot(),
                    "queued": self.queue_snapshot(),
                    "timestamp": int(time.time()),
                },
            )

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, mark_stop)
            except ValueError:
                pass
