from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.graph import run_segment_graph
from loom.view import load_event_rows, render_html_document


class P3ALangGraphTests(unittest.TestCase):
    def test_run_segment_graph_emits_harness_written_node_events_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            contract_path = (
                Path(__file__).resolve().parents[1]
                / "specs"
                / "MAT-REQ-001"
                / "segments"
                / "S1.yaml"
            )

            state = run_segment_graph(
                contract_path=contract_path,
                run_id="run-p3a-001",
                events_path=events_path,
            )

            rows, invalid_lines = load_event_rows(
                events_path,
                run_id="run-p3a-001",
                segment_id="MAT-REQ-001/S1",
            )

            self.assertEqual(invalid_lines, [])
            self.assertEqual(
                [(row["actor"], row["type"]) for row in rows],
                [
                    ("orchestrator", "step_started"),
                    ("orchestrator", "step_finished"),
                    ("work", "step_started"),
                    ("work", "step_finished"),
                    ("test", "step_started"),
                    ("test", "step_finished"),
                ],
            )
            self.assertEqual(state["segment"]["segment_id"], "MAT-REQ-001/S1")
            self.assertEqual(state["step"]["segment_id"], "MAT-REQ-001/S1")
            self.assertIn("fake diff", state["work_result"]["diff"])
            self.assertTrue(state["test_result"]["passed"])
            self.assertNotIn("raw_text", rows[1]["payload"]["result"]["segment"])
            self.assertEqual(
                rows[1]["payload"]["result"]["segment"],
                {
                    "contract_path": str(contract_path),
                    "acceptance_ids": [
                        "MAT-REQ-001/S1/AC1",
                        "MAT-REQ-001/S1/AC2",
                        "MAT-REQ-001/S1/AC3",
                        "MAT-REQ-001/S1/AC4",
                    ],
                    "segment_id": "MAT-REQ-001/S1",
                    "covers_req": "MAT-REQ-001",
                },
            )

    def test_existing_view_renders_the_three_node_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            contract_path = (
                Path(__file__).resolve().parents[1]
                / "specs"
                / "MAT-REQ-001"
                / "segments"
                / "S1.yaml"
            )

            run_segment_graph(
                contract_path=contract_path,
                run_id="run-p3a-view",
                events_path=events_path,
            )

            rows, invalid_lines = load_event_rows(
                events_path,
                run_id="run-p3a-view",
                segment_id="MAT-REQ-001/S1",
            )
            html = render_html_document(
                rows,
                invalid_lines,
                source_path=events_path,
                run_id="run-p3a-view",
                segment_id="MAT-REQ-001/S1",
            )

            self.assertIn("orchestrator", html)
            self.assertIn("work", html)
            self.assertIn("test", html)
            self.assertIn("step_started", html)
            self.assertIn("step_finished", html)
            self.assertIn("MAT-REQ-001/S1", html)


if __name__ == "__main__":
    unittest.main()
