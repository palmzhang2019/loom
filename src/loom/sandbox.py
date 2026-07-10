from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shlex import quote

from .events import DEFAULT_EVENTS_PATH
from .harness import run_observed

DEFAULT_EXECUTION_REPO_PATH = Path("~/workspace/lingua-web").expanduser()
DEFAULT_WORKTREE_ROOT = Path("~/.loom/worktrees").expanduser()


@dataclass(frozen=True)
class Sandbox:
    segment_id: str
    repo_path: Path
    worktree_path: Path
    branch_name: str
    base_branch: str


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

    return sandbox


def destroy_sandbox(
    sandbox: Sandbox,
    *,
    run_id: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
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

    if _branch_exists(sandbox.repo_path, sandbox.branch_name):
        result = run_observed(
            f"git branch -D {quote(sandbox.branch_name)}",
            segment_id=sandbox.segment_id,
            run_id=run_id,
            cwd=sandbox.repo_path,
            path=events_path,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr.strip() or "git branch -D failed")


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
