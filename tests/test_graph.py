from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.events import Event, append_event
from loom.graph import _load_segment_contract, run_segment_graph
from loom.harness import CommandResult
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
                work_runner=_mock_work_session,
                test_runner=_mock_test_session,
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
                    "title": "后端移除来源标签的路由与删除逻辑",
                    "acceptance": [
                        {
                            "id": "MAT-REQ-001/S1/AC1",
                            "text": '在 app/routes/upload.py 中新增一个与现有 POST /materials/{material_id}/tag/add 对称的移除路由,接收"移除某个来源标签关联"的请求',
                        },
                        {
                            "id": "MAT-REQ-001/S1/AC2",
                            "text": "删除的是 app/models.py 中 MaterialKnowledgeTagLink 的关联记录,不是 KnowledgeTag 标签本身",
                        },
                        {
                            "id": "MAT-REQ-001/S1/AC3",
                            "text": "删除成功后,该素材详情页读取到的已挂载来源标签列表中不再包含被移除项",
                        },
                        {
                            "id": "MAT-REQ-001/S1/AC4",
                            "text": "对不存在的关联发起删除,返回明确失败/无操作,不报 500",
                        },
                    ],
                    "anti_scope": [
                        {
                            "text": "前端移除入口与确认弹窗",
                            "kind": "defer",
                            "defer_to": "MAT-REQ-001/S2",
                        },
                        {
                            "text": "删除后页面重定向与反馈",
                            "kind": "defer",
                            "defer_to": "MAT-REQ-001/S3",
                        },
                        {
                            "text": "批量移除",
                            "kind": "out_of_req",
                        },
                        {
                            "text": "KnowledgeTag 标签本身的增删(只删 MaterialKnowledgeTagLink 关联)",
                            "kind": "out_of_req",
                        },
                        {
                            "text": "app/routes/knowledge.py 的 tag 过滤/反查 source tags 读路径(删关联后自然反映,S1 不主动碰)",
                            "kind": "out_of_req",
                        },
                        {
                            "text": "app/services/tagging.py 的标签规范化/绑定逻辑(S1 只做解绑,不改绑定侧)",
                            "kind": "out_of_req",
                        },
                    ],
                    "scope_paths": [
                        "app/routes/upload.py",
                        "app/models.py",
                    ],
                    "test_selectors": [
                        "tests/test_s3t_tagging.py",
                        "tests/test_s4bb_material_tag_wiring.py",
                    ],
                    "sequence_diagram": "\n".join(
                        [
                            "请求 -> upload路由: 移除关联(material_id, tag_id)",
                            "upload路由 -> models(MaterialKnowledgeTagLink): 删除该关联记录",
                            "models --> upload路由: 删除结果(成功/无该关联)",
                            "upload路由 --> 请求: 成功/失败响应",
                        ]
                    ),
                },
            )
            self.assertFalse((worktree_root / "MAT-REQ-001-S1").exists())
            command_runs = [row for row in rows if row["type"] == "command_run"]
            self.assertEqual(len(command_runs), 4)
            self.assertEqual(
                [row["payload"]["cmd"] for row in command_runs].count(
                    f"git worktree add -b loom/MAT-REQ-001-S1 {worktree_root / 'MAT-REQ-001-S1'} main"
                ),
                1,
            )
            self.assertEqual(command_runs[1]["payload"]["cmd"], "uv sync --extra dev")
            self.assertEqual(command_runs[1]["payload"]["exit_code"], 0)
            work_finished = next(
                row
                for row in rows
                if row["actor"] == "work" and row["type"] == "step_finished"
            )
            self.assertIn(
                str(worktree_root / "MAT-REQ-001-S1"),
                work_finished["payload"]["result"]["work_result"]["sandbox_path"],
            )

    def test_run_segment_graph_emits_started_and_completed_events_for_each_command(self) -> None:
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
                run_id="run-graph-command-events-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=_mock_work_session,
                test_runner=_mock_test_session,
            )

            rows, _ = load_event_rows(
                events_path,
                run_id="run-graph-command-events-001",
                segment_id="MAT-REQ-001/S1",
            )
            started_rows = [row for row in rows if row["type"] == "command_started"]
            finished_rows = [row for row in rows if row["type"] == "command_run"]

            self.assertEqual(len(started_rows), 4)
            self.assertEqual(len(finished_rows), 4)
            self.assertEqual(
                [(row["payload"]["cmd"], row["payload"]["cwd"]) for row in started_rows],
                [(row["payload"]["cmd"], row["payload"]["cwd"]) for row in finished_rows],
            )
            for row in finished_rows:
                self.assertIn("exit_code", row["payload"])
                self.assertIn("duration_seconds", row["payload"])

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
                work_runner=_mock_work_session,
                test_runner=_mock_test_session,
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

    def test_run_segment_graph_rejects_duplicate_run_id_before_second_sandbox_create(self) -> None:
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
                run_id="run-graph-dup-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=_mock_work_session,
                test_runner=_mock_test_session,
            )

            with self.assertRaisesRegex(RuntimeError, "run_id already exists"):
                run_segment_graph(
                    contract_path=contract_path,
                    run_id="run-graph-dup-001",
                    events_path=events_path,
                    execution_repo_path=execution_repo,
                    worktree_root=worktree_root,
                    work_runner=_mock_work_session,
                    test_runner=_mock_test_session,
                )

            rows, _ = load_event_rows(
                events_path,
                run_id="run-graph-dup-001",
                segment_id="MAT-REQ-001/S1",
            )
            self.assertEqual(
                [
                    row["payload"]["cmd"]
                    for row in rows
                    if row["type"] == "command_run"
                    and row["payload"]["cmd"].startswith("git worktree add ")
                ],
                [
                    f"git worktree add -b loom/MAT-REQ-001-S1 {worktree_root / 'MAT-REQ-001-S1'} main",
                ],
            )

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
                ["acceptance", "sequence_diagram", "test_selectors"],
            )
            self.assertEqual(
                asdict(received_input)["acceptance"],
                [
                    {
                        "id": "MAT-REQ-001/S1/AC1",
                        "text": '在 app/routes/upload.py 中新增一个与现有 POST /materials/{material_id}/tag/add 对称的移除路由,接收"移除某个来源标签关联"的请求',
                    },
                    {
                        "id": "MAT-REQ-001/S1/AC2",
                        "text": "删除的是 app/models.py 中 MaterialKnowledgeTagLink 的关联记录,不是 KnowledgeTag 标签本身",
                    },
                    {
                        "id": "MAT-REQ-001/S1/AC3",
                        "text": "删除成功后,该素材详情页读取到的已挂载来源标签列表中不再包含被移除项",
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
                        "请求 -> upload路由: 移除关联(material_id, tag_id)",
                        "upload路由 -> models(MaterialKnowledgeTagLink): 删除该关联记录",
                        "models --> upload路由: 删除结果(成功/无该关联)",
                        "upload路由 --> 请求: 成功/失败响应",
                    ]
                ),
            )
            self.assertEqual(
                received_input.test_selectors,
                [
                    "tests/test_s3t_tagging.py",
                    "tests/test_s4bb_material_tag_wiring.py",
                ],
            )
            self.assertFalse(hasattr(received_input, "diff"))
            self.assertFalse(hasattr(received_input, "summary"))
            self.assertFalse(hasattr(received_input, "files_touched"))
            self.assertNotIn(nonce, str(asdict(received_input)))

    def test_run_segment_graph_stops_before_work_when_uv_sync_fails(self) -> None:
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

            from loom import sandbox as sandbox_module

            original_run_observed = sandbox_module.run_observed

            def fake_run_observed(cmd, *, segment_id, run_id, cwd=None, path=None, payload=None):
                if cmd == "uv sync --extra dev":
                    result = CommandResult(
                        exit_code=2,
                        stdout="",
                        stderr="sync failed\n",
                        duration_seconds=0.2,
                    )
                    append_event(
                        Event(
                            ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
                    run_segment_graph(
                        contract_path=contract_path,
                        run_id="run-graph-sync-fail-001",
                        events_path=events_path,
                        execution_repo_path=execution_repo,
                        worktree_root=worktree_root,
                        work_runner=_mock_work_session,
                        test_runner=_mock_test_session,
                    )

            rows, invalid_lines = load_event_rows(
                events_path,
                run_id="run-graph-sync-fail-001",
                segment_id="MAT-REQ-001/S1",
            )
            self.assertEqual(invalid_lines, [])
            self.assertEqual([row["actor"] for row in rows if row["actor"] != "harness"], [])
            sandbox_path = worktree_root / "MAT-REQ-001-S1"
            self.assertEqual(
                [(row["payload"]["cmd"], row["payload"]["exit_code"]) for row in rows if row["type"] == "command_run"],
                [
                    (
                        f"git worktree add -b loom/MAT-REQ-001-S1 {sandbox_path} main",
                        0,
                    ),
                    ("uv sync --extra dev", 2),
                    (
                        f"git worktree remove --force {sandbox_path}",
                        0,
                    ),
                    ("git branch -D loom/MAT-REQ-001-S1", 0),
                ],
            )
            self.assertFalse(sandbox_path.exists())

    def test_run_segment_graph_stops_after_first_green_attempt(self) -> None:
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
            work_inputs = []
            test_inputs = []

            def work_runner(work_input) -> dict[str, object]:
                work_inputs.append(work_input)
                return {
                    "step_id": work_input.step_id,
                    "status": "succeeded",
                    "summary": "initial implement succeeded",
                    "sandbox_path": work_input.sandbox_path,
                    "branch_name": work_input.branch_name,
                    "observed_files_changed": [],
                    "failure_reasons": [],
                }

            def test_runner(test_input) -> dict[str, object]:
                test_inputs.append(test_input)
                return _mock_test_attempt_result(
                    base_dir=Path(tmpdir),
                    attempt=len(test_inputs),
                    passed=True,
                    stdout="all green\n",
                    stderr="",
                )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-cycle-green-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=work_runner,
                test_runner=test_runner,
            )

            self.assertEqual(state["status"], "passed")
            self.assertEqual(state["attempts"], 1)
            self.assertEqual(len(work_inputs), 1)
            self.assertEqual(len(test_inputs), 1)
            self.assertEqual(work_inputs[0].attempt_number, 1)
            self.assertEqual(work_inputs[0].mode, "implement")
            self.assertIsNone(work_inputs[0].previous_test_stdout)
            self.assertIsNone(work_inputs[0].previous_test_stderr)

    def test_run_segment_graph_retries_until_third_attempt_passes(self) -> None:
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
            work_inputs = []
            failure_texts = [
                "attempt-1 pytest failure\n",
                "attempt-2 pytest failure\n",
            ]

            def work_runner(work_input) -> dict[str, object]:
                work_inputs.append(work_input)
                return {
                    "step_id": work_input.step_id,
                    "status": "succeeded",
                    "summary": f"{work_input.mode} attempt {work_input.attempt_number}",
                    "sandbox_path": work_input.sandbox_path,
                    "branch_name": work_input.branch_name,
                    "observed_files_changed": [
                        {
                            "path": "app/routes/upload.py",
                            "change_type": "modified",
                            "before_hash": f"before-{work_input.attempt_number}",
                            "after_hash": f"after-{work_input.attempt_number}",
                        }
                    ],
                    "failure_reasons": [],
                }

            test_attempt = {"value": 0}

            def test_runner(test_input) -> dict[str, object]:
                test_attempt["value"] += 1
                attempt = test_attempt["value"]
                if attempt < 3:
                    return _mock_test_attempt_result(
                        base_dir=Path(tmpdir),
                        attempt=attempt,
                        passed=False,
                        stdout=f"attempt {attempt} stdout\n",
                        stderr=failure_texts[attempt - 1],
                    )
                return _mock_test_attempt_result(
                    base_dir=Path(tmpdir),
                    attempt=attempt,
                    passed=True,
                    stdout="fixed and green\n",
                    stderr="",
                )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-cycle-third-green-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=work_runner,
                test_runner=test_runner,
            )

            self.assertEqual(state["status"], "passed")
            self.assertEqual(state["attempts"], 3)
            self.assertEqual(len(work_inputs), 3)
            self.assertEqual(work_inputs[0].mode, "implement")
            self.assertEqual(work_inputs[1].mode, "fix")
            self.assertEqual(work_inputs[2].mode, "fix")
            self.assertEqual(work_inputs[1].previous_test_stderr, failure_texts[0])
            self.assertEqual(work_inputs[2].previous_test_stderr, failure_texts[1])
            self.assertEqual(
                work_inputs[1].previous_observed_files_changed,
                [
                    {
                        "path": "app/routes/upload.py",
                        "change_type": "modified",
                        "before_hash": "before-1",
                        "after_hash": "after-1",
                    }
                ],
            )

    def test_run_segment_graph_stops_after_four_failed_attempts(self) -> None:
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
            work_attempts = []
            test_attempts = {"value": 0}

            def work_runner(work_input) -> dict[str, object]:
                work_attempts.append(work_input.attempt_number)
                return {
                    "step_id": work_input.step_id,
                    "status": "succeeded",
                    "summary": f"attempt {work_input.attempt_number}",
                    "sandbox_path": work_input.sandbox_path,
                    "branch_name": work_input.branch_name,
                    "observed_files_changed": [],
                    "failure_reasons": [],
                }

            def test_runner(test_input) -> dict[str, object]:
                test_attempts["value"] += 1
                attempt = test_attempts["value"]
                return _mock_test_attempt_result(
                    base_dir=Path(tmpdir),
                    attempt=attempt,
                    passed=False,
                    stdout=f"attempt {attempt} stdout\n",
                    stderr=f"attempt {attempt} stderr\n",
                )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-cycle-hard-stop-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=work_runner,
                test_runner=test_runner,
            )

            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["attempts"], 4)
            self.assertEqual(work_attempts, [1, 2, 3, 4])
            self.assertEqual(test_attempts["value"], 4)
            self.assertEqual(state["test_result"]["attempt_number"], 4)
            self.assertTrue(state["test_result"]["stderr_path"].endswith("mock-test-attempt-4-stderr.txt"))

    def test_fix_attempt_receives_previous_failure_output_while_test_input_stays_spec_only(self) -> None:
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
            failure_nonce = "P3D3-FAILURE-NONCE"
            observed_nonce = "P3D3-OBSERVED-NONCE"
            work_inputs = []
            captured_test_inputs = []

            def work_runner(work_input) -> dict[str, object]:
                work_inputs.append(work_input)
                return {
                    "step_id": work_input.step_id,
                    "status": "succeeded",
                    "summary": f"work attempt {work_input.attempt_number}",
                    "sandbox_path": work_input.sandbox_path,
                    "branch_name": work_input.branch_name,
                    "observed_files_changed": [
                        {
                            "path": "app/routes/upload.py",
                            "change_type": "modified",
                            "before_hash": f"before-{work_input.attempt_number}",
                            "after_hash": observed_nonce,
                        }
                    ],
                    "failure_reasons": [],
                }

            def test_runner(test_input) -> dict[str, object]:
                captured_test_inputs.append(test_input)
                attempt = len(captured_test_inputs)
                if attempt == 1:
                    return _mock_test_attempt_result(
                        base_dir=Path(tmpdir),
                        attempt=attempt,
                        passed=False,
                        stdout=f"{failure_nonce} stdout\n",
                        stderr=f"{failure_nonce} stderr\n",
                    )
                return _mock_test_attempt_result(
                    base_dir=Path(tmpdir),
                    attempt=attempt,
                    passed=True,
                    stdout="green\n",
                    stderr="",
                )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-cycle-fix-context-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=work_runner,
                test_runner=test_runner,
            )

            self.assertEqual(state["status"], "passed")
            self.assertEqual(state["attempts"], 2)
            self.assertEqual(len(work_inputs), 2)
            self.assertEqual(work_inputs[1].mode, "fix")
            self.assertIn(failure_nonce, work_inputs[1].previous_test_stdout or "")
            self.assertIn(failure_nonce, work_inputs[1].previous_test_stderr or "")
            self.assertEqual(
                work_inputs[1].previous_observed_files_changed,
                [
                    {
                        "path": "app/routes/upload.py",
                        "change_type": "modified",
                        "before_hash": "before-1",
                        "after_hash": observed_nonce,
                    }
                ],
            )
            for test_input in captured_test_inputs:
                self.assertEqual(
                    list(asdict(test_input).keys()),
                    ["acceptance", "sequence_diagram", "test_selectors"],
                )
                self.assertNotIn(failure_nonce, str(asdict(test_input)))
                self.assertNotIn(observed_nonce, str(asdict(test_input)))

    def test_test_node_runs_pytest_in_same_sandbox_and_uses_observed_exit_code(self) -> None:
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
            selectors = [
                "tests/test_s3t_tagging.py",
                "tests/test_s4bb_material_tag_wiring.py",
            ]
            observed_commands: list[tuple[str, str | None]] = []

            def fake_run_observed(cmd, *, segment_id, run_id, cwd=None, path=None, payload=None):
                observed_commands.append((cmd, None if cwd is None else str(cwd)))
                self.assertEqual(segment_id, "MAT-REQ-001/S1")
                self.assertEqual(run_id, "run-graph-test-real-001")
                self.assertEqual(Path(path), events_path)
                self.assertTrue(str(cwd).endswith("loom-worktrees/MAT-REQ-001-S1"))
                self.assertEqual(
                    cmd,
                    "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                )
                result = CommandResult(
                    exit_code=5,
                    stdout="pretend-green-output\n",
                    stderr="real failure\n",
                    duration_seconds=1.5,
                )
                append_event(
                    Event(
                        ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        segment_id=segment_id,
                        run_id=run_id,
                        actor="harness",
                        type="command_run",
                        payload={
                            "cmd": cmd,
                            "cwd": str(cwd),
                            "exit_code": result.exit_code,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "duration_seconds": result.duration_seconds,
                        },
                    ),
                    path=path,
                )
                return result

            with patch("loom.graph.run_observed", side_effect=fake_run_observed):
                state = run_segment_graph(
                    contract_path=contract_path,
                    run_id="run-graph-test-real-001",
                    events_path=events_path,
                    execution_repo_path=execution_repo,
                    worktree_root=worktree_root,
                    work_runner=_mock_work_session,
                )

            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["attempts"], 4)
            self.assertFalse(state["test_result"]["passed"])
            self.assertEqual(state["test_result"]["exit_code"], 5)
            self.assertEqual(state["test_result"]["test_selectors"], selectors)
            self.assertTrue(Path(state["test_result"]["stdout_path"]).exists())
            self.assertTrue(Path(state["test_result"]["stderr_path"]).exists())
            self.assertEqual(
                observed_commands,
                [
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        str(worktree_root / "MAT-REQ-001-S1"),
                    ),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        str(worktree_root / "MAT-REQ-001-S1"),
                    ),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        str(worktree_root / "MAT-REQ-001-S1"),
                    ),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        str(worktree_root / "MAT-REQ-001-S1"),
                    ),
                ],
            )

            rows, _ = load_event_rows(
                events_path,
                run_id="run-graph-test-real-001",
                segment_id="MAT-REQ-001/S1",
            )
            command_rows = [row for row in rows if row["type"] == "command_run"]
            self.assertEqual(
                [(row["payload"]["cmd"], row["payload"]["exit_code"]) for row in command_rows],
                [
                    (
                        f"git worktree add -b loom/MAT-REQ-001-S1 {worktree_root / 'MAT-REQ-001-S1'} main",
                        0,
                    ),
                    ("uv sync --extra dev", 0),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        5,
                    ),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        5,
                    ),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        5,
                    ),
                    (
                        "uv run pytest tests/test_s3t_tagging.py tests/test_s4bb_material_tag_wiring.py",
                        5,
                    ),
                    (
                        f"git worktree remove --force {worktree_root / 'MAT-REQ-001-S1'}",
                        0,
                    ),
                    ("git branch -D loom/MAT-REQ-001-S1", 0),
                ],
            )

    def test_empty_contract_test_selectors_skip_pytest_and_emit_harness_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            execution_repo = Path(tmpdir) / "lingua-web"
            execution_repo.mkdir()
            _init_main_repo(execution_repo)
            worktree_root = Path(tmpdir) / "loom-worktrees"
            contract_path = Path(tmpdir) / "S-empty-tests.yaml"
            contract_path.write_text(
                dedent(
                    """\
                    segment_id: MAT-REQ-001/S9
                    covers_req: MAT-REQ-001
                    title: 空测试选择器契约
                    acceptance:
                      - id: MAT-REQ-001/S9/AC1
                        text: 只验证空测试选择器会诚实跳过
                    anti_scope:
                      - text: 不做别的
                        kind: out_of_req
                    depends_on: []
                    scope_paths:
                      - app/routes/upload.py
                    test_selectors: []
                    preview:
                      sequence_diagram: |
                        请求 -> 路由: 执行
                        路由 --> 请求: 完成
                    """
                ),
                encoding="utf-8",
            )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-graph-test-skip-001",
                events_path=events_path,
                execution_repo_path=execution_repo,
                worktree_root=worktree_root,
                work_runner=_mock_work_session,
            )

            self.assertEqual(state["status"], "passed")
            self.assertEqual(state["attempts"], 1)
            self.assertEqual(
                state["test_result"],
                {
                    "status": "skipped",
                    "summary": "本 run 未跑测试(空 selectors)",
                    "attempt_number": 1,
                    "test_selectors": [],
                },
            )

            rows, _ = load_event_rows(
                events_path,
                run_id="run-graph-test-skip-001",
                segment_id="MAT-REQ-001/S9",
            )
            self.assertEqual(
                [row["payload"]["cmd"] for row in rows if row["type"] == "command_run"],
                [
                    f"git worktree add -b loom/MAT-REQ-001-S9 {worktree_root / 'MAT-REQ-001-S9'} main",
                    "uv sync --extra dev",
                    f"git worktree remove --force {worktree_root / 'MAT-REQ-001-S9'}",
                    "git branch -D loom/MAT-REQ-001-S9",
                ],
            )
            skipped_rows = [
                row
                for row in rows
                if row["actor"] == "harness" and row["type"] == "test_skipped"
            ]
            self.assertEqual(len(skipped_rows), 1)
            self.assertEqual(skipped_rows[0]["segment_id"], "MAT-REQ-001/S9")
            self.assertEqual(skipped_rows[0]["run_id"], "run-graph-test-skip-001")
            self.assertEqual(
                skipped_rows[0]["payload"],
                {
                    "attempt": 1,
                    "summary": "本 run 未跑测试(空 selectors)",
                    "test_selectors": [],
                },
            )

    def test_load_segment_contract_rejects_test_selector_inside_scope_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            contract_path = Path(tmpdir) / "S-bad-tests.yaml"
            contract_path.write_text(
                dedent(
                    """\
                    segment_id: MAT-REQ-001/S10
                    covers_req: MAT-REQ-001
                    title: 非法测试范围重叠契约
                    acceptance:
                      - id: MAT-REQ-001/S10/AC1
                        text: 契约加载时拒绝 scope 与测试重叠
                    anti_scope:
                      - text: 不做别的
                        kind: out_of_req
                    depends_on: []
                    scope_paths:
                      - tests/
                    test_selectors:
                      - tests/test_s3t_tagging.py
                    preview:
                      sequence_diagram: |
                        请求 -> 路由: 执行
                        路由 --> 请求: 完成
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                r"test_selectors must not overlap scope_paths: tests/test_s3t_tagging.py",
            ):
                _load_segment_contract(contract_path)

    def test_work_node_spawns_codex_exec_and_records_observed_changes(self) -> None:
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

            def fake_run_observed(cmd, *, segment_id, run_id, cwd=None, path=None, payload=None):
                self.assertIn("codex exec", cmd)
                self.assertIn("--sandbox workspace-write", cmd)
                self.assertEqual(segment_id, "MAT-REQ-001/S1")
                self.assertEqual(run_id, "run-graph-work-real-001")
                self.assertEqual(Path(path), events_path)
                self.assertTrue(str(cwd).endswith("loom-worktrees/MAT-REQ-001-S1"))
                self.assertIn("--output-schema", cmd)
                self.assertIn("--output-last-message", cmd)
                self.assertIn("后端移除来源标签的路由与删除逻辑", Path(_extract_redirect_path(cmd, "<")).read_text(encoding="utf-8"))

                declaration_path = Path(_extract_flag_path(cmd, "--output-last-message"))
                declaration_path.parent.mkdir(parents=True, exist_ok=True)
                declaration_path.write_text(
                    json.dumps(
                        {
                            "summary": "implemented route and model changes",
                            "claimed_changed_files": [
                                "app/routes/upload.py",
                                "app/models.py",
                            ],
                            "notes": ["single-pass implement"],
                        }
                    ),
                    encoding="utf-8",
                )

                target = Path(cwd) / "app" / "routes"
                target.mkdir(parents=True, exist_ok=True)
                (target / "upload.py").write_text("implemented\n", encoding="utf-8")

                return CommandResult(
                    exit_code=0,
                    stdout="codex ok\n",
                    stderr="",
                    duration_seconds=0.25,
                )

            with patch("loom.graph.run_observed", side_effect=fake_run_observed):
                state = run_segment_graph(
                    contract_path=contract_path,
                    run_id="run-graph-work-real-001",
                    events_path=events_path,
                    execution_repo_path=execution_repo,
                    worktree_root=worktree_root,
                    test_runner=_mock_test_session,
                )

            self.assertEqual(state["work_result"]["exit_code"], 0)
            self.assertEqual(state["work_result"]["status"], "succeeded")
            self.assertEqual(
                [item["path"] for item in state["work_result"]["observed_files_changed"]],
                ["app/routes/upload.py"],
            )
            self.assertEqual(state["work_result"]["failure_reasons"], [])
            self.assertEqual(state["work_result"]["declaration"]["kind"], "agent_declaration")
            self.assertTrue(Path(state["work_result"]["declaration"]["path"]).exists())

            rows, _ = load_event_rows(
                events_path,
                run_id="run-graph-work-real-001",
                segment_id="MAT-REQ-001/S1",
            )
            self.assertEqual(
                [row["type"] for row in rows if row["actor"] == "harness"].count("files_changed"),
                1,
            )

    def test_work_node_records_out_of_scope_change_until_retry_limit(self) -> None:
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
            attempts = {"value": 0}

            def fake_run_observed(cmd, *, segment_id, run_id, cwd=None, path=None, payload=None):
                attempts["value"] += 1
                declaration_path = Path(_extract_flag_path(cmd, "--output-last-message"))
                declaration_path.parent.mkdir(parents=True, exist_ok=True)
                declaration_path.write_text(
                    json.dumps(
                        {
                            "summary": "changed wrong file",
                            "claimed_changed_files": ["README.md"],
                            "notes": ["out of scope on purpose"],
                        }
                    ),
                    encoding="utf-8",
                )
                (Path(cwd) / "README.md").write_text(f"changed {attempts['value']}\n", encoding="utf-8")
                return CommandResult(
                    exit_code=0,
                    stdout="codex ok\n",
                    stderr="",
                    duration_seconds=0.25,
                )

            with patch("loom.graph.run_observed", side_effect=fake_run_observed):
                state = run_segment_graph(
                    contract_path=contract_path,
                    run_id="run-graph-work-real-002",
                    events_path=events_path,
                    execution_repo_path=execution_repo,
                    worktree_root=worktree_root,
                    test_runner=_mock_test_session,
                )

            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["attempts"], 4)
            self.assertEqual(state["work_result"]["exit_code"], 0)
            self.assertEqual(state["work_result"]["status"], "failed")
            self.assertEqual(
                state["work_result"]["out_of_scope_paths"],
                ["README.md"],
            )
            self.assertIn("out_of_scope_changes", state["work_result"]["failure_reasons"])
            self.assertEqual(
                [item["path"] for item in state["work_result"]["observed_files_changed"]],
                ["README.md"],
            )
            self.assertEqual(len(state["work_attempts"]), 4)

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
                'name = "loom-graph-tests"',
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


def _mock_work_session(work_input) -> dict[str, object]:
    return {
        "step_id": work_input.step_id,
        "summary": "mock work completed",
        "sandbox_path": work_input.sandbox_path,
        "branch_name": work_input.branch_name,
        "observed_top_level": work_input.sandbox_path,
        "diff": f"fake diff for {work_input.segment_id}",
        "files_touched": [],
    }


def _mock_test_session(test_input) -> dict[str, object]:
    return {
        "passed": True,
        "summary": "mock tests passed",
        "evidence": "fixed-pass mock",
        "test_selectors": list(test_input.test_selectors),
    }


def _mock_test_attempt_result(
    *,
    base_dir: Path,
    attempt: int,
    passed: bool,
    stdout: str,
    stderr: str,
) -> dict[str, object]:
    stdout_path = base_dir / f"mock-test-attempt-{attempt}-stdout.txt"
    stderr_path = base_dir / f"mock-test-attempt-{attempt}-stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "passed": passed,
        "summary": f"mock test attempt {attempt}",
        "exit_code": 0 if passed else 1,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "test_selectors": [],
    }


def _extract_flag_path(cmd: str, flag: str) -> str:
    import shlex

    parts = shlex.split(cmd)
    index = parts.index(flag)
    return parts[index + 1]


def _extract_redirect_path(cmd: str, operator: str) -> str:
    import shlex

    parts = shlex.split(cmd, posix=True)
    index = parts.index(operator)
    return parts[index + 1]


if __name__ == "__main__":
    unittest.main()
