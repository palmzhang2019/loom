from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Actor = Literal["orchestrator", "work", "test", "review", "audit", "harness"]

_ALLOWED_ACTORS = {"orchestrator", "work", "test", "review", "audit", "harness"}
_SEGMENT_ID_PATTERN = re.compile(r"^[^/]+/S[1-9][0-9]*$")
DEFAULT_EVENTS_PATH = Path(__file__).resolve().parents[2] / "events.jsonl"


@dataclass(frozen=True)
class Event:
    ts: str
    segment_id: str
    run_id: str
    actor: Actor
    type: str
    payload: object

    def __post_init__(self) -> None:
        _require_non_empty("ts", self.ts)
        _require_non_empty("segment_id", self.segment_id)
        _require_non_empty("run_id", self.run_id)
        _require_non_empty("type", self.type)

        if self.actor not in _ALLOWED_ACTORS:
            raise ValueError(f"actor must be one of {_sorted_actors()}")
        if not _SEGMENT_ID_PATTERN.match(self.segment_id):
            raise ValueError("segment_id must match <REQ-ID>/S<n>")

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "segment_id": self.segment_id,
            "run_id": self.run_id,
            "actor": self.actor,
            "type": self.type,
            "payload": self.payload,
        }


def append_event(event: Event, path: Path | str = DEFAULT_EVENTS_PATH) -> None:
    events_path = Path(path)
    line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"

    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _require_non_empty(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _sorted_actors() -> list[str]:
    return sorted(_ALLOWED_ACTORS)
