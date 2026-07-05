from __future__ import annotations

import subprocess
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.harness import CommandResult, observe_files_changed, run_observed


def _git(git_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=git_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_git_repo(git_dir: Path) -> None:
    _git(git_dir, "init")
    _git(git_dir, "config", "user.name", "Loom Tests")
    _git(git_dir, "config", "user.email", "loom-tests@example.com")


def _commit_all(git_dir: Path, message: str) -> None:
    _git(git_dir, "add", ".")
    _git(git_dir, "commit", "-m", message)


class HarnessTests(unittest.TestCase):
    def test_run_observed_records_true_nonzero_exit_code(self) -> None:
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote('import sys; sys.exit(7)')}"

        with patch("loom.harness.append_event") as mock_append_event:
            result = run_observed(
                cmd,
                segment_id="MAT-REQ-001/S1",
                run_id="run-001",
            )

        self.assertIsInstance(result, CommandResult)
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(mock_append_event.call_count, 1)

        event = mock_append_event.call_args.args[0]
        self.assertEqual(event.actor, "harness")
        self.assertEqual(event.type, "command_run")
        self.assertEqual(event.payload["exit_code"], 7)

    def test_run_observed_captures_real_stdout_nonce(self) -> None:
        nonce = "nonce-7fd52d54c7db4f10"
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(f'print({nonce!r})')}"

        with patch("loom.harness.append_event") as mock_append_event:
            result = run_observed(
                cmd,
                segment_id="MAT-REQ-001/S1",
                run_id="run-001",
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn(nonce, result.stdout)
        self.assertEqual(mock_append_event.call_count, 1)

        event = mock_append_event.call_args.args[0]
        self.assertIn(nonce, event.payload["stdout"])

    def test_observe_files_changed_reports_actual_modified_file_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = Path(tmpdir)
            _init_git_repo(git_dir)
            target = git_dir / "tracked.txt"
            target.write_text("before\n", encoding="utf-8")
            _commit_all(git_dir, "baseline")

            with patch("loom.harness.append_event") as mock_append_event:
                changes = observe_files_changed(
                    lambda: target.write_text("after\n", encoding="utf-8"),
                    git_dir=git_dir,
                    segment_id="MAT-REQ-001/S1",
                    run_id="run-001",
                )

            expected_hash = _git(git_dir, "hash-object", "tracked.txt").strip()

            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].path, "tracked.txt")
            self.assertEqual(changes[0].change_type, "modified")
            self.assertEqual(changes[0].after_hash, expected_hash)

            event = mock_append_event.call_args.args[0]
            self.assertEqual(event.actor, "harness")
            self.assertEqual(event.type, "files_changed")
            self.assertEqual(
                event.payload["files"],
                [
                    {
                        "path": "tracked.txt",
                        "change_type": "modified",
                        "before_hash": changes[0].before_hash,
                        "after_hash": expected_hash,
                    }
                ],
            )

    def test_observe_files_changed_reports_actual_file_not_declared_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = Path(tmpdir)
            _init_git_repo(git_dir)
            declared = git_dir / "declared.txt"
            actual = git_dir / "actual.txt"
            declared.write_text("declared\n", encoding="utf-8")
            actual.write_text("before\n", encoding="utf-8")
            _commit_all(git_dir, "baseline")

            declared_path = "declared.txt"

            with patch("loom.harness.append_event") as mock_append_event:
                changes = observe_files_changed(
                    lambda: actual.write_text("after\n", encoding="utf-8"),
                    git_dir=git_dir,
                    segment_id="MAT-REQ-001/S1",
                    run_id="run-001",
                )

            self.assertEqual(declared_path, "declared.txt")
            self.assertEqual([change.path for change in changes], ["actual.txt"])

            event = mock_append_event.call_args.args[0]
            self.assertEqual(
                [item["path"] for item in event.payload["files"]],
                ["actual.txt"],
            )


if __name__ == "__main__":
    unittest.main()
