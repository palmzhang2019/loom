from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.view import load_event_rows, main, render_html_document


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines), encoding="utf-8")


class ViewTests(unittest.TestCase):
    def test_load_and_render_events_into_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            _write_lines(
                events_path,
                [
                    json.dumps(
                        {
                            "ts": "2026-07-05T00:00:00Z",
                            "segment_id": "MAT-REQ-001/S1",
                            "run_id": "run-001",
                            "actor": "harness",
                            "type": "command_run",
                            "payload": {"cmd": "echo hi", "exit_code": 0},
                        }
                    )
                    + "\n",
                    json.dumps(
                        {
                            "ts": "2026-07-05T00:00:01Z",
                            "segment_id": "MAT-REQ-002/S2",
                            "run_id": "run-002",
                            "actor": "work",
                            "type": "step_started",
                            "payload": {"note": "doing work"},
                        }
                    )
                    + "\n",
                ],
            )

            rows, invalid_lines = load_event_rows(
                events_path,
                run_id="run-001",
                segment_id="MAT-REQ-001/S1",
            )
            html = render_html_document(
                rows,
                invalid_lines,
                source_path=events_path,
                run_id="run-001",
                segment_id="MAT-REQ-001/S1",
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(len(invalid_lines), 0)
            self.assertEqual(rows[0]["run_id"], "run-001")
            self.assertIn("command_run", html)
            self.assertIn("MAT-REQ-001/S1", html)
            self.assertIn("echo hi", html)
            self.assertNotIn("run-002", html)

    def test_invalid_json_line_does_not_crash_and_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            _write_lines(
                events_path,
                [
                    '{"ts":"2026-07-05T00:00:00Z","segment_id":"MAT-REQ-001/S1","run_id":"run-001","actor":"harness","type":"files_changed","payload":{"files":[]}}\n',
                    '{"ts": "broken"\n',
                ],
            )

            rows, invalid_lines = load_event_rows(events_path)
            html = render_html_document(rows, invalid_lines, source_path=events_path)

            self.assertEqual(len(rows), 1)
            self.assertEqual(len(invalid_lines), 1)
            self.assertIn("Invalid JSON", html)
            self.assertIn("line 2", html)

    def test_main_writes_html_without_modifying_input_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            output_path = Path(tmpdir) / "events-view.html"
            original = (
                '{"ts":"2026-07-05T00:00:00Z","segment_id":"MAT-REQ-001/S1","run_id":"run-001","actor":"harness","type":"command_run","payload":{"cmd":"echo hi"}}\n'
            )
            events_path.write_text(original, encoding="utf-8")

            exit_code = main(
                [
                    "--events",
                    str(events_path),
                    "--output",
                    str(output_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(events_path.read_text(encoding="utf-8"), original)
            self.assertTrue(output_path.exists())
            html = output_path.read_text(encoding="utf-8")
            self.assertIn("command_run", html)
            self.assertIn("run-001", html)

    def test_script_entrypoint_generates_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            output_path = Path(tmpdir) / "events-view.html"
            events_path.write_text(
                '{"ts":"2026-07-05T00:00:00Z","segment_id":"MAT-REQ-001/S1","run_id":"run-001","actor":"harness","type":"files_changed","payload":{"files":[]}}\n',
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "src" / "loom" / "view.py"),
                    "--events",
                    str(events_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
