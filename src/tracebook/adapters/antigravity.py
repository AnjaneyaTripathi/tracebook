from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from tracebook.adapters.base import Adapter, tool_input, tool_kind
from tracebook.model import Capabilities, Event, Json, Source, number, text_content


class AntigravityAdapter(Adapter):
    id = "antigravity"
    display_name = "Antigravity"
    binary = "agy"
    verification = "installed 1.4.8; transcripts observed 1.4.8"

    def root(self) -> Path:
        custom = os.environ.get("ANTIGRAVITY_HOME")
        if custom:
            return Path(custom).expanduser()
        gemini_home = os.environ.get("GEMINI_CLI_HOME")
        if gemini_home:
            return Path(gemini_home).expanduser() / ".gemini" / "antigravity-cli"
        return Path.home() / ".gemini" / "antigravity-cli"

    def hook_config_path(self) -> Path:
        gemini_home = os.environ.get("GEMINI_CLI_HOME")
        base = Path(gemini_home).expanduser() if gemini_home else Path.home()
        return base / ".gemini" / "config" / "hooks.json"

    def session_id(self, source: Source) -> str:
        parts = Path(source.path).parts
        if "brain" in parts:
            idx = parts.index("brain")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        return Path(source.path).parent.parent.name

    def resolve_project(self, session_id: str) -> str | None:
        if not session_id or session_id == "unknown":
            return None

        # 1. Look in conversation_summaries.db
        db_path = self.root() / "conversation_summaries.db"
        if db_path.is_file():
            try:
                import json
                import sqlite3

                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
                    row = con.execute(
                        "SELECT workspace_uris FROM conversation_summaries WHERE conversation_id = ?",
                        (session_id,),
                    ).fetchone()
                    if row and row[0]:
                        uris = json.loads(row[0])
                        if isinstance(uris, list) and uris:
                            first_uri = str(uris[0])
                            if first_uri.startswith("file://"):
                                return first_uri[7:]
                            return first_uri
            except Exception:
                pass

        # 2. Look in cache/last_conversations.json
        cache_file = self.root() / "cache" / "last_conversations.json"
        if cache_file.is_file():
            try:
                import json

                with cache_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for directory, cid in data.items():
                            if cid == session_id:
                                return str(directory)
            except Exception:
                pass

        return None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            transcripts="yes",
            hooks_pre_tool="yes",
            hooks_post_tool="yes",
            hooks_prompt_submit="no",
            hooks_turn_end="yes",
            otel_logs="no",
            otel_metrics="no",
            reported_cost="no",
            token_input="yes",
            token_output="yes",
            token_cache_read="yes",
            token_cache_write="no",
            token_reasoning="partial",
            tool_durations="partial",
            subagents="yes",
            permission_events="partial",
            model_name_per_call="partial",
            prompt_text="yes",
            tool_io_content="yes",
            session_resume_detectable="partial",
        )

    def transcript_sources(self) -> list[Source]:
        brain = self.root() / "brain"
        if not brain.is_dir():
            return []
        sources = [
            Source(str(p), "jsonl") for p in brain.glob("*/.system_generated/logs/transcript.jsonl")
        ]
        return sorted(sources, key=lambda s: s.path)

    def parse_record(self, record: Json, state: Json) -> Iterable[Event]:
        step_index = record.get("step_index", 0)
        typ = record.get("type")
        session_id = str(state.get("session_id", "unknown"))

        if not state.get("cwd"):
            state["cwd"] = self.resolve_project(session_id)

        if not state.get("session_emitted"):
            state["session_emitted"] = True
            yield self.event(
                record,
                state,
                "session",
                session_id,
                {
                    "cwd": state.get("cwd"),
                    "agent_version": "1.4.8",
                    "verification": "locally observed",
                },
            )

        if typ == "USER_INPUT":
            if not state.get("turn_completed", True) and state.get("turn_id"):
                yield self.event(
                    record,
                    state,
                    "turn_end",
                    state["turn_id"],
                    {"status": "completed", "boundary_confidence": "inferred"},
                    state["turn_id"],
                )
            state["turn_id"] = str(step_index)
            state["turn_completed"] = False
            content = str(record.get("content", ""))
            prompt = content
            if "<USER_REQUEST>" in content:
                start = content.find("<USER_REQUEST>") + len("<USER_REQUEST>")
                end = content.find("</USER_REQUEST>")
                if end > start:
                    prompt = content[start:end].strip()
            if "<USER_SETTINGS_CHANGE>" in content:
                match = re.search(r"Model Selection` from \S+ to ([^.\n]+)", content)
                if match:
                    state["model"] = match.group(1).strip()
            yield self.event(
                record,
                state,
                "turn_start",
                str(step_index),
                {"prompt": prompt, "kind": "command" if prompt.startswith("/") else "prompt"},
                str(step_index),
            )

        elif typ == "PLANNER_RESPONSE":
            turn = state.get("turn_id", str(step_index))
            inp = record.get("input_tokens")
            out = record.get("output_tokens")
            cache = record.get("cache_read_tokens")
            if inp is not None or out is not None:
                yield self.event(
                    record,
                    state,
                    "model_call",
                    f"call-{step_index}",
                    {
                        "model": state.get("model", "Gemini 3.8 Flash (High)"),
                        "response": text_content(record.get("content")),
                        "input_uncached": number(inp),
                        "input_cache_read": number(cache),
                        "output": number(out),
                        "token_semantics_confidence": "locally_observed",
                    },
                    turn,
                )
            tool_calls = record.get("tool_calls", [])
            for idx, tool in enumerate(tool_calls if isinstance(tool_calls, list) else []):
                if isinstance(tool, dict):
                    name = str(tool.get("name", "unknown"))
                    tid = f"tool-{step_index}-{idx}"
                    args = tool.get("args")
                    if isinstance(args, dict) and state.get("cwd") is None:
                        path_arg = args.get("Path") or args.get("path")
                        if isinstance(path_arg, str) and path_arg.startswith("/"):
                            for parent in Path(path_arg).parents:
                                if (parent / ".git").is_dir():
                                    state["cwd"] = str(parent)
                                    break
                    yield self.event(
                        record,
                        state,
                        "tool_call",
                        tid,
                        {
                            "native_name": name,
                            "tool": tool_kind(name),
                            "input": tool_input(args),
                        },
                        turn,
                    )
                    state.setdefault("pending_tools", []).append(tid)
            if not tool_calls and record.get("content"):
                state["turn_completed"] = True
                yield self.event(
                    record,
                    state,
                    "turn_end",
                    turn,
                    {"status": "completed", "response": text_content(record.get("content"))},
                    turn,
                )

        elif typ == "GENERIC":
            turn = state.get("turn_id", str(step_index))
            pending = state.get("pending_tools", [])
            if pending:
                tid = pending.pop(0)
                status = (
                    "error"
                    if record.get("status") in {"ERROR", "FAILED"} or record.get("error")
                    else "completed"
                )
                yield self.event(
                    record,
                    state,
                    "tool_result",
                    tid,
                    {
                        "output": text_content(record.get("content")),
                        "status": status,
                        "error": record.get("error"),
                    },
                    turn,
                )
            else:
                yield self.event(
                    record,
                    state,
                    "lifecycle",
                    f"generic-{step_index}",
                    {"native_type": "generic_message"},
                    turn,
                )

        elif typ == "SYSTEM_MESSAGE":
            turn = state.get("turn_id", str(step_index))
            yield self.event(
                record,
                state,
                "lifecycle",
                f"sys-{step_index}",
                {"native_type": "system_message"},
                turn,
            )

    def parse_hook(
        self, payload: Json, argv: list[str] | None = None, env: Json | None = None
    ) -> Iterable[Event]:
        conversation_id = str(payload.get("conversationId", payload.get("session_id", "unknown")))
        step_idx = payload.get("stepIdx", payload.get("step_index", 0))
        turn = str(step_idx)
        state: Json = {"session_id": conversation_id, "turn_id": turn}

        event_name = ""
        if argv:
            for i, arg in enumerate(argv):
                if arg == "--event" and i + 1 < len(argv):
                    event_name = argv[i + 1]
                    break

        if event_name == "PreToolUse":
            tool_call = payload.get("toolCall", {})
            name = str(tool_call.get("name", "unknown"))
            yield self.event(
                payload,
                state,
                "tool_call",
                f"hook-tool-{step_idx}",
                {
                    "native_name": name,
                    "tool": tool_kind(name),
                    "input": tool_input(tool_call.get("args")),
                },
                turn,
            )
        elif event_name == "PostToolUse":
            status = "error" if payload.get("error") else "completed"
            yield self.event(
                payload,
                state,
                "tool_result",
                f"hook-tool-{step_idx}",
                {"status": status, "error": payload.get("error")},
                turn,
            )
        elif event_name == "SessionStart":
            yield self.event(
                payload,
                state,
                "session",
                conversation_id,
                {"cwd": (payload.get("workspacePaths") or [None])[0]},
            )
        elif event_name == "Stop":
            yield self.event(
                payload,
                state,
                "turn_end",
                turn,
                {
                    "status": "completed",
                    "termination_reason": payload.get("terminationReason"),
                },
                turn,
            )
