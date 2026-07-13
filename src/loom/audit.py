from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shlex import quote

from .events import DEFAULT_EVENTS_PATH, Event, append_event
from .graph import _is_in_scope, _load_segment_contract
from .harness import load_observed_file_changes, run_observed
from .sandbox import DEFAULT_EXECUTION_REPO_PATH


_OPENAI_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
_AWS_ACCESS_KEY_PATTERN = re.compile(
    r"(?<![0-9A-Z])AKIA[0-9A-Z]{16}(?![0-9A-Z])"
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"""
    (?<![A-Za-z0-9_])(?P<name>api[_-]?key|password|token)(?![A-Za-z0-9_])
    \s*=\s*
    (?:
        (?P<quote>["'])(?P<quoted_value>[^"'\r\n]{8,})(?P=quote)
        |
        (?P<unquoted_value>[A-Za-z0-9_+/=-]{12,})
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_HUNK_HEADER_PATTERN = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@"
)
_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "dummy",
    "example",
    "redacted",
    "replace-me",
    "replace_me",
    "secret-here",
    "secret_here",
    "your-",
    "your_",
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line_number: int
    category: str


def run_segment_audit(
    *,
    contract_path: Path | str,
    run_id: str,
    audited_branch: str,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    execution_repo_path: Path | str = DEFAULT_EXECUTION_REPO_PATH,
) -> Path:
    if not audited_branch.startswith("loom/"):
        raise ValueError("audit only supports retained loom/ branches")

    contract = _load_segment_contract(Path(contract_path))
    segment_id = str(contract["segment_id"])
    scope_paths = list(contract["scope_paths"])
    events_file = Path(events_path)
    repo_path = Path(execution_repo_path)

    observed_changes = load_observed_file_changes(
        events_path=events_file,
        segment_id=segment_id,
        run_id=run_id,
    )
    changed_paths = observed_changes.paths
    out_of_scope_paths = [
        path for path in changed_paths if not _is_in_scope(path, scope_paths)
    ]

    diff_result = run_observed(
        (
            "git -c core.quotePath=false diff --no-ext-diff --unified=0 "
            f"main...{quote(audited_branch)} --"
        ),
        segment_id=segment_id,
        run_id=run_id,
        cwd=repo_path,
        path=events_file,
        payload={
            "role": "audit",
            "artifact": "branch_diff",
            "stdout": "[omitted: audit diff may contain secrets]",
            "stderr": "[omitted: audit diff output is not persisted]",
        },
    )
    if diff_result.exit_code != 0:
        raise RuntimeError(diff_result.stderr.strip() or "git diff failed")

    secret_findings = _scan_added_diff_lines(diff_result.stdout)
    scope_verdict = (
        "blocked"
        if not observed_changes.has_observation or out_of_scope_paths
        else "passed"
    )
    secret_verdict = "blocked" if secret_findings else "passed"
    overall_verdict = (
        "blocked"
        if scope_verdict == "blocked" or secret_verdict == "blocked"
        else "passed"
    )

    report_path = _audit_runtime_dir(events_file, run_id) / "audit.md"
    report_path.write_text(
        _render_audit_report(
            segment_id=segment_id,
            run_id=run_id,
            audited_branch=audited_branch,
            scope_paths=scope_paths,
            changed_paths=changed_paths,
            has_scope_observation=observed_changes.has_observation,
            out_of_scope_paths=out_of_scope_paths,
            secret_findings=secret_findings,
            overall_verdict=overall_verdict,
        ),
        encoding="utf-8",
    )
    append_event(
        Event(
            ts=_utc_now(),
            segment_id=segment_id,
            run_id=run_id,
            actor="harness",
            type="audit_completed",
            payload={
                "report_path": str(report_path),
                "audited_branch": audited_branch,
                "verdict": overall_verdict,
            },
        ),
        path=events_file,
    )
    return report_path


def _scan_added_diff_lines(diff: str) -> list[SecretFinding]:
    findings: set[SecretFinding] = set()
    current_path: str | None = None
    new_line_number: int | None = None

    for line in diff.splitlines():
        if line.startswith("+++ "):
            current_path = _new_side_path(line[4:])
            continue

        hunk_match = _HUNK_HEADER_PATTERN.match(line)
        if hunk_match is not None:
            new_line_number = int(hunk_match.group("new_start"))
            continue

        if current_path is None or new_line_number is None:
            continue
        if line.startswith("+"):
            for category in _secret_categories(line[1:]):
                findings.add(
                    SecretFinding(
                        path=current_path,
                        line_number=new_line_number,
                        category=category,
                    )
                )
            new_line_number += 1
        elif line.startswith(" "):
            new_line_number += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue

    return sorted(
        findings,
        key=lambda finding: (finding.path, finding.line_number, finding.category),
    )


def _new_side_path(raw_path: str) -> str | None:
    path = raw_path.strip()
    if path == "/dev/null":
        return None
    if path.startswith("b/"):
        return path[2:]
    return path


def _secret_categories(line: str) -> list[str]:
    categories: set[str] = set()
    if _OPENAI_KEY_PATTERN.search(line):
        categories.add("openai_api_key")
    if _AWS_ACCESS_KEY_PATTERN.search(line):
        categories.add("aws_access_key_id")

    for match in _CREDENTIAL_ASSIGNMENT_PATTERN.finditer(line):
        value = match.group("quoted_value") or match.group("unquoted_value") or ""
        if not _is_real_credential_value(value, quoted=match.group("quoted_value") is not None):
            continue
        normalized_name = match.group("name").lower().replace("-", "_")
        categories.add(f"{normalized_name}_assignment")
    return sorted(categories)


def _is_real_credential_value(value: str, *, quoted: bool) -> bool:
    normalized = value.strip().lower()
    if len(normalized) < 8:
        return False
    if (
        normalized in {"none", "null", "password", "secret", "token"}
        or normalized.startswith("${")
        or (normalized.startswith("<") and normalized.endswith(">"))
        or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)
        or set(normalized) <= {"*", "-", "_", "x"}
    ):
        return False
    if not quoted and not any(character.isdigit() or character in "+/=-" for character in normalized):
        return False
    return True


def _render_audit_report(
    *,
    segment_id: str,
    run_id: str,
    audited_branch: str,
    scope_paths: list[str],
    changed_paths: list[str],
    has_scope_observation: bool,
    out_of_scope_paths: list[str],
    secret_findings: list[SecretFinding],
    overall_verdict: str,
) -> str:
    scope_verdict = (
        "blocked" if not has_scope_observation or out_of_scope_paths else "passed"
    )
    secret_verdict = "blocked" if secret_findings else "passed"
    scope_reason = (
        "no_observed_changes"
        if not has_scope_observation
        else "out_of_scope"
        if out_of_scope_paths
        else "all_observed_changes_within_scope"
    )
    secret_reason = (
        "secret_detected"
        if secret_findings
        else "no_high_confidence_secret_detected"
    )
    lines = [
        "# Loom Audit",
        "",
        f"- segment_id: `{segment_id}`",
        f"- run_id: `{run_id}`",
        f"- audited_branch: `{audited_branch}`",
        "",
        "## Scope gate",
        "",
        f"- scope gate: `{scope_verdict}`",
        f"- reason: `{scope_reason}`",
        "- contract scope_paths:",
        *_indented_markdown_path_list(scope_paths),
        "- harness-observed changed files:",
        *_indented_markdown_path_list(changed_paths),
    ]
    if out_of_scope_paths:
        lines.extend(
            [
                "- out-of-scope files:",
                *_indented_markdown_path_list(out_of_scope_paths),
            ]
        )

    lines.extend(
        [
            "",
            "## Secret scan gate",
            "",
            f"- secret scan gate: `{secret_verdict}`",
            f"- reason: `{secret_reason}`",
        ]
    )
    if secret_findings:
        lines.append("- detected locations and pattern categories (values omitted):")
        lines.extend(
            f"  - `{finding.path}:{finding.line_number}` — `{finding.category}`"
            for finding in secret_findings
        )

    lines.extend(
        [
            "",
            "## Overall verdict",
            "",
            f"- overall verdict: `{overall_verdict}`",
            "",
        ]
    )
    return "\n".join(lines)


def _indented_markdown_path_list(paths: list[str]) -> list[str]:
    if not paths:
        return ["  - (none)"]
    return [f"  - `{path}`" for path in paths]


def _audit_runtime_dir(events_path: Path, run_id: str) -> Path:
    runtime_dir = events_path.resolve().parent / "runs" / run_id / "audit"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit one retained Loom branch.")
    parser.add_argument("--contract", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--events", default=str(DEFAULT_EVENTS_PATH))
    parser.add_argument("--repo", default=str(DEFAULT_EXECUTION_REPO_PATH))
    args = parser.parse_args(argv)
    report_path = run_segment_audit(
        contract_path=args.contract,
        run_id=args.run_id,
        audited_branch=args.branch,
        events_path=args.events,
        execution_repo_path=args.repo,
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
