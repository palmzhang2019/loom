from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.events import Event, append_event
from loom.harness import CommandResult
from loom.review import ReviewSessionInput, _run_codex_review_session, run_segment_review
from loom.view import load_event_rows


CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "specs"
    / "MAT-REQ-001"
    / "segments"
    / "S1.yaml"
)
BRANCH_NAME = "loom/MAT-REQ-001-S1"
RUN_ID = "run-review-001"


def _git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_review_repo(repo_path: Path) -> None:
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.name", "Loom Tests")
    _git(repo_path, "config", "user.email", "loom-tests@example.com")
    app_path = repo_path / "app" / "routes"
    app_path.mkdir(parents=True)
    (app_path / "upload.py").write_text("baseline\n", encoding="utf-8")
    _git(repo_path, "add", "app/routes/upload.py")
    _git(repo_path, "commit", "-m", "baseline")
    _git(repo_path, "switch", "-c", BRANCH_NAME)
    (app_path / "upload.py").write_text("baseline\nremove route\n", encoding="utf-8")
    _git(repo_path, "add", "app/routes/upload.py")
    _git(repo_path, "commit", "-m", "segment artifact")
    _git(repo_path, "switch", "main")


def _append_files_changed(events_path: Path, paths: list[str]) -> None:
    append_event(
        Event(
            ts="2026-07-13T00:00:00Z",
            segment_id="MAT-REQ-001/S1",
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


def _mock_review(input_data: ReviewSessionInput) -> dict[str, object]:
    return {
        "opinions": [
            {
                "acceptance_id": item.id,
                "opinion": "满足",
                "reason": f"diff 中有与 {item.id} 对应的实现",
            }
            for item in input_data.acceptance
        ],
        "summary": "LLM 建议人类结合测试结果继续检查该实现。",
    }


class SegmentReviewTests(unittest.TestCase):
    def test_in_scope_changes_generate_all_ac_opinions_report_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_review_repo(repo_path)
            events_path = root / "events.jsonl"
            _append_files_changed(events_path, ["app/routes/upload.py"])
            received: list[ReviewSessionInput] = []

            def review_runner(input_data: ReviewSessionInput) -> dict[str, object]:
                received.append(input_data)
                return _mock_review(input_data)

            report_path = run_segment_review(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                reviewed_branch=BRANCH_NAME,
                events_path=events_path,
                execution_repo_path=repo_path,
                review_runner=review_runner,
            )

            self.assertEqual(report_path, root / "runs" / RUN_ID / "review" / "review.md")
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("## 硬事实(harness 观测)", report)
            self.assertIn("scope 检查结果: 合规", report)
            self.assertIn("app/routes/upload.py", report)
            self.assertIn("## LLM 建议(供人参考)", report)
            self.assertEqual(report.count("LLM意见:满足"), 4)
            for index in range(1, 5):
                self.assertIn(f"MAT-REQ-001/S1/AC{index}", report)
            self.assertNotIn("PASS", report)
            self.assertNotIn("FAIL", report)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].reviewed_branch, BRANCH_NAME)
            self.assertIn("diff --git a/app/routes/upload.py", received[0].diff)
            self.assertEqual(len(received[0].acceptance), 4)

            rows, invalid_lines = load_event_rows(events_path, run_id=RUN_ID)
            self.assertEqual(invalid_lines, [])
            completed = [row for row in rows if row["type"] == "review_completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["actor"], "harness")
            self.assertEqual(
                completed[0]["payload"],
                {
                    "report_path": str(report_path),
                    "reviewed_branch": BRANCH_NAME,
                },
            )

    def test_out_of_scope_change_is_reported_without_blocking_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_review_repo(repo_path)
            events_path = root / "events.jsonl"
            _append_files_changed(
                events_path,
                ["app/routes/upload.py", "tests/test_s3t_tagging.py"],
            )

            report_path = run_segment_review(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                reviewed_branch=BRANCH_NAME,
                events_path=events_path,
                execution_repo_path=repo_path,
                review_runner=_mock_review,
            )

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("scope 检查结果: 越界", report)
            self.assertIn("越界文件:", report)
            self.assertIn("tests/test_s3t_tagging.py", report)
            self.assertEqual(report.count("LLM意见:满足"), 4)

    def test_default_review_runner_uses_fresh_read_only_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            events_path = root / "events.jsonl"
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            input_data = ReviewSessionInput(
                segment_id="MAT-REQ-001/S1",
                run_id=RUN_ID,
                reviewed_branch=BRANCH_NAME,
                acceptance=[],
                diff="diff --git a/app/routes/upload.py b/app/routes/upload.py\n",
                execution_repo_path=str(repo_path),
                events_path=str(events_path),
            )

            def fake_run_observed(
                cmd, *, segment_id, run_id, cwd=None, path=None, payload=None
            ):
                output_path = root / "runs" / RUN_ID / "review" / "codex-review.json"
                output_path.write_text(
                    json.dumps({"opinions": [], "summary": "建议摘要"}),
                    encoding="utf-8",
                )
                self.assertIn("codex exec", cmd)
                self.assertIn("--sandbox read-only", cmd)
                self.assertIn("--ephemeral", cmd)
                self.assertEqual(Path(cwd), repo_path)
                return CommandResult(0, "", "", 0.1)

            with patch("loom.review.run_observed", side_effect=fake_run_observed):
                result = _run_codex_review_session(input_data)

            self.assertEqual(result, {"opinions": [], "summary": "建议摘要"})


if __name__ == "__main__":
    unittest.main()
