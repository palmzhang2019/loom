from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote
from typing import Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .events import DEFAULT_EVENTS_PATH, Event, append_event
from .graph import _load_segment_contract
from .handoff import (
    generate_pending_handoff,
    mark_handoff_merged,
    mark_handoff_rejected,
)
from .harness import run_observed
from .sandbox import DEFAULT_EXECUTION_REPO_PATH, _branch_exists


TARGET_BRANCH = "loom/rehearsal"


class MergeGateState(TypedDict, total=False):
    contract_path: str
    events_path: str
    execution_repo_path: str
    segment_id: str
    run_id: str
    source_branch: str
    target: str
    status: str
    refusal_reason: str
    review_report_path: str
    audit_report_path: str
    audit_verdict: str
    human_decision: str
    rejection_reason: str
    merge_commit: str
    handoff_path: str


DecisionProvider = Callable[[dict[str, object]], dict[str, str]]


def run_merge_gate(
    *,
    contract_path: Path | str,
    run_id: str,
    source_branch: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    decision_provider: DecisionProvider | None = None,
) -> MergeGateState:
    if not source_branch.startswith("loom/") or source_branch == TARGET_BRANCH:
        raise ValueError("source branch must be a loom/ segment branch")

    contract = _load_segment_contract(Path(contract_path))
    segment_id = str(contract["segment_id"])
    initial_state: MergeGateState = {
        "contract_path": str(contract_path),
        "events_path": str(events_path),
        "execution_repo_path": str(execution_repo_path),
        "segment_id": segment_id,
        "run_id": run_id,
        "source_branch": source_branch,
        "target": TARGET_BRANCH,
        "status": "checking_audit",
    }

    # InMemorySaver only carries this process's interrupt/resume mechanism. It is
    # never a progress or verdict truth source: merge_completed, merge_refused,
    # and merge_rejected events in events.jsonl are the durable observed facts.
    # If this process exits at the interrupt, its pending decision is deliberately
    # lost; a rerun reconstructs the gate from the retained branch and event pointers.
    graph = _build_merge_gate_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"merge-gate:{segment_id}:{run_id}"}}
    result = graph.invoke(initial_state, config=config)
    interrupts = result.get("__interrupt__", ())
    if not interrupts:
        return result
    if len(interrupts) != 1:
        raise RuntimeError("merge gate expected exactly one human interrupt")

    presentation = interrupts[0].value
    if not isinstance(presentation, dict):
        raise RuntimeError("merge gate interrupt payload must be an object")
    decision = (decision_provider or _terminal_decision_provider)(presentation)
    return graph.invoke(Command(resume=decision), config=config)


def _build_merge_gate_graph(*, checkpointer: InMemorySaver):
    workflow = StateGraph(MergeGateState)
    workflow.add_node("audit_precheck", _audit_precheck_node)
    workflow.add_node("handoff_generate", _handoff_generate_node)
    workflow.add_node("human_decision", _human_decision_node)
    workflow.add_node("approve", _approve_node)
    workflow.add_node("reject", _reject_node)
    workflow.add_edge(START, "audit_precheck")
    workflow.add_conditional_edges(
        "audit_precheck",
        _route_after_audit,
        {"refused": END, "human": "handoff_generate"},
    )
    workflow.add_edge("handoff_generate", "human_decision")
    workflow.add_conditional_edges(
        "human_decision",
        _route_after_human,
        {"approve": "approve", "reject": "reject"},
    )
    workflow.add_edge("approve", END)
    workflow.add_edge("reject", END)
    return workflow.compile(checkpointer=checkpointer)


def _audit_precheck_node(state: MergeGateState) -> MergeGateState:
    audit_payload = _latest_pointer_payload(
        events_path=Path(state["events_path"]),
        segment_id=state["segment_id"],
        run_id=state["run_id"],
        event_type="audit_completed",
        branch_field="audited_branch",
        source_branch=state["source_branch"],
    )
    if audit_payload is None:
        return _refuse_merge(state, reason="audit_missing")

    audit_verdict = audit_payload.get("verdict")
    audit_report_path = audit_payload.get("report_path")
    if audit_verdict == "blocked":
        return _refuse_merge(state, reason="audit_blocked")
    if audit_verdict != "passed" or not isinstance(audit_report_path, str):
        return _refuse_merge(state, reason="audit_invalid")

    review_payload = _latest_pointer_payload(
        events_path=Path(state["events_path"]),
        segment_id=state["segment_id"],
        run_id=state["run_id"],
        event_type="review_completed",
        branch_field="reviewed_branch",
        source_branch=state["source_branch"],
    )
    if review_payload is None:
        return _refuse_merge(state, reason="review_missing")
    review_report_path = review_payload.get("report_path")
    if not isinstance(review_report_path, str):
        return _refuse_merge(state, reason="review_invalid")

    return {
        "status": "awaiting_human",
        "review_report_path": review_report_path,
        "audit_report_path": audit_report_path,
        "audit_verdict": audit_verdict,
    }


def _handoff_generate_node(state: MergeGateState) -> MergeGateState:
    record_path = generate_pending_handoff(
        contract_path=state["contract_path"],
        run_id=state["run_id"],
        source_branch=state["source_branch"],
        events_path=state["events_path"],
        execution_repo_path=state["execution_repo_path"],
    )
    return {"handoff_path": str(record_path)}


def _human_decision_node(state: MergeGateState) -> MergeGateState:
    response = interrupt(
        {
            "segment_id": state["segment_id"],
            "run_id": state["run_id"],
            "review_report_path": state["review_report_path"],
            "audit_report_path": state["audit_report_path"],
            "audit_verdict": state["audit_verdict"],
            "source_branch": state["source_branch"],
            "target": state["target"],
        }
    )
    if not isinstance(response, dict):
        raise ValueError("human merge decision must be an object")
    decision = response.get("decision")
    if decision not in {"approve", "reject"}:
        raise ValueError("human merge decision must be approve or reject")
    reason = response.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("human rejection reason must be a string")
    if decision == "reject" and not reason.strip():
        raise ValueError("human rejection reason must be a non-empty string")
    return {
        "human_decision": decision,
        "rejection_reason": reason.strip(),
    }


def _approve_node(state: MergeGateState) -> MergeGateState:
    repo_path = Path(state["execution_repo_path"])
    source_branch = state["source_branch"]
    if not _branch_exists(repo_path, source_branch):
        raise FileNotFoundError(f"source branch not found: {source_branch}")

    if _branch_exists(repo_path, TARGET_BRANCH):
        _run_git(
            f"git switch {quote(TARGET_BRANCH)}",
            state=state,
        )
    else:
        _run_git(
            f"git switch -c {quote(TARGET_BRANCH)} main",
            state=state,
        )

    merge_message = (
        "loom rehearsal merge "
        f"segment_id={state['segment_id']} "
        f"run_id={state['run_id']} "
        f"source={source_branch}"
    )
    _run_git(
        f"git merge --no-ff -m {quote(merge_message)} {quote(source_branch)}",
        state=state,
    )
    merge_commit = _run_git("git rev-parse HEAD", state=state).strip()
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=state["segment_id"],
            run_id=state["run_id"],
            actor="harness",
            type="merge_completed",
            payload={
                "source_branch": source_branch,
                "target": TARGET_BRANCH,
                "merge_commit": merge_commit,
            },
        ),
        path=state["events_path"],
    )
    mark_handoff_merged(
        contract_path=state["contract_path"],
        run_id=state["run_id"],
        merge_commit=merge_commit,
        events_path=state["events_path"],
    )
    _run_git(f"git branch -d {quote(source_branch)}", state=state)
    return {"status": "merged", "merge_commit": merge_commit}


def _reject_node(state: MergeGateState) -> MergeGateState:
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=state["segment_id"],
            run_id=state["run_id"],
            actor="harness",
            type="merge_rejected",
            payload={
                "source_branch": state["source_branch"],
                "reason": state.get("rejection_reason", ""),
            },
        ),
        path=state["events_path"],
    )
    mark_handoff_rejected(
        contract_path=state["contract_path"],
        run_id=state["run_id"],
        reject_reason=state.get("rejection_reason", ""),
        events_path=state["events_path"],
    )
    return {"status": "rejected"}


def _refuse_merge(state: MergeGateState, *, reason: str) -> MergeGateState:
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=state["segment_id"],
            run_id=state["run_id"],
            actor="harness",
            type="merge_refused",
            payload={
                "source_branch": state["source_branch"],
                "target": TARGET_BRANCH,
                "reason": reason,
            },
        ),
        path=state["events_path"],
    )
    return {"status": "refused", "refusal_reason": reason}


def _latest_pointer_payload(
    *,
    events_path: Path,
    segment_id: str,
    run_id: str,
    event_type: str,
    branch_field: str,
    source_branch: str,
) -> dict[str, object] | None:
    if not events_path.exists():
        return None

    latest: dict[str, object] | None = None
    with events_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if (
                event.get("segment_id") != segment_id
                or event.get("run_id") != run_id
                or event.get("actor") != "harness"
                or event.get("type") != event_type
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get(branch_field) != source_branch:
                continue
            latest = payload
    return latest


def _route_after_audit(state: MergeGateState) -> str:
    return "refused" if state.get("status") == "refused" else "human"


def _route_after_human(state: MergeGateState) -> str:
    return state["human_decision"]


def _run_git(command: str, *, state: MergeGateState) -> str:
    result = run_observed(
        command,
        segment_id=state["segment_id"],
        run_id=state["run_id"],
        cwd=state["execution_repo_path"],
        path=state["events_path"],
        payload={"role": "merge_gate"},
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {command}")
    return result.stdout


def _terminal_decision_provider(presentation: dict[str, object]) -> dict[str, str]:
    print("Loom human merge gate")
    print(f"segment_id: {presentation['segment_id']}")
    print(f"run_id: {presentation['run_id']}")
    print(f"review report: {presentation['review_report_path']}")
    print(f"audit report: {presentation['audit_report_path']}")
    print(f"audit verdict: {presentation['audit_verdict']}")
    print(f"source branch: {presentation['source_branch']}")
    print(f"target: {presentation['target']}")
    while True:
        decision = input("Decision [approve/reject]: ").strip().lower()
        if decision in {"approve", "reject"}:
            break
        print("Enter approve or reject.")
    if decision == "approve":
        return {"decision": "approve"}
    reason = ""
    while not reason:
        reason = input("Reason (required): ").strip()
        if not reason:
            print("Enter a rejection reason.")
    return {"decision": "reject", "reason": reason}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Loom human merge gate.")
    parser.add_argument("--contract", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--events", default=str(DEFAULT_EVENTS_PATH))
    parser.add_argument("--repo", default=str(DEFAULT_EXECUTION_REPO_PATH))
    args = parser.parse_args(argv)
    result = run_merge_gate(
        contract_path=args.contract,
        run_id=args.run_id,
        source_branch=args.branch,
        events_path=args.events,
        execution_repo_path=args.repo,
    )
    print(f"merge gate status: {result['status']}")
    if "refusal_reason" in result:
        print(f"reason: {result['refusal_reason']}")
    if "merge_commit" in result:
        print(f"merge commit: {result['merge_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
