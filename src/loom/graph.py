from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .events import DEFAULT_EVENTS_PATH
from .harness import observe_step, run_observed
from .sandbox import (
    DEFAULT_EXECUTION_REPO_PATH,
    DEFAULT_WORKTREE_ROOT,
    Sandbox,
    create_sandbox,
    destroy_sandbox,
)


class GraphState(TypedDict, total=False):
    contract_path: str
    events_path: str
    run_id: str
    segment_id: str
    sandbox: dict[str, str]
    segment: dict[str, Any]
    step: dict[str, Any]
    work_result: dict[str, Any]
    test_result: dict[str, Any]


_TOP_LEVEL_FIELD_PATTERN = re.compile(r"^(segment_id|covers_req|title):\s*(.+)$", re.MULTILINE)
_ACCEPTANCE_ID_PATTERN = re.compile(r"^\s*-\s+id:\s*(.+)$", re.MULTILINE)


def run_segment_graph(
    *,
    contract_path: Path | str,
    run_id: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    worktree_root: Path | str = DEFAULT_WORKTREE_ROOT,
) -> GraphState:
    contract_file = Path(contract_path)
    segment_id = _extract_segment_id(contract_file)
    sandbox = create_sandbox(
        segment_id=segment_id,
        repo_path=execution_repo_path,
        worktree_root=worktree_root,
        run_id=run_id,
        events_path=events_path,
    )
    initial_state: GraphState = {
        "contract_path": str(contract_file),
        "events_path": str(events_path),
        "run_id": run_id,
        "segment_id": segment_id,
        "sandbox": _sandbox_to_state(sandbox),
    }
    graph = _build_graph()
    try:
        return graph.invoke(initial_state)
    finally:
        destroy_sandbox(
            sandbox,
            run_id=run_id,
            events_path=events_path,
        )


def _build_graph():
    workflow = StateGraph(GraphState)
    workflow.add_node("orchestrator", _orchestrator_node)
    workflow.add_node("work", _work_node)
    workflow.add_node("test", _test_node)
    workflow.add_edge(START, "orchestrator")
    workflow.add_edge("orchestrator", "work")
    workflow.add_edge("work", "test")
    workflow.add_edge("test", END)
    return workflow.compile()


def _orchestrator_node(state: GraphState) -> GraphState:
    def run() -> GraphState:
        segment = _load_segment_contract(Path(state["contract_path"]))
        return {
            "segment": segment,
            "step": {
                "id": f"{segment['segment_id']}/STEP-1",
                "segment_id": segment["segment_id"],
                "acceptance_ids": segment["acceptance_ids"],
            },
        }

    return observe_step(
        run,
        actor="orchestrator",
        step_name="orchestrator",
        segment_id=state["segment_id"],
        run_id=state["run_id"],
        path=state["events_path"],
    )


def _work_node(state: GraphState) -> GraphState:
    def run() -> GraphState:
        step = state["step"]
        sandbox = state["sandbox"]
        command = run_observed(
            "git rev-parse --show-toplevel",
            segment_id=state["segment_id"],
            run_id=state["run_id"],
            cwd=sandbox["worktree_path"],
            path=state["events_path"],
        )
        if command.exit_code != 0:
            raise RuntimeError(command.stderr.strip() or "mock work command failed")
        return {
            "work_result": {
                "step_id": step["id"],
                "summary": "mock work completed",
                "sandbox_path": sandbox["worktree_path"],
                "branch_name": sandbox["branch_name"],
                "observed_top_level": command.stdout.strip(),
                "diff": (
                    f"fake diff for {step['segment_id']}\n"
                    "--- a/src/backend/routes/materials.py\n"
                    "+++ b/src/backend/routes/materials.py\n"
                    "@@ mock @@\n"
                    "- old line\n"
                    "+ new line\n"
                ),
                "files_touched": [],
            }
        }

    return observe_step(
        run,
        actor="work",
        step_name="work",
        segment_id=state["segment_id"],
        run_id=state["run_id"],
        path=state["events_path"],
    )


def _test_node(state: GraphState) -> GraphState:
    def run() -> GraphState:
        return {
            "test_result": {
                "passed": True,
                "summary": "mock tests passed",
                "evidence": "fixed-pass mock",
            }
        }

    return observe_step(
        run,
        actor="test",
        step_name="test",
        segment_id=state["segment_id"],
        run_id=state["run_id"],
        path=state["events_path"],
    )


def _extract_segment_id(contract_path: Path) -> str:
    content = contract_path.read_text(encoding="utf-8")
    match = re.search(r"^segment_id:\s*(.+)$", content, re.MULTILINE)
    if match is None:
        raise ValueError(f"segment_id not found in contract: {contract_path}")
    return match.group(1).strip()


def _load_segment_contract(contract_path: Path) -> dict[str, Any]:
    content = contract_path.read_text(encoding="utf-8")
    parsed: dict[str, Any] = {
        "contract_path": str(contract_path),
        "acceptance_ids": [match.strip() for match in _ACCEPTANCE_ID_PATTERN.findall(content)],
    }

    top_level_fields = {
        field_name: raw_value.strip()
        for field_name, raw_value in _TOP_LEVEL_FIELD_PATTERN.findall(content)
    }

    if "segment_id" not in top_level_fields:
        raise ValueError(f"segment_id not found in contract: {contract_path}")
    if "covers_req" not in top_level_fields:
        raise ValueError(f"covers_req not found in contract: {contract_path}")

    parsed["segment_id"] = top_level_fields["segment_id"]
    parsed["covers_req"] = top_level_fields["covers_req"]

    return parsed


def _sandbox_to_state(sandbox: Sandbox) -> dict[str, str]:
    return {
        "repo_path": str(sandbox.repo_path),
        "worktree_path": str(sandbox.worktree_path),
        "branch_name": sandbox.branch_name,
        "base_branch": sandbox.base_branch,
    }
