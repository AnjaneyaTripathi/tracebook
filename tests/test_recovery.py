from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

from tracebook import hook
from tracebook.adapters import adapters
from tracebook.capture import Capture
from tracebook.config import Config
from tracebook.model import Event, utc
from tracebook.platform import daemon_lock, process_alive
from tracebook.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "db.sqlite3")
    yield value
    value.close()


def test_gemini_telemetry_concatenated_partial_json_and_discovery(store, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    registry = adapters()
    adapter = registry["gemini_cli"]
    adapter.root().mkdir()
    path = tmp_path / "telemetry.json"
    (adapter.root() / "settings.json").write_text(json.dumps({"telemetry": {"outfile": str(path)}}))
    record = {
        "hrTime": [1720000000, 1000000],
        "body": "gemini_cli.api_response",
        "attributes": {
            "session.id": "g",
            "prompt_id": "p",
            "model": "gemini-example",
            "input_token_count": 100,
            "cached_content_token_count": 30,
            "output_token_count": 10,
            "thoughts_token_count": 5,
        },
    }
    metric = {
        "scopeMetrics": [
            {
                "metrics": [
                    {
                        "descriptor": {"name": "gemini_cli.token.usage"},
                        "dataPoints": [{"attributes": {"session.id": "g"}, "value": 100}],
                        "aggregationTemporality": 1,
                    }
                ]
            }
        ]
    }
    path.write_text(json.dumps(record, indent=2) + "\n" + json.dumps(metric)[:40])
    source = adapter.transcript_sources()[0]
    assert source.format == "otel_file"
    capture = Capture(store, registry)
    assert capture.tail(adapter, source) == 1
    assert capture.tail(adapter, source) == 0
    with path.open("a") as stream:
        stream.write(json.dumps(metric)[40:] + "\n")
    assert capture.tail(adapter, source) == 1
    assert store.overview()["input_uncached"] == 70
    assert store.overview()["calls"] == 1
    assert store.rows("SELECT * FROM events WHERE kind='metric'")
    assert capture.tail(adapter, source) == 0
    (adapter.root() / "settings.json").write_text("bad")
    assert adapter.transcript_sources() == []


def test_generic_otel_service_mapping(store, tmp_path):
    adapter = adapters()["generic"]
    adapter.state_root = tmp_path
    (tmp_path / "otel-mappings.toml").write_text(
        '[services."example-agent".fields]\nsession_id="session"\nturn_id="turn"\nkind="type"\nnative_id="id"\nts="time"\ninput_uncached="tokens.in"\noutput="tokens.out"\n[services."example-agent".kinds]\nusage="model_call"\n'
    )
    record = {
        "service.name": "example-agent",
        "session": "s",
        "turn": "t",
        "type": "usage",
        "id": "c",
        "time": utc(2),
        "tokens": {"in": 20, "out": 3},
    }
    events = list(adapter.map_otel([record], []))
    assert events[0].source == "otel"
    with store.transaction():
        store.ingest(events[0])
    assert store.overview()["input_uncached"] == 20
    assert list(adapter.map_otel([{"service.name": "unmapped"}], []))[0].kind == "warning"


def test_spool_ack_cleanup_and_source_failure_isolation(store, tmp_path, monkeypatch):
    registry = adapters()
    capture = Capture(store, registry)
    for adapter in registry.values():
        monkeypatch.setattr(adapter, "transcript_sources", lambda: [])

    def unavailable():
        raise OSError("unavailable")

    monkeypatch.setattr(registry["codex"], "transcript_sources", unavailable)
    spool = tmp_path / "spool"
    spool.mkdir()
    path = spool / "99999999-5000.jsonl"
    path.write_text(
        json.dumps(
            {
                "agent": "codex",
                "received_at": 1,
                "payload_raw": json.dumps(
                    {"type": "agent-turn-complete", "thread-id": "s", "turn-id": "t"}
                ),
            }
        )
        + "\n"
    )
    monkeypatch.setattr("tracebook.capture.process_alive", lambda pid: False)
    assert asyncio.run(capture.poll_async(spool)) == 1
    assert "codex" in capture.errors
    assert not path.exists()
    assert not store.rows("SELECT * FROM adapter_state")
    assert asyncio.run(capture.poll_async(spool)) == 0
    path.write_text(
        '{"payload_raw":"sk-test-secret-value-12345678901234567890", "agent":"codex"}\n'
    )
    assert capture.poll(spool) == 1
    assert store.rows("SELECT * FROM events WHERE kind='warning'")
    assert "sk-test-secret-value" not in str(store.rows("SELECT payload FROM raw_payloads"))


def test_reconciliation_is_order_independent_for_prompts_and_model_calls(store):
    for order in (("otel", "transcript"), ("transcript", "otel")):
        session = "s" + order[0]
        with store.transaction():
            for source in order:
                store.ingest(
                    Event(
                        "codex",
                        session,
                        "model_call",
                        utc(2),
                        source,
                        "t",
                        source,
                        {"model": "test", "input_uncached": 30 if source == "otel" else 20},
                    )
                )
        assert (
            store.rows(
                "SELECT input_uncached FROM accounted_model_calls WHERE session_id=(SELECT id FROM sessions WHERE native_id=?)",
                (session,),
            )[0]["input_uncached"]
            == 30
        )
    assert store.overview()["calls"] == 2
    for order in (("hook", "transcript"), ("transcript", "hook")):
        session = "p" + order[0]
        with store.transaction():
            for source in order:
                store.ingest(
                    Event(
                        "claude_code",
                        session,
                        "turn_start",
                        utc(1),
                        source,
                        source,
                        source,
                        {"prompt": "Test instruction"},
                    )
                )
            store.ingest(
                Event(
                    "claude_code",
                    session,
                    "turn_end",
                    utc(3),
                    "stop",
                    source="hook",
                    data={"status": "completed"},
                )
            )
        rows = store.rows(
            "SELECT * FROM turns WHERE session_id=(SELECT id FROM sessions WHERE native_id=?) AND prompt IS NOT NULL",
            (session,),
        )
        assert len(rows) == 1 and rows[0]["status"] == "completed"


def test_daemon_lifecycle_and_exclusive_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import tracebook.daemon as module

    # Monotonic clocks may start near zero on freshly booted CI machines.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 10.0))

    registry = {"generic": adapters()["generic"]}
    monkeypatch.setattr(module, "adapters", lambda: registry)

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        async def serve(self):
            await asyncio.sleep(0.02)

    monkeypatch.setattr(module, "Server", FakeServer)
    with daemon_lock(tmp_path), pytest.raises(RuntimeError), daemon_lock(tmp_path):
        pass
    asyncio.run(module.serve(Config(root=tmp_path)))
    assert (tmp_path / "reports").is_dir()
    with daemon_lock(tmp_path):
        assert process_alive(os.getpid())
    monkeypatch.setattr(os, "kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    assert not process_alive(123)
    monkeypatch.setattr(os, "kill", lambda *args: (_ for _ in ()).throw(PermissionError()))
    assert process_alive(123)


def test_fast_notify_forwarding_survives_capture_failure(tmp_path, monkeypatch, capsys):
    called = []
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv("TRACEBOOK_HOME", str(blocker))
    payload = '{"thread-id":"s"}'
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "shim",
            "--agent",
            "codex",
            "--forward-argv",
            "2",
            "/bin/echo",
            "original-argument",
            payload,
        ],
    )
    monkeypatch.setattr(os, "posix_spawnp", lambda *args, **kwargs: called.append((args, kwargs)))
    hook.capture()
    assert called[0][0][1] == ["/bin/echo", "original-argument", payload]
    assert capsys.readouterr() == ("", "")
    monkeypatch.setattr(sys, "argv", ["shim", "--forward-argv", "9999"])
    hook.capture()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "text",
    [
        "port=80",
        "port=4318",
        "poll_seconds=nan",
        "scan_seconds=0",
        'capture_content="yes"',
        "delivery=[]",
        'redact_patterns=["["]',
    ],
)
def test_invalid_configuration_rejected(tmp_path, text):
    (tmp_path / "config.toml").write_text(text)
    with pytest.raises((ValueError, TypeError, re.error)):
        Config.load(tmp_path)


def test_full_history_codex_variants_are_mapped(store):
    from tracebook.testing import conform

    path = Path("tests/fixtures/codex/0.164.0/additional-records.jsonl")
    adapter = adapters()["codex"]
    events = conform(adapter, [path], complete=False)
    assert not any(event.kind == "warning" for event in events)
    assert {"tool_call", "tool_result", "subagent", "lifecycle"} <= {e.kind for e in events}
    with store.transaction():
        for event in events:
            store.ingest(event)
    assert store.rows("SELECT * FROM tool_calls WHERE tool='mcp'")[0]["duration_ms"] is not None
