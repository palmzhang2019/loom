from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from shlex import quote
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .events import DEFAULT_EVENTS_PATH
from .harness import CommandResult, observe_files_changed, observe_step, run_observed
from .sandbox import (
    DEFAULT_EXECUTION_REPO_PATH,
    DEFAULT_WORKTREE_ROOT,
    Sandbox,
    create_sandbox,
    destroy_sandbox,
    _segment_variant,
)


class GraphState(TypedDict, total=False):
    contract_path: str
    events_path: str
    run_id: str
    segment_id: str
    test_selectors: list[str]
    attempts: int
    status: str
    sandbox: dict[str, str]
    segment: dict[str, Any]
    step: dict[str, Any]
    work_result: dict[str, Any]
    work_attempts: list[dict[str, Any]]
    test_result: dict[str, Any]
    test_attempts: list[dict[str, Any]]


_TOP_LEVEL_FIELD_PATTERN = re.compile(r"^(segment_id|covers_req|title):\s*(.+)$", re.MULTILINE)
_ACCEPTANCE_ID_PATTERN = re.compile(r"^\s*-\s+id:\s*(.+)$", re.MULTILINE)
_ACCEPTANCE_ENTRY_PATTERN = re.compile(
    r"^\s*-\s+id:\s*(.+)\n\s+text:\s*(.+)$",
    re.MULTILINE,
)
_SEQUENCE_DIAGRAM_PATTERN = re.compile(
    r"^preview:\n\s+sequence_diagram:\s*\|\n(?P<body>(?:\s{4}.+\n?)*)",
    re.MULTILINE,
)
MAX_WORK_ATTEMPTS = 4


@dataclass(frozen=True)
class WorkSessionInput:
    step_id: str
    segment_id: str
    sandbox_path: str
    branch_name: str
    run_id: str
    events_path: str
    attempt_number: int
    max_attempts: int
    mode: str
    title: str
    acceptance: list[AcceptanceCriterion]
    anti_scope: list[dict[str, str]]
    scope_paths: list[str]
    sequence_diagram: str
    previous_observed_files_changed: list[dict[str, Any]]
    previous_work_failure_reasons: list[str]
    previous_test_exit_code: int | None
    previous_test_stdout_path: str | None
    previous_test_stderr_path: str | None
    previous_test_stdout: str | None
    previous_test_stderr: str | None


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    text: str


@dataclass(frozen=True)
class TestSessionInput:
    acceptance: list[AcceptanceCriterion]
    sequence_diagram: str
    test_selectors: list[str]


WorkRunner = Callable[[WorkSessionInput], dict[str, Any]]
TestRunner = Callable[[TestSessionInput], dict[str, Any]]


def run_segment_graph(
    *,
    contract_path: Path | str,
    run_id: str,
    test_selectors: list[str] | tuple[str, ...] = (),
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    worktree_root: Path | str = DEFAULT_WORKTREE_ROOT,
    work_runner: WorkRunner | None = None,
    test_runner: TestRunner | None = None,
) -> GraphState:
    contract_file = Path(contract_path)
    segment_id = _extract_segment_id(contract_file)
    _require_unused_run_id(
        events_path=events_path,
        run_id=run_id,
        segment_id=segment_id,
    )
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
        "test_selectors": list(test_selectors),
        "attempts": 0,
        "status": "running",
        "work_attempts": [],
        "test_attempts": [],
        "sandbox": _sandbox_to_state(sandbox),
    }
    graph = _build_graph(
        work_runner=work_runner or _run_codex_work_session,
        test_runner=test_runner,
    )
    try:
        return graph.invoke(initial_state)
    finally:
        destroy_sandbox(
            sandbox,
            run_id=run_id,
            events_path=events_path,
        )


def _build_graph(*, work_runner: WorkRunner, test_runner: TestRunner | None):
    workflow = StateGraph(GraphState)
    workflow.add_node("orchestrator", _orchestrator_node)
    workflow.add_node("work", _make_work_node(work_runner))
    workflow.add_node("test", _make_test_node(test_runner))
    workflow.add_edge(START, "orchestrator")
    workflow.add_edge("orchestrator", "work")
    workflow.add_edge("work", "test")
    workflow.add_conditional_edges(
        "test",
        _route_after_test,
        {
            "retry": "work",
            "end": END,
        },
    )
    return workflow.compile()


def _orchestrator_node(state: GraphState) -> GraphState:
    def run() -> GraphState:
        segment = _load_segment_contract(Path(state["contract_path"]))
        return {
            "segment": segment,
            "attempts": state.get("attempts", 0),
            "status": state.get("status", "running"),
            "work_attempts": list(state.get("work_attempts", [])),
            "test_attempts": list(state.get("test_attempts", [])),
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


def _make_work_node(work_runner: WorkRunner):
    def _work_node(state: GraphState) -> GraphState:
        attempt_number = state.get("attempts", 0) + 1
        mode = "implement" if attempt_number == 1 else "fix"

        def run() -> GraphState:
            step = state["step"]
            sandbox = state["sandbox"]
            segment = state["segment"]
            previous_work_result = state.get("work_result", {})
            previous_test_result = state.get("test_result", {})
            work_input = WorkSessionInput(
                step_id=step["id"],
                segment_id=step["segment_id"],
                sandbox_path=sandbox["worktree_path"],
                branch_name=sandbox["branch_name"],
                run_id=state["run_id"],
                events_path=state["events_path"],
                attempt_number=attempt_number,
                max_attempts=MAX_WORK_ATTEMPTS,
                mode=mode,
                title=segment["title"],
                acceptance=[
                    AcceptanceCriterion(id=item["id"], text=item["text"])
                    for item in segment["acceptance"]
                ],
                anti_scope=list(segment["anti_scope"]),
                scope_paths=list(segment["scope_paths"]),
                sequence_diagram=segment["sequence_diagram"],
                previous_observed_files_changed=list(previous_work_result.get("observed_files_changed", [])),
                previous_work_failure_reasons=list(previous_work_result.get("failure_reasons", [])),
                previous_test_exit_code=_optional_int(previous_test_result.get("exit_code")),
                previous_test_stdout_path=_optional_str(previous_test_result.get("stdout_path")),
                previous_test_stderr_path=_optional_str(previous_test_result.get("stderr_path")),
                previous_test_stdout=_load_optional_text(previous_test_result.get("stdout_path")),
                previous_test_stderr=_load_optional_text(previous_test_result.get("stderr_path")),
            )
            work_result = _enrich_work_result(work_runner(work_input), work_input)
            return {
                "attempts": attempt_number,
                "work_result": work_result,
                "work_attempts": [*state.get("work_attempts", []), work_result],
            }

        return observe_step(
            run,
            actor="work",
            step_name="work",
            segment_id=state["segment_id"],
            run_id=state["run_id"],
            path=state["events_path"],
            payload={"attempt": attempt_number, "mode": mode},
        )

    return _work_node


def _make_test_node(test_runner: TestRunner | None):
    def _test_node(state: GraphState) -> GraphState:
        attempt_number = state["attempts"]

        def run() -> GraphState:
            test_input = _load_test_session_input(
                Path(state["contract_path"]),
                test_selectors=state.get("test_selectors", []),
            )
            if test_runner is None:
                test_result = _run_pytest_test_session(
                    test_input,
                    segment_id=state["segment_id"],
                    sandbox_path=state["sandbox"]["worktree_path"],
                    run_id=state["run_id"],
                    events_path=state["events_path"],
                    attempt_number=attempt_number,
                )
            else:
                test_result = _enrich_test_result(test_runner(test_input), attempt_number)
            status = _status_after_test(
                work_result=state["work_result"],
                test_result=test_result,
                attempts=attempt_number,
            )
            return {
                "status": status,
                "test_result": test_result,
                "test_attempts": [*state.get("test_attempts", []), test_result],
            }

        return observe_step(
            run,
            actor="test",
            step_name="test",
            segment_id=state["segment_id"],
            run_id=state["run_id"],
            path=state["events_path"],
            payload={"attempt": attempt_number},
        )

    return _test_node


def _route_after_test(state: GraphState) -> str:
    return "retry" if state.get("status") == "retrying" else "end"


def _require_unused_run_id(*, events_path: Path | str, run_id: str, segment_id: str) -> None:
    path = Path(events_path)
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("run_id") != run_id:
                continue
            existing_segment_id = str(payload.get("segment_id", ""))
            raise RuntimeError(
                f"run_id already exists in events log: {run_id}"
                + (f" (segment_id={existing_segment_id})" if existing_segment_id else f" (expected segment_id={segment_id})")
            )


def _extract_segment_id(contract_path: Path) -> str:
    content = contract_path.read_text(encoding="utf-8")
    match = re.search(r"^segment_id:\s*(.+)$", content, re.MULTILINE)
    if match is None:
        raise ValueError(f"segment_id not found in contract: {contract_path}")
    return match.group(1).strip()


def _load_segment_contract(contract_path: Path) -> dict[str, Any]:
    parsed = _parse_segment_contract(contract_path)
    parsed["contract_path"] = str(contract_path)
    parsed["acceptance_ids"] = [item["id"] for item in parsed["acceptance"]]
    return parsed


def _load_test_session_input(
    contract_path: Path,
    *,
    test_selectors: list[str],
) -> TestSessionInput:
    parsed = _parse_segment_contract(contract_path)
    acceptance = [
        AcceptanceCriterion(id=item["id"], text=item["text"])
        for item in parsed["acceptance"]
    ]

    return TestSessionInput(
        acceptance=acceptance,
        sequence_diagram=parsed["sequence_diagram"],
        test_selectors=list(test_selectors),
    )


def _run_codex_work_session(work_input: WorkSessionInput) -> dict[str, Any]:
    runtime_dir = _work_runtime_dir(work_input)
    prompt_path = runtime_dir / "prompt.txt"
    schema_path = runtime_dir / "output-schema.json"
    declaration_path = runtime_dir / "codex-declaration.json"
    stdout_path = runtime_dir / "codex-stdout.txt"
    stderr_path = runtime_dir / "codex-stderr.txt"

    prompt_path.write_text(_build_work_prompt(work_input), encoding="utf-8")
    schema_path.write_text(json.dumps(_codex_output_schema(), indent=2), encoding="utf-8")

    command = (
        "codex exec "
        "--sandbox workspace-write "
        "--color never "
        f"--output-schema {quote(str(schema_path))} "
        f"--output-last-message {quote(str(declaration_path))} "
        f"- < {quote(str(prompt_path))}"
    )
    command_result: CommandResult | None = None

    def invoke() -> CommandResult:
        nonlocal command_result
        command_result = run_observed(
            command,
            segment_id=work_input.segment_id,
            run_id=work_input.run_id,
            cwd=work_input.sandbox_path,
            path=work_input.events_path,
            payload={"attempt": work_input.attempt_number, "mode": work_input.mode},
        )
        return command_result

    changes = observe_files_changed(
        invoke,
        git_dir=work_input.sandbox_path,
        segment_id=work_input.segment_id,
        run_id=work_input.run_id,
        path=work_input.events_path,
        payload={"attempt": work_input.attempt_number, "mode": work_input.mode},
    )
    if command_result is None:
        raise RuntimeError("codex exec did not produce a command result")

    stdout_path.write_text(command_result.stdout, encoding="utf-8")
    stderr_path.write_text(command_result.stderr, encoding="utf-8")

    declaration = _load_declaration(declaration_path)
    out_of_scope_paths = [
        change.path
        for change in changes
        if not _is_in_scope(change.path, work_input.scope_paths)
    ]
    failure_reasons: list[str] = []
    if command_result.exit_code != 0:
        failure_reasons.append("command_failed")
    if not changes:
        failure_reasons.append("no_files_changed")
    if out_of_scope_paths:
        failure_reasons.append("out_of_scope_changes")
    if not declaration["present"]:
        failure_reasons.append("missing_declaration")
    elif not declaration["json_valid"]:
        failure_reasons.append("invalid_declaration_json")

    return {
        "step_id": work_input.step_id,
        "status": "failed" if failure_reasons else "succeeded",
        "summary": f"codex exec {work_input.mode} attempt completed",
        "sandbox_path": work_input.sandbox_path,
        "branch_name": work_input.branch_name,
        "attempt_number": work_input.attempt_number,
        "mode": work_input.mode,
        "exit_code": command_result.exit_code,
        "artifact_dir": str(runtime_dir),
        "prompt_path": str(prompt_path),
        "output_schema_path": str(schema_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "declaration": declaration,
        "observed_files_changed": [
            {
                "path": change.path,
                "change_type": change.change_type,
                "before_hash": change.before_hash,
                "after_hash": change.after_hash,
            }
            for change in changes
        ],
        "out_of_scope_paths": out_of_scope_paths,
        "failure_reasons": failure_reasons,
    }


def _run_pytest_test_session(
    test_input: TestSessionInput,
    *,
    segment_id: str,
    sandbox_path: str,
    run_id: str,
    events_path: str,
    attempt_number: int,
) -> dict[str, Any]:
    if not test_input.test_selectors:
        raise ValueError("test_selectors are required for real test execution")

    runtime_dir = _test_runtime_dir(
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
        attempt_number=attempt_number,
    )
    stdout_path = runtime_dir / "pytest-stdout.txt"
    stderr_path = runtime_dir / "pytest-stderr.txt"
    selectors = " ".join(quote(selector) for selector in test_input.test_selectors)
    command_result = run_observed(
        f"uv run pytest {selectors}",
        segment_id=segment_id,
        run_id=run_id,
        cwd=sandbox_path,
        path=events_path,
        payload={"attempt": attempt_number},
    )
    stdout_path.write_text(command_result.stdout, encoding="utf-8")
    stderr_path.write_text(command_result.stderr, encoding="utf-8")
    return {
        "passed": command_result.exit_code == 0,
        "summary": "pytest test run completed",
        "attempt_number": attempt_number,
        "exit_code": command_result.exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "sandbox_path": sandbox_path,
        "test_selectors": list(test_input.test_selectors),
    }


def _parse_segment_contract(contract_path: Path) -> dict[str, Any]:
    content = contract_path.read_text(encoding="utf-8")
    top_level_fields = {
        field_name: raw_value.strip()
        for field_name, raw_value in _TOP_LEVEL_FIELD_PATTERN.findall(content)
    }
    if "segment_id" not in top_level_fields:
        raise ValueError(f"segment_id not found in contract: {contract_path}")
    if "covers_req" not in top_level_fields:
        raise ValueError(f"covers_req not found in contract: {contract_path}")
    if "title" not in top_level_fields:
        raise ValueError(f"title not found in contract: {contract_path}")

    acceptance = [
        {"id": criterion_id.strip(), "text": text.strip()}
        for criterion_id, text in _ACCEPTANCE_ENTRY_PATTERN.findall(content)
    ]
    if not acceptance:
        raise ValueError(f"acceptance not found in contract: {contract_path}")

    sequence_diagram = _extract_sequence_diagram(content, contract_path)
    anti_scope = _extract_block_items(content, header="anti_scope")
    scope_paths = _extract_block_scalars(content, header="scope_paths")
    if not scope_paths:
        raise ValueError(f"scope_paths not found in contract: {contract_path}")

    return {
        "segment_id": top_level_fields["segment_id"],
        "covers_req": top_level_fields["covers_req"],
        "title": top_level_fields["title"],
        "acceptance": acceptance,
        "anti_scope": anti_scope,
        "scope_paths": scope_paths,
        "sequence_diagram": sequence_diagram,
    }


def _extract_sequence_diagram(content: str, contract_path: Path) -> str:
    match = _SEQUENCE_DIAGRAM_PATTERN.search(content)
    if match is None:
        raise ValueError(f"preview.sequence_diagram not found in contract: {contract_path}")
    return "\n".join(
        line[4:] if line.startswith("    ") else line
        for line in match.group("body").splitlines()
    )


def _extract_block_items(content: str, *, header: str) -> list[dict[str, str]]:
    lines = content.splitlines()
    items: list[dict[str, str]] = []
    index = _find_header_line(lines, header)
    if index is None:
        return items

    i = index + 1
    while i < len(lines) and lines[i].startswith("  - "):
        current: dict[str, str] = {}
        first_key, first_value = _split_mapping_line(lines[i].strip()[2:].strip())
        current[first_key] = first_value
        i += 1
        while i < len(lines) and lines[i].startswith("    "):
            key, value = _split_mapping_line(lines[i].strip())
            current[key] = value
            i += 1
        items.append(current)
    return items


def _extract_block_scalars(content: str, *, header: str) -> list[str]:
    lines = content.splitlines()
    values: list[str] = []
    index = _find_header_line(lines, header)
    if index is None:
        return values

    i = index + 1
    while i < len(lines) and lines[i].startswith("  - "):
        values.append(lines[i].strip()[2:].strip())
        i += 1
    return values


def _find_header_line(lines: list[str], header: str) -> int | None:
    target = f"{header}:"
    for index, line in enumerate(lines):
        if line == target:
            return index
    return None


def _split_mapping_line(line: str) -> tuple[str, str]:
    key, raw_value = line.split(":", 1)
    return key.strip(), raw_value.strip()


def _work_runtime_dir(work_input: WorkSessionInput) -> Path:
    return _runtime_dir(
        segment_id=work_input.segment_id,
        run_id=work_input.run_id,
        events_path=work_input.events_path,
        step_name=f"work-attempt-{work_input.attempt_number}",
    )


def _test_runtime_dir(*, segment_id: str, run_id: str, events_path: str, attempt_number: int) -> Path:
    return _runtime_dir(
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
        step_name=f"test-attempt-{attempt_number}",
    )


def _runtime_dir(*, segment_id: str, run_id: str, events_path: str, step_name: str) -> Path:
    segment_variant = _segment_variant(segment_id)
    runtime_dir = Path(events_path).resolve().parent / "runs" / run_id / segment_variant / step_name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _build_work_prompt(work_input: WorkSessionInput) -> str:
    acceptance_lines = "\n".join(
        f"- {criterion.id}: {criterion.text}"
        for criterion in work_input.acceptance
    )
    anti_scope_lines = "\n".join(
        f"- [{item['kind']}] {item['text']}"
        + (f" -> {item['defer_to']}" if "defer_to" in item else "")
        for item in work_input.anti_scope
    )
    scope_lines = "\n".join(f"- {path}" for path in work_input.scope_paths)
    observed_changes_lines = "\n".join(
        f"- {item['path']} ({item['change_type']})"
        for item in work_input.previous_observed_files_changed
    ) or "- none"
    failure_reasons_lines = "\n".join(f"- {reason}" for reason in work_input.previous_work_failure_reasons) or "- none"
    previous_stdout = work_input.previous_test_stdout or "(stdout unavailable)"
    previous_stderr = work_input.previous_test_stderr or "(stderr unavailable)"
    return "\n".join(
        [
            f"{work_input.mode.title()} segment {work_input.segment_id} in this repository.",
            f"Attempt: {work_input.attempt_number} / {work_input.max_attempts}",
            "",
            "Contract:",
            f"Title: {work_input.title}",
            "Acceptance:",
            acceptance_lines,
            "Allowed scope_paths:",
            scope_lines,
            "Anti-scope:",
            anti_scope_lines,
            "Sequence diagram:",
            work_input.sequence_diagram,
            "",
            "Fix context from the previous attempt:"
            if work_input.mode == "fix"
            else "No prior failure context for the initial implement attempt.",
            "Observed sandbox changes (from harness files_changed):",
            observed_changes_lines,
            "Previous work failure reasons:",
            failure_reasons_lines,
            f"Previous pytest exit code: {work_input.previous_test_exit_code}"
            if work_input.previous_test_exit_code is not None
            else "Previous pytest exit code: unavailable",
            "Previous pytest stdout:",
            previous_stdout,
            "Previous pytest stderr:",
            previous_stderr,
            "",
            "Constraints:",
            "- Stay within the listed scope_paths.",
            "- Do not modify files outside scope_paths.",
            "- Return only JSON matching the provided output schema.",
        ]
    )


def _codex_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "claimed_changed_files": {
                "type": "array",
                "items": {"type": "string"},
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["summary", "claimed_changed_files", "notes"],
    }


def _load_declaration(declaration_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "agent_declaration",
        "path": str(declaration_path),
        "present": declaration_path.exists(),
        "json_valid": False,
    }
    if not declaration_path.exists():
        return payload
    try:
        json.loads(declaration_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return payload
    payload["json_valid"] = True
    return payload


def _is_in_scope(path: str, scope_paths: list[str]) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in scope_paths)


def _enrich_work_result(result: dict[str, Any], work_input: WorkSessionInput) -> dict[str, Any]:
    payload = dict(result)
    payload.setdefault("step_id", work_input.step_id)
    payload.setdefault("sandbox_path", work_input.sandbox_path)
    payload.setdefault("branch_name", work_input.branch_name)
    payload.setdefault("status", "succeeded")
    payload.setdefault("failure_reasons", [])
    payload.setdefault("observed_files_changed", [])
    payload["attempt_number"] = work_input.attempt_number
    payload["mode"] = work_input.mode
    return payload


def _enrich_test_result(result: dict[str, Any], attempt_number: int) -> dict[str, Any]:
    payload = dict(result)
    payload.setdefault("passed", False)
    payload.setdefault("test_selectors", [])
    payload["attempt_number"] = attempt_number
    return payload


def _status_after_test(*, work_result: dict[str, Any], test_result: dict[str, Any], attempts: int) -> str:
    work_succeeded = work_result.get("status", "succeeded") == "succeeded"
    test_passed = bool(test_result.get("passed"))
    if work_succeeded and test_passed:
        return "passed"
    if attempts >= MAX_WORK_ATTEMPTS:
        return "failed"
    return "retrying"


def _load_optional_text(path_value: Any) -> str | None:
    path_str = _optional_str(path_value)
    if path_str is None:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _sandbox_to_state(sandbox: Sandbox) -> dict[str, str]:
    return {
        "repo_path": str(sandbox.repo_path),
        "worktree_path": str(sandbox.worktree_path),
        "branch_name": sandbox.branch_name,
        "base_branch": sandbox.base_branch,
    }
