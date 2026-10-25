"""Resumable byte checkpoints. Source files are opened read-only."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import sqlite3
import tempfile
import time
import tomllib
from contextlib import closing
from pathlib import Path

import orjson

from tracebook.adapters.base import Adapter
from tracebook.model import Event, Json, Source, digest, obj, utc
from tracebook.platform import process_alive
from tracebook.store import Store

MAX_LINE = 8 * 1024 * 1024
MAX_SNAPSHOT = 64 * 1024 * 1024


class Capture:
    def __init__(self, store: Store, adapters: dict[str, Adapter]) -> None:
        self.store = store
        self.adapters = adapters
        self.sources: list[tuple[Adapter, Source]] = []
        self.last_scan = 0.0
        self.errors: dict[str, str] = {}
        self.since: str | None = None
        self.unchanged: dict[str, tuple[int, int, int]] = {}
        self.active_sources: set[str] = set()

    def scan(self) -> None:
        self.sources = []
        for adapter in self.adapters.values():
            try:
                self.sources.extend((adapter, source) for source in adapter.transcript_sources())
                self.errors.pop(adapter.id, None)
            except (OSError, ValueError) as exc:
                self.errors[adapter.id] = type(exc).__name__
        self.last_scan = time.monotonic()

    def record(
        self, adapter: Adapter, record: Json, state: Json, source: str = "transcript"
    ) -> int:
        self.store.raw(adapter.id, source, record)
        try:
            events = list(adapter.parse_record(record, state))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as exc:
            events = [
                adapter.event(
                    record,
                    state,
                    "warning",
                    data={"reason": type(exc).__name__, "native_type": record.get("type")},
                )
            ]
        for event in events:
            event.source = source
            if not self.since or event.ts >= self.since or event.kind == "session":
                self.store.ingest(event)
        return len(events)

    def tail(self, adapter: Adapter, source: Source) -> int:
        if source.format == "sqlite":
            return self.database(adapter, source)
        path = Path(source.path)
        count = 0
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            key = digest(adapter.id, stat.st_dev, stat.st_ino)
            checkpoint = self.store.state(key)
            offset = int(checkpoint.get("offset", 0))
            parser = obj(checkpoint.get("parser"))
            parser.setdefault("session_id", digest(adapter.id, path.name))
            if source.format == "json":
                if (
                    checkpoint.get("mtime_ns") == stat.st_mtime_ns
                    and checkpoint.get("size") == stat.st_size
                ):
                    return 0
                if stat.st_size > MAX_SNAPSHOT:
                    raise ValueError("Session snapshot exceeds 64 MiB safety limit")
                blob = stream.read(MAX_SNAPSHOT + 1)
                after = os.fstat(stream.fileno())
                if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
                    return 0
                try:
                    value = orjson.loads(blob)
                except ValueError:
                    # Atomic snapshots can be in the middle of a write. Retry later.
                    return 0
                with self.store.transaction():
                    parser = {"session_id": parser["session_id"]}
                    count = self.record(adapter, obj(value), parser)
                    self.store.checkpoint(
                        key,
                        str(path),
                        adapter.id,
                        {
                            "offset": stat.st_size,
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "parser": parser,
                        },
                    )
                return count
            if offset > stat.st_size:
                offset, parser = 0, {"session_id": parser["session_id"], "resumed": True}
            if offset == stat.st_size and checkpoint.get("mtime_ns") not in {
                None,
                stat.st_mtime_ns,
            }:
                offset, parser = 0, {"session_id": parser["session_id"], "resumed": True}
            if offset and checkpoint.get("tail_hash"):
                stream.seek(max(0, offset - 128))
                if digest(stream.read(min(offset, 128))) != checkpoint["tail_hash"]:
                    offset, parser = 0, {"session_id": parser["session_id"], "resumed": True}
            stream.seek(offset)
            blob = stream.read(MAX_LINE)
            if source.format == "otel_file":
                decoder = json.JSONDecoder()
                document = blob.decode("utf-8", errors="ignore")
                position = 0
                parsed: list[Json] = []
                while position < len(document):
                    start = position
                    while start < len(document) and document[start].isspace():
                        start += 1
                    try:
                        value, end = decoder.raw_decode(document, start)
                    except ValueError:
                        break
                    parsed.append(obj(value))
                    position = end
                consumed = len(document[:position].encode())
                if not consumed:
                    return 0
                with self.store.transaction():
                    for value in parsed:
                        self.store.raw(adapter.id, "otel", value)
                        for event in adapter.parse_otel_file(value):
                            self.store.ingest(event)
                            count += 1
                    offset += consumed
                    stream.seek(max(0, offset - 128))
                    self.store.checkpoint(
                        key,
                        str(path),
                        adapter.id,
                        {
                            "offset": offset,
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "tail_hash": digest(stream.read(min(offset, 128))),
                            "parser": parser,
                        },
                    )
                return count
            final_newline = blob.rfind(b"\n")
            if final_newline < 0:
                if len(blob) < MAX_LINE:
                    return 0
                # Skip an oversized record without mistaking its suffix for a new record.
                parser["discard_until_newline"] = True
                consumed = len(blob)
                lines = [b'{"type":"tracebook_oversized_record"}']
            else:
                consumed = final_newline + 1
                lines = blob[:consumed].splitlines()
                if parser.pop("discard_until_newline", False):
                    lines = lines[1:]
            with self.store.transaction():
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        record = orjson.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("Not an object")
                    except ValueError:
                        record = {"type": "malformed_json", "line": line.decode(errors="replace")}
                    if source.format == "spool":
                        try:
                            count += self.spool_record(record)
                        except (TypeError, ValueError, KeyError, AttributeError) as exc:
                            count += self.record(
                                adapter,
                                {
                                    "type": "malformed_spool",
                                    "reason": type(exc).__name__,
                                    "payload": record,
                                },
                                parser,
                            )
                    else:
                        count += self.record(adapter, record, parser)
                offset += consumed
                stream.seek(max(0, offset - 128))
                tail_hash = digest(stream.read(min(offset, 128)))
                self.store.checkpoint(
                    key,
                    str(path),
                    adapter.id,
                    {
                        "offset": offset,
                        "size": stat.st_size,
                        "tail_hash": tail_hash,
                        "mtime_ns": stat.st_mtime_ns,
                        "parser": parser,
                    },
                )
        return count

    def database(self, adapter: Adapter, source: Source) -> int:
        path = Path(source.path).resolve()
        key = digest(adapter.id, "sqlite", str(path))
        checkpoint = self.store.state(key)
        stamps = []
        for item in (path, Path(str(path) + "-wal")):
            if item.exists():
                stat = item.stat()
                stamps.append([stat.st_ino, stat.st_size, stat.st_mtime_ns])
        if checkpoint.get("stamps") == stamps:
            return 0
        count = 0
        state = obj(checkpoint.get("parser"))
        # Online backup includes committed WAL pages without writing agent tables.
        with (
            tempfile.TemporaryDirectory(prefix="tracebook-snapshot-") as directory,
            closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as origin,
            closing(sqlite3.connect(Path(directory) / "snapshot.db")) as snapshot,
        ):
            deadline = time.monotonic() + 5

            def progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() > deadline:
                    raise ValueError("Database snapshot busy; retrying later")

            origin.backup(snapshot, pages=256, progress=progress, sleep=0.01)
            snapshot.row_factory = sqlite3.Row
            with self.store.transaction():
                for record in adapter.database_records(snapshot, state):
                    count += self.record(adapter, record, state)
                self.store.checkpoint(
                    key, str(path), adapter.id, {"stamps": stamps, "parser": state}
                )
        return count

    def spool_record(self, envelope: Json) -> int:
        agent = str(envelope.get("agent", "generic"))
        adapter = self.adapters.get(agent, self.adapters["generic"])
        if "payload_raw" in envelope:
            raw = envelope.pop("payload_raw")
            try:
                payload = obj(orjson.loads(raw))
            except ValueError:
                payload = {
                    "truncated": bool(envelope.get("truncated")),
                    "parse_error": True,
                    "unparsed": raw,
                }
            envelope["payload"] = payload
        self.store.raw(agent, "hook", envelope)
        if envelope.get("canonical"):
            event = Event(**obj(envelope["canonical"]))
            self.store.ingest(event)
            return 1
        payload = obj(envelope.get("payload"))
        if payload.get("parse_error"):
            self.store.ingest(
                Event(
                    agent,
                    "unknown",
                    "warning",
                    utc(envelope.get("received_at")),
                    digest(envelope),
                    source="hook",
                    data={
                        "reason": "truncated_hook_payload"
                        if payload.get("truncated")
                        else "malformed_hook_payload"
                    },
                )
            )
            return 1
        payload.setdefault("timestamp", utc(envelope.get("received_at")))
        if envelope.get("event"):
            payload.setdefault("hook_event_name", envelope["event"])
        events = list(adapter.parse_hook(payload, [], {}))
        for event in events:
            if event.clock_source == "unknown":
                event.clock_source = "receive_wall"
            self.store.ingest(event)
        return len(events)

    def poll_sources(self, spool: Path, scan_seconds: float) -> list[tuple[Adapter, Source]]:
        scanning = time.monotonic() - self.last_scan > scan_seconds
        if scanning:
            self.scan()
        return [
            *((self.adapters["generic"], Source(str(p), "spool")) for p in spool.glob("*.jsonl")),
            *(
                (adapter, source)
                for adapter, source in self.sources
                if scanning
                or source.path in self.active_sources
                or source.path not in self.unchanged
                or source.format == "sqlite"
            ),
        ]

    def poll_one(self, adapter: Adapter, source: Source) -> int:
        try:
            stat = Path(source.path).stat()
            if stat.st_mtime > time.time() - 120:
                self.active_sources.add(source.path)
            else:
                self.active_sources.discard(source.path)
            fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if (
                source.format not in {"sqlite", "spool"}
                and self.unchanged.get(source.path) == fingerprint
            ):
                return 0
            count = self.tail(adapter, source)
            if not count:
                self.unchanged[source.path] = fingerprint
            if len(self.unchanged) > 10000:
                self.unchanged.clear()
            self.errors.pop(source.path, None)
            if source.format == "spool":
                self.collect_spool(adapter, Path(source.path))
            return count
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.errors[source.path] = type(exc).__name__
            return 0

    def collect_spool(self, adapter: Adapter, path: Path) -> None:
        match = re.fullmatch(r"(\d+)-(\d+)\.jsonl", path.name)
        if not match or process_alive(int(match[1])):
            return
        stat = path.stat()
        key = digest(adapter.id, stat.st_dev, stat.st_ino)
        if self.store.state(key).get("offset") == stat.st_size:
            # The shim has exited and the complete file is durably checkpointed.
            path.unlink()
            self.store.db.execute("DELETE FROM adapter_state WHERE id=?", (key,))

    def poll(self, spool: Path, scan_seconds: float = 30) -> int:
        return sum(self.poll_one(a, s) for a, s in self.poll_sources(spool, scan_seconds))

    async def poll_async(self, spool: Path, scan_seconds: float = 30) -> int:
        count = 0
        for adapter, source in self.poll_sources(spool, scan_seconds):
            count += self.poll_one(adapter, source)
            # Keep all SQLite writes on the event loop while yielding between files.
            await asyncio.sleep(0)
        return count

    def import_file(self, path: Path, mapping: Path) -> int:
        state: Json = {"mapping": tomllib.loads(mapping.read_text())}
        adapter = self.adapters["generic"]
        count = 0
        with path.open() as stream, self.store.transaction():
            if path.suffix.lower() == ".csv":
                records = (dict(row) for row in csv.DictReader(stream))
            else:
                records = (obj(json.loads(line)) for line in stream if line.strip())
            for record in records:
                count += self.record(adapter, record, state, "import")
        return count
