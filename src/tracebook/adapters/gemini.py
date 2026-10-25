from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter, tool_input, tool_kind
from tracebook.model import Capabilities, Event, Json, Source, digest, number, obj, text_content


class GeminiAdapter(Adapter):
    id = "gemini_cli"
    display_name = "Gemini CLI"
    binary = "gemini"
    otel_environment = {
        "GEMINI_TELEMETRY_ENABLED": "true",
        "GEMINI_TELEMETRY_TARGET": "local",
        "GEMINI_TELEMETRY_OTLP_ENDPOINT": "{endpoint}",
        "GEMINI_TELEMETRY_OTLP_PROTOCOL": "http",
        "GEMINI_TELEMETRY_LOG_PROMPTS": "false",
        "GEMINI_TELEMETRY_TRACES_ENABLED": "false",
    }
    otel_services = ("gemini-cli", "gemini_cli")
    otel_token_fields = {
        "input_uncached": "input_token_count",
        "input_cache_read": "cached_content_token_count",
        "output": "output_token_count",
        "reasoning": "thoughts_token_count",
        "total_reported": "total_token_count",
    }
    input_includes_cache = True

    def root(self) -> Path:
        return Path(os.environ.get("GEMINI_CLI_HOME", str(Path.home()))).expanduser() / ".gemini"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            transcripts="partial",
            hooks_pre_tool="partial",
            hooks_post_tool="partial",
            hooks_prompt_submit="partial",
            hooks_turn_end="partial",
            otel_logs="partial",
            otel_metrics="partial",
            token_input="partial",
            token_output="partial",
            token_cache_read="partial",
            token_reasoning="partial",
            tool_durations="partial",
            subagents="partial",
            permission_events="partial",
            model_name_per_call="partial",
            prompt_text="partial",
            tool_io_content="partial",
            session_resume_detectable="partial",
        )

    def parse_otel_file(self, record: Json) -> Iterable[Event]:
        attributes = obj(record.get("attributes"))
        clock = record.get("hrTime", record.get("hrTimeObserved", [0, 0]))
        nanoseconds = (
            (clock[0] * 1_000_000_000 + clock[1])
            if isinstance(clock, list) and len(clock) == 2
            else 0
        )
        if "scopeMetrics" in record:
            for scope in record.get("scopeMetrics", []):
                for metric in obj(scope).get("metrics", []):
                    descriptor = obj(metric.get("descriptor"))
                    for point in metric.get("dataPoints", []):
                        yield from self.map_otel(
                            [],
                            [
                                {
                                    **obj(point.get("attributes")),
                                    "_metric": descriptor.get("name"),
                                    "point": point,
                                    "temporality": metric.get("aggregationTemporality"),
                                    "_time_ns": nanoseconds,
                                }
                            ],
                        )
        else:
            yield from self.map_otel(
                [{**attributes, "_body": record.get("body"), "_time_ns": nanoseconds}], []
            )

    def transcript_sources(self) -> list[Source]:
        sources = [
            Source(str(p), "json" if p.suffix == ".json" else "jsonl")
            for pattern in (
                "tmp/**/chats/*.json",
                "tmp/**/chats/*.jsonl",
                "tmp/**/subagents/*.jsonl",
            )
            for p in self.root().glob(pattern)
        ]
        settings = self.root() / "settings.json"
        if settings.exists():
            import json

            try:
                outfile = obj(json.loads(settings.read_text()).get("telemetry")).get("outfile")
                if outfile and Path(outfile).expanduser().is_file():
                    sources.append(Source(str(Path(outfile).expanduser()), "otel_file"))
            except (ValueError, OSError, AttributeError):
                pass
        return sources

    def parse_record(self, record: Json, state: Json) -> Iterable[Event]:
        if "$set" in record:
            yield from self.parse_record(obj(record["$set"]), state)
            return
        if "$patch" in record:
            patch = obj(record["$patch"])
            updates = patch.get("updates", [patch])
            messages = obj(state.get("messages"))
            for update in updates if isinstance(updates, list) else []:
                if not isinstance(update, dict):
                    continue
                previous = obj(messages.get(str(update.get("id"))))
                if previous:
                    merged = {**previous, **update}
                    if "toolCalls" in update:
                        changes = {
                            str(t.get("id")): t for t in update["toolCalls"] if isinstance(t, dict)
                        }
                        merged["toolCalls"] = [
                            {**t, **changes.get(str(t.get("id")), {})}
                            for t in previous.get("toolCalls", [])
                        ]
                    yield from self.message(merged, state, replay=True)
            yield self.event(record, state, "lifecycle", data={"native_type": "$patch"})
            return
        if "$rewindTo" in record:
            yield self.event(
                record,
                state,
                "lifecycle",
                data={"native_type": "rewind", "rewind_to": record["$rewindTo"]},
            )
            return
        if record.get("sessionId") or "messages" in record:
            state["session_id"] = record.get("sessionId", state.get("session_id", "unknown"))
            state["timestamp"] = record.get("startTime", state.get("timestamp"))
            directories = record.get("directories", [])
            yield self.event(
                record,
                state,
                "session",
                state["session_id"],
                {
                    "cwd": directories[0]
                    if isinstance(directories, list) and directories
                    else None,
                    "project_hint": record.get("projectHash"),
                    "sidechain": record.get("kind") == "subagent",
                    "verification": "unverified",
                },
            )
            for message in record.get("messages", []):
                if isinstance(message, dict):
                    yield from self.message(message, state)
        elif record.get("type"):
            yield from self.message(record, state)
        elif "lastUpdated" in record:
            yield self.event(record, state, "lifecycle", data={"native_type": "metadata"})
        else:
            yield self.event(record, state, "warning", data={"reason": "unknown_record_type"})

    def message(self, record: Json, state: Json, replay: bool = False) -> Iterable[Event]:
        typ = record.get("type")
        native = str(record.get("id") or digest(record))
        messages = obj(state.setdefault("messages", {}))
        turns = obj(state.setdefault("message_turns", {}))
        if typ == "user":
            state["turn_id"] = native
        turn = str(turns.get(native, state.get("turn_id", native)))
        turns[native] = turn
        messages[native] = record
        if len(messages) > 200:
            messages.pop(next(iter(messages)))
            turns.pop(next(iter(turns)))
        if typ == "user":
            prompt = text_content(record.get("content"))
            yield self.event(
                record,
                state,
                "turn_start",
                native,
                {"prompt": prompt, "kind": "command" if prompt.startswith("/") else "prompt"},
                turn,
            )
        elif typ == "gemini":
            tokens = obj(record.get("tokens"))
            inp, cache = number(tokens.get("input")), number(tokens.get("cached"))
            if tokens:
                yield self.event(
                    record,
                    state,
                    "model_call",
                    native,
                    {
                        "model": record.get("model"),
                        "response": text_content(record.get("content")),
                        "input_uncached": max(0, inp - (cache or 0)) if inp is not None else None,
                        "input_cache_read": cache,
                        "output": number(tokens.get("output")),
                        "reasoning": number(tokens.get("thoughts")),
                        "total_reported": number(tokens.get("total")),
                        "token_semantics_confidence": "source_verified_local_unverified",
                    },
                    turn,
                )
            calls = record.get("toolCalls", [])
            for tool in calls if isinstance(calls, list) else []:
                if not isinstance(tool, dict):
                    continue
                name = str(tool.get("name", "unknown"))
                yield self.event(
                    record,
                    state,
                    "tool_call",
                    tool.get("id"),
                    {
                        "native_name": name,
                        "tool": tool_kind(name),
                        "input": tool_input(tool.get("args")),
                    },
                    turn,
                )
                if (
                    tool.get("status") in {"success", "error", "cancelled"}
                    or tool.get("result") is not None
                ):
                    yield self.event(
                        record,
                        state,
                        "tool_result",
                        tool.get("id"),
                        {
                            "output": tool.get("result"),
                            "status": {
                                "success": "completed",
                                "error": "error",
                                "cancelled": "interrupted",
                            }.get(str(tool.get("status")), "unknown"),
                        },
                        turn,
                    )
            if not calls and text_content(record.get("content")):
                yield self.event(
                    record,
                    state,
                    "turn_end",
                    turn,
                    {
                        "status": "completed",
                        "boundary_confidence": "inferred",
                        "response": text_content(record.get("content")),
                    },
                    turn,
                )
        elif typ == "error":
            yield self.event(
                record,
                state,
                "turn_end",
                turn,
                {"status": "error", "error": text_content(record.get("content"))},
                turn,
            )
        else:
            yield self.event(
                record,
                state,
                "lifecycle" if typ in {"info", "warning"} else "warning",
                native,
                {"native_type": typ},
                turn,
            )
