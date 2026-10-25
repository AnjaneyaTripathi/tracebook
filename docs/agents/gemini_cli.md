# Gemini CLI

**Not installed; unverified locally.** Fixtures are authored from official source
at revision `cd1b45c2db0a5ea7b7fc3b67c1ba5879601d6005` (checked 2026-10-24),
not copied from a real user session. No version number is invented.

Root is `${GEMINI_CLI_HOME:-$HOME}/.gemini`. Settings are JSON. Sessions are
`tmp/<project_hash>/chats/session-*`; current source writes JSONL, with older
JSON-per-session files still readable. Checkpoints in history are not equivalent
to full session recording. Recording works with telemetry disabled.

## Source schema

Conversation metadata: sessionId, projectHash, startTime, lastUpdated,
directories, kind (main/subagent). Legacy JSON has a messages array. New JSONL
contains metadata followed by message records, `$set` metadata updates,
`$patch` message updates and `$rewindTo` edits to active history. Retain original
events as historical evidence; rewinds are lifecycle events, not new prompts.

Messages: id, timestamp, type (user/gemini/info/error/warning), content (string
or Parts). Gemini messages can have model, tokens, thoughts and toolCalls.
Tools: id, name, args, result, status, timestamp, optional agentId.
User instruction begins a turn; model messages with tools do not finish it.
AfterAgent explicitly finishes; otherwise a final model text is an inferred
boundary. Missing finish after partial writes remains unknown.
Tool errors/cancellation follow status; error messages finish error turns.

Tokens: input=promptTokenCount (including cache), output=candidatesTokenCount,
cached=cachedContentTokenCount, thoughts=thoughtsTokenCount, tool=toolUsePromptTokenCount,
total=totalTokenCount. Uncached input is input minus cached; thoughts are separate
from output. Tool-use prompt tokens remain raw because they are not safely
attributable to one canonical billing bucket. Cost is unavailable unless an
explicit upstream telemetry field supplies it.

Hooks now exist: BeforeAgent/AfterAgent, BeforeTool/AfterTool, BeforeModel/
AfterModel, SessionStart/End, Notification, PreCompress. Command stdin JSON and
settings hook groups resemble Claude but event names and payloads differ.
Never emit stdout or stderr. Never register prompt/agent hooks that call models.

OTel: GEMINI_TELEMETRY_ENABLED=true, TARGET=local, OTLP_PROTOCOL=http,
OTLP_ENDPOINT=http://127.0.0.1:4318, LOG_PROMPTS=false, TRACES_ENABLED=false.
OUTFILE overrides the endpoint. Common fields include session.id; event names
start gemini_cli. Store cumulative metrics separately from per-call usage.

## Capability evidence

All available capabilities remain partial until verified against an installed
version. Source supports transcripts, hooks, logs/metrics, tokens, tools, models,
subagents and resume. No cache-write or reported-cost evidence. Capability
tests prove fixture handling only, not live installation interoperability.

Sources: [recording types](https://github.com/google-gemini/gemini-cli/blob/cd1b45c2db0a5ea7b7fc3b67c1ba5879601d6005/packages/core/src/services/chatRecordingTypes.ts),
[recording service](https://github.com/google-gemini/gemini-cli/blob/cd1b45c2db0a5ea7b7fc3b67c1ba5879601d6005/packages/core/src/services/chatRecordingService.ts),
[session management](https://geminicli.com/docs/cli/session-management/),
[hooks](https://geminicli.com/docs/hooks/reference/),
[telemetry](https://geminicli.com/docs/cli/telemetry/),
[configuration](https://geminicli.com/docs/reference/configuration/).
