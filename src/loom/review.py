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
from .harness import load_observed_changed_paths, run_observed
from .sandbox import DEFAULT_EXECUTION_REPO_PATH


@dataclass(frozen=True)
class ReverseSequenceInput:
    segment_id: str
    run_id: str
    reviewed_branch: str
    diff: str
    execution_repo_path: str
    events_path: str


@dataclass(frozen=True)
class ReviewSessionInput:
    segment_id: str
    run_id: str
    reviewed_branch: str
    acceptance: list[AcceptanceCriterion]
    diff: str
    contract_sequence_diagram: str
    reverse_sequence_diagram: str
    execution_repo_path: str
    events_path: str


ReverseSequenceRunner = Callable[[ReverseSequenceInput], str]
ReviewRunner = Callable[[ReviewSessionInput], dict[str, object]]
_CODEX_OUTPUT_SENTINEL = "loom: current codex session has not written output\n"


def run_segment_review(
    *,
    contract_path: Path | str,
    run_id: str,
    reviewed_branch: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
    reverse_sequence_runner: ReverseSequenceRunner | None = None,
    review_runner: ReviewRunner | None = None,
) -> Path:
    if not reviewed_branch.startswith("loom/"):
        raise ValueError("review only supports retained loom/ branches")

    contract = _load_segment_contract(Path(contract_path))
    segment_id = str(contract["segment_id"])
    events_file = Path(events_path)
    repo_path = Path(execution_repo_path)
    changed_paths = load_observed_changed_paths(
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

    reverse_input = ReverseSequenceInput(
        segment_id=segment_id,
        run_id=run_id,
        reviewed_branch=reviewed_branch,
        diff=diff_result.stdout,
        execution_repo_path=str(repo_path),
        events_path=str(events_file),
    )
    reverse_sequence_diagram = _validate_sequence_diagram(
        (reverse_sequence_runner or _run_codex_reverse_sequence_session)(
            reverse_input
        )
    )
    contract_sequence_diagram = str(contract["sequence_diagram"]).strip()
    review_input = ReviewSessionInput(
        segment_id=segment_id,
        run_id=run_id,
        reviewed_branch=reviewed_branch,
        acceptance=[
            AcceptanceCriterion(id=item["id"], text=item["text"])
            for item in contract["acceptance"]
        ],
        diff=diff_result.stdout,
        contract_sequence_diagram=contract_sequence_diagram,
        reverse_sequence_diagram=reverse_sequence_diagram,
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
            contract_sequence_diagram=contract_sequence_diagram,
            reverse_sequence_diagram=reverse_sequence_diagram,
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


def _run_codex_reverse_sequence_session(input_data: ReverseSequenceInput) -> str:
    runtime_dir = _review_runtime_dir(Path(input_data.events_path), input_data.run_id)
    prompt_path = runtime_dir / "reverse-sequence-prompt.txt"
    schema_path = runtime_dir / "reverse-sequence-output-schema.json"
    output_path = runtime_dir / "codex-reverse-sequence.json"
    prompt_path.write_text(
        _build_reverse_sequence_prompt(input_data),
        encoding="utf-8",
    )
    schema_path.write_text(
        json.dumps(_reverse_sequence_output_schema(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output_path.write_text(_CODEX_OUTPUT_SENTINEL, encoding="utf-8")
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
        payload={"role": "review", "artifact": "reverse_sequence_diagram"},
    )
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or "codex reverse sequence failed")
    if not output_path.exists():
        raise RuntimeError("codex reverse sequence did not produce structured output")
    output_text = output_path.read_text(encoding="utf-8")
    if output_text == _CODEX_OUTPUT_SENTINEL:
        raise RuntimeError("codex reverse sequence did not produce structured output")
    try:
        output = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("codex reverse sequence output is not valid JSON") from error
    if not isinstance(output, dict):
        raise RuntimeError("codex reverse sequence output must be a JSON object")
    return _validate_sequence_diagram(output.get("sequence_diagram"))


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
    output_path.write_text(_CODEX_OUTPUT_SENTINEL, encoding="utf-8")
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
    output_text = output_path.read_text(encoding="utf-8")
    if output_text == _CODEX_OUTPUT_SENTINEL:
        raise RuntimeError("codex review did not produce structured output")
    try:
        output = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("codex review output is not valid JSON") from error
    if not isinstance(output, dict):
        raise RuntimeError("codex review output must be a JSON object")
    return output


def _validate_sequence_diagram(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("reverse sequence diagram must be a string")
    sequence_diagram = value.strip()
    if not sequence_diagram or sequence_diagram.splitlines()[0].strip() != "sequenceDiagram":
        raise ValueError("reverse sequence diagram must start with sequenceDiagram")
    body_lines = [line.strip() for line in sequence_diagram.splitlines()[1:]]
    has_participant = any(line.startswith("participant ") for line in body_lines)
    message_arrows = ("->>", "-->>", "->", "-->", "-x", "--x", "-)", "--)")
    has_message_arrow = any(
        ":" in line and any(arrow in line.split(":", 1)[0] for arrow in message_arrows)
        for line in body_lines
    )
    if not has_participant or not has_message_arrow:
        raise ValueError(
            "reverse sequence diagram must contain participant declarations and a message arrow"
        )
    return sequence_diagram


def _validate_review_advice(
    advice: dict[str, object],
    acceptance: list[AcceptanceCriterion],
) -> dict[str, object]:
    opinions = advice.get("opinions")
    reverse_only_interactions = advice.get("reverse_only_interactions")
    contract_only_interactions = advice.get("contract_only_interactions")
    summary = advice.get("summary")
    if (
        not isinstance(opinions, list)
        or not isinstance(summary, str)
        or not summary.strip()
    ):
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
        "reverse_only_interactions": _validate_interaction_observations(
            reverse_only_interactions,
            field_name="reverse_only_interactions",
        ),
        "contract_only_interactions": _validate_interaction_observations(
            contract_only_interactions,
            field_name="contract_only_interactions",
        ),
        "summary": summary.strip(),
    }


def _validate_interaction_observations(
    value: object,
    *,
    field_name: str,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"review advice must contain {field_name}")
    interactions: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"each {field_name} item must be a non-empty string")
        interactions.append(item.strip())
    return interactions


def _render_review_report(
    *,
    segment_id: str,
    run_id: str,
    reviewed_branch: str,
    scope_paths: list[str],
    changed_paths: list[str],
    out_of_scope_paths: list[str],
    contract_sequence_diagram: str,
    reverse_sequence_diagram: str,
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

    lines.extend(
        [
            "",
            "## LLM 建议(供人参考)",
            "",
            "### 契约时序图(设计意图,人类所写)",
            "",
            "```mermaid",
            contract_sequence_diagram,
            "```",
            "",
            "### 反向生成时序图(LLM 从实现代码生成,供参考)",
            "",
            "```mermaid",
            reverse_sequence_diagram,
            "```",
            "",
            "### 图差异观察(LLM 软建议,供人参考)",
            "",
            "以下是 LLM 对两图的语义比较观察,不是漂移判定;是否构成漂移由人类判断。",
            "",
            "反向生成图中有、契约图中没有的交互:",
            *_markdown_observation_list(advice["reverse_only_interactions"]),
            "",
            "契约图中有、反向生成图中没有的交互:",
            *_markdown_observation_list(advice["contract_only_interactions"]),
            "",
            "### 逐条 AC review 意见(LLM 建议)",
            "",
        ]
    )
    for opinion in advice["opinions"]:
        lines.append(
            f"- `{opinion['acceptance_id']}` · LLM意见:{opinion['opinion']}"
            f"（理由:{opinion['reason']}）"
        )
    lines.extend(
        [
            "",
            "### 总体 review 摘要(LLM 建议)",
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


def _markdown_observation_list(interactions: object) -> list[str]:
    if not isinstance(interactions, list) or not interactions:
        return ["- (未观察到)"]
    return [f"- {interaction}" for interaction in interactions]


def _build_reverse_sequence_prompt(input_data: ReverseSequenceInput) -> str:
    return "\n".join(
        [
            "Generate an as-built Mermaid sequence diagram from the supplied implementation diff.",
            "This is advisory review information. Do not edit files, merge, delete branches, or run audit work.",
            "Use only the supplied diff. Do not inspect repository files or infer intended behavior from specifications or contracts.",
            "Return only JSON matching the provided schema.",
            "The sequence_diagram value must:",
            "- be a legal Mermaid sequenceDiagram beginning with the exact line sequenceDiagram;",
            "- declare participants and describe the implementation's actual call sequence;",
            "- 所有人类可见的 participant 名称和消息文字必须使用中文;使用 as 时 as 后的显示名必须为中文,内部标识可保留英文;",
            "- include branches and failure paths visible in the diff, using alt/else when applicable;",
            "- avoid judging whether the implementation matches any intended design.",
            "",
            "Branch diff relative to main:",
            input_data.diff or "(empty diff)",
        ]
    )


def _reverse_sequence_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "sequence_diagram": {"type": "string"},
        },
        "required": ["sequence_diagram"],
        "additionalProperties": False,
    }


def _build_review_prompt(input_data: ReviewSessionInput) -> str:
    acceptance = "\n".join(
        f"- {item.id}: {item.text}" for item in input_data.acceptance
    )
    return "\n".join(
        [
            f"Review retained branch {input_data.reviewed_branch} for segment {input_data.segment_id}.",
            "This is advisory review only. Do not edit files, merge, delete branches, or run audit work.",
            "For every acceptance criterion, return one opinion: 满足, 存疑, or 不满足, with a reason grounded only in the diff.",
            "The reverse sequence diagram was generated and fixed in a separate session before that session saw the contract diagram. Do not rewrite either diagram.",
            "Compare the two diagrams semantically. Ignore participant aliases or wording differences when they describe the same interaction.",
            "List interactions present only in the reverse diagram under reverse_only_interactions, and interactions present only in the contract diagram under contract_only_interactions.",
            "差异观察、逐条 AC 意见的理由和总体摘要必须使用中文;这包括 reverse_only_interactions、contract_only_interactions、reason 与 summary,opinion 使用 schema 给出的中文枚举。",
            "The comparison is an advisory observation only. Do not decide whether drift exists and do not return a drift verdict.",
            "Return only JSON matching the provided schema.",
            "",
            "Acceptance criteria:",
            acceptance,
            "",
            "Contract sequence diagram (human-authored design intent):",
            input_data.contract_sequence_diagram,
            "",
            "Reverse-generated sequence diagram (LLM-generated from implementation):",
            input_data.reverse_sequence_diagram,
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
            "reverse_only_interactions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "contract_only_interactions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "summary": {"type": "string"},
        },
        "required": [
            "opinions",
            "reverse_only_interactions",
            "contract_only_interactions",
            "summary",
        ],
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
