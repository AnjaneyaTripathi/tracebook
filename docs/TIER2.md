# Tier 2 discovery and scope

None of the Tier 2 binaries checked on 2026-10-25 was installed: opencode, aider,
goose, copilot, qwen, amp, cursor-agent/agent and droid. No claim of live compatibility
is made for them. Cline/Roo extension storage was not inspected.

OpenCode was selected for implementation because its current upstream SQLite
schema exposes stable session/message IDs, tools, per-response tokens and costs.
Its standalone adapter and synthetic fixtures are documented in
[agents/opencode.md](agents/opencode.md). Legacy OpenCode databases are explicitly
unsupported until versioned fixtures are added; they produce a warning.

| Agent | 0.1 status and reason |
|---|---|
| OpenCode | Implemented current v2 SQLite adapter; upstream-schema verified, locally unverified |
| Aider | Deferred. Official docs expose Markdown chat/input history and optional LLM history; exact token/tool reconciliation needs separate discovery |
| Goose | Deferred. Official docs identify local SQLite session storage; exact versioned schemas and fixtures have not yet been inspected |
| Cline / Roo Code | Deferred; extension storage and version discovery still required |
| GitHub Copilot CLI | Deferred; no local install or inspected schema |
| Qwen Code | Deferred; no assumption that Gemini-derived formats are identical |
| Amp | Deferred; no local install or inspected schema |
| Cursor Agent CLI | Deferred; no local install or inspected schema |
| Droid | Deferred; no local install or inspected schema |

These deferrals do **not** mean the agents expose no parseable data. The generic
wrapper, OTLP mapping and JSONL/CSV import remain available for them. Adding a
proper adapter requires only a new plugin plus discovery evidence and fixtures.

Sources: [Aider configuration](https://aider.chat/docs/config/options.html),
[Aider history](https://aider.chat/docs/faq.html),
[Goose sessions](https://github.com/aaif-goose/goose/blob/main/documentation/docs/guides/sessions/session-management.md).
