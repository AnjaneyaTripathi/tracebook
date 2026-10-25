from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter, tool_input, tool_kind
from tracebook.model import Capabilities, Event, Json, Source, digest, number, obj, text_content


class ClaudeAdapter(Adapter):
    id = "claude_code"
    display_name = "Claude Code"
    binary = "claude"
    verification = "installed 2.1.286; transcripts observed 2.1.285"
    otel_environment = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "{endpoint}",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOG_USER_PROMPTS": "0",
    }
    otel_services = ("claude-code", "claude_code")
    otel_token_fields = {
        "input_uncached": "input_tokens",
        "input_cache_read": "cache_read_tokens",
        "input_cache_write": "cache_creation_tokens",
        "output": "output_tokens",
    }

    def root(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            transcripts="yes",
            hooks_pre_tool="yes",
            hooks_post_tool="yes",
            hooks_prompt_submit="yes",
            hooks_turn_end="yes",
            otel_logs="yes",
            otel_metrics="yes",
            reported_cost="partial",
            token_input="yes",
            token_output="yes",
            token_cache_read="yes",
            token_cache_write="yes",
            token_reasoning="partial",
            tool_durations="partial",
            subagents="partial",
            permission_events="partial",
            model_name_per_call="yes",
            prompt_text="yes",
            tool_io_content="yes",
            session_resume_detectable="partial",
        )

    def transcript_sources(self) -> list[Source]:
        return [Source(str(p)) for p in self.root().glob("projects/**/*.jsonl")]

    def parse_record(self, record: Json, state: Json) -> Iterable[Event]:
        state["base_session_id"] = record.get(
            "sessionId",
            record.get(
                "session_id", state.get("base_session_id", state.get("session_id", "unknown"))
            ),
        )
        state["session_id"] = state["base_session_id"]
        if record.get("agentId"):
            state["session_id"] = f"{state['session_id']}/{record['agentId']}"
        typ = record.get("type")
        if not state.get("session_emitted"):
            state["session_emitted"] = True
            yield self.event(
                record,
                state,
                "session",
                state["session_id"],
                {
                    "cwd": record.get("cwd"),
                    "agent_version": record.get("version"),
                    "resumed": bool(record.get("parentUuid")),
                    "sidechain": bool(record.get("isSidechain")),
                },
            )
        metadata_key = (record.get("cwd"), record.get("version"))
        if record.get("cwd") and list(metadata_key) != state.get("metadata_key"):
            state["metadata_key"] = list(metadata_key)
            yield self.event(
                record,
                state,
                "session",
                state["session_id"],
                {"cwd": record.get("cwd"), "agent_version": record.get("version")},
            )
        message = obj(record.get("message"))
        content = message.get("content", [])
        blocks = content if isinstance(content, list) else []
        if typ == "user":
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            for block in results:
                yield self.event(
                    record,
                    state,
                    "tool_result",
                    block.get("tool_use_id"),
                    {
                        "output": block.get("content"),
                        "status": "error" if block.get("is_error") else "completed",
                    },
                )
            if results or record.get("isMeta") or record.get("isCompactSummary"):
                return
            state["turn_id"] = str(record.get("promptId") or record.get("uuid") or digest(record))
            prompt = text_content(content)
            yield self.event(
                record,
                state,
                "turn_start",
                state["turn_id"],
                {
                    "prompt": prompt,
                    "kind": "command"
                    if prompt.startswith("/") or "<command-name>" in prompt
                    else "sidechain"
                    if record.get("isSidechain")
                    else "prompt",
                    "sidechain": bool(record.get("isSidechain")),
                    "aliases": [record.get("uuid")],
                },
            )
        elif typ == "assistant":
            usage = obj(message.get("usage"))
            reasoning = number(obj(usage.get("output_tokens_details")).get("thinking_tokens"))
            output = number(usage.get("output_tokens"))
            data: Json = {
                "model": message.get("model"),
                "response": text_content(content),
                "input_uncached": number(usage.get("input_tokens")),
                "input_cache_read": number(usage.get("cache_read_input_tokens")),
                "input_cache_write": number(usage.get("cache_creation_input_tokens")),
                "output": max(0, output - (reasoning or 0)) if output is not None else None,
                "reasoning": reasoning,
                "token_semantics_confidence": "verified",
                "aliases": [message.get("id"), record.get("requestId")],
                "request_id": record.get("requestId"),
            }
            creation = obj(usage.get("cache_creation"))
            if creation:
                data["cache_write_tokens_by_ttl"] = {
                    ttl: number(creation[field])
                    for ttl, field in (
                        ("300", "ephemeral_5m_input_tokens"),
                        ("3600", "ephemeral_1h_input_tokens"),
                    )
                    if field in creation
                }
            if usage:
                yield self.event(
                    record, state, "model_call", record.get("requestId") or message.get("id"), data
                )
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name", "unknown"))
                    yield self.event(
                        record,
                        state,
                        "tool_call",
                        block.get("id"),
                        {
                            "native_name": name,
                            "tool": tool_kind(name),
                            "input": tool_input(block.get("input")),
                        },
                    )
            if message.get("stop_reason") == "end_turn":
                yield self.event(
                    record,
                    state,
                    "turn_end",
                    state.get("turn_id"),
                    {"status": "completed", "response": text_content(content)},
                )
            if record.get("isApiErrorMessage"):
                yield self.event(
                    record, state, "turn_end", state.get("turn_id"), {"status": "error"}
                )
        elif typ == "cost-state":
            yield self.event(
                record,
                state,
                "session",
                state["session_id"],
                {
                    "cost_reported": record.get("totalCostUSD"),
                    "cost_semantics": "agent_estimated_api_value",
                    "duration_ms": record.get("totalDuration"),
                },
            )
        elif typ == "system" and record.get("subtype") == "turn_duration":
            yield self.event(
                record,
                state,
                "turn_end",
                state.get("turn_id"),
                {"status": "completed", "duration_ms": record.get("durationMs")},
            )
        elif typ == "system" and record.get("subtype") == "compact_boundary":
            yield self.event(record, state, "compaction")
        elif typ in {
            "mode",
            "permission-mode",
            "system",
            "file-history-snapshot",
            "file-history-delta",
            "last-prompt",
            "atis-latch",
            "attachment",
            "ai-title",
            "queue-operation",
            "progress",
            "summary",
        }:
            yield self.event(record, state, "lifecycle", data={"native_type": typ})
        else:
            yield self.event(
                record, state, "warning", data={"native_type": typ, "reason": "unknown_record_type"}
            )
