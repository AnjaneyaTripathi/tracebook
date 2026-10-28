# Tracebook

Local diagnostics for AI coding agents: prompts, model usage, tools, failures,
evidence-linked findings and nightly reports in one private dashboard.

Tracebook makes **no model calls and adds zero model tokens**. Runtime network
listeners bind to `127.0.0.1`. Outbound report delivery is opt-in; the default is
local Markdown/JSON files. No analytics, remote fonts, pricing downloads or cloud sync.

## Install and run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). macOS and Linux are the
primary platforms. A built dashboard is included in the wheel and this checkout.

```sh
uv sync --locked
uv run tracebook init
uv run tracebook doctor
uv run tracebook install-hooks             # review the config diff
uv run tracebook install-hooks --apply      # backs up existing agent settings
uv run tracebook backfill                  # resumable; safe to repeat
uv run tracebook daemon
```

Open **http://127.0.0.1:8742**. Keep the checkout and its `.venv` at their installed
paths: hooks and service definitions use absolute executable paths. For a stable
installation independent of a checkout, build the wheel and install it with
`uv tool install dist/tracebook-0.1.0-py3-none-any.whl`, then reinstall hooks/service.
Restart agent sessions after changing their hook configuration.

Use `TRACEBOOK_HOME` or the global `--home DIRECTORY` option to relocate state.
Installed hooks explicitly retain the selected home. Source roots honor
`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`, and `XDG_DATA_HOME` where applicable.

## Agent coverage

| Adapter | Capture | Verification |
|---|---|---|
| Antigravity | Brain JSONL transcripts, hooks.json | Local binary 1.4.8; saved transcripts 1.4.8 |
| Claude Code | JSONL transcripts, silent hooks, OTLP | Local binary 2.1.286; saved transcripts 2.1.285 |
| OpenAI Codex | Rollouts, chained notify, OTLP | Local binary 0.163.0; saved transcripts 0.164.0 |
| Gemini CLI | JSON/JSONL sessions, hooks, OTLP and telemetry outfile | Fixture-tested against pinned upstream source; agent not installed locally |
| OpenCode | Current v2 SQLite sessions through read-only snapshots | Fixture-tested against pinned upstream source; agent not installed locally |
| Generic | Mapped JSONL/CSV, mapped OTLP, process wrapper | Integration-tested |

Capabilities differ. Every report and the Coverage screen show those differences.
Open a session and select **Inspect prompt activity** to see that prompt's model
calls, token categories, reported/estimated costs, duration and associated tools.
See [schema discovery](docs/SCHEMA_DISCOVERY.md), the generated
[capability matrix](docs/CAPABILITY_MATRIX.md), and [Tier 2 status](docs/TIER2.md).
Gemini and OpenCode are **not claimed to be live-verified**. Older unsupported
record types remain redacted raw evidence and increment parse warnings.

## Always-on service and reporting

```sh
uv run tracebook install-service            # inspect the user-service definition
uv run tracebook install-service --apply
# macOS:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.tracebook.plist
# Linux:
systemctl --user daemon-reload
systemctl --user enable --now tracebook
```

Edit `~/.tracebook/config.toml` to set your IANA `timezone`, `report_hour` (0–23),
and optional channels. Restart the daemon after changing configuration. Nightly
windows end at the configured local hour and handle DST. Missed windows catch up
after restart; identical windows and channel deliveries deduplicate locally.

```sh
uv run tracebook report --from 2026-10-24 --to 2026-10-25
uv run tracebook report --from 2026-10-24 --to 2026-10-25 --format json --send
uv run tracebook reanalyze
uv run tracebook otel-env --agent claude_code
uv run tracebook run -- your-agent --your-options
uv run tracebook import --agent generic --map examples/mapping.toml events.jsonl
```

`otel-env` prints adapter-owned setup instructions; it does not change telemetry
settings implicitly. HTTP/JSON and HTTP/protobuf OTLP are accepted on port 4318,
including gzip. Cumulative metrics are retained separately from per-call totals.
See [configuration and operations](docs/OPERATIONS.md) for pricing, delivery,
redaction, backups, troubleshooting and uninstall instructions.

## Development and validation

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check src tests scripts
uv run mypy --strict src
uv run pytest --cov=tracebook --cov-fail-under=85
cd frontend
npm ci
npm run build
npx playwright install chromium             # CI uses bundled Chromium
npm test                                   # local default: installed Chrome
```

The browser suite uses isolated, explicitly labeled example data. Run
`uv run python scripts/benchmark.py` for reproducible hook, ingestion and idle
measurements. Results and their limits are in [validation](docs/VALIDATION.md).
The private-repository CI checks Python on macOS/Linux and builds/tests the UI.

Adapters are Python entry-point plugins. `tracebook adapter new my_agent`
creates a separate installable package; [the adapter guide](docs/WRITING_AN_ADAPTER.md)
covers discovery, fixtures, canonical invariants and conformance tests.

This is a working 0.1 release with explicit coverage limits, not a promise that
every future agent format or arbitrary laptop will satisfy the measured targets.
See [architecture](docs/ARCHITECTURE.md) for reconciliation and durability details.
