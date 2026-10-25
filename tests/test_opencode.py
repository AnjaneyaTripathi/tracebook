import json
import sqlite3
from pathlib import Path

from tracebook.adapters import adapters
from tracebook.capture import Capture
from tracebook.model import Source
from tracebook.store import Store
from tracebook.testing import conform


def test_opencode_v2_wal_snapshot_updates_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    adapter = adapters()["opencode"]
    adapter.root().mkdir()
    path = adapter.root() / "opencode.db"
    fixture = Path("tests/fixtures/opencode/unverified/observed-schema.jsonl")
    events = conform(adapter, [fixture])
    assert {event.kind for event in events} >= {
        "turn_start",
        "model_call",
        "tool_call",
        "tool_result",
        "compaction",
    }
    database = sqlite3.connect(path)
    database.execute("PRAGMA journal_mode=WAL")
    database.executescript(
        "CREATE TABLE session_v2(id TEXT PRIMARY KEY,directory TEXT,version TEXT,parent_id TEXT,fork_session_id TEXT);CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);"
    )
    database.execute(
        "INSERT INTO session_v2 VALUES('ses_example','/workspace/example','source-schema',NULL,NULL)"
    )
    for seq, line in enumerate(fixture.read_text().splitlines()):
        record = json.loads(line)
        database.execute(
            "INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
            (
                record["id"],
                record["session_id"],
                record["type"],
                seq,
                seq,
                seq,
                json.dumps({k: v for k, v in record.items() if k not in {"id", "type"}}),
            ),
        )
    database.commit()
    store = Store(tmp_path / "tracebook/db.sqlite3")
    capture = Capture(store, adapters())
    source = adapter.transcript_sources()[0]
    assert capture.tail(adapter, source) > 0
    assert capture.tail(adapter, source) == 0
    usage = store.overview()
    assert usage["calls"] == 1 and usage["input_uncached"] == 100
    assert usage["reasoning"] == 5 and usage["cost_reported"] == 0.001
    assert store.rows("SELECT duration_ms FROM tool_calls")[0]["duration_ms"] == 300
    assert store.rows("SELECT status FROM turns")[0]["status"] == "completed"
    database.execute(
        "UPDATE session_message SET time_updated=5,data=json_set(data,'$.tokens.output',25) WHERE id='msg_assistant'"
    )
    database.commit()
    assert capture.tail(adapter, source) > 0
    assert store.overview()["output"] == 25
    assert database.execute("SELECT COUNT(*) FROM session_message").fetchone()[0] == 3
    database.close()
    store.close()


def test_unknown_sqlite_schema_is_visible_and_read_only(tmp_path):
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE future(value TEXT)")
    store = Store(tmp_path / "state/db.sqlite3")
    capture = Capture(store, adapters())
    assert capture.tail(adapters()["opencode"], Source(str(path), "sqlite")) == 1
    assert store.overview()["parse_warnings"] == 1
    assert "future" in str(store.rows("SELECT payload FROM raw_payloads"))
    store.close()
