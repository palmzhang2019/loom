from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.events import Event, append_event
from loom.harness import CommandResult
from loom.sandbox import (
    create_sandbox,
    delete_retained_branch,
    destroy_sandbox,
    list_retained_branches,
)
from loom.view import load_event_rows


def _git(git_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=git_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_main_repo(git_dir: Path) -> None:
    _git(git_dir, "init", "-b", "main")
    _git(git_dir, "config", "user.name", "Loom Tests")
    _git(git_dir, "config", "user.email", "loom-tests@example.com")
    (git_dir / "README.md").write_text("baseline\n", encoding="utf-8")
    (git_dir / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "loom-sandbox-tests"',
                'version = "0.1.0"',
                "requires-python = \">=3.11\"",
                "",
                "[project.optional-dependencies]",
                "dev = []",
                "",
                "[tool.uv]",
                "package = false",
                "",
                "[build-system]",
                'requires = ["hatchling"]',
                'build-backend = "hatchling.build"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    _git(git_dir, "add", "README.md")
    _git(git_dir, "add", "pyproject.toml")
    _git(git_dir, "commit", "-m", "baseline")


class SandboxTests(unittest.TestCase):
    def test_create_and_destroy_sandbox_uses_main_and_isolated_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "lingua-web"
            repo_dir.mkdir()
            _init_main_repo(repo_dir)
            worktree_root = Path(tmpdir) / "loom-worktrees"
            events_path = Path(tmpdir) / "events.jsonl"

            sandbox = create_sandbox(
                segment_id="MAT-REQ-001/S1",
                repo_path=repo_dir,
                worktree_root=worktree_root,
                run_id="run-sandbox-001",
                events_path=events_path,
            )

            self.assertEqual(sandbox.branch_name, "loom/MAT-REQ-001-S1")
            self.assertEqual(sandbox.worktree_path, worktree_root / "MAT-REQ-001-S1")
            self.assertTrue(sandbox.worktree_path.exists())
            self.assertEqual(
                _git(sandbox.worktree_path, "branch", "--show-current").strip(),
                "loom/MAT-REQ-001-S1",
            )
            self.assertEqual(
                _git(sandbox.worktree_path, "rev-parse", "--abbrev-ref", "main").strip(),
                "main",
            )
            self.assertTrue((sandbox.worktree_path / ".venv").exists())

            destroy_sandbox(
                sandbox,
                run_id="run-sandbox-001",
                events_path=events_path,
            )

            self.assertFalse(sandbox.worktree_path.exists())
            self.assertIn("loom/MAT-REQ-001-S1", _git(repo_dir, "branch", "--list"))

            rows, _ = load_event_rows(events_path, run_id="run-sandbox-001")
            command_runs = [row for row in rows if row["type"] == "command_run"]
            self.assertGreaterEqual(len(command_runs), 3)
            self.assertEqual(command_runs[1]["payload"]["cmd"], "uv sync --extra dev")
            self.assertEqual(
                command_runs[1]["payload"]["cwd"],
                str(worktree_root / "MAT-REQ-001-S1"),
            )
            self.assertEqual(command_runs[1]["payload"]["exit_code"], 0)

    def test_create_sandbox_cleans_up_when_uv_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "lingua-web"
            repo_dir.mkdir()
            _init_main_repo(repo_dir)
            worktree_root = Path(tmpdir) / "loom-worktrees"
            events_path = Path(tmpdir) / "events.jsonl"

            from loom import sandbox as sandbox_module

            original_run_observed = sandbox_module.run_observed

            def fake_run_observed(cmd, *, segment_id, run_id, cwd=None, path=None, payload=None):
                if cmd == "uv sync --extra dev":
                    result = CommandResult(
                        exit_code=3,
                        stdout="",
                        stderr="sync failed\n",
                        duration_seconds=0.2,
                    )
                    append_event(
                        Event(
                            ts="2026-07-11T00:00:00Z",
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
                            },
                        ),
                        path=path,
                    )
                    return result
                return original_run_observed(
                    cmd,
                    segment_id=segment_id,
                    run_id=run_id,
                    cwd=cwd,
                    path=path,
                )

            with patch("loom.sandbox.run_observed", side_effect=fake_run_observed):
                with self.assertRaisesRegex(RuntimeError, "uv sync failed"):
                    create_sandbox(
                        segment_id="MAT-REQ-001/S1",
                        repo_path=repo_dir,
                        worktree_root=worktree_root,
                        run_id="run-sandbox-sync-fail",
                        events_path=events_path,
                    )

            sandbox_path = worktree_root / "MAT-REQ-001-S1"
            self.assertFalse(sandbox_path.exists())
            self.assertNotIn(
                "loom/MAT-REQ-001-S1",
                _git(repo_dir, "branch", "--list"),
            )

            rows, _ = load_event_rows(events_path, run_id="run-sandbox-sync-fail")
            command_rows = [row for row in rows if row["type"] == "command_run"]
            self.assertEqual(
                [(row["payload"]["cmd"], row["payload"]["exit_code"]) for row in command_rows],
                [
                    (
                        f"git worktree add -b loom/MAT-REQ-001-S1 {sandbox_path} main",
                        0,
                    ),
                    ("uv sync --extra dev", 3),
                    (f"git worktree remove --force {sandbox_path}", 0),
                    ("git branch -D loom/MAT-REQ-001-S1", 0),
                ],
            )

    def test_list_and_delete_retained_branches_only_touch_loom_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "lingua-web"
            repo_dir.mkdir()
            _init_main_repo(repo_dir)
            events_path = Path(tmpdir) / "events.jsonl"

            _git(repo_dir, "branch", "loom/MAT-REQ-001-S1")
            _git(repo_dir, "branch", "loom/MAT-REQ-001-S2")
            _git(repo_dir, "branch", "feature/not-loom")

            append_event(
                Event(
                    ts="2026-07-12T00:00:00Z",
                    segment_id="MAT-REQ-001/S1",
                    run_id="run-retained-001",
                    actor="harness",
                    type="artifact_retained",
                    payload={"branch_name": "loom/MAT-REQ-001-S1", "status": "passed"},
                ),
                path=events_path,
            )
            append_event(
                Event(
                    ts="2026-07-12T00:01:00Z",
                    segment_id="MAT-REQ-001/S2",
                    run_id="run-retained-002",
                    actor="harness",
                    type="artifact_retained",
                    payload={"branch_name": "loom/MAT-REQ-001-S2", "status": "failed"},
                ),
                path=events_path,
            )

            retained = list_retained_branches(repo_path=repo_dir, events_path=events_path)
            self.assertEqual(
                [asdict(item) for item in retained],
                [
                    {"branch_name": "loom/MAT-REQ-001-S1", "status": "passed"},
                    {"branch_name": "loom/MAT-REQ-001-S2", "status": "failed"},
                ],
            )

            delete_retained_branch(repo_path=repo_dir, branch_name="loom/MAT-REQ-001-S1")

            self.assertNotIn("loom/MAT-REQ-001-S1", _git(repo_dir, "branch", "--list"))
            self.assertIn("loom/MAT-REQ-001-S2", _git(repo_dir, "branch", "--list"))
            self.assertIn("feature/not-loom", _git(repo_dir, "branch", "--list"))
            self.assertEqual(
                [asdict(item) for item in list_retained_branches(repo_path=repo_dir, events_path=events_path)],
                [{"branch_name": "loom/MAT-REQ-001-S2", "status": "failed"}],
            )


if __name__ == "__main__":
    unittest.main()
