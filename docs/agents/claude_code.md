# Claude Code

Observed binary: **2.1.286**. Local transcripts: `$CLAUDE_CONFIG_DIR/projects/**/*.jsonl`,
default `~/.claude/projects`; settings: root `settings.json`. Discovery found
9 files. `tests/fixtures/claude_code/2.1.286/observed.jsonl` contains sanitized
local records, including repeated split assistant message IDs.

## Transcript contract

Top-level `type`, `uuid`, `parentUuid`, `timestamp`, `sessionId`, `cwd`, `version`
are optional by record kind. User messages may have `promptId`; content is a
string or content-block list. Tool results are **user** messages containing
`tool_result` blocks and must not start a turn. Assistant blocks carry
`message.id`, `model`, `content`, `usage`, `stop_reason`, and sometimes `requestId`.
Merge usage by message/request ID, taking the latest values. `isSidechain` and
`agentId` identify auxiliary work where present. Preserve parent relationships;
never count sidechain work as a new main-thread user prompt.

A turn begins with a non-meta user instruction (not tool output), preferably
keyed by `promptId`, falling back to `uuid`. `end_turn`, Stop, or `turn_duration`
finish it. `tool_use` is not completion. Compaction markers are lifecycle events.
Tool failures have `is_error`; denials can be hook/telemetry decisions. Missing
completion and abort data remain unknown, not successful.

Observed extra records: mode, permission-mode, system/informational,
system/turn_duration, file-history-snapshot, file-history-delta, cost-state,
last-prompt, atis-latch, attachment, ai-title and queue-operation. Unknown types
are retained and counted rather than rejected. `cost-state.totalCostUSD` is a
cumulative session snapshot; never sum snapshots or spread them across calls.

## Tokens and value

`input_tokens` excludes cache reads and writes; use separate
`cache_read_input_tokens` and `cache_creation_input_tokens`. If
`output_tokens_details.thinking_tokens` exists, subtract it from output and store
it as reasoning. Otherwise reasoning is null. Cache TTL breakdowns and iterations
are extra evidence, not additional usage. Telemetry `cost_usd` is reported by
the agent but explicitly estimated by Claude; it is API-equivalent value.

## Hooks and telemetry

Command hooks receive stdin JSON: session_id, prompt_id (version dependent),
hook_event_name, cwd, transcript_path; tool hooks include tool_name, tool_input,
tool_use_id, and results. Hooks can inject output into context. Tracebook emits
no output, decisions, or model prompts and always exits zero.

Local OTel uses `CLAUDE_CODE_ENABLE_TELEMETRY=1`,
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`,
`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_LOGS_EXPORTER=otlp`,
`OTEL_METRICS_EXPORTER=otlp`. Keep prompt/tool/response content flags off.
Resource service `claude-code`; events `claude_code.*`. Correlation fields:
session.id, prompt.id, message.uuid, request_id, tool_use_id, event.sequence.
Sequence is per process, so it can reset within a resumed session.

## Capability evidence

Transcripts, input/output/cache read/cache write, model, prompt and tool content:
observed. Reasoning, subagents, resume, errors and tool durations: partial.
Pre/post tool, prompt-submit, turn-end, permission hooks, OTel logs/metrics and
reported API value: documented, with synthetic hook/OTLP fixtures required.
Missing capabilities must be visible in reports and the generated matrix.

Sources checked 2026-10-24:
[hooks](https://code.claude.com/docs/en/hooks),
[monitoring](https://code.claude.com/docs/en/monitoring-usage),
[settings](https://code.claude.com/docs/en/settings).
