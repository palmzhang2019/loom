from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote
from typing import Any, Callable

from .events import DEFAULT_EVENTS_PATH, Event, append_event
from .graph import AcceptanceCriterion, _is_in_scope, _load_segment_contract
from .harness import run_observed
from .sandbox import DEFAULT_EXECUTION_REPO_PATH


@dataclass(frozen=True)
class ReviewSessionInput:
    segment_id: str
    run_id: str
    reviewed_branch: str
    acceptance: list[AcceptanceCriterion]
    diff: str
    execution_repo_path: str
    events_path: str


ReviewRunner = Callable[[ReviewSessionInput], dict[str, object]]


def run_segment_review(
    *,
    contract_path: Path | str,
    run_id: str,
    reviewed_branch: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    review_runner: ReviewRunner | None = None,
) -> Path:
    if not reviewed_branch.startswith("loom/"):
        raise ValueError("review only supports retained loom/ branches")

    contract = _load_segment_contract(Path(contract_path))
    segment_id = str(contract["segment_id"])
    events_file = Path(events_path)
    repo_path = Path(execution_repo_path)
    changed_paths = _load_observed_changed_paths(
        events_path=events_file,
        segment_id=segment_id,
        run_id=run_id,
    )
    out_of_scope_paths = [
        path
        for path in changed_paths
        if not _is_in_scope(path, list(contract["scope_paths"]))
    ]

    diff_result = run_observed(
        f"git diff --no-ext-diff main...{quote(reviewed_branch)} --",
        segment_id=segment_id,
        run_id=run_id,
        cwd=repo_path,
        path=events_file,
        payload={"role": "review", "artifact": "branch_diff"},
    )
    if diff_result.exit_code != 0:
        raise RuntimeError(diff_result.stderr.strip() or "git diff failed")

    review_input = ReviewSessionInput(
        segment_id=segment_id,
        run_id=run_id,
        reviewed_branch=reviewed_branch,
        acceptance=[
            AcceptanceCriterion(id=item["id"], text=item["text"])
            for item in contract["acceptance"]
        ],
        diff=diff_result.stdout,
        execution_repo_path=str(repo_path),
        events_path=str(events_file),
    )
    advice = (review_runner or _run_codex_review_session)(review_input)
    normalized_advice = _validate_review_advice(advice, review_input.acceptance)

    report_path = _review_runtime_dir(events_file, run_id) / "review.md"
    report_path.write_text(
        _render_review_report(
            segment_id=segment_id,
            run_id=run_id,
            reviewed_branch=reviewed_branch,
            scope_paths=list(contract["scope_paths"]),
            changed_paths=changed_paths,
            out_of_scope_paths=out_of_scope_paths,
            advice=normalized_advice,
        ),
        encoding="utf-8",
    )
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor="harness",
            type="review_completed",
            payload={
                "report_path": str(report_path),
                "reviewed_branch": reviewed_branch,
            },
        ),
        path=events_file,
    )
    return report_path


def _run_codex_review_session(input_data: ReviewSessionInput) -> dict[str, object]:
    runtime_dir = _review_runtime_dir(Path(input_data.events_path), input_data.run_id)
    prompt_path = runtime_dir / "prompt.txt"
    schema_path = runtime_dir / "output-schema.json"
    output_path = runtime_dir / "codex-review.json"
    prompt_path.write_text(_build_review_prompt(input_data), encoding="utf-8")
    schema_path.write_text(
        json.dumps(_review_output_schema(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = run_observed(
        " ".join(
            [
                "codex exec",
                "--sandbox read-only",
                "--color never",
                f"--output-schema {quote(str(schema_path))}",
                f"--output-last-message {quote(str(output_path))}",
                f"- < {quote(str(prompt_path))}",
            ]
        ),
        segment_id=input_data.segment_id,
        run_id=input_data.run_id,
        cwd=input_data.execution_repo_path,
        path=input_data.events_path,
        payload={"role": "review"},
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or "codex review failed")
    if not output_path.exists():
        raise RuntimeError("codex review did not produce structured output")
    try:
        output = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError("codex review output is not valid JSON") from error
    if not isinstance(output, dict):
        raise RuntimeError("codex review output must be a JSON object")
    return output


def _load_observed_changed_paths(
    *,
    events_path: Path,
    segment_id: str,
    run_id: str,
) -> list[str]:
    if not events_path.exists():
        return []

    changed_paths: set[str] = set()
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
                or event.get("type") != "files_changed"
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            files = payload.get("files")
            if not isinstance(files, list):
                continue
            for file_change in files:
                if not isinstance(file_change, dict):
                    continue
                path = file_change.get("path")
                if isinstance(path, str) and path:
                    changed_paths.add(path)
    return sorted(changed_paths)


def _validate_review_advice(
    advice: dict[str, object],
    acceptance: list[AcceptanceCriterion],
) -> dict[str, object]:
    opinions = advice.get("opinions")
    summary = advice.get("summary")
    if not isinstance(opinions, list) or not isinstance(summary, str) or not summary.strip():
        raise ValueError("review advice must contain opinions and a non-empty summary")

    by_id: dict[str, dict[str, str]] = {}
    for item in opinions:
        if not isinstance(item, dict):
            raise ValueError("each review opinion must be an object")
        acceptance_id = item.get("acceptance_id")
        opinion = item.get("opinion")
        reason = item.get("reason")
        if (
            not isinstance(acceptance_id, str)
            or opinion not in {"满足", "存疑", "不满足"}
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError("each review opinion must contain AC id, opinion, and reason")
        if acceptance_id in by_id:
            raise ValueError(f"duplicate review opinion: {acceptance_id}")
        by_id[acceptance_id] = {
            "acceptance_id": acceptance_id,
            "opinion": opinion,
            "reason": reason,
        }

    expected_ids = [item.id for item in acceptance]
    if set(by_id) != set(expected_ids):
        raise ValueError("review advice must contain exactly one opinion for every acceptance")
    return {
        "opinions": [by_id[acceptance_id] for acceptance_id in expected_ids],
        "summary": summary.strip(),
    }


def _render_review_report(
    *,
    segment_id: str,
    run_id: str,
    reviewed_branch: str,
    scope_paths: list[str],
    changed_paths: list[str],
    out_of_scope_paths: list[str],
    advice: dict[str, object],
) -> str:
    scope_result = "越界" if out_of_scope_paths else "合规"
    lines = [
        "# Loom Review",
        "",
        f"- segment_id: `{segment_id}`",
        f"- run_id: `{run_id}`",
        f"- reviewed_branch: `{reviewed_branch}`",
        "",
        "## 硬事实(harness 观测)",
        "",
        "契约 scope_paths:",
        *_markdown_path_list(scope_paths),
        "",
        "实际改动文件:",
        *_markdown_path_list(changed_paths),
        "",
        f"scope 检查结果: {scope_result}",
    ]
    if out_of_scope_paths:
        lines.extend(["", "越界文件:", *_markdown_path_list(out_of_scope_paths)])

    lines.extend(["", "## LLM 建议(供人参考)", ""])
    for opinion in advice["opinions"]:
        lines.append(
            f"- `{opinion['acceptance_id']}` · LLM意见:{opinion['opinion']}"
            f"（理由:{opinion['reason']}）"
        )
    lines.extend(
        [
            "",
            "## 总体 review 摘要(LLM 建议)",
            "",
            str(advice["summary"]),
            "",
        ]
    )
    return "\n".join(lines)


def _markdown_path_list(paths: list[str]) -> list[str]:
    if not paths:
        return ["- (无)"]
    return [f"- `{path}`" for path in paths]


def _build_review_prompt(input_data: ReviewSessionInput) -> str:
    acceptance = "\n".join(
        f"- {item.id}: {item.text}" for item in input_data.acceptance
    )
    return "\n".join(
        [
            f"Review retained branch {input_data.reviewed_branch} for segment {input_data.segment_id}.",
            "This is advisory review only. Do not edit files, merge, delete branches, or run audit work.",
            "For every acceptance criterion, return one opinion: 满足, 存疑, or 不满足, with a reason grounded only in the diff.",
            "Return only JSON matching the provided schema.",
            "",
            "Acceptance criteria:",
            acceptance,
            "",
            "Branch diff relative to main:",
            input_data.diff or "(empty diff)",
        ]
    )


def _review_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "opinions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "acceptance_id": {"type": "string"},
                        "opinion": {
                            "type": "string",
                            "enum": ["满足", "存疑", "不满足"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["acceptance_id", "opinion", "reason"],
                    "additionalProperties": False,
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["opinions", "summary"],
        "additionalProperties": False,
    }


def _review_runtime_dir(events_path: Path, run_id: str) -> Path:
    runtime_dir = events_path.resolve().parent / "runs" / run_id / "review"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review one retained Loom branch.")
    parser.add_argument("--contract", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--events", default=str(DEFAULT_EVENTS_PATH))
    parser.add_argument("--repo", default=str(DEFAULT_EXECUTION_REPO_PATH))
    args = parser.parse_args(argv)
    report_path = run_segment_review(
        contract_path=args.contract,
        run_id=args.run_id,
        reviewed_branch=args.branch,
        events_path=args.events,
        execution_repo_path=args.repo,
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
