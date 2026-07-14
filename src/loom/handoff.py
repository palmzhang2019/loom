from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote
from typing import Literal, Sequence

from .events import DEFAULT_EVENTS_PATH, Event, append_event
from .graph import _load_segment_contract
from .harness import run_observed
from .sandbox import DEFAULT_EXECUTION_REPO_PATH


SeamKind = Literal["route", "function", "db"]
_HTTP_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}
_SEAM_KIND_ORDER = {"route": 0, "function": 1, "db": 2}


@dataclass(frozen=True)
class Seam:
    kind: SeamKind
    signature: str


@dataclass(frozen=True)
class SourcePair:
    path: str
    before: str | None
    after: str


@dataclass(frozen=True)
class _DbValue:
    signature: str


@dataclass(frozen=True)
class _Inventory:
    routes: set[str]
    functions: dict[str, str]
    db: dict[tuple[str, ...], _DbValue]


@dataclass(frozen=True)
class _BranchFacts:
    seams: list[Seam]
    test_files: list[str]


@dataclass(frozen=True)
class _ChangedPath:
    status: Literal["A", "M", "R"]
    before_path: str | None
    after_path: str


def generate_pending_handoff(
    *,
    contract_path: Path | str,
    run_id: str,
    source_branch: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
) -> Path:
    if not source_branch.startswith("loom/"):
        raise ValueError("handoff only supports retained loom/ branches")

    contract = _load_segment_contract(Path(contract_path))
    segment_id = str(contract["segment_id"])
    events_file = Path(events_path)
    branch_facts = _extract_branch_facts(
        repo_path=Path(execution_repo_path),
        source_branch=source_branch,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_file,
    )
    record_path = _handoff_path(events_file, run_id)
    _write_record(
        record_path,
        _render_pending_record(
            covers_req=str(contract["covers_req"]),
            seams=branch_facts.seams,
            deferred=_contract_deferred(contract),
            test_files=branch_facts.test_files,
        ),
    )
    _append_handoff_event(
        event_type="handoff_generated",
        merge_status="pending",
        record_path=record_path,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_file,
    )
    return record_path


def mark_handoff_merged(
    *,
    contract_path: Path | str,
    run_id: str,
    merge_commit: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
) -> Path:
    if not isinstance(merge_commit, str) or not merge_commit.strip():
        raise ValueError("merge_commit must be a non-empty string")

    contract = _load_segment_contract(Path(contract_path))
    events_file = Path(events_path)
    record_path = _handoff_path(events_file, run_id)
    if not record_path.exists():
        raise FileNotFoundError(f"pending handoff not found: {record_path}")

    text = record_path.read_text(encoding="utf-8")
    if text.count("merge_status: pending") != 1:
        raise ValueError("handoff must be pending before it can be marked merged")
    if text.count("  merge_commit: null") != 1:
        raise ValueError("pending handoff must have an empty merge_commit")
    updated = text.replace("merge_status: pending", "merge_status: merged", 1)
    updated = updated.replace(
        "  merge_commit: null",
        f"  merge_commit: {_yaml_string(merge_commit.strip())}",
        1,
    )
    _write_record(record_path, updated)
    _append_handoff_event(
        event_type="handoff_updated",
        merge_status="merged",
        record_path=record_path,
        segment_id=str(contract["segment_id"]),
        run_id=run_id,
        events_path=events_file,
    )
    return record_path


def mark_handoff_rejected(
    *,
    contract_path: Path | str,
    run_id: str,
    reject_reason: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
) -> Path:
    contract = _load_segment_contract(Path(contract_path))
    events_file = Path(events_path)
    record_path = _handoff_path(events_file, run_id)
    if not record_path.exists():
        raise FileNotFoundError(f"pending handoff not found: {record_path}")
    text = record_path.read_text(encoding="utf-8")
    if text.count("merge_status: pending") != 1:
        raise ValueError("handoff must be pending before it can be marked rejected")

    _write_rejected_record(
        record_path=record_path,
        contract=contract,
        reject_reason=_require_reason_string(reject_reason),
    )
    _append_handoff_event(
        event_type="handoff_updated",
        merge_status="rejected",
        record_path=record_path,
        segment_id=str(contract["segment_id"]),
        run_id=run_id,
        events_path=events_file,
    )
    return record_path


def extract_seams_from_branch(
    *,
    repo_path: Path,
    source_branch: str,
    segment_id: str,
    run_id: str,
    events_path: Path,
) -> list[Seam]:
    return _extract_branch_facts(
        repo_path=repo_path,
        source_branch=source_branch,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
    ).seams


def _extract_branch_facts(
    *,
    repo_path: Path,
    source_branch: str,
    segment_id: str,
    run_id: str,
    events_path: Path,
) -> _BranchFacts:
    merge_base = _run_git(
        f"git merge-base main {quote(source_branch)}",
        repo_path=repo_path,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
        artifact="merge_base",
    ).strip()
    if not merge_base:
        raise RuntimeError("git merge-base returned an empty revision")

    changed = _run_git(
        (
            "git diff --no-ext-diff --name-status -z --find-renames "
            f"--diff-filter=AMR {quote(merge_base)}..{quote(source_branch)} --"
        ),
        repo_path=repo_path,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
        artifact="changed_python_paths",
    )
    pairs: list[SourcePair] = []
    test_files: list[str] = []
    for change in _parse_name_status(changed):
        if change.status == "A" and _is_test_file_pointer(change.after_path):
            test_files.append(change.after_path)
        if not _is_production_python(change.after_path):
            continue
        before = None
        if (
            change.before_path is not None
            and _is_production_python(change.before_path)
        ):
            before = _read_git_source(
                repo_path=repo_path,
                revision=merge_base,
                path=change.before_path,
                segment_id=segment_id,
                run_id=run_id,
                events_path=events_path,
                snapshot="merge_base",
            )
        after = _read_git_source(
            repo_path=repo_path,
            revision=source_branch,
            path=change.after_path,
            segment_id=segment_id,
            run_id=run_id,
            events_path=events_path,
            snapshot="source_branch",
        )
        pairs.append(SourcePair(path=change.after_path, before=before, after=after))
    return _BranchFacts(
        seams=extract_python_seams(pairs),
        test_files=sorted(test_files),
    )


def extract_python_seams(pairs: Sequence[SourcePair]) -> list[Seam]:
    seams: set[Seam] = set()
    for pair in pairs:
        before = _empty_inventory() if pair.before is None else _inventory(pair.before, pair.path)
        after = _inventory(pair.after, pair.path)

        for signature in after.routes:
            if signature not in before.routes:
                seams.add(Seam(kind="route", signature=signature))

        for key, signature in after.functions.items():
            if before.functions.get(key) != signature:
                seams.add(Seam(kind="function", signature=signature))

        for key, value in after.db.items():
            previous = before.db.get(key)
            if previous is None or previous.signature != value.signature:
                seams.add(Seam(kind="db", signature=value.signature))

    return sorted(
        seams,
        key=lambda seam: (_SEAM_KIND_ORDER[seam.kind], seam.signature),
    )


def _inventory(source: str, path: str) -> _Inventory:
    try:
        module = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise ValueError(f"cannot extract seams from invalid Python: {path}") from error

    router_prefixes = _router_prefixes(module, path)
    routes: set[str] = set()
    functions: dict[str, str] = {}
    db: dict[tuple[str, ...], _DbValue] = {}

    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = _function_signature(node)
            if not node.name.startswith("_"):
                functions[node.name] = signature

            for decorator in node.decorator_list:
                route = _route_from_decorator(
                    decorator,
                    router_prefixes=router_prefixes,
                    path=path,
                )
                if route is None:
                    continue
                routes.add(route)

        if isinstance(node, ast.ClassDef):
            _collect_model_db(node, db, path)

    return _Inventory(routes=routes, functions=functions, db=db)


def _router_prefixes(module: ast.Module, path: str) -> dict[str, str | None]:
    prefixes: dict[str, str | None] = {}
    for node in module.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        if _terminal_name(value.func) not in {"APIRouter", "FastAPI"}:
            continue

        prefix_node = next(
            (keyword.value for keyword in value.keywords if keyword.arg == "prefix"),
            None,
        )
        if prefix_node is None:
            prefixes[target.id] = ""
        elif isinstance(prefix_node, ast.Constant) and isinstance(prefix_node.value, str):
            prefixes[target.id] = prefix_node.value
        else:
            prefixes[target.id] = None
    return prefixes


def _route_from_decorator(
    decorator: ast.expr,
    *,
    router_prefixes: dict[str, str | None],
    path: str,
) -> str | None:
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    if decorator.func.attr not in _HTTP_METHODS:
        return None
    if not isinstance(decorator.func.value, ast.Name):
        return None
    router_name = decorator.func.value.id
    if router_name not in router_prefixes:
        return None
    prefix = router_prefixes[router_name]
    if prefix is None:
        raise ValueError(f"dynamic FastAPI router prefix is not extractable: {path}")

    path_node: ast.expr | None = decorator.args[0] if decorator.args else None
    if path_node is None:
        path_node = next(
            (keyword.value for keyword in decorator.keywords if keyword.arg == "path"),
            None,
        )
    if not isinstance(path_node, ast.Constant) or not isinstance(path_node.value, str):
        raise ValueError(f"dynamic FastAPI route path is not extractable: {path}")
    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError(f"invalid FastAPI router prefix is not extractable: {path}")
    route_path = path_node.value
    if route_path == "" and not prefix:
        raise ValueError(f"empty FastAPI route path requires a prefix: {path}")
    if route_path and not route_path.startswith("/"):
        raise ValueError(f"invalid FastAPI route path is not extractable: {path}")

    method = decorator.func.attr.upper()
    full_path = _join_route_path(prefix, route_path)
    return f"{method} {full_path}"


def _join_route_path(prefix: str, route_path: str) -> str:
    if not prefix:
        joined = route_path or "/"
    elif not route_path:
        joined = prefix
    else:
        joined = prefix.rstrip("/") + "/" + route_path.lstrip("/")
    return joined


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    signature = f"{prefix}{node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        signature += f" -> {ast.unparse(node.returns)}"
    return signature


def _collect_model_db(
    node: ast.ClassDef,
    db: dict[tuple[str, ...], _DbValue],
    path: str,
) -> None:
    table_name: str | None = None
    for statement in node.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "__tablename__"
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            table_name = statement.value.value
            break
    if table_name is None:
        return

    db[("table", table_name)] = _DbValue(signature=f"table {table_name}")
    for statement in node.body:
        column_name: str | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                column_name = target.id
                value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            column_name = statement.target.id
            value = statement.value
        if column_name is None or not isinstance(value, ast.Call):
            continue
        if _terminal_name(value.func) != "Column":
            continue
        db[("column", table_name, column_name)] = _DbValue(
            signature=(
                f"column {table_name}.{column_name}: "
                f"{_canonical_column_call(value, path)}"
            ),
        )


def _canonical_column_call(node: ast.Call, path: str) -> str:
    positional: list[str] = []
    for argument in node.args:
        if isinstance(argument, ast.Starred):
            raise ValueError(f"dynamic SQLAlchemy Column is not extractable: {path}")
        positional.append(ast.unparse(argument))

    keywords: list[tuple[str, str]] = []
    for keyword in node.keywords:
        if keyword.arg is None:
            raise ValueError(f"dynamic SQLAlchemy Column is not extractable: {path}")
        keywords.append((keyword.arg, ast.unparse(keyword.value)))
    arguments = positional + [
        f"{name}={value}" for name, value in sorted(keywords)
    ]
    return f"Column({', '.join(arguments)})"


def _terminal_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _empty_inventory() -> _Inventory:
    return _Inventory(routes=set(), functions={}, db={})


def _parse_name_status(raw: str) -> list[_ChangedPath]:
    parts = raw.split("\0")
    if parts and parts[-1] == "":
        parts.pop()

    changed: list[_ChangedPath] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if status in {"A", "M"}:
            if index >= len(parts):
                raise RuntimeError("git diff returned malformed name-status output")
            path = parts[index]
            index += 1
            changed.append(
                _ChangedPath(
                    status=status,
                    before_path=path if status == "M" else None,
                    after_path=path,
                )
            )
            continue
        if status.startswith("R"):
            if index + 1 >= len(parts):
                raise RuntimeError("git diff returned malformed rename output")
            before_path = parts[index]
            after_path = parts[index + 1]
            index += 2
            changed.append(
                _ChangedPath(
                    status="R",
                    before_path=before_path,
                    after_path=after_path,
                )
            )
            continue
        raise RuntimeError(f"unexpected changed path status: {status}")
    return changed


def _is_under_tests(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py")


def _is_production_python(path: str) -> bool:
    return path.endswith(".py") and not _is_under_tests(path)


def _is_test_file_pointer(path: str) -> bool:
    return _is_under_tests(path) and Path(path).name.startswith("test_")


def _read_git_source(
    *,
    repo_path: Path,
    revision: str,
    path: str,
    segment_id: str,
    run_id: str,
    events_path: Path,
    snapshot: str,
) -> str:
    return _run_git(
        f"git show {quote(f'{revision}:{path}')}",
        repo_path=repo_path,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
        artifact="python_source_snapshot",
        payload={
            "source_path": path,
            "snapshot": snapshot,
            "stdout": "[omitted: source snapshot]",
            "stderr": "[omitted: source snapshot stderr]",
        },
    )


def _run_git(
    command: str,
    *,
    repo_path: Path,
    segment_id: str,
    run_id: str,
    events_path: Path,
    artifact: str,
    payload: dict[str, object] | None = None,
) -> str:
    result = run_observed(
        command,
        segment_id=segment_id,
        run_id=run_id,
        cwd=repo_path,
        path=events_path,
        payload={"role": "handoff", "artifact": artifact, **(payload or {})},
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {command}")
    return result.stdout


def _contract_deferred(contract: dict[str, object]) -> list[dict[str, str]]:
    anti_scope = contract.get("anti_scope", [])
    if not isinstance(anti_scope, list):
        raise ValueError("contract anti_scope must be a list")
    return [
        {
            "text": str(item["text"]),
            "origin": "contract",
            "defer_to": str(item["defer_to"]),
        }
        for item in anti_scope
        if isinstance(item, dict) and item.get("kind") == "defer"
    ]


def _render_pending_record(
    *,
    covers_req: str,
    seams: list[Seam],
    deferred: list[dict[str, str]],
    test_files: list[str],
) -> str:
    lines = [
        f"covers_req: {_yaml_plain(covers_req)}",
        "merge_status: pending",
    ]
    if seams:
        lines.append("seams:")
        for seam in seams:
            lines.extend(
                [
                    f"  - kind: {seam.kind}",
                    f"    signature: {_yaml_string(seam.signature)}",
                ]
            )
    else:
        lines.append("seams: []")
    lines.extend(_render_deferred(deferred))
    lines.extend(["key_decisions: []", "pointers:", "  merge_commit: null"])
    if test_files:
        lines.append("  test_files:")
        lines.extend(f"    - {_yaml_plain(path)}" for path in test_files)
    else:
        lines.append("  test_files: []")
    lines.extend(["  as_built_diagram: null", ""])
    return "\n".join(lines)


def _render_rejected_record(
    *,
    covers_req: str,
    reject_reason: str,
    deferred: list[dict[str, str]],
) -> str:
    lines = [
        f"covers_req: {_yaml_plain(covers_req)}",
        "merge_status: rejected",
        f"reject_reason: {_yaml_string(reject_reason)}",
        *_render_deferred(deferred),
        "",
    ]
    return "\n".join(lines)


def _render_deferred(deferred: list[dict[str, str]]) -> list[str]:
    if not deferred:
        return ["deferred: []"]
    lines = ["deferred:"]
    for item in deferred:
        lines.extend(
            [
                f"  - text: {_yaml_string(item['text'])}",
                "    origin: contract",
                f"    defer_to: {_yaml_plain(item['defer_to'])}",
            ]
        )
    return lines


def _yaml_plain(value: str) -> str:
    if not value or any(character.isspace() for character in value) or ":" in value:
        return _yaml_string(value)
    return value


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _handoff_path(events_path: Path, run_id: str) -> Path:
    return events_path.resolve().parent / "runs" / run_id / "handoff" / "handoff.yaml"


def _write_record(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_rejected_record(
    *,
    record_path: Path,
    contract: dict[str, object],
    reject_reason: str,
) -> None:
    _write_record(
        record_path,
        _render_rejected_record(
            covers_req=str(contract["covers_req"]),
            reject_reason=_require_reason_string(reject_reason),
            deferred=_contract_deferred(contract),
        ),
    )


def _append_handoff_event(
    *,
    event_type: str,
    merge_status: str,
    record_path: Path,
    segment_id: str,
    run_id: str,
    events_path: Path,
) -> None:
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor="harness",
            type=event_type,
            payload={"path": str(record_path), "merge_status": merge_status},
        ),
        path=events_path,
    )


def _latest_merge_rejection(
    *,
    events_path: Path,
    segment_id: str,
    run_id: str,
    source_branch: str,
) -> str | None:
    if not events_path.exists():
        return None
    latest: str | None = None
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
                or event.get("type") != "merge_rejected"
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("source_branch") != source_branch:
                continue
            reason = payload.get("reason")
            if isinstance(reason, str):
                latest = reason
    return latest


def _materialize_from_existing_events(
    *,
    contract_path: Path,
    run_id: str,
    source_branch: str,
    events_path: Path,
    execution_repo_path: Path,
) -> Path:
    contract = _load_segment_contract(contract_path)
    segment_id = str(contract["segment_id"])
    rejection_reason = _latest_merge_rejection(
        events_path=events_path,
        segment_id=segment_id,
        run_id=run_id,
        source_branch=source_branch,
    )
    if rejection_reason is None:
        return generate_pending_handoff(
            contract_path=contract_path,
            run_id=run_id,
            source_branch=source_branch,
            events_path=events_path,
            execution_repo_path=execution_repo_path,
        )

    record_path = _handoff_path(events_path, run_id)
    _write_rejected_record(
        record_path=record_path,
        contract=contract,
        reject_reason=rejection_reason,
    )
    _append_handoff_event(
        event_type="handoff_generated",
        merge_status="rejected",
        record_path=record_path,
        segment_id=segment_id,
        run_id=run_id,
        events_path=events_path,
    )
    return record_path


def _require_reason_string(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reject_reason must be a non-empty string")
    return reason.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Loom handoff from a retained branch or existing rejection."
    )
    parser.add_argument("--contract", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--events", default=str(DEFAULT_EVENTS_PATH))
    parser.add_argument("--repo", default=str(DEFAULT_EXECUTION_REPO_PATH))
    args = parser.parse_args(argv)
    record_path = _materialize_from_existing_events(
        contract_path=Path(args.contract),
        run_id=args.run_id,
        source_branch=args.branch,
        events_path=Path(args.events),
        execution_repo_path=Path(args.repo).expanduser(),
    )
    print(record_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
