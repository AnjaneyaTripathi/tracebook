# Writing an adapter

1. Record the installed agent version and inspect official documentation and real
   local files before implementing a parser. If absent, pin upstream evidence and
   label the adapter and fixtures unverified. Never commit real prompts or secrets.
2. Run `tracebook adapter new example_agent`. Install the generated package into
   the same environment as Tracebook. It registers a `tracebook.adapters` entry
   point; core, analysis, reporting and UI code need no changes.
3. Subclass `tracebook.adapters.base.Adapter` (or a related adapter). Implement
   `root`, `capabilities`, `transcript_sources`, and `parse_record`. Override
   `detect`, `install_hooks`, `uninstall_hooks`, `parse_hook`, `map_otel`,
   `parse_otel_file`, `database_records` or `otel_setup` where needed. Unsupported
   defaults do not install hooks. Installers belong to the adapter and must back
   up, preserve user settings, validate and support idempotent dry-run/uninstall.

`Source(path, format)` supports jsonl, whole-session json, concatenated SDK JSON
(`otel_file`), and sqlite. SQLite adapters receive a temporary snapshot connection
in `database_records`; they never receive a writable source database. Persist
only bounded, serializable parser/checkpoint state. For changed-row SQLite capture,
use a watermark that also replays same-timestamp updates. Schema changes must yield
warnings rather than silently reading unrelated tables.

Emit `Event(agent, session_id, kind, ts, native_id, turn_id=..., data=...)`. Use
native stable identifiers; a content hash is the fallback. For revisions, reuse
the ID. Supply aliases when one logical call has several source IDs. Timestamp
strings are UTC; never silently treat a local timestamp as UTC without evidence.
Document prompt boundaries, resumed sessions, compaction, errors, denials and
subagents. No missing counter is zero. Token input/cache/output/reasoning buckets
must be disjoint and non-negative; ambiguity belongs in `token_semantics_confidence`.

Tools use `tool`, `native_name`, normalized `input`, result `output`, `status`, and
optional `duration_ms`. Keep the same native ID for tool_call/tool_result. Include
command, file_path, pattern or url in normalized inputs where available. Detectors
must not require a vendor-specific tool name. Explicit sidechain instructions may
use `kind='sidechain'` so they do not inflate main user-prompt counts.

OTLP resource service names route via `otel_services`. Either declare semantic
`otel_token_fields` with inclusion flags or override `map_otel`. Preserve metrics
as metrics; do not sum a cumulative session metric as per-call usage. Third-party
plugins execute trusted Python in the daemon; install only plugins you trust.

## Conformance

```python
from pathlib import Path
from tracebook.testing import conform
from my_adapter import MyAdapter

events = conform(MyAdapter(), [Path('tests/fixtures/version/session.jsonl')])
```

The kit checks deterministic replay IDs, collision-free identities, canonical
vocabulary, UTC ordering of the normalized read view, non-negative tokens, linked
tool results and idempotent redaction. Revisions may reuse an ID. For partial
snapshots with intentionally missing tool calls use `complete=False` and document
why. Parsers are allowed to receive out-of-order source records; never falsify
timestamps merely to pass the ordered-view check. Also test actual source replay,
file/DB updates, unknown records, provenance merging and every capability declared
`yes`. `partial` must describe the missing evidence or version dependency.

All persistence passes through Store's redactor. For third-party tests include
credential-shaped content and assert it is absent from both raw and normalized
tables. Hooks must be tested as subprocesses for return code 0 and empty stdout
and stderr, including malformed/large payloads and an unwritable state directory.

Regenerate the capability document with
`tracebook adapter capabilities --output docs/CAPABILITY_MATRIX.md`.
