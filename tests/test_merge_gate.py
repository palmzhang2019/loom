from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.events import Event, append_event
from loom.sandbox import list_retained_branches
from loom.view import load_event_rows


CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "specs"
    / "MAT-REQ-001"
    / "segments"
    / "S1.yaml"
)
SEGMENT_ID = "MAT-REQ-001/S1"
RUN_ID = "run-merge-gate-001"
SOURCE_BRANCH = "loom/MAT-REQ-001-S1"
TARGET_BRANCH = "loom/rehearsal"


def _git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _branch_exists(repo_path: Path, branch_name: str) -> bool:
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_path,
        check=False,
    )
    return completed.returncode == 0


def _init_merge_repo(repo_path: Path) -> tuple[str, str]:
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.name", "Loom Tests")
    _git(repo_path, "config", "user.email", "loom-tests@example.com")
    (repo_path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-m", "baseline")
    main_commit = _git(repo_path, "rev-parse", "main").strip()

    _git(repo_path, "switch", "-c", SOURCE_BRANCH)
    artifact_path = repo_path / "app" / "routes" / "upload.py"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "",
                'router = APIRouter(prefix="/materials")',
                "",
                '@router.post("/{material_id}/tag/remove")',
                "async def remove_material_tag(material_id: int, tag_id: int) -> bool:",
                "    return True",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _git(repo_path, "add", "app/routes/upload.py")
    _git(repo_path, "commit", "-m", "segment artifact")
    source_commit = _git(repo_path, "rev-parse", "HEAD").strip()
    _git(repo_path, "switch", "main")
    _git(repo_path, "branch", "feature/not-loom")
    return main_commit, source_commit


def _append_gate_inputs(root: Path, *, audit_verdict: str) -> Path:
    events_path = root / "events.jsonl"
    review_path = root / "runs" / RUN_ID / "review" / "review.md"
    audit_path = root / "runs" / RUN_ID / "audit" / "audit.md"
    review_path.parent.mkdir(parents=True)
    audit_path.parent.mkdir(parents=True)
    review_path.write_text("# Review\n\nSoft advice only.\n", encoding="utf-8")
    audit_path.write_text(
        f"# Audit\n\n- overall verdict: `{audit_verdict}`\n",
        encoding="utf-8",
    )
    append_event(
        Event(
            ts="2026-07-14T00:00:00Z",
            segment_id=SEGMENT_ID,
            run_id=RUN_ID,
            actor="harness",
            type="artifact_retained",
            payload={"branch_name": SOURCE_BRANCH, "status": "passed"},
        ),
        path=events_path,
    )
    append_event(
        Event(
            ts="2026-07-14T00:01:00Z",
            segment_id=SEGMENT_ID,
            run_id=RUN_ID,
            actor="harness",
            type="review_completed",
            payload={
                "report_path": str(review_path),
                "reviewed_branch": SOURCE_BRANCH,
            },
        ),
        path=events_path,
    )
    append_event(
        Event(
            ts="2026-07-14T00:02:00Z",
            segment_id=SEGMENT_ID,
            run_id=RUN_ID,
            actor="harness",
            type="audit_completed",
            payload={
                "report_path": str(audit_path),
                "audited_branch": SOURCE_BRANCH,
                "verdict": audit_verdict,
            },
        ),
        path=events_path,
    )
    return events_path


def _run_merge_gate(
    *,
    repo_path: Path,
    events_path: Path,
    decision_provider,
):
    merge_gate = importlib.import_module("loom.merge_gate")
    return merge_gate.run_merge_gate(
        contract_path=CONTRACT_PATH,
        run_id=RUN_ID,
        source_branch=SOURCE_BRANCH,
        events_path=events_path,
        execution_repo_path=repo_path,
        decision_provider=decision_provider,
    )


def _handoff_path(root: Path) -> Path:
    return root / "runs" / RUN_ID / "handoff" / "handoff.yaml"


def _top_level_keys(text: str) -> list[str]:
    return [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith(" ")
    ]


class HumanMergeGateTests(unittest.TestCase):
    def test_blocked_audit_refuses_before_interrupt_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            main_commit, _ = _init_merge_repo(repo_path)
            events_path = _append_gate_inputs(root, audit_verdict="blocked")
            presentations: list[dict[str, object]] = []

            result = _run_merge_gate(
                repo_path=repo_path,
                events_path=events_path,
                decision_provider=lambda value: presentations.append(value) or {"decision": "approve"},
            )

            self.assertEqual(result["status"], "refused")
            self.assertEqual(presentations, [])
            self.assertTrue(_branch_exists(repo_path, SOURCE_BRANCH))
            self.assertFalse(_branch_exists(repo_path, TARGET_BRANCH))
            self.assertEqual(_git(repo_path, "rev-parse", "main").strip(), main_commit)

            rows, invalid_lines = load_event_rows(events_path, run_id=RUN_ID)
            self.assertEqual(invalid_lines, [])
            refused = [row for row in rows if row["type"] == "merge_refused"]
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["actor"], "harness")
            self.assertEqual(
                refused[0]["payload"],
                {
                    "source_branch": SOURCE_BRANCH,
                    "target": TARGET_BRANCH,
                    "reason": "audit_blocked",
                },
            )
            self.assertFalse(
                any(row["type"] in {"merge_completed", "merge_rejected"} for row in rows)
            )
            self.assertFalse(_handoff_path(root).exists())
            self.assertFalse(
                any(row["type"] in {"handoff_generated", "handoff_updated"} for row in rows)
            )

    def test_passed_audit_and_human_approve_merges_rehearsal_and_deletes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            main_commit, source_commit = _init_merge_repo(repo_path)
            events_path = _append_gate_inputs(root, audit_verdict="passed")
            presentations: list[dict[str, object]] = []
            pending_records: list[str] = []

            def approve(value: dict[str, object]) -> dict[str, str]:
                presentations.append(value)
                pending_records.append(_handoff_path(root).read_text(encoding="utf-8"))
                return {"decision": "approve"}

            result = _run_merge_gate(
                repo_path=repo_path,
                events_path=events_path,
                decision_provider=approve,
            )

            self.assertEqual(len(presentations), 1)
            self.assertEqual(len(pending_records), 1)
            self.assertIn("merge_status: pending", pending_records[0])
            self.assertIn(
                'signature: "POST /materials/{material_id}/tag/remove"',
                pending_records[0],
            )
            self.assertEqual(
                presentations[0],
                {
                    "segment_id": SEGMENT_ID,
                    "run_id": RUN_ID,
                    "review_report_path": str(root / "runs" / RUN_ID / "review" / "review.md"),
                    "audit_report_path": str(root / "runs" / RUN_ID / "audit" / "audit.md"),
                    "audit_verdict": "passed",
                    "source_branch": SOURCE_BRANCH,
                    "target": TARGET_BRANCH,
                },
            )
            self.assertEqual(result["status"], "merged")
            self.assertFalse(_branch_exists(repo_path, SOURCE_BRANCH))
            self.assertTrue(_branch_exists(repo_path, TARGET_BRANCH))
            self.assertTrue(_branch_exists(repo_path, "feature/not-loom"))
            self.assertEqual(_git(repo_path, "rev-parse", "main").strip(), main_commit)
            merge_commit = _git(repo_path, "rev-parse", TARGET_BRANCH).strip()
            self.assertEqual(result["merge_commit"], merge_commit)
            self.assertEqual(
                len(_git(repo_path, "show", "-s", "--format=%P", merge_commit).split()),
                2,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", source_commit, TARGET_BRANCH],
                    cwd=repo_path,
                    check=False,
                ).returncode,
                0,
            )
            merge_message = _git(repo_path, "show", "-s", "--format=%B", merge_commit)
            self.assertIn(f"segment_id={SEGMENT_ID}", merge_message)
            self.assertIn(f"run_id={RUN_ID}", merge_message)
            final_handoff = _handoff_path(root).read_text(encoding="utf-8")
            self.assertIn("merge_status: merged", final_handoff)
            self.assertIn(f'merge_commit: "{merge_commit}"', final_handoff)
            self.assertIn("seams:", final_handoff)

            rows, _ = load_event_rows(events_path, run_id=RUN_ID)
            completed = [row for row in rows if row["type"] == "merge_completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["actor"], "harness")
            self.assertEqual(
                completed[0]["payload"],
                {
                    "source_branch": SOURCE_BRANCH,
                    "target": TARGET_BRANCH,
                    "merge_commit": merge_commit,
                },
            )
            lifecycle = [
                row["type"]
                for row in rows
                if row["type"]
                in {"handoff_generated", "merge_completed", "handoff_updated"}
            ]
            self.assertEqual(
                lifecycle,
                ["handoff_generated", "merge_completed", "handoff_updated"],
            )
            commands = [
                row["payload"]["cmd"]
                for row in rows
                if row["type"] == "command_run"
            ]
            self.assertFalse(any("push" in command for command in commands))
            self.assertFalse(any("worktree prune" in command for command in commands))

    def test_passed_audit_and_human_reject_keeps_source_and_marks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            main_commit, _ = _init_merge_repo(repo_path)
            events_path = _append_gate_inputs(root, audit_verdict="passed")
            presentations: list[dict[str, object]] = []
            pending_records: list[str] = []

            def reject(value: dict[str, object]) -> dict[str, str]:
                presentations.append(value)
                pending_records.append(_handoff_path(root).read_text(encoding="utf-8"))
                return {"decision": "reject", "reason": "human requested changes"}

            result = _run_merge_gate(
                repo_path=repo_path,
                events_path=events_path,
                decision_provider=reject,
            )

            self.assertEqual(len(presentations), 1)
            self.assertEqual(len(pending_records), 1)
            self.assertIn("merge_status: pending", pending_records[0])
            self.assertIn("seams:", pending_records[0])
            self.assertEqual(result["status"], "rejected")
            self.assertTrue(_branch_exists(repo_path, SOURCE_BRANCH))
            self.assertFalse(_branch_exists(repo_path, TARGET_BRANCH))
            self.assertTrue(_branch_exists(repo_path, "feature/not-loom"))
            self.assertEqual(_git(repo_path, "rev-parse", "main").strip(), main_commit)

            rows, _ = load_event_rows(events_path, run_id=RUN_ID)
            rejected = [row for row in rows if row["type"] == "merge_rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["actor"], "harness")
            self.assertEqual(
                rejected[0]["payload"],
                {
                    "source_branch": SOURCE_BRANCH,
                    "reason": "human requested changes",
                },
            )
            retained = list_retained_branches(
                repo_path=repo_path,
                events_path=events_path,
            )
            self.assertIn(
                {"branch_name": SOURCE_BRANCH, "status": "rejected"},
                [asdict(item) for item in retained],
            )
            self.assertFalse(any(row["type"] == "merge_completed" for row in rows))
            final_handoff = _handoff_path(root).read_text(encoding="utf-8")
            self.assertEqual(
                _top_level_keys(final_handoff),
                ["covers_req", "merge_status", "reject_reason", "deferred"],
            )
            self.assertIn("merge_status: rejected", final_handoff)
            self.assertIn('reject_reason: "human requested changes"', final_handoff)
            self.assertNotIn("seams:", final_handoff)
            lifecycle = [
                row["type"]
                for row in rows
                if row["type"]
                in {"handoff_generated", "merge_rejected", "handoff_updated"}
            ]
            self.assertEqual(
                lifecycle,
                ["handoff_generated", "merge_rejected", "handoff_updated"],
            )


if __name__ == "__main__":
    unittest.main()
