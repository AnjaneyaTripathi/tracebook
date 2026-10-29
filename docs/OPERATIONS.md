# Configuration and operations

## State and privacy

Default state is `~/.tracebook`: config/pricing TOML, a WAL SQLite database,
short-lived hook spool, report artifacts and a daemon lock. Directories are 0700;
database, spool, config, backups and reports are 0600 on POSIX. Permissions are
best-effort on Windows. Local processes running as your user can read local data.
No agent transcript is modified. The SQLite adapter uses read-only online backup
to include committed WAL data before parsing a temporary copy.

Hooks are stdlib-only Python launched with `-S`, append a bounded envelope and
always exit 0 without stdout/stderr. The daemon performs JSON parsing and uniform
redaction before either raw or canonical database persistence. The spool temporarily
contains unredacted source content until acknowledged and removed. If the daemon
is stopped, spool files accumulate privately for the next start. Do not publish
the state directory or agent configuration backups.

Built-in redaction masks credential-shaped keys, common API-token formats, bearer
tokens, private PEM keys, secret assignments and URL user/password pairs. Pattern
redaction cannot recognize every possible secret or personal detail. Add regexes
with `redact_patterns = ['your-pattern']`. `capture_content = false` suppresses
known prompt/tool-content fields while retaining numeric usage. This setting
affects new persistence; it does not retroactively erase existing records.

There is no default LLM summarizer. No context-injected hook text, model request,
or automatic telemetry export to a vendor is added by Tracebook. Existing agent
telemetry settings remain under your control. Codex's existing notify command is
preserved as argv and dispatched asynchronously with the original JSON argument;
its output is muted to preserve the silent-hook contract.

## Configuration

Copy choices from [examples/config.toml](../examples/config.toml). Both ports must
be distinct unprivileged ports. Polling defaults to two seconds, source discovery
to thirty seconds. Active files poll every two seconds; unchanged historical files
are checked at the discovery interval, so a resumed cold transcript may take up
to thirty seconds to appear. Hooks continue to drain on the fast poll. `metadata_env = ['MY_RUN_LABEL']` explicitly selects wrapper
environment metadata; no variables are collected by default. Child-process trees
are sampled every 500 ms on POSIX and are best-effort; short-lived processes can
be missed. Wrapper status always says `process_only`, without invented tokens.

Set `timezone = 'Asia/Kolkata'`, for example, and `report_hour = 23`. Date-only
ad hoc boundaries are UTC; explicit ISO offsets are also accepted. End boundaries
are exclusive. Daily scheduled boundaries use local wall time, including DST.
The scheduler persists its cursor and catches up at most 31 windows per pass.

Default delivery is a private local `delivered/` directory. HTTPS webhook and
STARTTLS SMTP delivery require explicit enabled settings. Credentials are read
from named environment variables and are never stored in report errors. Ensure
your service manager passes those variables if you enable external delivery.
Concurrent daemon/CLI deliveries claim outbox entries atomically; abandoned
claims become retryable after five minutes. Retries use durable exponential
backoff, capped at a day. The outbox does not send
a previously acknowledged report twice merely because its window was rerun.
Webhook receivers should honor `Idempotency-Key`. SMTP gets a stable Message-ID,
but external delivery is at-least-once: an acknowledgement lost during a crash
can cause a retry. SMTP cannot provide universal exactly-once delivery.

## Pricing and interpretation

Pricing is **off by default**. Costs that agents report themselves are always
shown, labelled as agent-reported. With pricing off, Tracebook computes no
estimates: estimated values, "unpriced" counts and the missing-rate finding do
not appear, and `pricing.toml` is not created. Enable estimates explicitly with
`pricing_enabled = true` in `config.toml` and restart the daemon. Stored
estimates are kept and reappear when it is enabled.

`pricing.toml` is intentionally empty initially: unknown model cost stays NULL.
Enter verified USD-per-million rates under `[models.'provider/model']`, aliases,
optional cache/reasoning rates and long-context tiers using
[examples/pricing.toml](../examples/pricing.toml). Set `as_of` to the verified date.
Tracebook does not fetch or guess rates. Run `tracebook reanalyze` after edits.
The example includes official Standard API rates verified on 2026-10-25 for
seven named GPT/Claude models, source URLs and per-call long-context tiers.
Copy only the entries you want; preserve any existing negotiated rates. This
revalues historical usage at the rate card's date, rather than reconstructing
historical invoices. `cache_write_by_ttl` maps durations in seconds to rates;
canonical `cache_write_tokens_by_ttl` partitions writes without double-counting.
Replay Claude sources with `backfill --agent claude_code --reset` to recover TTL
metadata from transcripts captured before this field was supported.
Reports include per-model token categories, estimated/reported values, unpriced
record counts and pricing provenance. Standard estimates exclude service-tier
and regional modifiers, taxes, provider tool fees and account discounts.
Nonzero token buckets without a known applicable rate remain unpriced. Agent
reported cost takes precedence in totals; estimated cost is shown separately and
only fills calls without reported cost. Preserve original provenance even when an
agent's reported value is itself an estimate. Subscription usage is API-equivalent
value, not billed spend; select `billing_mode = 'api' | 'subscription' | 'mixed'`.

## Generic OTLP and imports

Unknown OTLP service names use the generic adapter. Save a mapping under
`[services.'your-service'.fields]` in `~/.tracebook/otel-mappings.toml`; see
[the example](../examples/otel-mappings.toml). Resource and log attributes are
flattened; literal dotted keys take precedence over nested-path lookup. `_body`,
`_event_name`, `_time_ns` are available from the OTLP envelope. Map an ISO timestamp
attribute to `ts` or omit it to retain an unknown timestamp. The same declarative
`fields` and `kinds` mapping is accepted for JSONL/CSV import. Token fields must
already follow canonical disjoint semantics; do not map a cumulative metric to
a per-call counter. Use a dedicated adapter for more complex transformations.

## Maintenance

`doctor` reports install/config availability and capabilities. `/api/health`
shows capture errors without raw transcript contents. The Overview parse-warning
count is useful after an agent upgrade. Unknown records stay in `raw_payloads`.
`backfill` checkpoints by inode and offset and resumes after interruption;
`backfill --reset` replays source files, still deduplicating canonical IDs.
`--since` limits normalized records, while redacted raw evidence may still include
older records encountered in a selected file. Do not infer a retention policy
from this option. Retention/deletion automation is not included in 0.1.

Back up with SQLite's `.backup` command or stop the daemon and copy the whole
state directory. Never copy only the database file while ignoring an active WAL.
Migration versions newer than this package are refused. Agent configuration
updates are backed up, preserve existing settings/TOML comments, refuse malformed
files and symlinks, and support dry-run. JSON files with comments are refused
instead of silently discarding comments. Backups can contain user secrets.

To stop/uninstall:

```sh
# macOS
launchctl bootout gui/$(id -u)/local.tracebook
# Linux
systemctl --user disable --now tracebook
tracebook uninstall-hooks --apply
```

Remove the generated user-service definition if no longer wanted. Retain or
delete your private state directory according to your own retention needs.
Uninstall only removes Tracebook-owned hook entries and restores chained notify.
The web server rejects unexpected Host/Origin headers and uses a restrictive CSP;
do not expose it through a reverse proxy or change its loopback binding.
