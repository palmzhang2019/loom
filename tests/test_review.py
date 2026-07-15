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
from loom.review import (
    ReviewSessionInput,
    _run_codex_review_session,
    _validate_sequence_diagram,
    run_segment_review,
)
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
REVERSE_SEQUENCE_DIAGRAM = """sequenceDiagram
    participant Client as 请求方
    participant Route as upload路由
    participant Link as MaterialKnowledgeTagLink
    participant Audit as 审计记录
    Client->>Route: POST 移除关联(material_id, tag_id)
    Route->>Link: 查询并删除关联
    alt 删除成功
        Link-->>Route: 删除成功
        Route->>Audit: 记录删除动作
        Route-->>Client: 成功响应
    else 关联不存在
        Link-->>Route: 未找到关联
        Route-->>Client: 404 响应
    end"""


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
        "reverse_only_interactions": ["upload路由 -> 审计记录: 记录删除动作"],
        "contract_only_interactions": [
            "upload路由 -> MaterialKnowledgeTagLink: 单独查询关联记录"
        ],
        "summary": "LLM 建议人类结合测试结果继续检查该实现。",
    }


def _mock_reverse_sequence(input_data: object) -> str:
    return REVERSE_SEQUENCE_DIAGRAM


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
                reverse_sequence_runner=_mock_reverse_sequence,
                review_runner=review_runner,
            )

            self.assertEqual(report_path, root / "runs" / RUN_ID / "review" / "review.md")
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("## 硬事实(harness 观测)", report)
            self.assertIn("scope 检查结果: 合规", report)
            self.assertIn("app/routes/upload.py", report)
            self.assertIn("## LLM 建议(供人参考)", report)
            hard_facts, llm_advice = report.split("## LLM 建议(供人参考)", 1)
            self.assertNotIn("```mermaid", hard_facts)
            self.assertNotIn("\n## ", llm_advice)
            self.assertEqual(llm_advice.count("```mermaid"), 2)
            self.assertIn("### 契约时序图(设计意图,人类所写)", llm_advice)
            self.assertIn(
                "### 反向生成时序图(LLM 从实现代码生成,供参考)",
                llm_advice,
            )
            self.assertIn("### 图差异观察(LLM 软建议,供人参考)", llm_advice)
            self.assertIn("### 逐条 AC review 意见(LLM 建议)", llm_advice)
            self.assertIn("sequenceDiagram", llm_advice)
            self.assertIn(REVERSE_SEQUENCE_DIAGRAM, llm_advice)
            self.assertIn(
                "反向生成图中有、契约图中没有的交互",
                llm_advice,
            )
            self.assertIn(
                "契约图中有、反向生成图中没有的交互",
                llm_advice,
            )
            self.assertIn("upload路由 -> 审计记录: 记录删除动作", llm_advice)
            self.assertIn(
                "upload路由 -> MaterialKnowledgeTagLink: 单独查询关联记录",
                llm_advice,
            )
            self.assertIn("是否构成漂移由人类判断", llm_advice)
            reverse_observations = llm_advice.split(
                "反向生成图中有、契约图中没有的交互:\n",
                1,
            )[1].split("\n\n契约图中有、反向生成图中没有的交互:", 1)[0]
            contract_observations = llm_advice.split(
                "契约图中有、反向生成图中没有的交互:\n",
                1,
            )[1].split("\n\n### 逐条 AC review 意见", 1)[0]
            self.assertIn("upload路由 -> 审计记录: 记录删除动作", reverse_observations)
            self.assertNotIn("单独查询关联记录", reverse_observations)
            self.assertIn("单独查询关联记录", contract_observations)
            self.assertNotIn("审计记录", contract_observations)
            self.assertEqual(report.count("LLM意见:满足"), 4)
            for index in range(1, 5):
                self.assertIn(f"MAT-REQ-001/S1/AC{index}", report)
            self.assertNotIn("PASS", report)
            self.assertNotIn("FAIL", report)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].reviewed_branch, BRANCH_NAME)
            self.assertIn("diff --git a/app/routes/upload.py", received[0].diff)
            self.assertEqual(len(received[0].acceptance), 4)
            self.assertTrue(received[0].contract_sequence_diagram.startswith("sequenceDiagram"))
            self.assertEqual(
                received[0].reverse_sequence_diagram,
                REVERSE_SEQUENCE_DIAGRAM,
            )
            contract_block = (
                f"```mermaid\n{received[0].contract_sequence_diagram}\n```"
            )
            reverse_block = f"```mermaid\n{REVERSE_SEQUENCE_DIAGRAM}\n```"
            self.assertEqual(llm_advice.count(contract_block), 1)
            self.assertEqual(llm_advice.count(reverse_block), 1)
            self.assertLess(llm_advice.index(contract_block), llm_advice.index(reverse_block))

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
                reverse_sequence_runner=_mock_reverse_sequence,
                review_runner=_mock_review,
            )

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("scope 检查结果: 越界", report)
            self.assertIn("越界文件:", report)
            self.assertIn("tests/test_s3t_tagging.py", report)
            self.assertEqual(report.count("LLM意见:满足"), 4)

    def test_default_sessions_run_in_order_and_reverse_prompt_excludes_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            events_path = root / "events.jsonl"
            repo_path = root / "lingua-web"
            repo_path.mkdir()
            contract_diagram_nonce = "CONTRACT_SEQUENCE_MUST_NOT_LEAK_7f1c"
            acceptance_nonce = "ACCEPTANCE_MUST_NOT_LEAK_98ad"
            diff_nonce = "DIFF_IMPLEMENTATION_ONLY_43be"
            contract_path = root / "S1.yaml"
            contract_path.write_text(
                "\n".join(
                    [
                        "segment_id: MAT-REQ-001/S1",
                        "covers_req: MAT-REQ-001",
                        "title: 隔离测试",
                        "acceptance:",
                        "  - id: MAT-REQ-001/S1/AC1",
                        f"    text: {acceptance_nonce}",
                        "anti_scope: []",
                        "scope_paths:",
                        "  - app/routes/upload.py",
                        "test_selectors: []",
                        "preview:",
                        "  sequence_diagram: |",
                        "    sequenceDiagram",
                        "        participant Contract",
                        f"        Contract->>Contract: {contract_diagram_nonce}",
                    ]
                ),
                encoding="utf-8",
            )
            _append_files_changed(events_path, ["app/routes/upload.py"])
            runtime_dir = root / "runs" / RUN_ID / "review"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "codex-reverse-sequence.json").write_text(
                '{"sequence_diagram":"STALE_REVERSE_OUTPUT"}',
                encoding="utf-8",
            )
            (runtime_dir / "codex-review.json").write_text(
                '{"summary":"STALE_COMPARISON_OUTPUT"}',
                encoding="utf-8",
            )
            reverse_prompts: list[str] = []
            comparison_prompts: list[str] = []
            codex_commands: list[str] = []

            def fake_run_observed(
                cmd, *, segment_id, run_id, cwd=None, path=None, payload=None
            ):
                if cmd.startswith("git diff"):
                    return CommandResult(
                        0,
                        (
                            "diff --git a/app/routes/upload.py b/app/routes/upload.py\n"
                            f"+{diff_nonce}\n"
                        ),
                        "",
                        0.1,
                    )
                codex_commands.append(cmd)
                self.assertIn("codex exec", cmd)
                self.assertIn("--sandbox read-only", cmd)
                self.assertNotIn("resume", cmd)
                self.assertEqual(Path(cwd), repo_path)
                if len(codex_commands) == 1:
                    prompt = (runtime_dir / "reverse-sequence-prompt.txt").read_text(
                        encoding="utf-8"
                    )
                    reverse_prompts.append(prompt)
                    self.assertIn(diff_nonce, prompt)
                    self.assertNotIn(contract_diagram_nonce, prompt)
                    self.assertNotIn(acceptance_nonce, prompt)
                    self.assertNotIn("Acceptance criteria:", prompt)
                    self.assertIn(
                        "所有人类可见的 participant 名称和消息文字必须使用中文",
                        prompt,
                    )
                    self.assertEqual(
                        payload,
                        {"role": "review", "artifact": "reverse_sequence_diagram"},
                    )
                    reverse_output_path = runtime_dir / "codex-reverse-sequence.json"
                    self.assertNotIn(
                        "STALE_REVERSE_OUTPUT",
                        reverse_output_path.read_text(encoding="utf-8"),
                    )
                    reverse_output_path.write_text(
                        json.dumps({"sequence_diagram": REVERSE_SEQUENCE_DIAGRAM}),
                        encoding="utf-8",
                    )
                else:
                    prompt = (runtime_dir / "prompt.txt").read_text(encoding="utf-8")
                    comparison_prompts.append(prompt)
                    self.assertIn(diff_nonce, prompt)
                    self.assertIn(contract_diagram_nonce, prompt)
                    self.assertIn(acceptance_nonce, prompt)
                    self.assertIn(REVERSE_SEQUENCE_DIAGRAM, prompt)
                    self.assertIn(
                        "差异观察、逐条 AC 意见的理由和总体摘要必须使用中文",
                        prompt,
                    )
                    comparison_output_path = runtime_dir / "codex-review.json"
                    self.assertNotIn(
                        "STALE_COMPARISON_OUTPUT",
                        comparison_output_path.read_text(encoding="utf-8"),
                    )
                    comparison_output_path.write_text(
                        json.dumps(
                            {
                                "opinions": [
                                    {
                                        "acceptance_id": "MAT-REQ-001/S1/AC1",
                                        "opinion": "存疑",
                                        "reason": "由人类结合两图判断",
                                    }
                                ],
                                "reverse_only_interactions": [],
                                "contract_only_interactions": [],
                                "summary": "两图语义比较仅供参考",
                            }
                        ),
                        encoding="utf-8",
                    )
                return CommandResult(0, "", "", 0.1)

            with patch("loom.review.run_observed", side_effect=fake_run_observed):
                run_segment_review(
                    contract_path=contract_path,
                    run_id=RUN_ID,
                    reviewed_branch=BRANCH_NAME,
                    events_path=events_path,
                    execution_repo_path=repo_path,
                )

            self.assertEqual(len(reverse_prompts), 1)
            self.assertEqual(len(comparison_prompts), 1)
            self.assertEqual(len(codex_commands), 2)
            self.assertIn("codex-reverse-sequence.json", codex_commands[0])
            self.assertIn("codex-review.json", codex_commands[1])

    def test_reverse_sequence_validation_rejects_non_mermaid_body(self) -> None:
        invalid_diagrams = [
            "sequenceDiagram\nnot a participant or interaction",
            "sequenceDiagram\n    participant Client",
        ]
        for diagram in invalid_diagrams:
            with self.subTest(diagram=diagram):
                with self.assertRaisesRegex(ValueError, "participant.*message arrow"):
                    _validate_sequence_diagram(diagram)

    def test_default_review_runner_uses_fresh_read_only_codex_comparison_session(self) -> None:
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
                contract_sequence_diagram=(
                    "sequenceDiagram\n"
                    "    participant Contract\n"
                    "    Contract->>Contract: 契约交互"
                ),
                reverse_sequence_diagram=(
                    "sequenceDiagram\n"
                    "    participant Built\n"
                    "    Built->>Built: 实现交互"
                ),
                execution_repo_path=str(repo_path),
                events_path=str(events_path),
            )

            def fake_run_observed(
                cmd, *, segment_id, run_id, cwd=None, path=None, payload=None
            ):
                output_path = root / "runs" / RUN_ID / "review" / "codex-review.json"
                output_path.write_text(
                    json.dumps(
                        {
                            "opinions": [],
                            "reverse_only_interactions": ["实现交互"],
                            "contract_only_interactions": ["契约交互"],
                            "summary": "建议摘要",
                        }
                    ),
                    encoding="utf-8",
                )
                prompt_path = root / "runs" / RUN_ID / "review" / "prompt.txt"
                prompt = prompt_path.read_text(encoding="utf-8")
                self.assertIn("Contract->>Contract: 契约交互", prompt)
                self.assertIn("Built->>Built: 实现交互", prompt)
                self.assertIn(
                    "差异观察、逐条 AC 意见的理由和总体摘要必须使用中文",
                    prompt,
                )
                self.assertIn("codex exec", cmd)
                self.assertIn("--sandbox read-only", cmd)
                self.assertNotIn("resume", cmd)
                self.assertNotIn("--ephemeral", cmd)
                self.assertEqual(Path(cwd), repo_path)
                return CommandResult(0, "", "", 0.1)

            with patch("loom.review.run_observed", side_effect=fake_run_observed):
                result = _run_codex_review_session(input_data)

            self.assertEqual(
                result,
                {
                    "opinions": [],
                    "reverse_only_interactions": ["实现交互"],
                    "contract_only_interactions": ["契约交互"],
                    "summary": "建议摘要",
                },
            )


if __name__ == "__main__":
    unittest.main()
