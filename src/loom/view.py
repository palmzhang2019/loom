from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from loom.events import DEFAULT_EVENTS_PATH
else:
    from .events import DEFAULT_EVENTS_PATH


@dataclass(frozen=True)
class InvalidLine:
    line_number: int
    raw_line: str
    error: str


def load_event_rows(
    path: Path | str,
    *,
    run_id: str | None = None,
    segment_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[InvalidLine]]:
    events_path = Path(path)
    rows: list[dict[str, Any]] = []
    invalid_lines: list[InvalidLine] = []

    with events_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue

            try:
                decoded = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                invalid_lines.append(
                    InvalidLine(
                        line_number=line_number,
                        raw_line=raw_line.rstrip("\n"),
                        error=str(exc),
                    )
                )
                continue

            if not isinstance(decoded, dict):
                invalid_lines.append(
                    InvalidLine(
                        line_number=line_number,
                        raw_line=raw_line.rstrip("\n"),
                        error="Top-level JSON value must be an object",
                    )
                )
                continue

            row = {
                "line_number": line_number,
                "ts": str(decoded.get("ts", "")),
                "segment_id": str(decoded.get("segment_id", "")),
                "run_id": str(decoded.get("run_id", "")),
                "actor": str(decoded.get("actor", "")),
                "type": str(decoded.get("type", "")),
                "payload": decoded.get("payload"),
                "payload_json": json.dumps(
                    decoded.get("payload"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            }

            if run_id is not None and row["run_id"] != run_id:
                continue
            if segment_id is not None and row["segment_id"] != segment_id:
                continue

            rows.append(row)

    return rows, invalid_lines


def render_html_document(
    rows: list[dict[str, Any]],
    invalid_lines: list[InvalidLine],
    *,
    source_path: Path | str,
    run_id: str | None = None,
    segment_id: str | None = None,
) -> str:
    actor_styles = {
        "harness": "background:#d6f5df;color:#0f5132;border-color:#93d3a2;",
        "orchestrator": "background:#e3f2fd;color:#0b5394;border-color:#9fc5e8;",
        "work": "background:#fff3cd;color:#7a4b00;border-color:#f1d27a;",
        "test": "background:#fce5cd;color:#8a3c12;border-color:#f4b183;",
        "review": "background:#eadcf8;color:#5b2c83;border-color:#c9b3e6;",
        "audit": "background:#f4cccc;color:#7a1f1f;border-color:#e6a8a8;",
    }
    type_styles = {
        "command_started": "background:#e0f2fe;color:#075985;border-color:#7dd3fc;",
        "command_run": "background:#f4f4f5;color:#111827;border-color:#d4d4d8;",
        "files_changed": "background:#dcfce7;color:#166534;border-color:#86efac;",
        "step_started": "background:#dbeafe;color:#1d4ed8;border-color:#93c5fd;",
        "test_run": "background:#fef3c7;color:#92400e;border-color:#fcd34d;",
        "gate_blocked": "background:#fee2e2;color:#991b1b;border-color:#fca5a5;",
    }

    filters = []
    if run_id:
        filters.append(f"run_id = {run_id}")
    if segment_id:
        filters.append(f"segment_id = {segment_id}")
    filters_label = ", ".join(filters) if filters else "none"

    body_rows = []
    for row in rows:
        actor_style = actor_styles.get(
            row["actor"],
            "background:#e5e7eb;color:#1f2937;border-color:#cbd5e1;",
        )
        type_style = type_styles.get(
            row["type"],
            "background:#ede9fe;color:#4c1d95;border-color:#c4b5fd;",
        )
        payload_json = row["payload_json"]
        payload_summary = _payload_summary(payload_json)
        body_rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"<td>{html.escape(row['ts'])}</td>",
                    f"<td>{html.escape(row['segment_id'])}</td>",
                    f"<td>{html.escape(row['run_id'])}</td>",
                    (
                        "<td>"
                        f"<span class=\"pill\" style=\"{actor_style}\">{html.escape(row['actor'])}</span>"
                        "</td>"
                    ),
                    (
                        "<td>"
                        f"<span class=\"pill\" style=\"{type_style}\">{html.escape(row['type'])}</span>"
                        "</td>"
                    ),
                    (
                        "<td>"
                        "<details>"
                        f"<summary>{html.escape(payload_summary)}</summary>"
                        f"<pre>{html.escape(payload_json)}</pre>"
                        "</details>"
                        "</td>"
                    ),
                    "</tr>",
                ]
            )
        )

    invalid_section = ""
    if invalid_lines:
        items = []
        for invalid in invalid_lines:
            items.append(
                "\n".join(
                    [
                        "<li>",
                        (
                            f"<strong>Invalid JSON at line {invalid.line_number}</strong>"
                            f" <code>{html.escape(invalid.error)}</code>"
                        ),
                        f"<pre>{html.escape(invalid.raw_line)}</pre>",
                        "</li>",
                    ]
                )
            )
        invalid_section = (
            "<section class=\"invalid\">"
            "<h2>Invalid Lines</h2>"
            "<ul>"
            + "".join(items)
            + "</ul>"
            "</section>"
        )

    empty_state = ""
    if not rows:
        empty_state = "<p class=\"empty\">No events matched the current filters.</p>"

    source = html.escape(str(source_path))
    rows_html = "\n".join(body_rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loom Events View</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4efe6;
      --panel: #fffdf8;
      --line: #d9cfbf;
      --text: #1f2933;
      --muted: #52606d;
      --accent: #155e75;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      background:
        radial-gradient(circle at top left, #fff7ed, transparent 25%),
        linear-gradient(180deg, #f7f1e7 0%, #efe6d7 100%);
      color: var(--text);
    }}
    main {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    .hero {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 24px;
      box-shadow: 0 14px 40px rgba(45, 55, 72, 0.08);
      margin-bottom: 20px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
      font-family: Georgia, "Times New Roman", serif;
      letter-spacing: 0.02em;
    }}
    .meta {{
      color: var(--muted);
      margin: 8px 0 0;
    }}
    .meta code {{
      background: #f4f1ea;
      padding: 1px 6px;
      border-radius: 999px;
    }}
    .table-wrap {{
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 14px 40px rgba(45, 55, 72, 0.08);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 960px;
    }}
    th, td {{
      padding: 14px 12px;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid #ece3d5;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #fcf7ef;
      font-size: 0.9rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    tr:hover td {{
      background: #fffaf0;
    }}
    .pill {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid;
      font-size: 0.84rem;
      font-weight: 700;
      white-space: nowrap;
    }}
    details {{
      min-width: 280px;
    }}
    summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 700;
    }}
    pre {{
      margin: 10px 0 0;
      padding: 12px;
      border-radius: 12px;
      background: #1f2933;
      color: #f8fafc;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.84rem;
      line-height: 1.45;
    }}
    .invalid {{
      margin-top: 20px;
      background: #fff1f2;
      border: 1px solid #fecdd3;
      border-radius: 18px;
      padding: 20px 24px;
      box-shadow: 0 14px 40px rgba(45, 55, 72, 0.08);
    }}
    .invalid ul {{
      margin: 0;
      padding-left: 18px;
    }}
    .empty {{
      margin: 20px 0 0;
      color: var(--muted);
      font-style: italic;
    }}
    @media (max-width: 720px) {{
      main {{ padding: 20px 12px 32px; }}
      .hero, .table-wrap, .invalid {{ border-radius: 14px; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>Loom Event Log</h1>
      <p class="meta">Source: <code>{source}</code></p>
      <p class="meta">Filters: <code>{html.escape(filters_label)}</code></p>
      <p class="meta">Visible events: <code>{len(rows)}</code> · Invalid lines: <code>{len(invalid_lines)}</code></p>
    </section>
    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ts</th>
            <th>segment_id</th>
            <th>run_id</th>
            <th>actor</th>
            <th>type</th>
            <th>payload</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </section>
    {empty_state}
    {invalid_section}
  </main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Loom events.jsonl into a static HTML view.")
    parser.add_argument(
        "--events",
        default=str(DEFAULT_EVENTS_PATH),
        help="Path to the events.jsonl file to read.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to the generated HTML file.",
    )
    parser.add_argument("--run-id", help="Only include events for this run_id.")
    parser.add_argument("--segment-id", help="Only include events for this segment_id.")
    args = parser.parse_args(argv)

    rows, invalid_lines = load_event_rows(
        args.events,
        run_id=args.run_id,
        segment_id=args.segment_id,
    )
    document = render_html_document(
        rows,
        invalid_lines,
        source_path=args.events,
        run_id=args.run_id,
        segment_id=args.segment_id,
    )

    output_path = Path(args.output)
    output_path.write_text(document, encoding="utf-8")
    return 0


def _payload_summary(payload_json: str) -> str:
    single_line = " ".join(payload_json.split())
    if len(single_line) <= 96:
        return single_line
    return single_line[:93] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
