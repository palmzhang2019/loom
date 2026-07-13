from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from .events import DEFAULT_EVENTS_PATH, Actor, Event, append_event


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


@dataclass(frozen=True)
class FileChange:
    path: str
    change_type: str
    before_hash: str | None
    after_hash: str | None


@dataclass(frozen=True)
class ObservedFileChanges:
    paths: list[str]
    has_observation: bool


T = TypeVar("T")


def load_observed_file_changes(
    *,
    events_path: Path | str,
    segment_id: str,
    run_id: str,
) -> ObservedFileChanges:
    path = Path(events_path)
    if not path.exists():
        return ObservedFileChanges(paths=[], has_observation=False)

    changed_paths: set[str] = set()
    has_observation = False
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if (
                event.get("segment_id") != segment_id
                or event.get("run_id") != run_id
                or event.get("actor") != "harness"
                or event.get("type") != "files_changed"
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            files = payload.get("files")
            if not isinstance(files, list):
                continue
            has_observation = True
            for file_change in files:
                if not isinstance(file_change, dict):
                    continue
                changed_path = file_change.get("path")
                if isinstance(changed_path, str) and changed_path:
                    changed_paths.add(changed_path)
    return ObservedFileChanges(
        paths=sorted(changed_paths),
        has_observation=has_observation,
    )


def load_observed_changed_paths(
    *,
    events_path: Path | str,
    segment_id: str,
    run_id: str,
) -> list[str]:
    return load_observed_file_changes(
        events_path=events_path,
        segment_id=segment_id,
        run_id=run_id,
    ).paths


def run_observed(
    cmd: str,
    *,
    segment_id: str,
    run_id: str,
    cwd: Path | str | None = None,
    path: Path | str = DEFAULT_EVENTS_PATH,
    payload: dict[str, object] | None = None,
) -> CommandResult:
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor="harness",
            type="command_started",
            payload={
                "cmd": cmd,
                "cwd": str(cwd) if cwd is not None else None,
                **(payload or {}),
            },
        ),
        path=path,
    )
    started_at = time.monotonic()
    completed = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    duration_seconds = time.monotonic() - started_at

    result = CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=duration_seconds,
    )
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor="harness",
            type="command_run",
            payload={
                "cmd": cmd,
                "cwd": str(cwd) if cwd is not None else None,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_seconds": result.duration_seconds,
                **(payload or {}),
            },
        )
        ,
        path=path,
    )
    return result


def observe_files_changed(
    work: Callable[[], object],
    *,
    git_dir: Path | str,
    segment_id: str,
    run_id: str,
    path: Path | str = DEFAULT_EVENTS_PATH,
    payload: dict[str, object] | None = None,
) -> list[FileChange]:
    repo_dir = Path(git_dir)
    before = _capture_git_state(repo_dir)

    try:
        work()
    finally:
        after = _capture_git_state(repo_dir)
        changes = _diff_git_states(before, after)
        append_event(
            Event(
                ts=_utc_now(),
                segment_id=segment_id,
                run_id=run_id,
                actor="harness",
                type="files_changed",
                payload={
                    "files": [
                        {
                            "path": change.path,
                            "change_type": change.change_type,
                            "before_hash": change.before_hash,
                            "after_hash": change.after_hash,
                            }
                        for change in changes
                    ],
                    **(payload or {}),
                },
            ),
            path=path,
        )

    return changes


def observe_step(
    work: Callable[[], T],
    *,
    actor: Actor,
    step_name: str,
    segment_id: str,
    run_id: str,
    path: Path | str = DEFAULT_EVENTS_PATH,
    payload: dict[str, object] | None = None,
) -> T:
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor=actor,
            type="step_started",
            payload={"step": step_name, **(payload or {})},
        ),
        path=path,
    )
    result = work()
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor=actor,
            type="step_finished",
            payload={"step": step_name, **(payload or {}), "result": result},
        ),
        path=path,
    )
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _capture_git_state(git_dir: Path) -> dict[str, str]:
    paths = _git_tracked_and_untracked_paths(git_dir)
    state: dict[str, str] = {}

    for path in paths:
        full_path = git_dir / path
        if full_path.exists():
            state[path] = _git_hash_object(git_dir, path)

    return state


def _diff_git_states(before: dict[str, str], after: dict[str, str]) -> list[FileChange]:
    changes: list[FileChange] = []

    for path in sorted(set(before) | set(after)):
        before_hash = before.get(path)
        after_hash = after.get(path)

        if before_hash == after_hash:
            continue
        if before_hash is None:
            change_type = "added"
        elif after_hash is None:
            change_type = "deleted"
        else:
            change_type = "modified"

        changes.append(
            FileChange(
                path=path,
                change_type=change_type,
                before_hash=before_hash,
                after_hash=after_hash,
            )
        )

    return changes


def _git_tracked_and_untracked_paths(git_dir: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=git_dir,
        capture_output=True,
        check=True,
    )
    return [
        path
        for path in completed.stdout.decode("utf-8").split("\0")
        if path
    ]


def _git_hash_object(git_dir: Path, path: str) -> str:
    completed = subprocess.run(
        ["git", "hash-object", "--", path],
        cwd=git_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()
