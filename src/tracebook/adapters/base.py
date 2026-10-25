from __future__ import annotations

import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from tracebook.model import (
    Capabilities,
    Check,
    Diff,
    Event,
    Installation,
    Json,
    Source,
    digest,
    obj,
    utc,
)

TOOL_MAP = {
    "shell": ("bash", "shell", "exec_command", "run_shell_command", "commandexecution", "shell_command", "write_stdin"),
    "file_read": ("read", "read_file", "read_many_files", "view_image", "imageview"),
    "file_write": ("write", "write_file"),
    "file_edit": ("edit", "multiedit", "replace", "apply_patch", "filechange"),
    "file_search": ("glob", "grep", "list_directory", "search_file_content", "find_files"),
    "web_fetch": ("webfetch", "web_fetch"),
    "web_search": ("websearch", "web_search", "google_web_search"),
    "subagent": ("task", "agent", "spawn_agent", "collabagenttoolcall", "subagentactivity", "delegate_to_agent"),
    "notebook": ("notebookedit",),
    "todo_plan": ("todowrite", "write_todos", "update_plan", "enterplanmode", "exitplanmode"),
    "ask_user": ("askuserquestion", "ask_user", "request_user_input", "request_user_input_async"),
}


def tool_kind(name: str) -> str:
    low = name.lower().split(".")[-1]
    if low.startswith("mcp") or name.startswith("mcp__"):
        return "mcp"
    return next((key for key, names in TOOL_MAP.items() if low in names), "other")


def tool_input(value: object) -> Json:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, dict) else {"input": value}
        except ValueError:
            value = {"input": value}
    raw = obj(value)
    normalized = dict(raw)
    for target, keys in {"command": ("command", "cmd"), "file_path": ("file_path", "path", "file"), "pattern": ("pattern", "query"), "url": ("url",)}.items():
        for key in keys:
            if key in raw:
                normalized[target] = raw[key]
                break
    return normalized


class Adapter(ABC):
    id: str
    display_name: str
    binary: str = ""
    verification: str = "unverified"
    otel_services: tuple[str, ...] = ()
    otel_token_fields: dict[str, str] = {}
    input_includes_cache = False
    output_includes_reasoning = False

    @abstractmethod
    def root(self) -> Path: ...

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def transcript_sources(self) -> list[Source]: ...

    @abstractmethod
    def parse_record(self, record: Json, state: Json) -> Iterable[Event]: ...

    def detect(self) -> Installation | None:
        binary = shutil.which(self.binary)
        if not binary:
            return None
        try:
            run = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5, check=False)
            version = run.stdout.strip().splitlines()[0] if run.stdout.strip() else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            version = "unknown"
        return Installation(binary, version, str(self.root()), str(self.root()))

    def install_hooks(self, dry_run: bool = True) -> Diff:
        from tracebook.install import edit_hooks
        return edit_hooks(self, dry_run=dry_run)

    def uninstall_hooks(self, dry_run: bool = False) -> Diff:
        from tracebook.install import edit_hooks
        return edit_hooks(self, uninstall=True, dry_run=dry_run)

    def doctor(self) -> list[Check]:
        installation = self.detect()
        return [Check("installed", installation is not None, installation.version if installation else "Binary not installed"), Check("transcripts", bool(self.transcript_sources()), str(self.root())), Check("verification", self.verification != "unverified", self.verification)]

    def parse_transcript(self, source: Source, chunk: bytes, state: Json | None = None) -> Iterable[Event]:
        state = state if state is not None else {}
        state.setdefault("session_id", digest(self.id, source.path))
        for line in chunk.splitlines():
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Record must be an object")
                yield from self.parse_record(record, state)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
                yield self.event({"type": "malformed", "digest": digest(line)}, state, "warning", data={"reason": type(exc).__name__})

    def event(self, record: Json, state: Json, kind: str, native_id: object = None, data: Json | None = None, turn_id: str | None = None) -> Event:
        ts = record.get("timestamp", record.get("time", state.get("timestamp")))
        return Event(self.id, str(state.get("session_id", "unknown")), kind, utc(ts), str(native_id or record.get("uuid") or record.get("id") or digest(record)), turn_id or state.get("turn_id"), data=data or {}, clock_source="agent_wall" if ts else "unknown")

    def parse_hook(self, payload: Json, argv: list[str] | None = None, env: Json | None = None) -> Iterable[Event]:
        state: Json = {"session_id": payload.get("session_id", payload.get("thread-id", "unknown")), "turn_id": payload.get("prompt_id", payload.get("turn-id"))}
        name = str(payload.get("hook_event_name", payload.get("type", "unknown")))
        native = payload.get("tool_use_id", payload.get("tool_call_id", state.get("turn_id"))) or digest(payload)
        data: Json = {"native_event": name, "cwd": payload.get("cwd")}
        if name in {"UserPromptSubmit", "BeforeAgent"}:
            kind = "turn_start"
            state["turn_id"] = str(state.get("turn_id") or native)
            data["prompt"] = payload.get("prompt")
        elif name in {"Stop", "AfterAgent", "agent-turn-complete"}:
            kind = "turn_end"
            data.update(status="completed", response=payload.get("last-assistant-message", payload.get("prompt_response")))
        elif name in {"PreToolUse", "BeforeTool", "PostToolUse", "AfterTool", "PostToolUseFailure"}:
            kind = "tool_call" if name in {"PreToolUse", "BeforeTool"} else "tool_result"
            name_native = str(payload.get("tool_name", "unknown"))
            data.update(tool=tool_kind(name_native), native_name=name_native, input=tool_input(payload.get("tool_input")), output=payload.get("tool_response"), status="error" if name == "PostToolUseFailure" or payload.get("error") else "completed")
        elif name in {"PermissionRequest", "PermissionDenied", "Notification"}:
            kind = "permission"
            data.update(decision=payload.get("decision", "denied" if name == "PermissionDenied" else "unknown"), message=payload.get("message"))
        elif name in {"PreCompact", "PreCompress"}:
            kind = "compaction"
        elif name in {"SubagentStart", "SubagentStop"}:
            kind = "subagent"
            data["child_session_id"] = payload.get("agent_id")
        elif name in {"SessionStart", "SessionEnd"}:
            kind = "session"
            data["resumed"] = payload.get("source") == "resume"
        else:
            kind = "warning"
        event = self.event(payload, state, kind, native, data)
        event.source = "hook"
        yield event

    def map_otel(self, logs: list[Json], metrics: list[Json]) -> Iterable[Event]:
        from tracebook.otel import map_records
        yield from map_records(self, logs, metrics)
