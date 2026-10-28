# Antigravity

Observed binary: **1.4.8** (`agy`). Local transcripts:
`$ANTIGRAVITY_HOME/brain/<conversation_id>/.system_generated/logs/transcript.jsonl`
(default `~/.gemini/antigravity-cli/brain/...`); settings: root `settings.json`; hooks:
`~/.gemini/config/hooks.json`. Discovery found 8 local conversation sessions.
`tests/fixtures/antigravity/1.4.8/observed.jsonl` contains sanitized local records.

## Transcript contract

Records are newline-delimited JSON containing `step_index`, `source`, `type`, `status`,
and `created_at` (ISO 8601 UTC).

Record types:
* `USER_INPUT`: Human instruction (`source: USER_EXPLICIT`). Starts a turn. Content
  contains `<USER_REQUEST>` prompt blocks and optional `<USER_SETTINGS_CHANGE>`
  model selections.
* `PLANNER_RESPONSE`: Assistant output and tool decisions (`source: MODEL`). Includes
  `input_tokens`, `cache_read_tokens`, `output_tokens`, `thinking`, `content`, and
  optional `tool_calls`. When `tool_calls` is empty and `content` is present, finishes
  the turn.
* `GENERIC`: Tool execution result (`source: MODEL`). Contains `content` and optional
  `error`. Follows the sequential order of preceding `tool_calls`.
* `SYSTEM_MESSAGE`: System notifications (`source: SYSTEM`). Retained as lifecycle events.

## Tokens and metrics

* `input_tokens`: Uncached prompt input tokens.
* `cache_read_tokens`: Tokens read from context cache.
* `output_tokens`: Candidate output tokens.
* `thinking`: Free-form model thinking/reasoning text.

## Hooks and lifecycle

Antigravity executes lifecycle hooks defined in `hooks.json` (such as `~/.gemini/config/hooks.json`).
Supported events: `PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, `PreInvocation`, `PostInvocation`.
Antigravity expects JSON output on stdout (`{}` for diagnostics capture). Tracebook installs
a non-intrusive hook shim under the `"tracebook"` hook group that outputs `{}` and records
diagnostics to `spool/*.jsonl`.
