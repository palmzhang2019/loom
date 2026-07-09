from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.sandbox import create_sandbox, destroy_sandbox
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
    _git(git_dir, "add", "README.md")
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

            destroy_sandbox(
                sandbox,
                run_id="run-sandbox-001",
                events_path=events_path,
            )

            self.assertFalse(sandbox.worktree_path.exists())
            self.assertNotIn(
                "loom/MAT-REQ-001-S1",
                _git(repo_dir, "branch", "--list"),
            )

            rows, _ = load_event_rows(events_path, run_id="run-sandbox-001")
            command_runs = [row for row in rows if row["type"] == "command_run"]
            self.assertGreaterEqual(len(command_runs), 3)


if __name__ == "__main__":
    unittest.main()
