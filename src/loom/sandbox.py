from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shlex import quote

from .events import DEFAULT_EVENTS_PATH, Event, append_event
from .harness import run_observed

DEFAULT_EXECUTION_REPO_PATH = Path("~/workspace/lingua-web").expanduser()
DEFAULT_WORKTREE_ROOT = Path("~/.loom/worktrees").expanduser()
_RETAINED_EVENT_TYPE = "artifact_retained"


@dataclass(frozen=True)
class Sandbox:
    segment_id: str
    repo_path: Path
    worktree_path: Path
    branch_name: str
    base_branch: str


@dataclass(frozen=True)
class RetainedBranch:
    branch_name: str
    status: str


def create_sandbox(
    *,
    segment_id: str,
    repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    worktree_root: Path | str = DEFAULT_WORKTREE_ROOT,
    run_id: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
) -> Sandbox:
    repo_dir = Path(repo_path)
    root_dir = Path(worktree_root)
    segment_variant = _segment_variant(segment_id)
    sandbox = Sandbox(
        segment_id=segment_id,
        repo_path=repo_dir,
        worktree_path=root_dir / segment_variant,
        branch_name=f"loom/{segment_variant}",
        base_branch="main",
    )

    _require_local_branch(repo_dir, sandbox.base_branch)
    _require_missing_branch(repo_dir, sandbox.branch_name)
    if sandbox.worktree_path.exists():
        raise FileExistsError(f"sandbox worktree path already exists: {sandbox.worktree_path}")

    root_dir.mkdir(parents=True, exist_ok=True)
    result = run_observed(
        (
            "git worktree add "
            f"-b {quote(sandbox.branch_name)} "
            f"{quote(str(sandbox.worktree_path))} "
            f"{quote(sandbox.base_branch)}"
        ),
        segment_id=segment_id,
        run_id=run_id,
        cwd=repo_dir,
        path=events_path,
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree add failed")

    sync_result = run_observed(
        "uv sync --extra dev",
        segment_id=segment_id,
        run_id=run_id,
        cwd=sandbox.worktree_path,
        path=events_path,
    )
    if sync_result.exit_code != 0:
        sync_detail = sync_result.stderr.strip() or "non-zero exit code"
        sync_error = RuntimeError(f"uv sync failed: {sync_detail}")
        try:
            destroy_sandbox(
                sandbox,
                run_id=run_id,
                events_path=events_path,
                delete_branch=True,
            )
        except RuntimeError as cleanup_error:
            raise RuntimeError(f"{sync_error}; cleanup failed: {cleanup_error}") from sync_error
        raise sync_error

    return sandbox


def retain_sandbox_artifact(
    sandbox: Sandbox,
    *,
    run_id: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    status: str,
) -> None:
    commit_paths = _list_commit_candidate_paths(sandbox.worktree_path)
    if commit_paths:
        add_result = run_observed(
            "git add -A -- " + " ".join(quote(path) for path in commit_paths),
            segment_id=sandbox.segment_id,
            run_id=run_id,
            cwd=sandbox.worktree_path,
            path=events_path,
        )
        if add_result.exit_code != 0:
            raise RuntimeError(add_result.stderr.strip() or "git add failed")

        commit_message = (
            f"loom artifact {sandbox.segment_id} "
            f"run_id={run_id} status={status}"
        )
        commit_result = run_observed(
            f"git commit -m {quote(commit_message)}",
            segment_id=sandbox.segment_id,
            run_id=run_id,
            cwd=sandbox.worktree_path,
            path=events_path,
        )
        if commit_result.exit_code != 0:
            raise RuntimeError(commit_result.stderr.strip() or "git commit failed")

    append_event(
        Event(
            ts=_utc_now(),
            segment_id=sandbox.segment_id,
            run_id=run_id,
            actor="harness",
            type=_RETAINED_EVENT_TYPE,
            payload={
                "branch_name": sandbox.branch_name,
                "status": status,
            },
        ),
        path=events_path,
    )
    destroy_sandbox(
        sandbox,
        run_id=run_id,
        events_path=events_path,
        delete_branch=False,
    )


def destroy_sandbox(
    sandbox: Sandbox,
    *,
    run_id: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    delete_branch: bool = False,
) -> None:
    if sandbox.worktree_path.exists():
        result = run_observed(
            f"git worktree remove --force {quote(str(sandbox.worktree_path))}",
            segment_id=sandbox.segment_id,
            run_id=run_id,
            cwd=sandbox.repo_path,
            path=events_path,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr.strip() or "git worktree remove failed")

    if delete_branch and _branch_exists(sandbox.repo_path, sandbox.branch_name):
        result = run_observed(
            f"git branch -D {quote(sandbox.branch_name)}",
            segment_id=sandbox.segment_id,
            run_id=run_id,
            cwd=sandbox.repo_path,
            path=events_path,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr.strip() or "git branch -D failed")


def list_retained_branches(
    *,
    repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
) -> list[RetainedBranch]:
    repo_dir = Path(repo_path)
    statuses = _load_retained_branch_statuses(Path(events_path))
    return [
        RetainedBranch(branch_name=branch_name, status=statuses.get(branch_name, "unknown"))
        for branch_name in _list_local_branches(repo_dir)
        if branch_name.startswith("loom/")
    ]


def delete_retained_branch(
    *,
    repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    branch_name: str,
) -> None:
    repo_dir = Path(repo_path)
    if not branch_name.startswith("loom/"):
        raise ValueError("retained branch deletion only supports loom/ branches")
    if not _branch_exists(repo_dir, branch_name):
        raise FileNotFoundError(f"retained branch not found: {branch_name}")
    completed = subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git branch -D failed")


def _segment_variant(segment_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", segment_id).strip("-")


def _require_local_branch(repo_path: Path, branch_name: str) -> None:
    if not _branch_exists(repo_path, branch_name):
        raise ValueError(f"local branch not found: {branch_name}")


def _require_missing_branch(repo_path: Path, branch_name: str) -> None:
    if _branch_exists(repo_path, branch_name):
        raise FileExistsError(f"sandbox branch already exists: {branch_name}")


def _branch_exists(repo_path: Path, branch_name: str) -> bool:
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_path,
        check=False,
    )
    return completed.returncode == 0


def _list_commit_candidate_paths(worktree_path: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--modified", "--deleted", "--others", "--exclude-standard"],
        cwd=worktree_path,
        capture_output=True,
        check=True,
    )
    return sorted(
        path
        for path in completed.stdout.decode("utf-8").split("\0")
        if path
        and path != ".venv"
        and path != "uv.lock"
        and not path.startswith(".venv/")
    )


def _list_local_branches(repo_path: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(branch for branch in completed.stdout.splitlines() if branch.strip())


def _load_retained_branch_statuses(events_path: Path) -> dict[str, str]:
    if not events_path.exists():
        return {}

    statuses: dict[str, str] = {}
    with events_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_payload = payload.get("payload")
            if not isinstance(event_payload, dict):
                continue
            if payload.get("type") == _RETAINED_EVENT_TYPE:
                branch_name = event_payload.get("branch_name")
                status = event_payload.get("status")
                if isinstance(branch_name, str) and isinstance(status, str):
                    statuses[branch_name] = status
            elif payload.get("type") == "merge_rejected":
                branch_name = event_payload.get("source_branch")
                if isinstance(branch_name, str):
                    statuses[branch_name] = "rejected"
    return statuses


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or delete retained Loom branches.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-retained", help="List retained loom/ branches.")
    list_parser.add_argument("--repo", default=str(DEFAULT_EXECUTION_REPO_PATH))
    list_parser.add_argument("--events", default=str(DEFAULT_EVENTS_PATH))

    delete_parser = subparsers.add_parser("delete-retained", help="Delete one retained loom/ branch.")
    delete_parser.add_argument("branch_name")
    delete_parser.add_argument("--repo", default=str(DEFAULT_EXECUTION_REPO_PATH))

    args = parser.parse_args(argv)
    if args.command == "list-retained":
        for item in list_retained_branches(repo_path=args.repo, events_path=args.events):
            print(f"{item.branch_name}\t{item.status}")
        return 0

    if args.command == "delete-retained":
        delete_retained_branch(repo_path=args.repo, branch_name=args.branch_name)
        return 0

    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
