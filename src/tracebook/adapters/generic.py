from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter
from tracebook.model import KINDS, Capabilities, Event, Json, Source, digest, obj
from tracebook.platform import home


def field(record: Json, path: str) -> object:
    value: object = record
    for key in path.split("."):
        value = obj(value).get(key)
    return value


class GenericAdapter(Adapter):
    id = "generic"
    display_name = "Generic / process wrapper"

    def root(self) -> Path:
        return home()

    def capabilities(self) -> Capabilities:
        return Capabilities(transcripts="partial", otel_logs="partial", otel_metrics="partial", token_input="partial", token_output="partial", prompt_text="partial", tool_io_content="partial")

    def transcript_sources(self) -> list[Source]:
        return []

    def parse_record(self, record: Json, state: Json) -> Iterable[Event]:
        mapping = obj(state.get("mapping"))
        mapped = {k: field(record, str(v)) for k, v in obj(mapping.get("fields")).items()} if mapping else record
        state["session_id"] = str(mapped.get("session_id") or state.get("session_id", "generic"))
        state["turn_id"] = mapped.get("turn_id")
        kind = str(mapped.get("kind", "warning"))
        kind = str(obj(mapping.get("kinds")).get(kind, kind))
        yield self.event({"timestamp": mapped.get("ts", mapped.get("timestamp"))}, state, kind if kind in KINDS else "warning", mapped.get("native_id") or mapped.get("id") or digest(record), obj(mapped.get("data")) or {k: v for k, v in mapped.items() if k not in {"session_id", "turn_id", "kind", "ts", "id", "native_id"}})
