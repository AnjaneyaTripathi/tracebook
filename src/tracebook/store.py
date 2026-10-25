"""SQLite writer, immutable redacted evidence and field-level reconciliation."""

from __future__ import annotations

import functools
import hashlib
import json
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import orjson

from tracebook.model import (
    KINDS,
    STATUSES,
    TOKEN_FIELDS,
    TOOLS,
    Event,
    Json,
    digest,
    now,
    number,
    obj,
    utc,
)
from tracebook.platform import private_dir
from tracebook.privacy import Redactor

SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, version TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT, remote_hash TEXT);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, agent TEXT NOT NULL, native_id TEXT NOT NULL, agent_version TEXT, project_id TEXT, started_at TEXT, ended_at TEXT, resumed INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent TEXT NOT NULL, native_id TEXT NOT NULL, started_at TEXT, ended_at TEXT, status TEXT NOT NULL DEFAULT 'unknown', kind TEXT NOT NULL DEFAULT 'prompt', prompt TEXT, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT, agent TEXT NOT NULL, ts TEXT NOT NULL, model TEXT, input_uncached INTEGER, input_cache_read INTEGER, input_cache_write INTEGER, output INTEGER, reasoning INTEGER, total_reported INTEGER, cost_reported REAL, cost_estimated REAL, usage_format TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_calls(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT, agent TEXT NOT NULL, native_id TEXT NOT NULL, ts TEXT NOT NULL, ended_at TEXT, tool TEXT NOT NULL DEFAULT 'other', native_name TEXT, status TEXT NOT NULL DEFAULT 'unknown', duration_ms REAL, input TEXT, output TEXT, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, agent TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT, kind TEXT NOT NULL, ts TEXT NOT NULL, native_id TEXT NOT NULL, data TEXT NOT NULL, provenance TEXT NOT NULL, clock_source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_aliases(agent TEXT, session_id TEXT, kind TEXT, alias TEXT, event_id TEXT NOT NULL, PRIMARY KEY(agent,session_id,kind,alias));
CREATE TABLE IF NOT EXISTS raw_payloads(id TEXT PRIMARY KEY, agent TEXT NOT NULL, source TEXT NOT NULL, record_type TEXT, received_at TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS adapter_state(id TEXT PRIMARY KEY, path TEXT NOT NULL, agent TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, detector TEXT NOT NULL, severity TEXT NOT NULL, agent TEXT, session_id TEXT, turn_id TEXT, ts TEXT NOT NULL, title TEXT NOT NULL, evidence TEXT NOT NULL, recommendation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, window_start TEXT NOT NULL, window_end TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, report_id TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt TEXT NOT NULL, last_error TEXT);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent,started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id,started_at);
CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent,started_at);
CREATE INDEX IF NOT EXISTS idx_calls_turn ON model_calls(turn_id,ts);
CREATE INDEX IF NOT EXISTS idx_calls_session ON model_calls(session_id,ts);
CREATE INDEX IF NOT EXISTS idx_tools_turn ON tool_calls(turn_id,ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id,ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind,ts);
CREATE INDEX IF NOT EXISTS idx_raw_agent ON raw_payloads(agent,received_at);
CREATE INDEX IF NOT EXISTS idx_findings_time ON findings(ts,severity);
CREATE VIEW IF NOT EXISTS accounted_model_calls AS SELECT m.* FROM model_calls m
 WHERE COALESCE(m.usage_format,'') != 'uncorrelated_telemetry' AND (COALESCE(m.usage_format,'') != 'legacy_delta' OR NOT EXISTS
 (SELECT 1 FROM model_calls n WHERE n.session_id=m.session_id AND n.turn_id IS m.turn_id AND n.usage_format='response'));
"""
RANK = {"process": 1, "import": 1, "hook": 2, "transcript": 3, "otel": 4}


@functools.lru_cache(maxsize=2048)
def project_identity(cwd: str, remote: str | None = None) -> tuple[str, str, str | None]:
    root = str(Path(cwd).expanduser().resolve())
    if Path(root).is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", root, "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0:
                root = result.stdout.strip()
                result = subprocess.run(
                    ["git", "-C", root, "config", "--get", "remote.origin.url"],
                    text=True,
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
                remote = remote or result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    remote_hash = digest(remote) if remote else None
    return digest(root, remote_hash), root, remote_hash


def encode(value: object) -> str:
    return orjson.dumps(value).decode()


class Store:
    def __init__(self, path: Path, redactor: Redactor | None = None) -> None:
        private_dir(path.parent)
        self.path = path
        self.redactor = redactor or Redactor()
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._writer_lock = threading.RLock()
        self._agents_seen: dict[str, object] = {}
        self._sessions_seen: dict[str, str] = {}
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > 1:
            raise ValueError(f"Database schema {version} is newer than this Tracebook")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO migrations VALUES(1,?)", (now(),))
        self.db.execute("PRAGMA user_version=1")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._writer_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                self._agents_seen.clear()
                self._sessions_seen.clear()
                raise
            else:
                self.db.commit()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[Json]:
        with self.reader() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def raw(self, agent: str, source: str, record: Json) -> bool:
        self.redactor.begin_record()
        clean = self.redactor.clean(record)
        payload = orjson.dumps(clean, option=orjson.OPT_SORT_KEYS)
        key = hashlib.blake2b(
            agent.encode() + b"\0" + source.encode() + b"\0" + payload, digest_size=16
        ).hexdigest()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO raw_payloads VALUES(?,?,?,?,?,?)",
            (key, agent, source, str(record.get("type", "unknown")), now(), payload),
        )
        return cursor.rowcount == 1

    def state(self, key: str) -> Json:
        row = self.db.execute("SELECT state FROM adapter_state WHERE id=?", (key,)).fetchone()
        return obj(json.loads(row[0])) if row else {}

    def checkpoint(self, key: str, path: str, agent: str, state: Json) -> None:
        self.db.execute(
            "INSERT INTO adapter_state VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,state=excluded.state,updated_at=excluded.updated_at",
            (key, path, agent, encode(self.redactor.clean(state)), now()),
        )

    def ingest(self, event: Event) -> None:
        event.ts = utc(event.ts)
        if event.kind not in KINDS:
            event.data["reason"] = "unknown_canonical_kind"
            event.kind = "warning"
        if event.kind == "model_call":
            for key in TOKEN_FIELDS:
                if key in event.data:
                    event.data[key] = number(event.data[key])
        if "tool" in event.data and event.data["tool"] not in TOOLS:
            event.data["tool"] = "other"
        if "status" in event.data and event.data["status"] not in STATUSES:
            event.data["status"] = "unknown"
        event.data = self.redactor.clean(event.data)
        sid = digest(event.agent, event.session_id)
        tid = digest(event.agent, event.session_id, event.turn_id) if event.turn_id else None
        if event.turn_id:
            linked = self.db.execute(
                "SELECT e.turn_id FROM event_aliases a JOIN events e ON e.id=a.event_id WHERE a.agent=? AND a.session_id=? AND a.kind='turn_start' AND a.alias=?",
                (event.agent, sid, event.turn_id),
            ).fetchone()
            if linked:
                tid = linked[0]
        event_id = event.id
        aliases = (
            [event.native_id, *event.data.get("aliases", [])]
            if event.kind in {"turn_start", "model_call"}
            else []
        )
        for alias in aliases:
            if alias:
                match = self.db.execute(
                    "SELECT event_id FROM event_aliases WHERE agent=? AND session_id=? AND kind=? AND alias=?",
                    (event.agent, sid, event.kind, str(alias)),
                ).fetchone()
                if match:
                    event_id = match[0]
                    break
        old = self.db.execute(
            "SELECT data,provenance,ts,turn_id FROM events WHERE id=?", (event_id,)
        ).fetchone()
        if not old and event.kind == "turn_start" and event.data.get("prompt"):
            opposite = "hook" if event.source == "transcript" else "transcript"
            candidates = self.db.execute(
                "SELECT id FROM events WHERE session_id=? AND kind='turn_start' AND json_extract(data,'$.prompt')=? AND ABS((julianday(ts)-julianday(?))*86400)<=5 AND json_extract(provenance,'$.prompt.source')=?",
                (sid, event.data["prompt"], event.ts, opposite),
            ).fetchall()
            if len(candidates) == 1:
                event_id = candidates[0][0]
                old = self.db.execute(
                    "SELECT data,provenance,ts,turn_id FROM events WHERE id=?", (event_id,)
                ).fetchone()
                event.data["correlation_confidence"] = "prompt_and_time_window"
        if (
            not tid
            and event.source == "hook"
            and event.kind
            in {"turn_end", "tool_call", "tool_result", "permission", "compaction", "subagent"}
        ):
            latest = self.db.execute(
                "SELECT id FROM turns WHERE session_id=? AND started_at<=? AND (ended_at IS NULL OR ended_at>=?) ORDER BY started_at DESC LIMIT 2",
                (sid, event.ts, event.ts),
            ).fetchall()
            if len(latest) == 1:
                tid = latest[0][0]
                event.data["correlation_confidence"] = "session_active_turn"
        if not old and event.source == "transcript" and event.kind == "model_call":
            candidates = self.db.execute(
                "SELECT id FROM events WHERE session_id=? AND kind='model_call' AND json_extract(data,'$.model') IS ? AND ABS((julianday(ts)-julianday(?))*86400)<=2 AND NOT EXISTS (SELECT 1 FROM json_each(events.provenance) p WHERE json_extract(p.value,'$.source')='transcript')",
                (sid, event.data.get("model"), event.ts),
            ).fetchall()
            if len(candidates) == 1:
                event_id = candidates[0][0]
                old = self.db.execute(
                    "SELECT data,provenance,ts,turn_id FROM events WHERE id=?", (event_id,)
                ).fetchone()
                event.data["correlation_confidence"] = "unique_time_window"
            elif len(candidates) > 1:
                for candidate in candidates:
                    self.db.execute(
                        "UPDATE model_calls SET usage_format='uncorrelated_telemetry' WHERE id=?",
                        (candidate[0],),
                    )
        if not old and event.source == "otel" and event.kind == "model_call":
            # A unique nearby transcript call may enrich telemetry lacking a shared ID.
            # Ambiguous candidates remain separate, explicitly excluded from usage totals.
            candidates = self.db.execute(
                "SELECT id FROM events WHERE session_id=? AND kind='model_call' AND json_extract(data,'$.model') IS ? AND ABS((julianday(ts)-julianday(?))*86400)<=2 AND json_extract(provenance,'$.model.source')='transcript'",
                (sid, event.data.get("model"), event.ts),
            ).fetchall()
            if len(candidates) == 1:
                event_id = candidates[0][0]
                old = self.db.execute(
                    "SELECT data,provenance,ts,turn_id FROM events WHERE id=?", (event_id,)
                ).fetchone()
                event.data["correlation_confidence"] = "unique_time_window"
            elif len(candidates) > 1:
                event.data["usage_format"] = "uncorrelated_telemetry"
                event.data["correlation_confidence"] = "ambiguous"
        merged: Json = json.loads(old[0]) if old else {}
        provenance: Json = json.loads(old[1]) if old else {}
        for field, value in event.data.items():
            previous = obj(provenance.get(field))
            eligible = (
                not previous
                or RANK.get(event.source, 1) > RANK.get(str(previous.get("source")), 0)
                or (
                    RANK.get(event.source, 1) == RANK.get(str(previous.get("source")), 0)
                    and event.ts >= str(previous.get("ts", ""))
                )
            )
            if value is not None and eligible:
                # Sparse updates and a weaker unknown status must not erase evidence.
                if (
                    field == "status"
                    and value == "unknown"
                    and merged.get(field) in {"completed", "error", "interrupted"}
                ):
                    continue
                merged[field] = value
                provenance[field] = {
                    "source": event.source,
                    "ts": event.ts,
                    "native_id": event.native_id,
                }
        tid = old[3] if old and old[3] else tid
        ts = min(event.ts, old[2]) if old and old[2] != "1970-01-01T00:00:00.000Z" else event.ts
        ledger_data = merged
        if event.kind in {"tool_call", "tool_result"}:
            # Tool IO is stored once in its canonical table, with an explicit ledger reference.
            ledger_data = {k: v for k, v in merged.items() if k not in {"input", "output"}}
            ledger_data["tool_ref"] = digest(event.agent, event.session_id, event.native_id)
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET turn_id=excluded.turn_id,ts=excluded.ts,data=excluded.data,provenance=excluded.provenance",
            (
                event_id,
                event.agent,
                sid,
                tid,
                event.kind,
                ts,
                event.native_id,
                encode(ledger_data),
                encode(provenance),
                event.clock_source,
            ),
        )
        for alias in aliases:
            if alias:
                self.db.execute(
                    "INSERT OR IGNORE INTO event_aliases VALUES(?,?,?,?,?)",
                    (event.agent, sid, event.kind, str(alias), event_id),
                )
        version = merged.get("agent_version")
        if event.agent not in self._agents_seen or (
            version and version != self._agents_seen[event.agent]
        ):
            self.db.execute(
                "INSERT INTO agents VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET version=COALESCE(excluded.version,agents.version),updated_at=excluded.updated_at",
                (event.agent, version, now()),
            )
            self._agents_seen[event.agent] = version
        previous_start = self._sessions_seen.get(sid)
        if previous_start is None or previous_start.startswith("1970") or event.ts < previous_start:
            self.db.execute(
                "INSERT INTO sessions(id,agent,native_id,started_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET started_at=CASE WHEN sessions.started_at='1970-01-01T00:00:00.000Z' THEN excluded.started_at WHEN excluded.started_at='1970-01-01T00:00:00.000Z' THEN sessions.started_at ELSE MIN(sessions.started_at,excluded.started_at) END",
                (sid, event.agent, event.session_id, event.ts),
            )
            self._sessions_seen[sid] = event.ts
        if len(self._sessions_seen) > 4096:
            self._sessions_seen.clear()
        if tid:
            self.db.execute(
                "INSERT OR IGNORE INTO turns(id,session_id,agent,native_id) VALUES(?,?,?,?)",
                (tid, sid, event.agent, event.turn_id),
            )
        if event.kind == "session":
            project = None
            if merged.get("cwd"):
                identity, root, remote_hash = project_identity(
                    str(merged["cwd"]), merged.get("remote")
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO projects VALUES(?,?,?)", (identity, root, remote_hash)
                )
                project = identity
            self.db.execute(
                "UPDATE sessions SET agent_version=COALESCE(?,agent_version),project_id=COALESCE(?,project_id),resumed=MAX(resumed,?),data=? WHERE id=?",
                (
                    merged.get("agent_version"),
                    project,
                    int(bool(merged.get("resumed"))),
                    encode(merged),
                    sid,
                ),
            )
        elif event.kind == "turn_start" and tid:
            self.db.execute(
                "UPDATE turns SET started_at=CASE WHEN started_at IS NULL OR started_at>? THEN ? ELSE started_at END,kind=?,prompt=COALESCE(?,prompt),data=? WHERE id=?",
                (ts, ts, merged.get("kind", "prompt"), merged.get("prompt"), encode(merged), tid),
            )
        elif event.kind == "turn_end" and tid:
            self.db.execute(
                "UPDATE turns SET ended_at=MAX(COALESCE(ended_at,''),?),status=?,data=json_patch(data,?) WHERE id=?",
                (event.ts, merged.get("status", "unknown"), encode(merged), tid),
            )
            self.db.execute(
                "UPDATE sessions SET ended_at=MAX(COALESCE(ended_at,''),?) WHERE id=?",
                (event.ts, sid),
            )
        elif event.kind == "model_call":
            fields = [merged.get(k) for k in TOKEN_FIELDS]
            self.db.execute(
                "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET turn_id=excluded.turn_id,model=excluded.model,input_uncached=excluded.input_uncached,input_cache_read=excluded.input_cache_read,input_cache_write=excluded.input_cache_write,output=excluded.output,reasoning=excluded.reasoning,total_reported=excluded.total_reported,cost_reported=excluded.cost_reported,usage_format=excluded.usage_format,data=excluded.data",
                (
                    event_id,
                    sid,
                    tid,
                    event.agent,
                    ts,
                    merged.get("model"),
                    *fields,
                    merged.get("cost_reported"),
                    merged.get("cost_estimated"),
                    merged.get("usage_format"),
                    encode(merged),
                ),
            )
        elif event.kind in {"tool_call", "tool_result"}:
            tool_id = digest(event.agent, event.session_id, event.native_id)
            self.db.execute(
                "INSERT OR IGNORE INTO tool_calls(id,session_id,turn_id,agent,native_id,ts) VALUES(?,?,?,?,?,?)",
                (tool_id, sid, tid, event.agent, event.native_id, ts),
            )
            if event.kind == "tool_call":
                self.db.execute(
                    "UPDATE tool_calls SET turn_id=COALESCE(?,turn_id),tool=?,native_name=?,input=COALESCE(?,input),data=json_patch(data,?) WHERE id=?",
                    (
                        tid,
                        merged.get("tool", "other"),
                        merged.get("native_name"),
                        encode(merged["input"]) if "input" in merged else None,
                        encode(ledger_data),
                        tool_id,
                    ),
                )
            else:
                self.db.execute(
                    "UPDATE tool_calls SET turn_id=COALESCE(?,turn_id),ended_at=?,status=?,duration_ms=?,output=COALESCE(?,output),data=json_patch(data,?) WHERE id=?",
                    (
                        tid,
                        event.ts,
                        merged.get("status", "unknown"),
                        merged.get("duration_ms"),
                        encode(merged["output"]) if "output" in merged else None,
                        encode(ledger_data),
                        tool_id,
                    ),
                )

    def overview(self, start: str = "", end: str = "9999", agent: str | None = None) -> Json:
        where = "ts>=? AND ts<?" + (" AND agent=?" if agent else "")
        args: tuple[object, ...] = (start, end, agent) if agent else (start, end)
        usage = self.rows(
            f"SELECT COUNT(*) calls,SUM(input_uncached) input_uncached,SUM(input_cache_read) input_cache_read,SUM(input_cache_write) input_cache_write,SUM(output) output,SUM(reasoning) reasoning,SUM(cost_reported) cost_reported,SUM(CASE WHEN cost_reported IS NULL THEN cost_estimated END) cost_estimated,SUM(CASE WHEN cost_reported IS NULL AND cost_estimated IS NULL THEN 1 ELSE 0 END) unpriced_calls FROM accounted_model_calls WHERE {where}",
            args,
        )[0]
        counts: Json = {}
        for name, table, time_column, condition in (
            ("sessions", "sessions", "started_at", "1=1"),
            ("turns", "turns", "started_at", "kind='prompt'"),
            ("tools", "tool_calls", "ts", "1=1"),
            ("parse_warnings", "events", "ts", "kind='warning'"),
            ("findings", "findings", "ts", "1=1"),
        ):
            counts[name] = self.rows(
                f"SELECT COUNT(*) n FROM {table} WHERE {time_column}>=? AND {time_column}<? AND {condition}"
                + (" AND agent=?" if agent else ""),
                args,
            )[0]["n"]
        usage.update(counts)
        usage["agents"] = self.rows(
            f"SELECT agent,COUNT(*) calls,SUM(COALESCE(input_uncached,0)+COALESCE(input_cache_read,0)+COALESCE(input_cache_write,0)+COALESCE(output,0)+COALESCE(reasoning,0)) tokens,SUM(COALESCE(cost_reported,cost_estimated)) value FROM accounted_model_calls WHERE {where} GROUP BY agent",
            args,
        )
        usage["daily"] = self.rows(
            f"SELECT substr(ts,1,10) day,COUNT(*) calls,SUM(COALESCE(input_uncached,0)+COALESCE(input_cache_read,0)+COALESCE(input_cache_write,0)+COALESCE(output,0)+COALESCE(reasoning,0)) tokens FROM accounted_model_calls WHERE {where} GROUP BY day ORDER BY day DESC LIMIT 30",
            args,
        )
        return usage
