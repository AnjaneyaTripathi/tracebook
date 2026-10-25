from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from tracebook import hook
from tracebook.adapters import adapters
from tracebook.adapters.base import tool_input, tool_kind
from tracebook.capture import Capture
from tracebook.config import Config
from tracebook.install import edit_hooks, owned
from tracebook.model import Event, Source, number, utc
from tracebook.privacy import Redactor
from tracebook.store import Store
from tracebook.testing import assert_invariants


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "db.sqlite3")
    yield value
    value.close()


@pytest.mark.parametrize("agent", ["claude_code", "codex", "gemini_cli"])
def test_hook_capabilities_from_schema_fixtures(agent):
    adapter = adapters()[agent]
    cases = {
        "PreToolUse": "tool_call",
        "PostToolUse": "tool_result",
        "PostToolUseFailure": "tool_result",
        "BeforeTool": "tool_call",
        "AfterTool": "tool_result",
        "UserPromptSubmit": "turn_start",
        "BeforeAgent": "turn_start",
        "Stop": "turn_end",
        "AfterAgent": "turn_end",
        "agent-turn-complete": "turn_end",
        "PermissionRequest": "permission",
        "PermissionDenied": "permission",
        "Notification": "permission",
        "PreCompact": "compaction",
        "PreCompress": "compaction",
        "SubagentStart": "subagent",
        "SubagentStop": "subagent",
        "SessionStart": "session",
        "SessionEnd": "session",
        "future": "warning",
    }
    for name, kind in cases.items():
        event = list(
            adapter.parse_hook(
                {
                    "session_id": "s",
                    "prompt_id": "p",
                    "hook_event_name": name,
                    "tool_name": "Bash",
                    "tool_use_id": "tool",
                    "prompt": "Test",
                    "timestamp": utc(1),
                    "tool_input": {"command": "true"},
                }
            )
        )[0]
        assert event.kind == kind
        assert event.source == "hook"
        assert event.turn_id == "p"
    caps = asdict(adapter.capabilities())
    fixture_kinds = {
        "transcripts": {"session"},
        "token_input": {"model_call"},
        "token_output": {"model_call"},
        "prompt_text": {"turn_start"},
        "tool_io_content": {"tool_call"},
    }
    if agent != "gemini_cli":
        source = next(Path("tests/fixtures", agent).glob("*/observed.jsonl"))
        kinds = {
            event.kind
            for event in adapter.parse_transcript(Source(str(source)), source.read_bytes())
        }
        for key, expected in fixture_kinds.items():
            if caps[key] == "yes":
                assert expected <= kinds


def test_hook_inprocess_fail_open_boundaries(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRACEBOOK_HOME", str(tmp_path))
    monkeypatch.setattr(
        sys, "argv", ["tracebook-hook", "--agent", "claude_code", "--event", "Stop"]
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"x": ["a" * 80000], "nested": {"value": "y"}}))
    )
    hook.capture()
    assert capsys.readouterr().out == ""
    envelope = json.loads(next((tmp_path / "spool").glob("*.jsonl")).read_text())
    assert envelope["truncated"]
    assert len(envelope["payload_raw"]) == 65536
    monkeypatch.setattr(
        sys, "argv", ["tracebook-hook", "--agent", "codex", "--forward", "[]", "bad json"]
    )
    hook.capture()
    assert not capsys.readouterr().out


def test_spool_malformed_and_canonical_events(store, tmp_path):
    registry = adapters()
    capture = Capture(store, registry)
    spool = tmp_path / "spool"
    spool.mkdir()
    records = [
        {
            "agent": "codex",
            "received_at": 1,
            "payload": {"type": "agent-turn-complete", "thread-id": "s", "turn-id": "t"},
        },
        {
            "agent": "generic",
            "canonical": asdict(
                Event(
                    "generic", "p", "process", utc(2), "process", data={"coverage": "process_only"}
                )
            ),
        },
        {"agent": "generic", "canonical": {"invalid": True}},
    ]
    (spool / "events.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\nnot-json\n")
    capture.last_scan = float("inf")
    assert capture.poll(spool) >= 4
    assert capture.poll(spool) == 0
    assert store.rows("SELECT status FROM turns")[0]["status"] == "completed"
    assert store.rows("SELECT * FROM events WHERE kind='warning'")
    capture.sources = [(registry["codex"], Source(str(tmp_path / "absent")))]
    capture.poll(spool)
    assert capture.errors


def test_generic_jsonl_csv_and_malformed_parser(store, tmp_path):
    mapping = tmp_path / "map.toml"
    mapping.write_text(
        '[fields]\nsession_id="session"\nturn_id="turn"\nkind="type"\nnative_id="id"\nts="timestamp"\ninput_uncached="tokens.input"\noutput="tokens.output"\n[kinds]\ncompletion="model_call"\n'
    )
    file = tmp_path / "events.jsonl"
    file.write_text(
        json.dumps(
            {
                "session": "s",
                "turn": "t",
                "type": "completion",
                "id": "a",
                "timestamp": utc(1),
                "tokens": {"input": 20, "output": 3},
            }
        )
        + "\n"
    )
    capture = Capture(store, adapters())
    assert capture.import_file(file, mapping) == 1
    assert capture.import_file(file, mapping) == 1
    assert store.overview()["calls"] == 1
    file = tmp_path / "events.csv"
    file.write_text("session,turn,type,id,timestamp\ns,t,completion,b,2026-01-01T00:00:00Z\n")
    assert capture.import_file(file, mapping) == 1
    parser = capture.adapters["generic"]
    assert list(parser.parse_transcript(Source("bad"), b"[]\nnot-json\n"))[0].kind == "warning"
    assert list(parser.parse_record({"kind": "future"}, {}))[0].kind == "warning"


def test_import_keeps_good_records_around_malformed_lines(store, tmp_path, monkeypatch):
    import tracebook.capture as module

    monkeypatch.setattr(module, "MAX_LINE", 128)
    mapping = tmp_path / "map.toml"
    mapping.write_text("")
    file = tmp_path / "mixed.jsonl"
    file.write_bytes(
        b'{"kind":"turn_start","id":"one","turn_id":"one","session_id":"s","prompt":"first"}\n'
        b'\n{"api_key":"sk-secret-value",invalid}\n[]\n'
        + b"x" * 300
        + b'\n{"kind":"turn_start","id":"two","turn_id":"two","session_id":"s","prompt":"last"}\n'
    )
    capture = Capture(store, adapters())
    assert capture.import_file(file, mapping) == 5
    assert capture.import_file(file, mapping) == 5
    assert store.overview()["turns"] == 2
    assert store.overview()["parse_warnings"] == 3
    assert len(store.rows("SELECT * FROM raw_payloads")) == 5
    assert "sk-secret-value" not in str(store.rows("SELECT * FROM raw_payloads"))


def test_truncation_oversize_and_invalid_snapshot(store, tmp_path, monkeypatch):
    import tracebook.capture as module

    capture = Capture(store, adapters())
    adapter = capture.adapters["codex"]
    path = tmp_path / "capture.jsonl"
    path.write_text('{"type":"unknown"}\n' * 3)
    capture.tail(adapter, Source(str(path)))
    path.write_text('{"type":"new"}\n')
    assert capture.tail(adapter, Source(str(path))) == 1
    monkeypatch.setattr(module, "MAX_LINE", 100)
    path.write_bytes(b"x" * 150 + b'\n{"type":"new-record"}\n')
    assert capture.tail(adapter, Source(str(path))) == 1
    assert capture.tail(adapter, Source(str(path))) == 1
    snapshot = tmp_path / "session.json"
    snapshot.write_text('{"incomplete":')
    assert capture.tail(capture.adapters["gemini_cli"], Source(str(snapshot), "json")) == 0
    monkeypatch.setattr(module, "MAX_SNAPSHOT", 2)
    with pytest.raises(ValueError):
        capture.tail(capture.adapters["gemini_cli"], Source(str(snapshot), "json"))


def test_gemini_updates_errors_rewinds(store):
    adapter = adapters()["gemini_cli"]
    state = {}
    source = Path("tests/fixtures/gemini_cli/unverified/session.json")
    events = list(adapter.parse_record(json.loads(source.read_text()), state))
    events += list(
        adapter.parse_record(
            {
                "$patch": {
                    "updates": [
                        {"id": "g-model-1", "toolCalls": [{"id": "g-tool", "result": "updated"}]}
                    ]
                }
            },
            state,
        )
    )
    events += list(adapter.parse_record({"$rewindTo": "g-user"}, state))
    events += list(
        adapter.parse_record(
            {
                "type": "error",
                "id": "error",
                "content": "interrupted",
                "timestamp": "2026-10-24T10:00:05Z",
            },
            state,
        )
    )
    events += list(adapter.parse_record({"type": "info", "id": "info", "timestamp": utc(2)}, state))
    events += list(adapter.parse_record({"future": True}, state))
    assert_invariants(events)
    with store.transaction():
        for event in events:
            store.ingest(event)
    assert store.overview()["calls"] == 2
    assert store.rows("SELECT status FROM turns")[0]["status"] == "error"
    assert list(adapter.parse_record({"lastUpdated": utc(1)}, state))[0].kind == "lifecycle"


def test_discovery_overrides_and_detection(tmp_path, monkeypatch):
    registry = adapters()
    for key, env in [
        ("codex", "CODEX_HOME"),
        ("claude_code", "CLAUDE_CONFIG_DIR"),
        ("gemini_cli", "GEMINI_CLI_HOME"),
    ]:
        monkeypatch.setenv(env, str(tmp_path / key))
        adapter = registry[key]
        root = adapter.root()
        assert str(tmp_path / key) in str(root)
        assert adapter.transcript_sources() == []
        root.mkdir(parents=True)
        assert adapter.uninstall_hooks(dry_run=True)
    monkeypatch.setattr("shutil.which", lambda binary: None)
    assert registry["codex"].detect() is None
    assert not registry["codex"].doctor()[0].ok
    monkeypatch.setattr("shutil.which", lambda binary: "/example/bin/agent")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "agent 1.0\n", ""),
    )
    assert registry["codex"].detect().version == "agent 1.0"
    assert registry["codex"].install_hooks(dry_run=True).changed


def test_config_validation_and_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACEBOOK_HOME", str(tmp_path))
    assert Config.load().root == tmp_path
    config = tmp_path / "config.toml"
    config.write_text('billing_mode="subscription"\nport=9999\nreport_hour=12\n')
    assert Config.load().port == 9999
    config.write_text('billing_mode="invalid"\n')
    with pytest.raises(ValueError):
        Config.load()
    config.write_text("report_hour=24\n")
    with pytest.raises(ValueError):
        Config.load()
    assert utc("invalid").startswith("1970")
    assert number("invalid") is None
    assert tool_input("[1]") == {"input": "[1]"}
    assert tool_kind("mcp__example__tool") == "mcp"
    assert tool_kind("unknown") == "other"
    assert Redactor().clean({}, depth=41) == "[depth-limited]"
    assert owned('"') is False
    assert not edit_hooks(adapters()["generic"]).changed
