# OpenCode — unverified locally

Discovery: binary absent on this machine (2026-10-25). No local OpenCode database exists.
This adapter targets the current upstream v2 SQLite schema at revision
`097a93e511e2cd7d4fbfc12e0623b721b02f23b9`. Fixtures are synthetic schema-derived
examples, not observed agent executions. Older releases have incompatible storage;
unrecognized database layouts are retained as warnings rather than guessed.

Official troubleshooting documentation places data under `$XDG_DATA_HOME/opencode`
(default `~/.local/share/opencode`), with `opencode.db`. User configuration is JSON/JSONC;
this adapter does not modify it. The CLI has a session export command, but native v2
SQLite is the automatic capture source in this release. Plugins provide in-process
application events; no command-hook or OTel integration is installed here.

## Observed upstream schema

`session_v2`: id, directory, version, parent_id, fork_session_id, timestamps, cumulative
cost/token counters. `session_message`: id, session_id, type, seq, time_created,
time_updated, JSON data. Data omits the id/type held in columns. Never sum session counters.
Messages include user, assistant, compaction, synthetic, system, shell, skill and switching
notifications. Assistant content combines text, reasoning and tools. Tool state is streaming,
running, completed or error, with separate creation/run/completion millisecond timestamps.
Model references contain providerID and id. Tool errors have state.error; assistant errors
have error. Unknown permission/interrupt payloads remain unclassified evidence.

User messages start canonical turns; assistant finish=stop/length or error ends them.
Compaction/synthetic/system records never start user prompts. A parent session is a
sidechain, with its own session identity; forks mark resumed history. Input, output,
reasoning and both cache buckets are disjoint, as confirmed by upstream TokenUsage.total.
Cost is agent-reported API-equivalent value, not billed spend. Missing counters stay null.

## Capabilities

Partial/unverified: transcripts, reported cost, all five token buckets, tool durations,
subagents, per-call model, prompt/tool content, resume detection. No command hooks, OTel,
or dedicated permission events. The generated matrix is authoritative for the adapter's
current declaration. Tests build a WAL-mode SQLite fixture from the checked schema and
exercise live changes, replay and unknown schemas through the generic read-only snapshot
framework. Source connections use mode=ro and SQLite online backup; no agent tables are written.

Sources: [storage](https://opencode.ai/docs/troubleshooting/),
[CLI/export](https://opencode.ai/docs/cli/),
[SQL schema](https://github.com/anomalyco/opencode/blob/097a93e511e2cd7d4fbfc12e0623b721b02f23b9/packages/core/src/session/sql.ts),
[message schema](https://github.com/anomalyco/opencode/blob/097a93e511e2cd7d4fbfc12e0623b721b02f23b9/packages/schema/src/session-message.ts),
[token semantics](https://github.com/anomalyco/opencode/blob/097a93e511e2cd7d4fbfc12e0623b721b02f23b9/packages/schema/src/token-usage.ts).
