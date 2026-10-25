"""Resumable byte checkpoints. Source files are opened read-only."""
from __future__ import annotations

import csv
import json
import os
import time
import tomllib
from pathlib import Path

import orjson

from tracebook.adapters.base import Adapter
from tracebook.model import Event, Json, Source, digest, obj, utc
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

    def scan(self) -> None:
        self.sources = [(adapter, source) for adapter in self.adapters.values() for source in adapter.transcript_sources()]
        self.last_scan = time.monotonic()

    def record(self, adapter: Adapter, record: Json, state: Json, source: str = "transcript") -> int:
        self.store.raw(adapter.id, source, record)
        try:
            events = list(adapter.parse_record(record, state))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as exc:
            events = [adapter.event(record, state, "warning", data={"reason": type(exc).__name__, "native_type": record.get("type")})]
        for event in events:
            event.source = source
            self.store.ingest(event)
        return len(events)

    def tail(self, adapter: Adapter, source: Source) -> int:
        path = Path(source.path)
        count = 0
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            key = digest(adapter.id, stat.st_dev, stat.st_ino)
            checkpoint = self.store.state(key)
            offset = int(checkpoint.get("offset", 0))
            parser = obj(checkpoint.get("parser"))
            parser.setdefault("session_id", digest(adapter.id, path.name))
            if source.format in {"json", "otel_file"}:
                if checkpoint.get("mtime_ns") == stat.st_mtime_ns and checkpoint.get("size") == stat.st_size:
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
                    if source.format == "otel_file":
                        from tracebook.otel import ingest_otlp
                        count = ingest_otlp(self.store, self.adapters, obj(value), "logs", own_transaction=False)
                    else:
                        parser = {"session_id": parser["session_id"]}
                        count = self.record(adapter, obj(value), parser)
                    self.store.checkpoint(key, str(path), adapter.id, {"offset": stat.st_size, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "parser": parser})
                return count
            if offset > stat.st_size:
                offset, parser = 0, {"session_id": parser["session_id"], "resumed": True}
            if offset and checkpoint.get("tail_hash"):
                stream.seek(max(0, offset - 128))
                if digest(stream.read(min(offset, 128))) != checkpoint["tail_hash"]:
                    offset, parser = 0, {"session_id": parser["session_id"], "resumed": True}
            stream.seek(offset)
            blob = stream.read(MAX_LINE)
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
                        count += self.spool_record(record)
                    else:
                        count += self.record(adapter, record, parser)
                offset += consumed
                stream.seek(max(0, offset - 128))
                tail_hash = digest(stream.read(min(offset, 128)))
                self.store.checkpoint(key, str(path), adapter.id, {"offset": offset, "size": stat.st_size, "tail_hash": tail_hash, "parser": parser})
        return count

    def spool_record(self, envelope: Json) -> int:
        agent = str(envelope.get("agent", "generic"))
        adapter = self.adapters.get(agent, self.adapters["generic"])
        self.store.raw(agent, "hook", envelope)
        if envelope.get("canonical"):
            event = Event(**obj(envelope["canonical"]))
            self.store.ingest(event)
            return 1
        payload = obj(envelope.get("payload"))
        payload.setdefault("timestamp", utc(envelope.get("received_at")))
        payload.setdefault("hook_event_name", envelope.get("event"))
        events = list(adapter.parse_hook(payload, [], {}))
        for event in events:
            if event.clock_source == "unknown":
                event.clock_source = "receive_wall"
            self.store.ingest(event)
        return len(events)

    def poll(self, spool: Path, scan_seconds: float = 30) -> int:
        if time.monotonic() - self.last_scan > scan_seconds:
            self.scan()
        count = 0
        sources = [*self.sources, *((self.adapters["generic"], Source(str(p), "spool")) for p in spool.glob("*.jsonl"))]
        for adapter, source in sources:
            try:
                count += self.tail(adapter, source)
                self.errors.pop(source.path, None)
            except (OSError, ValueError) as exc:
                self.errors[source.path] = str(exc)
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
