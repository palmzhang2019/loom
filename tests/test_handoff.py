from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.events import Event, append_event
from loom.view import load_event_rows


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "specs" / "MAT-REQ-001" / "segments" / "S1.yaml"
SEGMENT_ID = "MAT-REQ-001/S1"
RUN_ID = "run-handoff-001"
SOURCE_BRANCH = "loom/MAT-REQ-001-S1"


BASE_SOURCE = """\
from fastapi import APIRouter
from sqlalchemy import Column, Integer

router = APIRouter(prefix="/materials")


def normalize_tag(value: int) -> str:
    return str(value)


def delete_link(session, link) -> None:
    return None


class Material:
    __tablename__ = "materials"

    id = Column(Integer, primary_key=True)
"""


HEAD_SOURCE = """\
from fastapi import APIRouter
from sqlalchemy import Column, Integer, String

router = APIRouter(prefix="/materials")


def normalize_tag(value: str) -> str:
    return value


def delete_link(session, link) -> None:
    session.delete(link)
    session.commit()


def _internal_helper() -> None:
    pass


@router.delete("/{material_id}/source-tag/{tag_id}")
async def remove_material_tag(material_id: int, tag_id: int) -> bool:
    return True


class Material:
    __tablename__ = "materials"

    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=True)


class AuditLog:
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
"""


def _git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_seam_repo(repo_path: Path) -> None:
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.name", "Loom Tests")
    _git(repo_path, "config", "user.email", "loom-tests@example.com")
    source_path = repo_path / "app" / "models_and_routes.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(BASE_SOURCE, encoding="utf-8")
    _git(repo_path, "add", "app/models_and_routes.py")
    _git(repo_path, "commit", "-m", "baseline")

    _git(repo_path, "switch", "-c", SOURCE_BRANCH)
    source_path.write_text(HEAD_SOURCE, encoding="utf-8")
    _git(repo_path, "add", "app/models_and_routes.py")
    _git(repo_path, "commit", "-m", "segment artifact")
    _git(repo_path, "switch", "main")


def _top_level_keys(text: str) -> list[str]:
    return [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith(" ")
    ]


def _handoff_path(root: Path, run_id: str = RUN_ID) -> Path:
    return root / "runs" / run_id / "handoff" / "handoff.yaml"


def _append_observed_test_files(events_path: Path) -> None:
    append_event(
        Event(
            ts="2026-07-14T00:00:00Z",
            segment_id=SEGMENT_ID,
            run_id=RUN_ID,
            actor="test",
            type="step_finished",
            payload={
                "step": "test",
                "result": {
                    "test_result": {
                        "status": "passed",
                        "test_selectors": [
                            "tests/test_s3t_tagging.py",
                            "tests/test_s4bb_material_tag_wiring.py",
                        ],
                    }
                },
            },
        ),
        path=events_path,
    )


class HandoffRecordTests(unittest.TestCase):
    def test_pending_handoff_extracts_only_structural_seams_and_contract_fields(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_seam_repo(repo_path)
            events_path = root / "events.jsonl"
            _append_observed_test_files(events_path)

            record_path = handoff.generate_pending_handoff(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                source_branch=SOURCE_BRANCH,
                events_path=events_path,
                execution_repo_path=repo_path,
            )

            self.assertEqual(record_path, _handoff_path(root))
            text = record_path.read_text(encoding="utf-8")
            self.assertEqual(
                _top_level_keys(text),
                [
                    "covers_req",
                    "merge_status",
                    "seams",
                    "deferred",
                    "key_decisions",
                    "pointers",
                ],
            )
            self.assertIn("covers_req: MAT-REQ-001", text)
            self.assertIn("merge_status: pending", text)
            self.assertIn(
                'signature: "DELETE /materials/{material_id}/source-tag/{tag_id}"',
                text,
            )
            self.assertIn(
                'signature: "async remove_material_tag(material_id: int, tag_id: int) -> bool"',
                text,
            )
            self.assertIn('signature: "normalize_tag(value: str) -> str"', text)
            self.assertIn('signature: "table audit_log"', text)
            self.assertIn(
                'signature: "column audit_log.id: Column(Integer, primary_key=True)"',
                text,
            )
            self.assertIn(
                'signature: "column materials.source: Column(String, nullable=True)"',
                text,
            )
            self.assertNotIn("_internal_helper", text)
            self.assertNotIn("session.delete", text)
            self.assertNotIn("delete_link", text)
            self.assertEqual(text.count("kind: db"), 3)
            self.assertIn('text: "前端移除入口与确认弹窗"', text)
            self.assertIn("origin: contract", text)
            self.assertIn("defer_to: MAT-REQ-001/S2", text)
            self.assertIn('text: "删除后页面重定向与反馈"', text)
            self.assertNotIn("批量移除", text)
            self.assertIn("key_decisions: []", text)
            self.assertIn("merge_commit: null", text)
            self.assertIn("test_files:", text)
            self.assertIn("- tests/test_s3t_tagging.py", text)
            self.assertIn("- tests/test_s4bb_material_tag_wiring.py", text)
            self.assertIn("as_built_diagram: null", text)

            rows, invalid_lines = load_event_rows(events_path, run_id=RUN_ID)
            self.assertEqual(invalid_lines, [])
            generated = [row for row in rows if row["type"] == "handoff_generated"]
            self.assertEqual(len(generated), 1)
            self.assertEqual(generated[0]["actor"], "harness")
            self.assertEqual(
                generated[0]["payload"],
                {"path": str(record_path), "merge_status": "pending"},
            )

    def test_pending_handoff_does_not_claim_unobserved_contract_tests(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_seam_repo(repo_path)
            events_path = root / "events.jsonl"

            record_path = handoff.generate_pending_handoff(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                source_branch=SOURCE_BRANCH,
                events_path=events_path,
                execution_repo_path=repo_path,
            )

            text = record_path.read_text(encoding="utf-8")
            self.assertIn("  test_files: []", text)
            self.assertNotIn("tests/test_s3t_tagging.py", text)
            self.assertNotIn("tests/test_s4bb_material_tag_wiring.py", text)

    def test_extractor_avoids_route_and_db_false_positives(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        before = """\
from fastapi import APIRouter
from sqlalchemy import Column, String

router = APIRouter(prefix="/items")

@router.get("/")
def list_items(limit: int = 10) -> list[str]:
    return []

def maintenance() -> None:
    pass

class Item:
    __tablename__ = "items"
    name = Column(String, index=True, nullable=True)
"""
        after = """\
from fastapi import APIRouter
from sqlalchemy import Column, String

router = APIRouter(prefix="/items")

@router.get("/")
def list_items(limit: str = "10") -> list[str]:
    return []

def maintenance() -> None:
    _add_column_if_missing("items", "shadow", "TEXT")

class Item:
    __tablename__ = "items"
    name = Column(String, nullable=True, index=True)
"""

        seams = handoff.extract_python_seams(
            [handoff.SourcePair(path="app/items.py", before=before, after=after)]
        )

        self.assertEqual(
            [(seam.kind, seam.signature) for seam in seams],
            [("function", "list_items(limit: str='10') -> list[str]")],
        )

    def test_adding_one_route_decorator_does_not_redeclare_existing_routes(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        before = """\
from fastapi import APIRouter

router = APIRouter(prefix="/items")

@router.get("/a")
@router.get("/b")
def list_items() -> list[str]:
    return []
"""
        after = before.replace(
            '@router.get("/a")',
            '@router.get("/new")\n@router.get("/a")',
        )

        seams = handoff.extract_python_seams(
            [handoff.SourcePair(path="app/items.py", before=before, after=after)]
        )

        self.assertEqual(
            [(seam.kind, seam.signature) for seam in seams],
            [("route", "GET /items/new")],
        )

    def test_approve_updates_pending_record_to_merged_with_actual_commit(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_seam_repo(repo_path)
            events_path = root / "events.jsonl"
            record_path = handoff.generate_pending_handoff(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                source_branch=SOURCE_BRANCH,
                events_path=events_path,
                execution_repo_path=repo_path,
            )

            updated_path = handoff.mark_handoff_merged(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                merge_commit="abc123merge",
                events_path=events_path,
            )

            self.assertEqual(updated_path, record_path)
            text = record_path.read_text(encoding="utf-8")
            self.assertIn("merge_status: merged", text)
            self.assertIn('merge_commit: "abc123merge"', text)
            self.assertIn("seams:", text)

            rows, _ = load_event_rows(events_path, run_id=RUN_ID)
            updated = [row for row in rows if row["type"] == "handoff_updated"]
            self.assertEqual(len(updated), 1)
            self.assertEqual(updated[0]["actor"], "harness")
            self.assertEqual(
                updated[0]["payload"],
                {"path": str(record_path), "merge_status": "merged"},
            )

    def test_reject_keeps_only_contract_data_reason_and_rejected_status(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_seam_repo(repo_path)
            events_path = root / "events.jsonl"
            record_path = handoff.generate_pending_handoff(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                source_branch=SOURCE_BRANCH,
                events_path=events_path,
                execution_repo_path=repo_path,
            )

            updated_path = handoff.mark_handoff_rejected(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                reject_reason="human requested changes",
                events_path=events_path,
            )

            self.assertEqual(updated_path, record_path)
            text = record_path.read_text(encoding="utf-8")
            self.assertEqual(
                _top_level_keys(text),
                ["covers_req", "merge_status", "reject_reason", "deferred"],
            )
            self.assertIn("covers_req: MAT-REQ-001", text)
            self.assertIn("merge_status: rejected", text)
            self.assertIn('reject_reason: "human requested changes"', text)
            self.assertIn('text: "前端移除入口与确认弹窗"', text)
            self.assertNotIn("seams:", text)
            self.assertNotIn("pointers:", text)
            self.assertNotIn("key_decisions:", text)

            rows, _ = load_event_rows(events_path, run_id=RUN_ID)
            updated = [row for row in rows if row["type"] == "handoff_updated"]
            self.assertEqual(len(updated), 1)
            self.assertEqual(
                updated[0]["payload"],
                {"path": str(record_path), "merge_status": "rejected"},
            )

    def test_rejected_update_requires_pending_state_and_human_reason(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _init_seam_repo(repo_path)
            events_path = root / "events.jsonl"
            handoff.generate_pending_handoff(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                source_branch=SOURCE_BRANCH,
                events_path=events_path,
                execution_repo_path=repo_path,
            )

            with self.assertRaisesRegex(ValueError, "reject_reason"):
                handoff.mark_handoff_rejected(
                    contract_path=CONTRACT_PATH,
                    run_id=RUN_ID,
                    reject_reason="   ",
                    events_path=events_path,
                )

            handoff.mark_handoff_merged(
                contract_path=CONTRACT_PATH,
                run_id=RUN_ID,
                merge_commit="abc123merge",
                events_path=events_path,
            )
            with self.assertRaisesRegex(ValueError, "pending"):
                handoff.mark_handoff_rejected(
                    contract_path=CONTRACT_PATH,
                    run_id=RUN_ID,
                    reject_reason="late rejection",
                    events_path=events_path,
                )

    def test_cli_materializes_existing_rejection_without_diff_or_seams(self) -> None:
        handoff = importlib.import_module("loom.handoff")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            _git(repo_path, "init", "-b", "main")
            events_path = root / "events.jsonl"
            append_event(
                Event(
                    ts="2026-07-14T13:58:35Z",
                    segment_id=SEGMENT_ID,
                    run_id=RUN_ID,
                    actor="harness",
                    type="merge_rejected",
                    payload={
                        "source_branch": SOURCE_BRANCH,
                        "reason": "existing human rejection",
                    },
                ),
                path=events_path,
            )

            exit_code = handoff.main(
                [
                    "--contract",
                    str(CONTRACT_PATH),
                    "--run-id",
                    RUN_ID,
                    "--branch",
                    SOURCE_BRANCH,
                    "--events",
                    str(events_path),
                    "--repo",
                    str(repo_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            record_path = _handoff_path(root)
            text = record_path.read_text(encoding="utf-8")
            self.assertEqual(
                _top_level_keys(text),
                ["covers_req", "merge_status", "reject_reason", "deferred"],
            )
            self.assertIn('reject_reason: "existing human rejection"', text)
            self.assertNotIn("seams:", text)

            rows, invalid_lines = load_event_rows(events_path, run_id=RUN_ID)
            self.assertEqual(invalid_lines, [])
            self.assertFalse(any(row["type"] == "command_run" for row in rows))
            generated = [row for row in rows if row["type"] == "handoff_generated"]
            self.assertEqual(
                [row["payload"] for row in generated],
                [{"path": str(record_path), "merge_status": "rejected"}],
            )

    def test_only_handoff_yaml_is_git_trackable_under_runs(self) -> None:
        handoff_result = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "runs/example/handoff/handoff.yaml",
            ],
            cwd=ROOT,
            check=False,
        )
        review_result = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "runs/example/review/review.md",
            ],
            cwd=ROOT,
            check=False,
        )
        unrelated_handoff_result = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--no-index",
                "runs/example/handoff/raw-trace.txt",
            ],
            cwd=ROOT,
            check=False,
        )

        self.assertEqual(handoff_result.returncode, 1)
        self.assertEqual(review_result.returncode, 0)
        self.assertEqual(unrelated_handoff_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
