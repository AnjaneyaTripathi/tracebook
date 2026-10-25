from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter, tool_input, tool_kind
from tracebook.model import Capabilities, Event, Json, Source, digest, number, obj, text_content


def usage_tokens(usage: Json) -> Json:
    inp, cached, written = (
        number(usage.get(k))
        for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens")
    )
    out, reasoning = (number(usage.get(k)) for k in ("output_tokens", "reasoning_output_tokens"))
    return {
        "input_uncached": max(0, inp - (cached or 0) - (written or 0)) if inp is not None else None,
        "input_cache_read": cached,
        "input_cache_write": written,
        "output": max(0, out - (reasoning or 0)) if out is not None else None,
        "reasoning": reasoning,
        "total_reported": number(usage.get("total_tokens")),
        "token_semantics_confidence": "ambiguous"
        if (inp is not None and (cached or 0) + (written or 0) > inp)
        or (out is not None and (reasoning or 0) > out)
        else "verified",
    }


class CodexAdapter(Adapter):
    id = "codex"
    display_name = "OpenAI Codex"
    binary = "codex"
    verification = "installed 0.163.0; transcripts observed 0.164.0"
    otel_services = ("codex", "codex_cli", "codex-cli")
    otel_token_fields = {
        "input_uncached": "input_token_count",
        "input_cache_read": "cached_input_token_count",
        "output": "output_token_count",
        "reasoning": "reasoning_output_token_count",
        "total_reported": "total_token_count",
    }
    input_includes_cache = True
    output_includes_reasoning = True

    def root(self) -> Path:
        return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            transcripts="yes",
            hooks_turn_end="yes",
            otel_logs="yes",
            otel_metrics="partial",
            token_input="yes",
            token_output="yes",
            token_cache_read="yes",
            token_cache_write="partial",
            token_reasoning="yes",
            tool_durations="partial",
            subagents="partial",
            permission_events="partial",
            model_name_per_call="partial",
            prompt_text="yes",
            tool_io_content="yes",
            session_resume_detectable="partial",
        )

    def transcript_sources(self) -> list[Source]:
        return [
            Source(str(p))
            for directory in ("sessions", "archived_sessions")
            for p in (self.root() / directory).glob("**/*.jsonl")
        ]

    def parse_record(self, record: Json, state: Json) -> Iterable[Event]:
        typ = record.get("type")
        payload = obj(record.get("payload"))
        sub = payload.get("type")
        if typ == "session_meta":
            state["session_id"] = str(
                payload.get("id", payload.get("session_id", state.get("session_id", "unknown")))
            )
            source = obj(payload.get("source"))
            yield self.event(
                record,
                state,
                "session",
                state["session_id"],
                {
                    "cwd": payload.get("cwd"),
                    "agent_version": payload.get("cli_version"),
                    "remote": obj(payload.get("git")).get("repository_url"),
                    "resumed": bool(payload.get("forked_from_id")),
                    "parent_session_id": obj(source.get("subagent")).get("parent_thread_id"),
                },
            )
            return
        if payload.get("turn_id"):
            state["turn_id"] = str(payload["turn_id"])
        metadata = obj(payload.get("internal_chat_message_metadata_passthrough"))
        if metadata.get("turn_id"):
            state["turn_id"] = str(metadata["turn_id"])
        if typ == "turn_context":
            state["model"] = payload.get("model")
            yield self.event(
                record, state, "session", state.get("session_id"), {"cwd": payload.get("cwd")}
            )
        elif typ == "token_usage_record":
            yield self.event(
                record,
                state,
                "model_call",
                payload.get("response_id"),
                {
                    **usage_tokens(obj(payload.get("usage"))),
                    "model": state.get("model"),
                    "usage_format": "response",
                    "aliases": [payload.get("response_id")],
                },
            )
        elif typ == "event_msg":
            if sub == "task_started":
                state["turn_id"] = str(payload.get("turn_id") or digest(record))
                state["ended"] = False
                yield self.event(
                    record,
                    state,
                    "turn_start",
                    state["turn_id"],
                    {
                        "kind": "prompt",
                        "parent_turn_id": obj(payload.get("turn_attribution")).get(
                            "parent_turn_id"
                        ),
                    },
                )
            elif sub in {"task_complete", "turn_aborted", "error"}:
                state["ended"] = True
                yield self.event(
                    record,
                    state,
                    "turn_end",
                    state.get("turn_id"),
                    {
                        "status": {
                            "task_complete": "completed",
                            "turn_aborted": "interrupted",
                            "error": "error",
                        }[str(sub)],
                        "duration_ms": payload.get("duration_ms"),
                        "response": payload.get("last_agent_message"),
                        "error": payload.get("message"),
                    },
                )
            elif sub == "token_count":
                info = obj(payload.get("info"))
                total = obj(info.get("total_token_usage"))
                previous = obj(state.get("total_usage"))
                if total and total != previous:
                    delta = {
                        k: max(0, (number(v) or 0) - (number(previous.get(k)) or 0))
                        for k, v in total.items()
                    }
                    state["total_usage"] = total
                    yield self.event(
                        record,
                        state,
                        "model_call",
                        digest(total),
                        {
                            **usage_tokens(delta),
                            "model": state.get("model"),
                            "usage_format": "legacy_delta",
                        },
                    )
            elif sub == "user_message":
                if not state.get("turn_id") or state.get("ended"):
                    state["turn_id"] = digest(record)
                    state["ended"] = False
                yield self.event(
                    record,
                    state,
                    "turn_start",
                    state["turn_id"],
                    {
                        "prompt": payload.get("message"),
                        "kind": "command"
                        if str(payload.get("message", "")).startswith("/")
                        else "prompt",
                    },
                )
            elif sub == "item_completed":
                yield from self.item(record, payload, state)
            elif sub in {"mcp_tool_call_end", "patch_apply_end", "web_search_end"}:
                yield from self.tool_end(record, payload, state)
            elif sub == "sub_agent_activity":
                yield self.event(
                    record,
                    state,
                    "subagent",
                    payload.get("event_id"),
                    {
                        "child_session_id": payload.get("agent_thread_id"),
                        "agent_path": payload.get("agent_path"),
                        "activity": payload.get("kind"),
                    },
                )
            elif sub == "thread_rolled_back":
                yield self.event(
                    record,
                    state,
                    "lifecycle",
                    data={"native_type": sub, "rolled_back_turns": payload.get("num_turns")},
                )
            elif sub in {
                "agent_message",
                "agent_reasoning",
                "thread_settings_applied",
                "context_compacted",
            }:
                yield self.event(record, state, "lifecycle", data={"native_type": sub})
            else:
                yield self.event(
                    record,
                    state,
                    "warning",
                    data={"native_type": sub, "reason": "unknown_event_type"},
                )
        elif typ == "response_item":
            if sub == "message" and payload.get("role") == "user":
                prompt = text_content(payload.get("content"))
                if prompt.startswith(
                    ("<environment_context>", "<permissions instructions>", "<turn_aborted>")
                ):
                    yield self.event(
                        record, state, "lifecycle", data={"native_type": "environment"}
                    )
                    return
                if not state.get("turn_id") or state.get("ended"):
                    state["turn_id"] = str(payload.get("id") or digest(record))
                    state["ended"] = False
                yield self.event(
                    record,
                    state,
                    "turn_start",
                    state["turn_id"],
                    {"prompt": prompt, "kind": "command" if prompt.startswith("/") else "prompt"},
                )
            elif sub in {"function_call", "custom_tool_call", "tool_search_call"}:
                name = str(
                    payload.get("name", "tool_search" if sub == "tool_search_call" else "unknown")
                )
                yield self.event(
                    record,
                    state,
                    "tool_call",
                    payload.get("call_id"),
                    {
                        "native_name": name,
                        "tool": tool_kind(name),
                        "input": tool_input(payload.get("arguments", payload.get("input"))),
                    },
                )
            elif sub in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                yield self.event(
                    record,
                    state,
                    "tool_result",
                    payload.get("call_id"),
                    {"output": payload.get("output", payload.get("tools")), "status": "unknown"},
                )
            elif sub in {"message", "reasoning", "agent_message", "additional_tools"}:
                yield self.event(record, state, "lifecycle", data={"native_type": sub})
            else:
                yield self.event(record, state, "warning", data={"native_type": sub})
        elif typ == "compacted":
            yield self.event(record, state, "compaction")
        elif typ in {"world_state", "inter_agent_communication_metadata"}:
            yield self.event(record, state, "lifecycle", data={"native_type": typ})
        else:
            yield self.event(
                record, state, "warning", data={"native_type": typ, "reason": "unknown_record_type"}
            )

    def item(self, record: Json, payload: Json, state: Json) -> Iterable[Event]:
        item = obj(payload.get("item"))
        kind = item.get("type")
        native = item.get("id")
        if kind == "UserMessage":
            state["turn_id"] = str(payload.get("turn_id") or native or digest(record))
            yield self.event(
                record,
                state,
                "turn_start",
                state["turn_id"],
                {"prompt": text_content(item.get("content")), "kind": "prompt"},
            )
        elif kind in {"CommandExecution", "McpToolCall", "FileChange"}:
            name = str(item.get("tool", kind))
            duration = item.get("duration")
            if isinstance(duration, dict):
                duration = (duration.get("secs", 0) * 1000) + (duration.get("nanos", 0) / 1e6)
            start, end = payload.get("started_at_ms"), payload.get("completed_at_ms")
            if (
                duration is None
                and isinstance(start, (float, int))
                and isinstance(end, (float, int))
            ):
                duration = max(0, end - start)
            inputs = obj(item.get("arguments")) or {
                "command": item.get("command"),
                "changes": item.get("changes"),
            }
            yield self.event(
                record,
                state,
                "tool_call",
                native,
                {
                    "native_name": name,
                    "tool": "mcp" if kind == "McpToolCall" else tool_kind(str(kind)),
                    "input": tool_input(inputs),
                },
            )
            failed = item.get("exit_code") not in (None, 0) or str(
                item.get("status", "")
            ).lower() in {"failed", "error", "declined"}
            yield self.event(
                record,
                state,
                "tool_result",
                native,
                {
                    "output": item.get("aggregated_output", item.get("result", item.get("stderr"))),
                    "exit_code": item.get("exit_code"),
                    "duration_ms": duration,
                    "status": "error" if failed else "completed",
                },
            )
        elif kind in {"SubAgentActivity", "CollabAgentToolCall"}:
            yield self.event(
                record,
                state,
                "subagent",
                native,
                {
                    "child_session_id": item.get("agent_thread_id"),
                    "agent_path": item.get("agent_path"),
                    "model": item.get("model"),
                },
            )
        elif kind == "ContextCompaction":
            yield self.event(record, state, "compaction", native)
        elif kind == "FunctionCallOutput":
            yield self.event(
                record,
                state,
                "tool_result",
                native,
                {
                    "native_name": item.get("name"),
                    "output": item.get("output"),
                    "status": "unknown",
                },
            )
        elif kind in {"AgentMessage", "Reasoning", "ImageView", "Extension", "Plan"}:
            yield self.event(record, state, "lifecycle", native, {"native_type": kind})
        else:
            yield self.event(record, state, "warning", native, {"native_type": kind})

    def tool_end(self, record: Json, payload: Json, state: Json) -> Iterable[Event]:
        kind, native = payload.get("type"), payload.get("call_id")
        if kind == "mcp_tool_call_end":
            invocation, result = obj(payload.get("invocation")), obj(payload.get("result"))
            success = obj(result.get("Ok"))
            duration = obj(payload.get("duration"))
            yield self.event(
                record,
                state,
                "tool_call",
                native,
                {
                    "tool": "mcp",
                    "native_name": str(invocation.get("server", ""))
                    + "."
                    + str(invocation.get("tool", "unknown")),
                    "input": tool_input(invocation.get("arguments")),
                },
            )
            yield self.event(
                record,
                state,
                "tool_result",
                native,
                {
                    "output": success.get("content", result.get("Err")),
                    "status": "error" if "Err" in result or success.get("isError") else "completed",
                    "duration_ms": (duration.get("secs", 0) * 1000 + duration.get("nanos", 0) / 1e6)
                    if duration
                    else None,
                },
            )
        elif kind == "patch_apply_end":
            yield self.event(
                record,
                state,
                "tool_result",
                native,
                {
                    "output": {
                        "stdout": payload.get("stdout"),
                        "stderr": payload.get("stderr"),
                        "changes": payload.get("changes"),
                    },
                    "status": "completed"
                    if payload.get("success") is True
                    else "error"
                    if payload.get("success") is False
                    else "unknown",
                },
            )
        else:
            yield self.event(
                record,
                state,
                "tool_call",
                native,
                {
                    "tool": "web_search",
                    "native_name": "web_search",
                    "input": {"pattern": payload.get("query"), "action": payload.get("action")},
                },
            )
            yield self.event(record, state, "tool_result", native, {"status": "completed"})

    def otel_setup(self, endpoint: str) -> str:
        return (
            "# Merge into user config.toml; do not duplicate an existing [otel] table.\n"
            "[otel]\nlog_user_prompt = false\n"
            'exporter = { otlp-http = { endpoint = "'
            + endpoint
            + '/v1/logs", protocol = "binary" } }\n'
        )
