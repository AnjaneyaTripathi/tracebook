"""Adapter conformance checks third parties can run with pytest or plain Python."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter
from tracebook.model import KINDS, TOKEN_FIELDS, TOOLS, Event, Source
from tracebook.privacy import Redactor


def assert_invariants(events: Iterable[Event], complete: bool = True) -> list[Event]:
    redactor = Redactor()
    unique: dict[str, Event] = {}
    calls: set[tuple[str, str, str]] = set()
    results: set[tuple[str, str, str]] = set()
    for event in events:
        assert event.kind in KINDS, event.kind
        assert event.agent and event.session_id and event.native_id
        assert event.ts.endswith("Z"), "Timestamps must be UTC"
        assert event.id == event.id, "Event IDs must be deterministic"
        event.data = redactor.clean(event.data)
        assert redactor.clean(event.data) == event.data, "Redaction must be idempotent"
        for field in TOKEN_FIELDS if event.kind == "model_call" else ():
            amount = event.data.get(field)
            assert amount is None or (isinstance(amount, int) and amount >= 0), field
        if "tool" in event.data:
            assert event.data["tool"] in TOOLS
        identity = (event.agent, event.session_id, event.native_id)
        if event.kind == "tool_call":
            calls.add(identity)
        if event.kind == "tool_result":
            results.add(identity)
        if event.id in unique:
            prior = unique[event.id]
            assert (prior.kind, prior.native_id, prior.session_id) == (
                event.kind,
                event.native_id,
                event.session_id,
            ), "ID collision"
        unique[event.id] = event
    if complete:
        assert results <= calls, f"Orphan results: {results - calls}"
    ordered = sorted(unique.values(), key=lambda e: (e.session_id, e.ts, e.id))
    for first, second in zip(ordered, ordered[1:], strict=False):
        if first.session_id == second.session_id:
            assert first.ts <= second.ts
    return ordered


def conform(adapter: Adapter, fixtures: list[Path], complete: bool = True) -> list[Event]:
    first = [
        e
        for path in fixtures
        for e in adapter.parse_transcript(Source(str(path)), path.read_bytes())
    ]
    second = [
        e
        for path in fixtures
        for e in adapter.parse_transcript(Source(str(path)), path.read_bytes())
    ]
    assert [e.id for e in first] == [e.id for e in second], "Replay must preserve IDs"
    return assert_invariants(first, complete)
