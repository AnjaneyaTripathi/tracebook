"""Versioned, vendor-neutral capture contract."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

Json = dict[str, Any]
Support = Literal["yes", "no", "partial"]
KINDS = frozenset({"session", "turn_start", "turn_end", "model_call", "tool_call", "tool_result", "permission", "compaction", "subagent", "metric", "process", "warning", "lifecycle"})
TOOLS = frozenset({"shell", "file_read", "file_write", "file_edit", "file_search", "web_fetch", "web_search", "subagent", "mcp", "notebook", "todo_plan", "ask_user", "other"})
STATUSES = frozenset({"completed", "interrupted", "error", "compacted", "unknown"})
TOKEN_FIELDS = ("input_uncached", "input_cache_read", "input_cache_write", "output", "reasoning", "total_reported")


def digest(*parts: object) -> str:
    return hashlib.blake2b(json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode(), digest_size=16).hexdigest()


def utc(value: object = None) -> str:
    """Normalize source times; invalid/missing times have a deterministic sentinel."""
    try:
        if isinstance(value, (int, float)):
            stamp = value / 1000 if value > 1e11 else value
            dt = datetime.fromtimestamp(stamp, UTC)
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        else:
            dt = datetime(1970, 1, 1, tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        dt = datetime(1970, 1, 1, tzinfo=UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now() -> str:
    return utc(datetime.now(UTC).isoformat())


def obj(value: object) -> Json:
    return value if isinstance(value, dict) else {}


def text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(v.get("text", "")) for v in value if isinstance(v, dict))
    return ""


def number(value: object) -> int | None:
    try:
        return max(0, int(str(value))) if value is not None and not isinstance(value, bool) else None
    except (ValueError, OverflowError):
        return None


@dataclass(slots=True)
class Event:
    agent: str
    session_id: str
    kind: str
    ts: str
    native_id: str
    turn_id: str | None = None
    source: str = "transcript"
    data: Json = field(default_factory=dict)
    clock_source: str = "agent_wall"
    schema_version: int = 1

    @property
    def id(self) -> str:
        return digest(self.agent, self.session_id, self.kind, self.native_id)

    def to_dict(self) -> Json:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Capabilities:
    transcripts: Support = "no"
    hooks_pre_tool: Support = "no"
    hooks_post_tool: Support = "no"
    hooks_prompt_submit: Support = "no"
    hooks_turn_end: Support = "no"
    otel_logs: Support = "no"
    otel_metrics: Support = "no"
    reported_cost: Support = "no"
    token_input: Support = "no"
    token_output: Support = "no"
    token_cache_read: Support = "no"
    token_cache_write: Support = "no"
    token_reasoning: Support = "no"
    tool_durations: Support = "no"
    subagents: Support = "no"
    permission_events: Support = "no"
    model_name_per_call: Support = "no"
    prompt_text: Support = "no"
    tool_io_content: Support = "no"
    session_resume_detectable: Support = "no"


@dataclass(frozen=True, slots=True)
class Installation:
    binary: str
    version: str
    config_dir: str
    data_dir: str


@dataclass(frozen=True, slots=True)
class Source:
    path: str
    format: str = "jsonl"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Diff:
    path: str
    text: str
    changed: bool

