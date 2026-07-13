from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.audit import run_segment_audit
from loom.events import Event, append_event
from loom.view import load_event_rows


CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "specs"
    / "MAT-REQ-001"
    / "segments"
    / "S1.yaml"
)
BRANCH_NAME = "loom/MAT-REQ-001-S1"
RUN_ID = "run-audit-001"
SEGMENT_ID = "MAT-REQ-001/S1"


def _git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_audit_repo(
    repo_path: Path,
    *,
    main_content: str = "baseline\n",
    branch_files: dict[str, str],
) -> None:
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.name", "Loom Tests")
    _git(repo_path, "config", "user.email", "loom-tests@example.com")
    upload_path = repo_path / "app" / "routes" / "upload.py"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text(main_content, encoding="utf-8")
    _git(repo_path, "add", "app/routes/upload.py")
    _git(repo_path, "commit", "-m", "baseline")

    _git(repo_path, "switch", "-c", BRANCH_NAME)
    for relative_path, content in branch_files.items():
        path = repo_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-m", "segment artifact")
    _git(repo_path, "switch", "main")


def _append_files_changed(events_path: Path, paths: list[str]) -> None:
    append_event(
        Event(
            ts="2026-07-13T00:00:00Z",
            segment_id=SEGMENT_ID,
            run_id=RUN_ID,
            actor="harness",
            type="files_changed",
            payload={
                "files": [
                    {
                        "path": path,
                        "change_type": "modified",
                        "before_hash": "before",
                        "after_hash": "after",
                    }
                    for path in paths
                ]
            },
        ),
        path=events_path,
    )


def _run_audit(root: Path, repo_path: Path, changed_paths: list[str]) -> Path:
    events_path = root / "events.jsonl"
    _append_files_changed(events_path, changed_paths)
    return run_segment_audit(
        contract_path=CONTRACT_PATH,
        run_id=RUN_ID,
        audited_branch=BRANCH_NAME,
        events_path=events_path,
        execution_repo_path=repo_path,
    )


class SegmentAuditTests(unittest.TestCase):
    def test_clean_in_scope_diff_passes_both_gates_and_records_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_audit_repo(
                repo_path,
                branch_files={"app/routes/upload.py": "baseline\nremove route\n"},
            )

            report_path = _run_audit(root, repo_path, ["app/routes/upload.py"])

            self.assertEqual(
                report_path,
                root / "runs" / RUN_ID / "audit" / "audit.md",
            )
            report = report_path.read_text(encoding="utf-8")
            self.assertIn(f"segment_id: `{SEGMENT_ID}`", report)
            self.assertIn(f"run_id: `{RUN_ID}`", report)
            self.assertIn(f"audited_branch: `{BRANCH_NAME}`", report)
            self.assertIn("scope gate: `passed`", report)
            self.assertIn("reason: `all_observed_changes_within_scope`", report)
            self.assertIn("secret scan gate: `passed`", report)
            self.assertIn("reason: `no_high_confidence_secret_detected`", report)
            self.assertIn("overall verdict: `passed`", report)
            self.assertNotIn("建议", report)
            self.assertNotIn("LLM", report)

            rows, invalid_lines = load_event_rows(root / "events.jsonl", run_id=RUN_ID)
            self.assertEqual(invalid_lines, [])
            completed = [row for row in rows if row["type"] == "audit_completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["actor"], "harness")
            self.assertEqual(
                completed[0]["payload"],
                {
                    "report_path": str(report_path),
                    "audited_branch": BRANCH_NAME,
                    "verdict": "passed",
                },
            )

    def test_out_of_scope_observed_change_blocks_scope_and_overall_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_audit_repo(
                repo_path,
                branch_files={
                    "app/routes/upload.py": "baseline\nremove route\n",
                    "tests/outside.py": "outside scope\n",
                },
            )

            report_path = _run_audit(
                root,
                repo_path,
                ["app/routes/upload.py", "tests/outside.py"],
            )

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("scope gate: `blocked`", report)
            self.assertIn("reason: `out_of_scope`", report)
            self.assertIn("out-of-scope files:", report)
            self.assertIn("`tests/outside.py`", report)
            self.assertIn("secret scan gate: `passed`", report)
            self.assertIn("overall verdict: `blocked`", report)

            rows, _ = load_event_rows(root / "events.jsonl", run_id=RUN_ID)
            completed = [row for row in rows if row["type"] == "audit_completed"]
            self.assertEqual(completed[0]["payload"]["verdict"], "blocked")

    def test_high_confidence_secrets_on_added_lines_block_without_echoing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            openai_key = "sk-" + "A" * 32
            aws_key = "AKIA" + "B" * 16
            api_key = "live-" + "3" * 24
            password = "RealPassword" + "7" * 8 + "!"
            token = "tok-" + "9" * 24
            _init_audit_repo(
                repo_path,
                branch_files={
                    "app/routes/upload.py": "\n".join(
                        [
                            "baseline",
                            f'client_secret = "{openai_key}"',
                            f'aws_access_key_id = "{aws_key}"',
                            f'api_key = "{api_key}"',
                            f'password = "{password}"',
                            f"token={token}",
                            "",
                        ]
                    )
                },
            )

            report_path = _run_audit(root, repo_path, ["app/routes/upload.py"])

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("scope gate: `passed`", report)
            self.assertIn("secret scan gate: `blocked`", report)
            self.assertIn("reason: `secret_detected`", report)
            self.assertIn("`app/routes/upload.py:2` — `openai_api_key`", report)
            self.assertIn("`app/routes/upload.py:3` — `aws_access_key_id`", report)
            self.assertIn("`app/routes/upload.py:4` — `api_key_assignment`", report)
            self.assertIn("`app/routes/upload.py:5` — `password_assignment`", report)
            self.assertIn("`app/routes/upload.py:6` — `token_assignment`", report)
            self.assertIn("overall verdict: `blocked`", report)

            event_log = (root / "events.jsonl").read_text(encoding="utf-8")
            secret_values = [openai_key, aws_key, api_key, password, token]
            self.assertFalse(any(value in report for value in secret_values))
            self.assertFalse(any(value in event_log for value in secret_values))

    def test_secret_present_only_on_deleted_line_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            removed_secret = "sk-" + "C" * 32
            _init_audit_repo(
                repo_path,
                main_content=f'baseline\napi_key = "{removed_secret}"\n',
                branch_files={"app/routes/upload.py": "baseline\n"},
            )

            report_path = _run_audit(root, repo_path, ["app/routes/upload.py"])

            report = report_path.read_text(encoding="utf-8")
            event_log = (root / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("secret scan gate: `passed`", report)
            self.assertIn("overall verdict: `passed`", report)
            self.assertNotIn(removed_secret, report)
            self.assertNotIn(removed_secret, event_log)


if __name__ == "__main__":
    unittest.main()
