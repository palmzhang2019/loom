from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.events import DEFAULT_EVENTS_PATH, Event, append_event


class RecordingHandle:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, chunk: str) -> int:
        self.writes.append(chunk)
        return len(chunk)

    def __enter__(self) -> "RecordingHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class EventTests(unittest.TestCase):
    def test_default_events_path_points_to_repo_root(self) -> None:
        expected = Path(__file__).resolve().parents[1] / "events.jsonl"
        self.assertEqual(DEFAULT_EVENTS_PATH, expected)

    def test_append_event_writes_one_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            event = Event(
                ts="2026-06-20T10:00:00Z",
                segment_id="MAT-REQ-001/S1",
                run_id="run-001",
                actor="work",
                type="step_started",
                payload={"step": "draft schema"},
            )

            append_event(event, path=events_path)

            lines = events_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(
                json.loads(lines[0]),
                {
                    "ts": "2026-06-20T10:00:00Z",
                    "segment_id": "MAT-REQ-001/S1",
                    "run_id": "run-001",
                    "actor": "work",
                    "type": "step_started",
                    "payload": {"step": "draft schema"},
                },
            )

    def test_append_event_appends_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"

            append_event(
                Event(
                    ts="2026-06-20T10:00:00Z",
                    segment_id="MAT-REQ-001/S1",
                    run_id="run-001",
                    actor="work",
                    type="step_started",
                    payload={"step": "draft schema"},
                ),
                path=events_path,
            )
            append_event(
                Event(
                    ts="2026-06-20T10:01:00Z",
                    segment_id="MAT-REQ-001/S1",
                    run_id="run-001",
                    actor="harness",
                    type="file_changed",
                    payload={"path": "src/loom/events.py"},
                ),
                path=events_path,
            )

            lines = events_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["type"], "step_started")
            self.assertEqual(json.loads(lines[1])["type"], "file_changed")

    def test_append_event_writes_complete_jsonl_line_in_one_write(self) -> None:
        handle = RecordingHandle()
        event = Event(
            ts="2026-06-20T10:00:00Z",
            segment_id="MAT-REQ-001/S1",
            run_id="run-001",
            actor="work",
            type="step_started",
            payload={"step": "draft schema"},
        )

        with patch("pathlib.Path.open", return_value=handle):
            append_event(event, path="events.jsonl")

        self.assertEqual(
            handle.writes,
            [
                "{\"ts\": \"2026-06-20T10:00:00Z\", \"segment_id\": \"MAT-REQ-001/S1\", \"run_id\": \"run-001\", \"actor\": \"work\", \"type\": \"step_started\", \"payload\": {\"step\": \"draft schema\"}}\n"
            ],
        )

    def test_append_event_keeps_special_characters_jsonl_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            event = Event(
                ts="2026-06-20T10:00:00Z",
                segment_id="MAT-REQ-001/S1",
                run_id="run-001",
                actor="work",
                type="step_started",
                payload={
                    "message": "line 1\nline 2",
                    "quote": "\"quoted\"",
                    "unicode": "東京",
                },
            )

            append_event(event, path=events_path)

            lines = events_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), event.to_dict())

    def test_event_rejects_invalid_segment_id(self) -> None:
        with self.assertRaises(ValueError):
            Event(
                ts="2026-06-20T10:00:00Z",
                segment_id="MAT-REQ-001",
                run_id="run-001",
                actor="work",
                type="step_started",
                payload={"step": "draft schema"},
            )

    def test_event_rejects_unknown_actor(self) -> None:
        with self.assertRaises(ValueError):
            Event(
                ts="2026-06-20T10:00:00Z",
                segment_id="MAT-REQ-001/S1",
                run_id="run-001",
                actor="planner",
                type="step_started",
                payload={"step": "draft schema"},
            )


if __name__ == "__main__":
    unittest.main()
