# OpenAI Codex CLI

Observed binary: **0.163.0**. `$CODEX_HOME` defaults to `~/.codex`;
user config is `config.toml`, rollouts are `sessions/**/*.jsonl` (also inspect
archived_sessions). `history.jsonl` is not the rich rollout. 599 session files
were present. Sanitized observed records are in `tests/fixtures/codex/0.164.0/`.

## Records and boundaries

Envelope: timestamp, type, payload; newer records also have ordinal.
`session_meta` has id/session_id, cwd, cli_version, source, model_provider, git.
`turn_context` gives turn_id, model and cwd. `task_started`/`task_complete` and
`turn_aborted` event payloads define turn lifetime; newer records have explicit
root_turn_id, parent_turn_id, attribution and millisecond durations.
`response_item/message` includes user content but also system/developer messages.
Only actual user instructions start prompts. Built-in commands are command turns.
Compacted and ContextCompaction records are never user turns.

Function/custom tool calls have call_id, name, namespace, arguments/input;
outputs reference call_id and can be strings or blocks. Newer item_completed
records contain typed UserMessage, AgentMessage, CommandExecution, FileChange,
McpToolCall, CollabAgentToolCall, SubAgentActivity and other records. They may
duplicate response events. Prefer native call IDs and use completion details
to enrich known calls. Unknown tool names retain their native spelling.
Inter-agent messages are not user turns. Resumed history can repeat IDs.

## Usage

Current `token_usage_record`: response_id, turn_id, usage, turn_token_usage,
thread_token_usage. Use `usage` once per response_id. Legacy token_count has
last_token_usage and total_token_usage; derive nonnegative deltas from cumulative
totals and ignore repeated snapshots. Do not add legacy counters where response
records exist. Input includes cached input; subtract cached_input_tokens and
cache_write_input_tokens to obtain uncached. Reasoning_output_tokens is within
output_tokens; subtract to obtain non-reasoning output. Preserve total_tokens.
Absent counters are null. Unexpected relationships lower confidence.
No per-call cost was observed. Unknown prices remain null.

## Notify, telemetry and config

`notify` is a single argv command list; Codex appends one JSON argument containing
type=agent-turn-complete, thread-id, turn-id, cwd, input-messages and
last-assistant-message. It does not provide pre-tool events. Preserve an existing
notify command through call-through; never overwrite it. TOML editing must
preserve comments. Tracebook's own notification output is always empty.

`[otel] log_user_prompt=false` and an `otlp-http` exporter use `/v1/logs`,
protocol `binary` or `json`. Service metadata includes conversation.id, model,
app.version and originator. Documented events include codex.api_request,
codex.sse_event, codex.user_prompt, codex.tool_decision and codex.tool_result.
Metrics support is documented but depends on analytics settings; enabling
analytics solely for Tracebook would conflict with the local-only intention.

## Capability evidence

Transcripts, tokens including reasoning, tool content, prompts and per-turn
models: observed. Completion notify and OTel: documented. Pre-tool/post-tool/
prompt-submit hooks: false for the notify mechanism. Duration, permission,
subagents, cache-write, resume detection and per-call models: partial.
Reported cost: false absent upstream evidence.

Sources checked 2026-10-24:
[advanced config](https://learn.chatgpt.com/docs/config-file/config-advanced),
[config reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Full-history discovery follow-up

A full local backfill exposed additional variants saved in `additional-records.jsonl`
(all text, paths and identifiers replaced): mcp_tool_call_end has call_id, invocation
(server/tool/arguments), duration (secs/nanos), and result.Ok.content/isError or an
error alternative. patch_apply_end carries call_id, changes, stdout/stderr, success
and status. web_search_end has call_id and action/query. sub_agent_activity has
agent_thread_id, event_id, occurred_at_ms and kind. Response-item tool_search_call
and tool_search_output share call_id. Typed Plan is lifecycle evidence; typed
FunctionCallOutput supplies id/name/namespace/output. thread_rolled_back records
num_turns and must not erase already-captured evidence. These observations extend
the initial sampled schema; unknown variants continue to be retained as warnings.
