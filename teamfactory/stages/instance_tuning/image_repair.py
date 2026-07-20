from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teamfactory.remote import q, run_remote
from teamfactory.stages.agent2_stage3.stage import Agent2Stage3, safe_name

from .validation import archive_path


@dataclass
class RepairTransaction:
    task_id: str
    instance_dir: Path
    local_backup: Path
    local_new: Path
    archive: str
    remote_archive_backup: str
    remote_archive_new: str
    final_image: str
    old_image_tag: str
    remote_stage: str


def _token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:80]


class InstanceRepairCommitter:
    def __init__(self, args: Any, run_id: str) -> None:
        self.args = args
        self.run_id = _token(run_id)

    def prepare_remote_image(
        self,
        instance: Path,
        remote_bundle: str,
        image_commands: list[str],
    ) -> RepairTransaction:
        task_id = instance.name
        archive = archive_path(instance / "task.toml")
        final_image = safe_name(f"teamfactory-instance-{task_id}")
        suffix = _token(f"{self.run_id}-{task_id}")[-100:]
        old_tag = f"{final_image}:pre-tuning-{suffix}"
        remote_new = f"{archive}.tuning-new-{suffix}"
        remote_backup = f"{archive}.tuning-backup-{suffix}"
        remote_stage = f"{remote_bundle}/image-rebuild"
        context = f"{remote_stage}/context"
        dockerfile = [f"FROM {old_tag}", "USER root"]
        dockerfile.extend(f"RUN {command}" for command in image_commands)
        dockerfile.extend(["COPY start.md /workspace/start.md", "WORKDIR /workspace"])
        dockerfile_text = "\n".join(dockerfile) + "\n"
        script = f"""
set -euo pipefail
archive={q(archive)}
final_image={q(final_image)}
old_tag={q(old_tag)}
remote_new={q(remote_new)}
context={q(context)}
rm -rf "$context" {q(remote_new)}
mkdir -p "$context"
docker load -i "$archive" >/dev/null
docker image inspect "$final_image:latest" >/dev/null
docker tag "$final_image:latest" "$old_tag"
cp {q(remote_bundle + '/instance/environment/start.md')} "$context/start.md"
cat > "$context/Dockerfile" <<'DOCKERFILE'
{dockerfile_text}DOCKERFILE
docker build --pull=false -t "$final_image:latest" "$context"
docker run --rm --entrypoint /bin/bash "$final_image:latest" -lc 'test -x /bin/bash && test -s /workspace/start.md'
docker save -o "$remote_new" "$final_image:latest"
test -s "$remote_new"
tar -tf "$remote_new" >/dev/null
"""
        result = run_remote(self.args, script, timeout=self.args.image_rebuild_timeout)
        if result.returncode != 0:
            self._cleanup_failed_prepare(final_image, old_tag, remote_new, remote_stage)
            raise RuntimeError(f"remote image rebuild failed: {result.stdout[-6000:]}")

        try:
            leak_report = Agent2Stage3().run_remote_image_leak_check(
                self.args, final_image, remote_new, remote_stage
            )
        except Exception:
            self._cleanup_failed_prepare(final_image, old_tag, remote_new, remote_stage)
            raise
        if leak_report.get("fatal"):
            self._cleanup_failed_prepare(final_image, old_tag, remote_new, remote_stage)
            raise ValueError(f"rebuilt image failed leak check: {leak_report.get('fatal_hits', [])[:5]}")

        parent = instance.parent
        local_backup = parent / f".{task_id}.tuning-backup-{suffix}"
        local_new = parent / f".{task_id}.tuning-new-{suffix}"
        return RepairTransaction(
            task_id=task_id,
            instance_dir=instance,
            local_backup=local_backup,
            local_new=local_new,
            archive=archive,
            remote_archive_backup=remote_backup,
            remote_archive_new=remote_new,
            final_image=final_image,
            old_image_tag=old_tag,
            remote_stage=remote_stage,
        )

    def commit(self, tx: RepairTransaction, candidate: Path) -> None:
        if tx.local_backup.exists() or tx.local_new.exists():
            raise RuntimeError(f"stale local tuning transaction for {tx.task_id}")
        shutil.copytree(candidate, tx.local_new, symlinks=True)
        tx.instance_dir.rename(tx.local_backup)
        try:
            tx.local_new.rename(tx.instance_dir)
            script = f"""
set -euo pipefail
test -s {q(tx.remote_archive_new)}
rm -f {q(tx.remote_archive_backup)}
mv {q(tx.archive)} {q(tx.remote_archive_backup)}
mv {q(tx.remote_archive_new)} {q(tx.archive)}
"""
            result = run_remote(self.args, script, timeout=self.args.transfer_timeout)
            if result.returncode != 0:
                raise RuntimeError(f"remote archive commit failed: {result.stdout[-3000:]}")
        except Exception:
            if tx.instance_dir.exists():
                shutil.rmtree(tx.instance_dir)
            tx.local_backup.rename(tx.instance_dir)
            run_remote(
                self.args,
                f"test ! -e {q(tx.remote_archive_backup)} || {{ rm -f {q(tx.archive)}; mv {q(tx.remote_archive_backup)} {q(tx.archive)}; }}",
                timeout=self.args.transfer_timeout,
            )
            self._restore_image_tag(tx.final_image, tx.old_image_tag)
            raise

    def rollback(self, tx: RepairTransaction) -> None:
        if tx.local_backup.exists():
            if tx.instance_dir.exists():
                shutil.rmtree(tx.instance_dir)
            tx.local_backup.rename(tx.instance_dir)
        run_remote(
            self.args,
            f"test ! -e {q(tx.remote_archive_backup)} || {{ rm -f {q(tx.archive)}; mv {q(tx.remote_archive_backup)} {q(tx.archive)}; }}; rm -f {q(tx.remote_archive_new)}",
            timeout=self.args.transfer_timeout,
        )
        self._restore_image_tag(tx.final_image, tx.old_image_tag)
        run_remote(
            self.args,
            f"docker image rm {q(tx.old_image_tag)} >/dev/null 2>&1 || true; rm -rf {q(tx.remote_stage)}",
            timeout=self.args.transfer_timeout,
        )

    def finalize(self, tx: RepairTransaction) -> None:
        if tx.local_backup.exists():
            shutil.rmtree(tx.local_backup)
        if tx.local_new.exists():
            shutil.rmtree(tx.local_new)
        run_remote(
            self.args,
            f"rm -f {q(tx.remote_archive_backup)} {q(tx.remote_archive_new)}; docker image rm {q(tx.old_image_tag)} >/dev/null 2>&1 || true; rm -rf {q(tx.remote_stage)}",
            timeout=self.args.transfer_timeout,
        )

    def discard_prepared(self, tx: RepairTransaction) -> None:
        run_remote(
            self.args,
            f"rm -f {q(tx.remote_archive_new)}; docker image rm {q(tx.old_image_tag)} >/dev/null 2>&1 || true; rm -rf {q(tx.remote_stage)}",
            timeout=self.args.transfer_timeout,
        )

    def _restore_image_tag(self, final_image: str, old_tag: str) -> None:
        run_remote(
            self.args,
            f"docker image inspect {q(old_tag)} >/dev/null 2>&1 && docker tag {q(old_tag)} {q(final_image + ':latest')} || true",
            timeout=120,
        )

    def _cleanup_failed_prepare(
        self, final_image: str, old_tag: str, remote_new: str, remote_stage: str
    ) -> None:
        run_remote(
            self.args,
            f"docker image inspect {q(old_tag)} >/dev/null 2>&1 && docker tag {q(old_tag)} {q(final_image + ':latest')} || true; "
            f"docker image rm {q(old_tag)} >/dev/null 2>&1 || true; "
            f"rm -f {q(remote_new)}; rm -rf {q(remote_stage)}",
            timeout=self.args.transfer_timeout,
        )
