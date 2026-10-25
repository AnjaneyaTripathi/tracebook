# Validation and limits

Local validation on macOS / Python 3.12.13:

- Strict mypy and Ruff pass.
- Python suite covers canonical reconciliation, all adapters, malformed records,
  replay/rotation/partial lines, WAL snapshots, hook silence, secrets, pricing,
  every detector, API/OTLP, scheduler/DST, outbox retries and daemon lifecycle.
- Coverage gate is 85% across the package, including branches; recent local run
  is approximately 92% with 65 tests. CI runs the same checks on Linux and macOS.
- Three Chromium/Chrome browser tests exercise desktop/mobile rendering, session
  filtering, tool IO, provenance, capabilities and report generation. Screenshots
  were inspected using isolated, labeled synthetic data. No runtime user data is
  embedded in the dashboard or committed as screenshots.
- Node dependency audit reported zero known vulnerabilities when installed.

## Reproducible benchmark

Run `uv run python scripts/benchmark.py`. Recorded 2026-10-25 on this Mac:

| Measurement | Result | Requested target |
|---|---:|---:|
| Hook process launch + write, p95 (120 samples) | 19.3 ms | <20 ms |
| Hook median | 17.64 ms | — |
| Transcript ingestion | 52.38 MB/s | >=50 MB/s |
| Idle daemon CPU, 5-second sample | 0.3% | <1% |
| Idle daemon RSS | 54.8 MiB | <150 MB |

The hook measurement includes Python process startup, validates return code 0 and
empty output, and uses the installed `-S` launcher. It is close to the target;
machine load and interpreter location matter. The ingestion benchmark sends
50.7 MB / 12,001 tool-output-heavy records through parsing, redaction and SQLite
WAL persistence. It is **not a guarantee of 50 MB/s for every record mix**: small
model/turn records with multiple projections cost more per byte. Idle measurements
use empty source directories; large-history live validation is separate. Benchmark
numbers are measurements, not universal hardware guarantees.

## Compatibility and unresolved limits

Claude/Codex schemas were sampled locally; Gemini/OpenCode were not installed and
remain fixture-verified only. Live Gemini hook/OTLP interoperability and Windows
behavior are not claimed verified. OpenCode handles the pinned current v2 SQLite
schema; older layouts surface warnings. Other Tier 2 adapters remain deferred.

Heuristic time-window reconciliation cannot prove identity when agents omit
correlation IDs. Ambiguous telemetry is excluded from totals and preserved as
evidence. Source data without prompt IDs, durations, permission outcomes or model
usage cannot supply complete per-prompt coverage. Capability notes make this visible.

The process wrapper cannot observe model tokens and may miss short-lived children.
UI session details cap turns/tools at 1,000; stored records remain accessible through
SQL and the paginated event API. Retention controls and an optional LLM summarizer
are not implemented. External SMTP/webhook acknowledgement failures can require an
at-least-once retry; use receiver idempotency where supported. The repository must
remain private; no public deployment, package publication or telemetry is configured.

## Live local installation

The initial full-history backfill processed 525,843 events from 609 sessions.
SQLite quick_check returned `ok`. The actual dashboard/OTLP daemon starts through
a user launchd job, reports a clean health endpoint, and generated a private local
nightly report. After loading this history its RSS measured 68.4 MiB. A concurrent
capture/reporting sample used 5.8% CPU; this is active workload, not the isolated
idle benchmark above. Source discovery subsequently gained a cold-file scan path
to avoid opening historical files on every two-second poll.

The full history exposed additional Codex variants, now documented and covered by
sanitized fixtures. Historical warnings from an earlier parser remain evidence;
a source replay may add newly understood canonical records without erasing old
warnings. Pricing remains unconfigured for locally observed private/unknown model
IDs; costs correctly remain unavailable until verified rates are supplied.
