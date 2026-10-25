from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tracebook import hook
from tracebook.adapters import adapters
from tracebook.adapters.claude import ClaudeAdapter
from tracebook.adapters.codex import CodexAdapter
from tracebook.adapters.gemini import GeminiAdapter
from tracebook.capture import Capture
from tracebook.install import edit_hooks
from tracebook.model import Event, Source, utc
from tracebook.privacy import Redactor
from tracebook.store import Store
from tracebook.testing import conform


@pytest.fixture
def store(tmp_path: Path):
    value = Store(tmp_path / "db.sqlite3")
    yield value
    value.close()


def test_entry_points_and_observed_conformance():
    registry = adapters()
    assert {"claude_code", "codex", "gemini_cli", "generic"} <= registry.keys()
    for agent in ("claude_code", "codex"):
        paths = list(Path("tests/fixtures", agent).glob("*/observed.jsonl"))
        events = conform(registry[agent], paths, complete=False)
        assert any(e.kind == "model_call" for e in events)
        assert any(e.kind == "tool_call" for e in events)


def test_redaction_recursive_and_embedded():
    secret = "sk-" + "a" * 30
    data = {
        "API_KEY": "value",
        "nested": [{"text": f"export API_KEY={secret}\nBearer abc.def.ghi"}],
        "input_tokens": 100,
    }
    clean = Redactor().clean(data)
    assert secret not in json.dumps(clean)
    assert clean["API_KEY"] == "[REDACTED]"
    assert clean["input_tokens"] == 100
    assert Redactor().clean(clean) == clean
    assert (
        Redactor(capture_content=False).clean({"prompt": "private"})["prompt"]
        == "[content disabled]"
    )


@pytest.mark.parametrize("agent", ["claude_code", "codex", "gemini_cli", "unknown"])
@pytest.mark.parametrize(
    "payload",
    ['{"session_id":"test","prompt":"example"}', "not json", '{"huge":"' + "x" * 300000 + '"}'],
)
def test_hook_always_silent_and_zero(tmp_path: Path, agent: str, payload: str):
    command = [sys.executable, "-S", hook.__file__, "--agent", agent]
    # Large payloads go through stdin to stay below OS argv limits.
    if agent == "codex" and len(payload) < 10000:
        command.append(payload)
    result = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "TRACEBOOK_HOME": str(tmp_path)},
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    files = list((tmp_path / "spool").glob("*.jsonl"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["agent"] == agent
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_hook_failure_and_forward(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    output = tmp_path / "forwarded"
    forward = [
        sys.executable,
        "-c",
        "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
        str(output),
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            hook.__file__,
            "--agent",
            "codex",
            "--forward",
            json.dumps(forward),
            '{"type":"agent-turn-complete"}',
        ],
        capture_output=True,
        env={**os.environ, "TRACEBOOK_HOME": str(blocker)},
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    import time

    deadline = time.monotonic() + 3
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(output.read_text())["type"] == "agent-turn-complete"


@pytest.mark.parametrize(
    "cls,env",
    [
        (ClaudeAdapter, "CLAUDE_CONFIG_DIR"),
        (CodexAdapter, "CODEX_HOME"),
        (GeminiAdapter, "GEMINI_CLI_HOME"),
    ],
)
def test_hook_config_idempotent_preserves_and_uninstalls(tmp_path: Path, monkeypatch, cls, env):
    monkeypatch.setenv(env, str(tmp_path))
    adapter = cls()
    adapter.root().mkdir(exist_ok=True)
    path = adapter.root() / ("config.toml" if adapter.id == "codex" else "settings.json")
    original = (
        '# Keep this comment\nmodel = "example"\nnotify = ["printf", "existing"]\n'
        if adapter.id == "codex"
        else json.dumps(
            {
                "existing": True,
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "printf existing"}]}]},
            }
        )
    )
    path.write_text(original)
    assert edit_hooks(adapter).changed
    assert path.read_text() == original
    edit_hooks(adapter, dry_run=False)
    installed = path.read_text()
    assert not edit_hooks(adapter, dry_run=False).changed
    assert path.read_text() == installed
    assert list(path.parent.glob("*.bak"))
    edit_hooks(adapter, uninstall=True, dry_run=False)
    if adapter.id == "codex":
        assert path.read_text() == original
    else:
        assert json.loads(path.read_text()) == json.loads(original)
    path.write_text("invalid {{{")
    with pytest.raises(ValueError):
        edit_hooks(adapter, dry_run=False)
    assert path.read_text() == "invalid {{{"


def test_transaction_rolls_back(store: Store):
    with pytest.raises(RuntimeError), store.transaction():
        store.ingest(
            Event("generic", "s", "turn_start", utc(1), "t", "t", data={"prompt": "hello"})
        )
        raise RuntimeError("crash")
    assert store.rows("SELECT * FROM turns") == []


def test_redacted_raw_and_normalized_idempotent(store: Store):
    secret = "sk-" + "x" * 24
    event = Event(
        "generic",
        "s",
        "model_call",
        utc(1),
        "m",
        "t",
        data={"model": "unknown", "input_uncached": 10, "response": secret},
    )
    with store.transaction():
        store.raw("generic", "import", {"content": secret})
        store.ingest(event)
        store.ingest(event)
    assert len(store.rows("SELECT * FROM model_calls")) == 1
    assert secret not in store.path.read_bytes().decode(errors="ignore")
    assert secret not in json.dumps(store.rows("SELECT data FROM events"))
    assert store.overview()["unpriced_calls"] == 1


def test_usage_sources_aliases_and_unknowns(store: Store):
    registry = adapters()
    capture = Capture(store, registry)
    state = {"session_id": "s", "turn_id": "t", "model": "example"}
    legacy = {
        "timestamp": utc(1),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                }
            },
        },
    }
    modern = {
        "timestamp": utc(1),
        "type": "token_usage_record",
        "payload": {
            "turn_id": "t",
            "response_id": "r",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        },
    }
    with store.transaction():
        capture.record(registry["codex"], legacy, state)
        capture.record(registry["codex"], modern, state)
        capture.record(registry["codex"], legacy, state)
        capture.record(registry["codex"], {"type": "future"}, state)
    summary = store.overview()
    assert summary["input_uncached"] == 40
    assert summary["output"] == 15
    assert summary["reasoning"] == 5
    assert summary["calls"] == 1
    assert summary["parse_warnings"] == 1


def test_incremental_partial_restart_rotation_and_copytruncate(tmp_path: Path, store: Store):
    capture = Capture(store, adapters())
    adapter = capture.adapters["codex"]
    path = tmp_path / "session.jsonl"
    record = {
        "timestamp": utc(1),
        "type": "session_meta",
        "payload": {"id": "s", "cli_version": "fixture"},
    }
    data = json.dumps(record).encode() + b"\n"
    path.write_bytes(data[:20])
    assert capture.tail(adapter, Source(str(path))) == 0
    with path.open("ab") as f:
        f.write(data[20:])
    assert capture.tail(adapter, Source(str(path))) > 0
    assert Capture(store, adapters()).tail(adapter, Source(str(path))) == 0
    renamed = tmp_path / "moved.jsonl"
    path.rename(renamed)
    assert capture.tail(adapter, Source(str(renamed))) == 0
    path.write_bytes(data)
    capture.tail(adapter, Source(str(path)))
    assert len(store.rows("SELECT * FROM sessions")) == 1
    record["payload"]["id"] = "different"
    path.write_text(json.dumps(record) + "\n")
    capture.tail(adapter, Source(str(path)))
    assert len(store.rows("SELECT * FROM sessions")) == 2


def test_gemini_json_and_jsonl_reconcile(store: Store):
    capture = Capture(store, adapters())
    adapter = capture.adapters["gemini_cli"]
    root = Path("tests/fixtures/gemini_cli/unverified")
    capture.tail(adapter, Source(str(root / "session.json"), "json"))
    capture.tail(adapter, Source(str(root / "session.jsonl")))
    assert store.overview()["calls"] == 2
    assert store.overview()["input_uncached"] == 130
    assert len(store.rows("SELECT * FROM tool_calls")) == 1
    assert store.rows("SELECT status FROM turns")[0]["status"] == "completed"


def test_claude_split_usage_and_tool_results(store: Store):
    capture = Capture(store, adapters())
    adapter = capture.adapters["claude_code"]
    state = {}
    records = [
        {
            "type": "user",
            "sessionId": "s",
            "uuid": "u",
            "promptId": "p",
            "timestamp": utc(1),
            "message": {"content": "hello"},
        },
        {
            "type": "assistant",
            "sessionId": "s",
            "uuid": "a1",
            "timestamp": utc(2),
            "message": {
                "id": "m",
                "model": "model",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "Bash",
                        "input": {"command": "false"},
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": "s",
            "uuid": "a2",
            "timestamp": utc(3),
            "message": {
                "id": "m",
                "model": "model",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [],
            },
        },
        {
            "type": "user",
            "sessionId": "s",
            "uuid": "r",
            "timestamp": utc(4),
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call",
                        "is_error": True,
                        "content": "failed",
                    }
                ]
            },
        },
    ]
    with store.transaction():
        for record in records:
            capture.record(adapter, record, state)
    assert store.overview()["turns"] == 1
    assert store.overview()["calls"] == 1
    assert store.overview()["output"] == 5
    assert store.rows("SELECT status FROM tool_calls")[0]["status"] == "error"


def test_field_provenance_and_sparse_updates(store: Store):
    with store.transaction():
        store.ingest(
            Event(
                "claude_code",
                "s",
                "model_call",
                utc(1),
                "request",
                "t",
                data={"model": "model", "input_uncached": 5, "aliases": ["message"]},
            )
        )
        store.ingest(
            Event(
                "claude_code",
                "s",
                "model_call",
                utc(1),
                "message",
                "t",
                "otel",
                {"model": "model", "cost_reported": 0.01, "input_uncached": None},
            )
        )
    row = store.rows("SELECT * FROM model_calls")[0]
    assert row["input_uncached"] == 5 and row["cost_reported"] == 0.01
    event = store.rows("SELECT provenance FROM events")[0]
    provenance = json.loads(event["provenance"])
    assert provenance["input_uncached"]["source"] == "transcript"
    assert provenance["cost_reported"]["source"] == "otel"
