# Schema discovery

Discovery completed before product implementation on 2026-10-24. The repository
was empty apart from its license. No agent settings were changed during discovery.
Local files were read without running inference. Fixture content, identifiers,
commands and paths have been replaced; no original prompts are committed.

| Agent | Installed | Evidence | Validation |
|---|---|---|---|
| Claude Code | 2.1.286 | 9 local project JSONL files; official docs | locally observed |
| Codex CLI | 0.163.0 | 599 local rollout files; official docs | locally observed |
| Gemini CLI | absent | official docs and upstream source revision below | unverified locally |

## Corrections to the initial hypotheses

* Codex has `token_usage_record` keyed by response ID as well as cumulative
  `event_msg/token_count`. Counting both would duplicate usage. Prefer response
  records; retain legacy cumulative evidence without adding it again.
* Codex `event_msg/item_completed` also contains typed UserMessage,
  CommandExecution, McpToolCall, FileChange and SubAgentActivity records. These
  can duplicate response items and must enrich, rather than blindly append.
* Claude has `promptId`, `requestId`, split assistant blocks, thinking-token
  details and `cost-state` session snapshots. Session totals are not per-call
  costs and cannot be summed across snapshots.
* Claude's documented telemetry `cost_usd` is the agent's estimated API value,
  not an invoice. Preserve that distinction even in the `cost_reported` column.
* Gemini source now writes JSONL metadata, messages, `$set`, `$patch`, and
  `$rewindTo` records; older whole-session JSON remains supported. Docs still
  describe the common chats directory. Both source formats need coverage.
* `GEMINI_CLI_HOME` points to a parent directory containing `.gemini`, whereas
  `CODEX_HOME` and `CLAUDE_CONFIG_DIR` point directly at their configuration roots.
* Gemini persists tokens and tool executions without enabling telemetry.
* Reasoning is a subset of output for Codex and Claude; Gemini's thoughts are
  separate from candidate output. Canonical `output` means non-reasoning output
  so summing output plus reasoning never double-counts.
* No transcript format is a stable public API. Unknown records are retained as
  redacted raw evidence and counted; capability declarations distinguish partial
  and unverified coverage from tested end-to-end behavior.

## Evidence and limitations

See [Claude](agents/claude_code.md), [Codex](agents/codex.md), and
[Gemini](agents/gemini_cli.md). Public-source Gemini fixtures are synthetic,
schema-derived examples, not claims of a live installed Gemini test. Snapshots
of local files can start/end mid-turn; incomplete turns must stay unknown.
Timestamps are event timestamps, not an assurance of arrival order. The store
orders events when reading; it does not silently fabricate monotonic wall time.

The supplied specification ends in section 8. The implementation will use the
entities and constraints explicitly described in sections 0–7 and document any
remaining acceptance gaps instead of asserting production readiness prematurely.

## Additional discovery

Saved fixture directories identify the transcript's own version, which differs
from the current binary: Claude 2.1.285 and Codex 0.164.0. Provenance preserves both
values. Gemini's telemetry outfile uses concatenated pretty SDK JSON objects, not
JSONL. OpenCode current upstream uses session_v2/session_message SQLite tables,
not the original JSON-session hypothesis; see [its discovery](agents/opencode.md).
