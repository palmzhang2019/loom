from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.graph import run_segment_graph
from loom.view import load_event_rows, render_html_document


class P3ALangGraphTests(unittest.TestCase):
    def test_run_segment_graph_uses_sandbox_and_emits_node_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            execution_repo = Path(tmpdir) / "lingua-web"
            execution_repo.mkdir()
            _init_main_repo(execution_repo)
            worktree_root = Path(tmpdir) / "loom-worktrees"
            contract_path = (
                Path(__file__).resolve().parents[1]
                / "specs"
                / "MAT-REQ-001"
                / "segments"
                / "S1.yaml"
            )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
            )

            rows, invalid_lines = load_event_rows(
                events_path,
                run_id="run-graph-001",
                segment_id="MAT-REQ-001/S1",
            )

            self.assertEqual(invalid_lines, [])
            self.assertEqual(
                [(row["actor"], row["type"]) for row in rows if row["actor"] != "harness"],
                [
                    ("orchestrator", "step_started"),
                    ("orchestrator", "step_finished"),
                    ("work", "step_started"),
                    ("work", "step_finished"),
                    ("test", "step_started"),
                    ("test", "step_finished"),
                ],
            )
            self.assertEqual(state["segment"]["segment_id"], "MAT-REQ-001/S1")
            self.assertEqual(state["step"]["segment_id"], "MAT-REQ-001/S1")
            self.assertIn("fake diff", state["work_result"]["diff"])
            self.assertTrue(state["test_result"]["passed"])
            orchestrator_finished = next(
                row
                for row in rows
                if row["actor"] == "orchestrator" and row["type"] == "step_finished"
            )
            self.assertNotIn("raw_text", orchestrator_finished["payload"]["result"]["segment"])
            self.assertEqual(
                orchestrator_finished["payload"]["result"]["segment"],
                {
                    "contract_path": str(contract_path),
                    "acceptance_ids": [
                        "MAT-REQ-001/S1/AC1",
                        "MAT-REQ-001/S1/AC2",
                        "MAT-REQ-001/S1/AC3",
                        "MAT-REQ-001/S1/AC4",
                    ],
                    "segment_id": "MAT-REQ-001/S1",
                    "covers_req": "MAT-REQ-001",
                },
            )
            self.assertFalse((worktree_root / "MAT-REQ-001-S1").exists())
            command_runs = [row for row in rows if row["type"] == "command_run"]
            self.assertGreaterEqual(len(command_runs), 4)
            work_finished = next(
                row
                for row in rows
                if row["actor"] == "work" and row["type"] == "step_finished"
            )
            self.assertIn(
                str(worktree_root / "MAT-REQ-001-S1"),
                work_finished["payload"]["result"]["work_result"]["sandbox_path"],
            )

    def test_existing_view_renders_the_three_node_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            execution_repo = Path(tmpdir) / "lingua-web"
            execution_repo.mkdir()
            _init_main_repo(execution_repo)
            worktree_root = Path(tmpdir) / "loom-worktrees"
            contract_path = (
                Path(__file__).resolve().parents[1]
                / "specs"
                / "MAT-REQ-001"
                / "segments"
                / "S1.yaml"
            )

            run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-view",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
            )

            rows, invalid_lines = load_event_rows(
                events_path,
                run_id="run-graph-view",
                segment_id="MAT-REQ-001/S1",
            )
            html = render_html_document(
                rows,
                invalid_lines,
                source_path=events_path,
                run_id="run-graph-view",
                segment_id="MAT-REQ-001/S1",
            )

            self.assertIn("orchestrator", html)
            self.assertIn("work", html)
            self.assertIn("test", html)
            self.assertIn("step_started", html)
            self.assertIn("step_finished", html)
            self.assertIn("MAT-REQ-001/S1", html)

    def test_test_session_receives_only_contract_spec_and_not_work_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            execution_repo = Path(tmpdir) / "lingua-web"
            execution_repo.mkdir()
            _init_main_repo(execution_repo)
            worktree_root = Path(tmpdir) / "loom-worktrees"
            contract_path = (
                Path(__file__).resolve().parents[1]
                / "specs"
                / "MAT-REQ-001"
                / "segments"
                / "S1.yaml"
            )
            nonce = "P3C-NONCE-STRUCTURAL-ISOLATION"
            captured_test_inputs = []

            def mock_work_session(work_input) -> dict[str, object]:
                self.assertEqual(work_input.segment_id, "MAT-REQ-001/S1")
                return {
                    "step_id": work_input.step_id,
                    "summary": "mock work completed",
                    "sandbox_path": work_input.sandbox_path,
                    "branch_name": work_input.branch_name,
                    "observed_top_level": work_input.sandbox_path,
                    "diff": f"fake diff carrying {nonce}",
                    "files_touched": [],
                }

            def mock_test_session(test_input) -> dict[str, object]:
                captured_test_inputs.append(test_input)
                return {
                    "passed": True,
                    "summary": "mock tests passed",
                    "evidence": "fixed-pass mock",
                }

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-p3c-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=mock_work_session,
                test_runner=mock_test_session,
            )

            self.assertIn(nonce, state["work_result"]["diff"])
            self.assertEqual(len(captured_test_inputs), 1)

            received_input = captured_test_inputs[0]
            self.assertEqual(
                list(asdict(received_input).keys()),
                ["acceptance", "sequence_diagram"],
            )
            self.assertEqual(
                asdict(received_input)["acceptance"],
                [
                    {
                        "id": "MAT-REQ-001/S1/AC1",
                        "text": '存在一个后端路由,接收"移除某个来源标签关联"的请求',
                    },
                    {
                        "id": "MAT-REQ-001/S1/AC2",
                        "text": "删除的是【标签与素材的关联记录】,不是标签本身",
                    },
                    {
                        "id": "MAT-REQ-001/S1/AC3",
                        "text": "删除成功后,该素材的来源标签列表中不再包含被移除项",
                    },
                    {
                        "id": "MAT-REQ-001/S1/AC4",
                        "text": "对不存在的关联发起删除,返回明确失败/无操作,不报 500",
                    },
                ],
            )
            self.assertEqual(
                received_input.sequence_diagram,
                "\n".join(
                    [
                        "请求 -> 路由: 移除关联(material_id, tag_id)",
                        "路由 -> 模型: 删除关联记录",
                        "模型 --> 路由: 删除结果",
                        "路由 --> 请求: 成功/失败响应",
                    ]
                ),
            )
            self.assertFalse(hasattr(received_input, "diff"))
            self.assertFalse(hasattr(received_input, "summary"))
            self.assertFalse(hasattr(received_input, "files_touched"))
            self.assertNotIn(nonce, str(asdict(received_input)))

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


if __name__ == "__main__":
    unittest.main()
