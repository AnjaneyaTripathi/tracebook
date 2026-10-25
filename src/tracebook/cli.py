from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from tracebook.adapters import adapters
from tracebook.analysis import analyze
from tracebook.capture import Capture
from tracebook.config import Config
from tracebook.model import Event, now, utc
from tracebook.platform import atomic_write, private_dir
from tracebook.pricing import PriceBook
from tracebook.privacy import Redactor
from tracebook.reports import deliver, report
from tracebook.service import install_service
from tracebook.store import Store

app = typer.Typer(
    no_args_is_help=True, help="Local diagnostics for AI coding agents. No model calls."
)
adapter_app = typer.Typer(no_args_is_help=True)
app.add_typer(adapter_app, name="adapter")


@app.callback()
def main(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option(help="Private state directory; defaults to TRACEBOOK_HOME or ~/.tracebook"),
    ] = None,
) -> None:
    ctx.obj = Config.load(home.expanduser().resolve() if home else None)


def configuration(ctx: typer.Context) -> Config:
    config: Config = ctx.obj
    return config


def open_store(config: Config) -> Store:
    return Store(
        config.database, Redactor(config.redact_patterns, capture_content=config.capture_content)
    )


@app.command()
def init(ctx: typer.Context) -> None:
    """Initialize private storage. Never modify agent settings implicitly."""
    config = configuration(ctx)
    private_dir(config.root)
    for name in ("spool", "reports"):
        private_dir(config.root / name)
    if not (config.root / "config.toml").exists():
        atomic_write(
            config.root / "config.toml",
            'billing_mode = "mixed"\ntimezone = "UTC"\nreport_hour = 23\ncapture_content = true\n\n[delivery.file]\nenabled = true\n',
        )
    if not (config.root / "pricing.toml").exists():
        atomic_write(
            config.root / "pricing.toml",
            '# USD per million tokens. Add verified provider/model rates here.\nas_of = "'
            + datetime.now(UTC).date().isoformat()
            + '"\n[models]\n',
        )
    open_store(config).close()
    typer.echo(f"Initialized {config.root}")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Report installation, capture coverage and configuration checks."""
    config = configuration(ctx)
    result = {
        "home": str(config.root),
        "database_exists": config.database.exists(),
        "bind": "127.0.0.1",
        "ports": [config.port, config.otlp_port],
        "zero_model_tokens": True,
        "agents": {
            key: [asdict(check) for check in adapter.doctor()]
            for key, adapter in adapters().items()
        },
    }
    typer.echo(json.dumps(result, indent=2))


@app.command()
def daemon(ctx: typer.Context) -> None:
    """Serve the local dashboard, OTLP receiver and capture loop."""
    from tracebook.daemon import serve

    config = configuration(ctx)
    typer.echo(f"Tracebook http://127.0.0.1:{config.port} · OTLP 127.0.0.1:{config.otlp_port}")
    asyncio.run(serve(config))


@app.command("install-hooks")
def install_hooks(
    agent: Annotated[str, typer.Option()] = "all",
    apply: Annotated[
        bool, typer.Option("--apply", help="Write config after making a timestamped backup")
    ] = False,
) -> None:
    """Show a diff by default; --apply installs silent capture hooks."""
    registry = adapters()
    if agent != "all" and agent not in registry:
        raise typer.BadParameter("Unknown adapter")
    for key, adapter in registry.items():
        if key == agent or (agent == "all" and adapter.detect() is not None):
            diff = adapter.install_hooks(dry_run=not apply)
            typer.echo(diff.text or f"{key}: already configured")


@app.command("uninstall-hooks")
def uninstall_hooks(
    agent: Annotated[str, typer.Option()] = "all",
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Remove only Tracebook-owned entries and restore chained notify."""
    registry = adapters()
    if agent != "all" and agent not in registry:
        raise typer.BadParameter("Unknown adapter")
    for key, adapter in registry.items():
        if key == agent or (agent == "all" and adapter.root().exists()):
            diff = adapter.uninstall_hooks(dry_run=not apply)
            typer.echo(diff.text or f"{key}: no changes")


@app.command("install-service")
def service(ctx: typer.Context, apply: Annotated[bool, typer.Option("--apply")] = False) -> None:
    """Generate a launchd/systemd user service. Does not start it."""
    diff = install_service(configuration(ctx), dry_run=not apply)
    typer.echo(diff.path + "\n" + diff.text)
    if apply:
        typer.echo(
            "Service installed. Enable it with launchctl bootstrap gui/$(id -u) <plist> on macOS, or systemctl --user enable --now tracebook on Linux."
        )


@app.command()
def backfill(
    ctx: typer.Context,
    agent: Annotated[str, typer.Option()] = "all",
    since: Annotated[str | None, typer.Option()] = None,
    reset: Annotated[
        bool, typer.Option(help="Replay file checkpoints; canonical records still deduplicate")
    ] = False,
) -> None:
    """Resume ingestion of local transcripts, with progress."""
    config = configuration(ctx)
    registry = adapters()
    if agent != "all" and agent not in registry:
        raise typer.BadParameter("Unknown adapter")
    store = open_store(config)
    capture = Capture(store, registry)
    capture.since = utc(since) if since else None
    total = 0
    try:
        for key, adapter in registry.items():
            if agent != "all" and key != agent:
                continue
            if reset:
                store.db.execute("DELETE FROM adapter_state WHERE agent=?", (key,))
            sources = adapter.transcript_sources()
            for index, source in enumerate(sources, 1):
                try:
                    while True:
                        processed = capture.tail(adapter, source)
                        total += processed
                        if not processed or source.format in {"json", "otel_file"}:
                            break
                except (OSError, ValueError) as exc:
                    typer.echo(f"{key}: skipped unreadable source ({type(exc).__name__})", err=True)
                if index % 25 == 0 or index == len(sources):
                    typer.echo(f"{key}: {index}/{len(sources)} files, {total} events processed")
        PriceBook(config.root / "pricing.toml").apply(store)
        findings = analyze(store)
        typer.echo(f"Backfill complete: {total} events processed; {findings} findings.")
    finally:
        store.close()


@app.command("import")
def import_data(
    ctx: typer.Context,
    file: Path,
    mapping: Annotated[Path, typer.Option("--map")],
    agent: Annotated[str, typer.Option()] = "generic",
) -> None:
    """Import JSONL/CSV using an explicit TOML field map."""
    if agent != "generic":
        raise typer.BadParameter("Use --agent generic with a mapping file")
    store = open_store(configuration(ctx))
    try:
        typer.echo(f"Imported {Capture(store, adapters()).import_file(file, mapping)} events")
    finally:
        store.close()


@app.command()
def reanalyze(ctx: typer.Context) -> None:
    """Recalculate local prices and rule findings from canonical evidence."""
    config = configuration(ctx)
    store = open_store(config)
    try:
        PriceBook(config.root / "pricing.toml").apply(store)
        typer.echo(f"Recomputed {analyze(store)} findings")
    finally:
        store.close()


@app.command("report")
def make_report(
    ctx: typer.Context,
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
    send: Annotated[bool, typer.Option(help="Enqueue configured delivery channels")] = False,
    format: Annotated[str, typer.Option()] = "markdown",
) -> None:
    """Generate an idempotent ad hoc report (last 24h by default)."""
    config = configuration(ctx)
    store = open_store(config)
    try:
        result = report(
            store,
            config,
            adapters(),
            start or utc((datetime.now(UTC) - timedelta(days=1)).isoformat()),
            end or now(),
            enqueue=send,
        )
        typer.echo(json.dumps(result, indent=2) if format == "json" else result["body"])
        if send:
            typer.echo(f"Delivered {deliver(store, config)} queued reports")
    finally:
        store.close()


@app.command("otel-env")
def otel_env(agent: Annotated[str, typer.Option()]) -> None:
    """Print local-only OTel configuration with content logging disabled."""
    if agent == "claude_code":
        values = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOG_USER_PROMPTS": "0",
            "OTEL_LOG_TOOL_DETAILS": "0",
            "OTEL_LOG_ASSISTANT_RESPONSES": "0",
        }
    elif agent == "gemini_cli":
        values = {
            "GEMINI_TELEMETRY_ENABLED": "true",
            "GEMINI_TELEMETRY_TARGET": "local",
            "GEMINI_TELEMETRY_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "GEMINI_TELEMETRY_OTLP_PROTOCOL": "http",
            "GEMINI_TELEMETRY_LOG_PROMPTS": "false",
            "GEMINI_TELEMETRY_TRACES_ENABLED": "false",
        }
    elif agent == "codex":
        typer.echo(
            '# Merge into user config.toml; do not create a duplicate [otel] table.\n[otel]\nlog_user_prompt = false\nexporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }\n# Existing analytics settings are left unchanged.'
        )
        return
    else:
        raise typer.BadParameter("Select claude_code, codex or gemini_cli")
    typer.echo("\n".join(f"export {key}={value}" for key, value in values.items()))
    typer.echo("# Content logging is disabled. Transcript capture still follows capture_content.")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(ctx: typer.Context) -> None:
    """Record a child process: tracebook run -- command args..."""
    command = ctx.args
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise typer.BadParameter("Provide a command after --")
    store = open_store(configuration(ctx))
    identity = str(uuid.uuid4())
    started = time.monotonic_ns()
    timestamp = now()
    code = 127
    try:
        with store.transaction():
            store.ingest(
                Event(
                    "generic",
                    identity,
                    "session",
                    timestamp,
                    identity,
                    data={"cwd": os.getcwd(), "coverage": "process_only"},
                    source="process",
                )
            )
            store.ingest(
                Event(
                    "generic",
                    identity,
                    "turn_start",
                    timestamp,
                    identity,
                    identity,
                    "process",
                    {"prompt": " ".join(command), "coverage": "process_only", "kind": "command"},
                )
            )
            store.ingest(
                Event(
                    "generic",
                    identity,
                    "process",
                    timestamp,
                    identity + ":start",
                    identity,
                    "process",
                    {
                        "command": command,
                        "cwd": os.getcwd(),
                        "coverage": "process_only",
                        "monotonic_ns": started,
                    },
                )
            )
        try:
            child = subprocess.Popen(command)
            try:
                code = child.wait()
            except KeyboardInterrupt:
                child.send_signal(2)
                code = child.wait()
        except OSError as exc:
            typer.echo(str(exc), err=True)
        with store.transaction():
            duration = (time.monotonic_ns() - started) / 1e6
            status = "completed" if code == 0 else "interrupted" if code in {-2, 130} else "error"
            store.ingest(
                Event(
                    "generic",
                    identity,
                    "process",
                    now(),
                    identity + ":end",
                    identity,
                    "process",
                    {"exit_code": code, "duration_ms": duration, "coverage": "process_only"},
                )
            )
            store.ingest(
                Event(
                    "generic",
                    identity,
                    "turn_end",
                    now(),
                    identity,
                    identity,
                    "process",
                    {"status": status, "duration_ms": duration, "exit_code": code},
                )
            )
    finally:
        store.close()
    raise typer.Exit(code if code >= 0 else 128 - code)


@adapter_app.command("new")
def adapter_new(id: str, directory: Annotated[Path, typer.Option()] = Path(".")) -> None:
    """Create an independently installable third-party adapter scaffold."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", id):
        raise typer.BadParameter("Use 2–41 lowercase letters, digits and underscores")
    root = directory / f"tracebook-{id}"
    if root.exists():
        raise typer.BadParameter("Destination already exists")
    module = f"tracebook_{id}"
    atomic_write(
        root / "pyproject.toml",
        f'[build-system]\nrequires=["hatchling"]\nbuild-backend="hatchling.build"\n[project]\nname="tracebook-{id}"\nversion="0.1.0"\nrequires-python=">=3.12"\ndependencies=["tracebook"]\n[project.entry-points."tracebook.adapters"]\n{id}="{module}:CustomAdapter"\n',
    )
    atomic_write(
        root / module / "__init__.py",
        'from tracebook.adapters.generic import GenericAdapter\n\nclass CustomAdapter(GenericAdapter):\n    """Replace schema mapping after discovering real agent fixtures."""\n    id = '
        + repr(id)
        + "\n    display_name = "
        + repr(id.replace("_", " ").title())
        + "\n",
    )
    typer.echo(str(root))


@adapter_app.command("capabilities")
def capabilities(output: Annotated[Path | None, typer.Option()] = None) -> None:
    """Print or regenerate the capability matrix from installed adapters."""
    registry = adapters()
    fields = list(asdict(next(iter(registry.values())).capabilities()))
    lines = [
        "# Adapter capability matrix",
        "",
        "Generated by `tracebook adapter capabilities --output docs/CAPABILITY_MATRIX.md`.",
        "",
        "yes = supported fixture contract; partial = incomplete or locally unverified; no = unavailable.",
        "",
        "| Capability | " + " | ".join(registry) + " |",
        "|---|" + "---|" * len(registry),
    ]
    for field in fields:
        lines.append(
            "| "
            + field
            + " | "
            + " | ".join(asdict(a.capabilities())[field] for a in registry.values())
            + " |"
        )
    lines += ["", *[f"- {a.display_name}: {a.verification}" for a in registry.values()], ""]
    content = "\n".join(lines)
    if output:
        atomic_write(output, content)
    else:
        typer.echo(content)
