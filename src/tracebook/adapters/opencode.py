"""OpenCode v2 schema, verified against pinned upstream source, not a local install."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter, tool_input, tool_kind
from tracebook.model import Capabilities, Event, Json, Source, number, obj, text_content, utc


class OpenCodeAdapter(Adapter):
    id = "opencode"
    display_name = "OpenCode"
    binary = "opencode"
    verification = "unverified; upstream v2 schema 097a93e5"

    def root(self) -> Path:
        return Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "opencode"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            transcripts="partial",
            reported_cost="partial",
            token_input="partial",
            token_output="partial",
            token_cache_read="partial",
            token_cache_write="partial",
            token_reasoning="partial",
            tool_durations="partial",
            subagents="partial",
            model_name_per_call="partial",
            prompt_text="partial",
            tool_io_content="partial",
            session_resume_detectable="partial",
        )

    def transcript_sources(self) -> list[Source]:
        return [Source(str(path), "sqlite") for path in self.root().glob("opencode*.db")]

    def database_records(self, database: sqlite3.Connection, state: Json) -> Iterable[Json]:
        tables = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"session_v2", "session_message"} <= tables:
            yield {"type": "unsupported_database_schema", "tables": sorted(tables)}
            return
        watermark = state.get("watermark", 0)
        rows = database.execute(
            "SELECT m.*,s.directory,s.version,s.parent_id,s.fork_session_id,(SELECT u.id FROM session_message u WHERE u.session_id=m.session_id AND u.type='user' AND u.seq<=m.seq ORDER BY u.seq DESC LIMIT 1) turn_id FROM session_message m JOIN session_v2 s ON s.id=m.session_id WHERE m.time_updated>=? ORDER BY m.session_id,m.seq",
            (watermark,),
        )
        for row in rows:
            try:
                data = obj(json.loads(row["data"]))
            except ValueError:
                data = {"type": "malformed_database_record", "unparsed": row["data"]}
            yield {
                **data,
                **{
                    key: row[key]
                    for key in (
                        "id",
                        "session_id",
                        "turn_id",
                        "directory",
                        "version",
                        "parent_id",
                        "fork_session_id",
                    )
                },
                "type": data.get("type", row["type"]),
            }
            state["watermark"] = max(state.get("watermark", 0), row["time_updated"])

    def parse_record(self, record: Json, state: Json) -> Iterable[Event]:
        state["session_id"] = record.get("session_id", state.get("session_id", "unknown"))
        state["turn_id"] = record.get("turn_id", state.get("turn_id"))
        clock = obj(record.get("time"))
        created = number(clock.get("created"))
        record = {**record, "timestamp": utc(created / 1000 if created is not None else None)}
        if record.get("directory"):
            yield self.event(
                record,
                state,
                "session",
                state["session_id"],
                {
                    "cwd": record["directory"],
                    "agent_version": record.get("version"),
                    "parent_session_id": record.get("parent_id"),
                    "resumed": bool(record.get("fork_session_id")),
                    "sidechain": bool(record.get("parent_id")),
                },
            )
        typ = record.get("type")
        if typ == "user":
            state["turn_id"] = record.get("id")
            prompt = str(record.get("text", ""))
            yield self.event(
                record,
                state,
                "turn_start",
                state["turn_id"],
                {
                    "prompt": prompt,
                    "kind": "command"
                    if prompt.startswith("/")
                    else "sidechain"
                    if record.get("parent_id")
                    else "prompt",
                },
            )
        elif typ == "assistant":
            tokens, model = obj(record.get("tokens")), obj(record.get("model"))
            cache = obj(tokens.get("cache"))
            if tokens or "cost" in record:
                yield self.event(
                    record,
                    state,
                    "model_call",
                    record.get("id"),
                    {
                        "model": str(model.get("providerID", "")) + "/" + str(model.get("id", ""))
                        if model.get("id")
                        else None,
                        "input_uncached": number(tokens.get("input")),
                        "input_cache_read": number(cache.get("read")),
                        "input_cache_write": number(cache.get("write")),
                        "output": number(tokens.get("output")),
                        "reasoning": number(tokens.get("reasoning")),
                        "cost_reported": record.get("cost"),
                        "cost_semantics": "agent_reported_api_value",
                        "token_semantics_confidence": "upstream_source",
                        "response": text_content(record.get("content")),
                    },
                )
            for block in record.get("content", []):
                if not isinstance(block, dict) or block.get("type") != "tool":
                    continue
                name, tool, timing = (
                    str(block.get("name", "unknown")),
                    obj(block.get("state")),
                    obj(block.get("time")),
                )
                inputs = tool_input(tool.get("input"))
                if "filePath" in inputs:
                    inputs["file_path"] = inputs["filePath"]
                start = timing.get("ran", timing.get("created"))
                yield self.event(
                    {
                        "timestamp": utc(start / 1000)
                        if isinstance(start, (int, float))
                        else record["timestamp"]
                    },
                    state,
                    "tool_call",
                    block.get("id"),
                    {"native_name": name, "tool": tool_kind(name), "input": inputs},
                )
                if tool.get("status") in {"completed", "error"}:
                    end = timing.get("completed")
                    yield self.event(
                        {
                            "timestamp": utc(end / 1000)
                            if isinstance(end, (int, float))
                            else record["timestamp"]
                        },
                        state,
                        "tool_result",
                        block.get("id"),
                        {
                            "status": tool["status"],
                            "output": tool.get("content", tool.get("error")),
                            "duration_ms": max(0, end - start)
                            if isinstance(end, (int, float)) and isinstance(start, (int, float))
                            else None,
                        },
                    )
            if record.get("error") or record.get("finish") in {"stop", "length"}:
                end = clock.get("completed")
                yield self.event(
                    {
                        "timestamp": utc(end / 1000)
                        if isinstance(end, (int, float))
                        else record["timestamp"]
                    },
                    state,
                    "turn_end",
                    state.get("turn_id"),
                    {"status": "error" if record.get("error") else "completed"},
                )
        elif typ == "compaction":
            yield self.event(record, state, "compaction", data={"reason": record.get("reason")})
        elif typ in {
            "synthetic",
            "system",
            "skill",
            "agent-switched",
            "model-switched",
            "location-switched",
        }:
            yield self.event(record, state, "lifecycle", data={"native_type": typ})
        else:
            yield self.event(
                record, state, "warning", data={"reason": "unknown_record_type", "native_type": typ}
            )
