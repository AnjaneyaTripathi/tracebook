"""Serve deterministic synthetic data for browser QA. Never reads local agent data."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

from tracebook.adapters import adapters
from tracebook.analysis import analyze
from tracebook.api import create_api
from tracebook.config import Config
from tracebook.model import Event, utc
from tracebook.reports import report
from tracebook.store import Store


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tracebook-preview-") as directory:
        config = Config(root=Path(directory), port=18742, pricing_enabled=True)
        store = Store(config.database)
        current = datetime.now(UTC)
        prompts = [
            "[Example] Add request tracing to the checkout API",
            "[Example] Fix session expiry after a refresh",
            "[Example] Review the database migration",
            "[Example] Build an accessible search interface",
            "[Example] Investigate the failing integration tests",
            "[Example] Document the background job lifecycle",
        ]
        with store.transaction():
            for day in range(14):
                for agent_index, agent in enumerate(("claude_code", "codex", "gemini_cli")):
                    session = f"example-{day}-{agent}"
                    dt = current - timedelta(days=day, minutes=agent_index * 37)
                    stamp = utc(dt.isoformat())
                    store.ingest(
                        Event(
                            agent,
                            session,
                            "session",
                            stamp,
                            session,
                            data={
                                "cwd": f"/example/{'storefront' if agent_index != 2 else 'design-system'}",
                                "agent_version": "example",
                            },
                        )
                    )
                    for index in range(3):
                        turn = f"{session}-turn-{index}"
                        store.ingest(
                            Event(
                                agent,
                                session,
                                "turn_start",
                                stamp,
                                turn,
                                turn,
                                data={
                                    "prompt": prompts[(day + agent_index + index) % len(prompts)]
                                },
                            )
                        )
                        for call in range(2 + day % 3):
                            native = f"{turn}-call-{call}"
                            store.ingest(
                                Event(
                                    agent,
                                    session,
                                    "model_call",
                                    stamp,
                                    native,
                                    turn,
                                    data={
                                        "model": "example-model",
                                        "input_uncached": 1200 + (14 - day) * 134 + call * 80,
                                        "input_cache_read": 8000 + day * 390,
                                        "output": 400 + call * 119,
                                        "reasoning": 220,
                                        "cost_reported": 0.025 + call * 0.01,
                                        "duration_ms": 2500,
                                    },
                                )
                            )
                            store.ingest(
                                Event(
                                    agent,
                                    session,
                                    "tool_call",
                                    stamp,
                                    native,
                                    turn,
                                    data={
                                        "tool": "shell",
                                        "native_name": "run_tests",
                                        "input": {"command": "pytest tests/test_checkout.py"},
                                    },
                                )
                            )
                            store.ingest(
                                Event(
                                    agent,
                                    session,
                                    "tool_result",
                                    stamp,
                                    native,
                                    turn,
                                    data={
                                        "status": "error" if day == 1 else "completed",
                                        "output": "Example: assertion failed"
                                        if day == 1
                                        else "Example: 8 passed",
                                        "duration_ms": 1800,
                                    },
                                )
                            )
                        store.ingest(
                            Event(
                                agent,
                                session,
                                "turn_end",
                                utc((dt + timedelta(seconds=45)).isoformat()),
                                turn,
                                turn,
                                data={"status": "error" if day == 1 else "completed"},
                            )
                        )
            # A session with no recorded usage must stay out of the default session list.
            store.ingest(
                Event(
                    "codex",
                    "example-idle",
                    "session",
                    utc(current.isoformat()),
                    "example-idle",
                    data={"cwd": "/example/storefront"},
                )
            )
        analyze(store)
        report(
            store,
            config,
            adapters(),
            utc((current - timedelta(days=1)).isoformat()),
            utc(current.isoformat()),
            enqueue=False,
        )
        uvicorn.run(
            create_api(store, config, adapters()),
            host="127.0.0.1",
            port=config.port,
            log_level="warning",
            access_log=False,
        )
        store.close()


if __name__ == "__main__":
    main()
