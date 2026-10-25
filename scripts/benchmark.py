"""Reproducible local measurements. Run: uv run python scripts/benchmark.py."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

from tracebook import hook
from tracebook.adapters import adapters
from tracebook.capture import Capture
from tracebook.model import Source
from tracebook.store import Store


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tracebook-bench-") as directory:
        root = Path(directory)
        env = {**os.environ, "TRACEBOOK_HOME": str(root / "hook")}
        durations = []
        for _ in range(120):
            start = time.perf_counter()
            result = subprocess.run(
                [sys.executable, "-S", hook.__file__, "--agent", "claude_code"],
                input=b'{"session_id":"benchmark","hook_event_name":"Stop"}',
                capture_output=True,
                env=env,
                check=True,
            )
            assert not result.stdout and not result.stderr
            durations.append((time.perf_counter() - start) * 1000)
        path = root / "transcript.jsonl"
        with path.open("w") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {"id": "bench"},
                    }
                )
                + "\n"
            )
            for i in range(12000):
                stream.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": f"call-{i}",
                                "output": "test output " * 340,
                            },
                        }
                    )
                    + "\n"
                )
        store = Store(root / "capture/db.sqlite3")
        registry = adapters()
        capture = Capture(store, registry)
        start = time.perf_counter()
        while capture.tail(registry["codex"], Source(str(path))):
            pass
        elapsed = time.perf_counter() - start
        size = path.stat().st_size
        store.close()
        daemon_root = root / "daemon"
        daemon_root.mkdir()
        (daemon_root / "config.toml").write_text("port=18742\notlp_port=14318\n")
        empty = root / "empty"
        empty.mkdir()
        command = [
            str(Path(sys.executable).parent / "tracebook"),
            "--home",
            str(daemon_root),
            "daemon",
        ]
        child = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "CLAUDE_CONFIG_DIR": str(empty),
                "CODEX_HOME": str(empty),
                "GEMINI_CLI_HOME": str(empty),
            },
        )
        try:
            time.sleep(2)
            if child.poll() is not None:
                raise RuntimeError(
                    child.stderr.read().decode() if child.stderr else "Daemon exited"
                )
            process = psutil.Process(child.pid)
            rss = process.memory_info().rss / 1024 / 1024
            cpu = process.cpu_percent(interval=5)
        finally:
            child.terminate()
            child.wait(timeout=10)
        result = {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "hook_launch_samples": len(durations),
            "hook_p50_ms": round(statistics.median(durations), 2),
            "hook_p95_ms": round(sorted(durations)[int(len(durations) * 0.95)], 2),
            "transcript_records": 12001,
            "transcript_bytes": size,
            "ingest_seconds": round(elapsed, 3),
            "ingest_MB_per_second": round(size / elapsed / 1_000_000, 2),
            "idle_cpu_percent_5s": cpu,
            "daemon_RSS_MiB": round(rss, 1),
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
