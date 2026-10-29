"""Generate rich deterministic synthetic data and take high-resolution screenshots for docs/README."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
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


def build_synthetic_store(config: Config) -> None:
    store = Store(config.database)
    current = datetime.now(UTC)

    # 1. Antigravity Parent Session
    ag_session = "demo-antigravity-parent"
    dt_ag = current - timedelta(minutes=45)
    stamp_ag = utc(dt_ag.isoformat())

    with store.transaction():
        # Antigravity session
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "session",
                stamp_ag,
                ag_session,
                data={
                    "cwd": "/workspace/tracebook",
                    "agent_version": "1.4.8",
                    "parent_native_id": None,
                    "is_subagent": False,
                },
            )
        )

        # Turn 1
        t1 = f"{ag_session}-turn-1"
        t1_start = dt_ag
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "turn_start",
                utc(t1_start.isoformat()),
                t1,
                t1,
                data={"prompt": "Investigate prompt cache behavior and turn latency breakdown"},
            )
        )
        c1 = f"{t1}-call-1"
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "model_call",
                utc((t1_start + timedelta(seconds=1)).isoformat()),
                c1,
                t1,
                data={
                    "model": "Gemini 3.8 Flash (High)",
                    "input_uncached": 2420,
                    "input_cache_read": 164000,
                    "output": 420,
                    "reasoning": 180,
                    "cost_reported": 0.016,
                    "duration_ms": 1950,
                },
            )
        )
        tool1 = f"{t1}-tool-1"
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "tool_call",
                utc((t1_start + timedelta(seconds=3)).isoformat()),
                tool1,
                t1,
                data={
                    "tool": "read_file",
                    "native_name": "read_file",
                    "input": {
                        "Path": "/workspace/tracebook/src/tracebook/adapters/antigravity.py",
                        "StartLine": 1,
                        "EndLine": 45,
                    },
                },
            )
        )
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "tool_result",
                utc((t1_start + timedelta(seconds=3, milliseconds=350)).isoformat()),
                tool1,
                t1,
                data={
                    "status": "completed",
                    "output": "File contents (45 lines)",
                    "duration_ms": 350,
                },
            )
        )
        c2 = f"{t1}-call-2"
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "model_call",
                utc((t1_start + timedelta(seconds=4)).isoformat()),
                c2,
                t1,
                data={
                    "model": "Gemini 3.8 Flash (High)",
                    "input_uncached": 1850,
                    "input_cache_read": 166500,
                    "output": 840,
                    "reasoning": 320,
                    "cost_reported": 0.019,
                    "duration_ms": 2200,
                },
            )
        )
        tool2 = f"{t1}-tool-2"
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "tool_call",
                utc((t1_start + timedelta(seconds=6)).isoformat()),
                tool2,
                t1,
                data={
                    "tool": "shell",
                    "native_name": "run_command",
                    "input": {"CommandLine": "uv run pytest tests/test_antigravity.py"},
                },
            )
        )
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "tool_result",
                utc((t1_start + timedelta(seconds=7, milliseconds=800)).isoformat()),
                tool2,
                t1,
                data={"status": "completed", "output": "76 passed in 2.40s", "duration_ms": 1800},
            )
        )
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "turn_end",
                utc((t1_start + timedelta(seconds=8)).isoformat()),
                t1,
                t1,
                data={"status": "completed"},
            )
        )

        # Turn 2 - Model Switch to Gemini 3 Argon
        t2 = f"{ag_session}-turn-2"
        t2_start = dt_ag + timedelta(minutes=5)
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "turn_start",
                utc(t2_start.isoformat()),
                t2,
                t2,
                data={"prompt": "Render interactive context trajectory and model switch badges"},
            )
        )
        c3 = f"{t2}-call-1"
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "model_call",
                utc((t2_start + timedelta(seconds=1)).isoformat()),
                c3,
                t2,
                data={
                    "model": "Gemini 4 Argon (High)",
                    "input_uncached": 2900,
                    "input_cache_read": 172000,
                    "output": 1250,
                    "reasoning": 650,
                    "cost_reported": 0.038,
                    "duration_ms": 2900,
                },
            )
        )
        store.ingest(
            Event(
                "antigravity",
                ag_session,
                "turn_end",
                utc((t2_start + timedelta(seconds=4)).isoformat()),
                t2,
                t2,
                data={"status": "completed"},
            )
        )

        # 2. Antigravity Subagent (Child Session)
        child_session = "demo-antigravity-subagent-auditor"
        store.ingest(
            Event(
                "antigravity",
                child_session,
                "session",
                utc((dt_ag + timedelta(minutes=1)).isoformat()),
                child_session,
                data={
                    "cwd": "/workspace/tracebook",
                    "agent_version": "1.4.8",
                    "parent_native_id": ag_session,
                    "subagent_role": "Codebase Auditor",
                    "subagent_type": "research",
                    "is_subagent": True,
                },
            )
        )
        ct1 = f"{child_session}-turn-1"
        store.ingest(
            Event(
                "antigravity",
                child_session,
                "turn_start",
                utc((dt_ag + timedelta(minutes=1, seconds=2)).isoformat()),
                ct1,
                ct1,
                data={"prompt": "Audit recent adapters for schema consistency and token tracking"},
            )
        )
        store.ingest(
            Event(
                "antigravity",
                child_session,
                "model_call",
                utc((dt_ag + timedelta(minutes=1, seconds=4)).isoformat()),
                f"{ct1}-call-1",
                ct1,
                data={
                    "model": "Gemini 3.8 Flash (High)",
                    "input_uncached": 3400,
                    "input_cache_read": 45000,
                    "output": 750,
                    "cost_reported": 0.009,
                    "duration_ms": 1400,
                },
            )
        )
        store.ingest(
            Event(
                "antigravity",
                child_session,
                "turn_end",
                utc((dt_ag + timedelta(minutes=1, seconds=6)).isoformat()),
                ct1,
                ct1,
                data={"status": "completed"},
            )
        )

        # 3. Claude Code Session
        claude_session = "demo-claude-refactor"
        dt_cl = current - timedelta(hours=2)
        store.ingest(
            Event(
                "claude_code",
                claude_session,
                "session",
                utc(dt_cl.isoformat()),
                claude_session,
                data={"cwd": "/workspace/tracebook", "agent_version": "2.1.286"},
            )
        )
        for i in range(4):
            turn_id = f"{claude_session}-turn-{i}"
            t_start = dt_cl + timedelta(minutes=i * 4)
            store.ingest(
                Event(
                    "claude_code",
                    claude_session,
                    "turn_start",
                    utc(t_start.isoformat()),
                    turn_id,
                    turn_id,
                    data={
                        "prompt": f"Refactor prompt metrics and add test coverage for turn #{i + 1}"
                    },
                )
            )
            store.ingest(
                Event(
                    "claude_code",
                    claude_session,
                    "model_call",
                    utc((t_start + timedelta(seconds=2)).isoformat()),
                    f"{turn_id}-call-1",
                    turn_id,
                    data={
                        "model": "claude-sonnet-5-5",
                        "input_uncached": 2,
                        "input_cache_read": 35000 + i * 8500,
                        "input_cache_write": 2800,
                        "output": 560,
                        "reasoning": 310,
                        "cost_reported": 0.045,
                        "duration_ms": 2800,
                    },
                )
            )
            store.ingest(
                Event(
                    "claude_code",
                    claude_session,
                    "tool_call",
                    utc((t_start + timedelta(seconds=4)).isoformat()),
                    f"{turn_id}-tool-1",
                    turn_id,
                    data={
                        "tool": "file_edit",
                        "native_name": "Edit",
                        "input": {"file_path": "frontend/src/PromptCard.tsx"},
                    },
                )
            )
            store.ingest(
                Event(
                    "claude_code",
                    claude_session,
                    "tool_result",
                    utc((t_start + timedelta(seconds=4, milliseconds=200)).isoformat()),
                    f"{turn_id}-tool-1",
                    turn_id,
                    data={
                        "status": "completed",
                        "output": "File updated successfully",
                        "duration_ms": 200,
                    },
                )
            )
            store.ingest(
                Event(
                    "claude_code",
                    claude_session,
                    "turn_end",
                    utc((t_start + timedelta(seconds=5)).isoformat()),
                    turn_id,
                    turn_id,
                    data={"status": "completed"},
                )
            )

        # 4. OpenAI Codex Session
        codex_session = "demo-codex-fastapi"
        dt_cx = current - timedelta(hours=5)
        store.ingest(
            Event(
                "codex",
                codex_session,
                "session",
                utc(dt_cx.isoformat()),
                codex_session,
                data={"cwd": "/workspace/service", "agent_version": "0.164.0"},
            )
        )
        cx_turn = f"{codex_session}-turn-1"
        store.ingest(
            Event(
                "codex",
                codex_session,
                "turn_start",
                utc(dt_cx.isoformat()),
                cx_turn,
                cx_turn,
                data={"prompt": "Migrate API routes to Starlette endpoints and add health check"},
            )
        )
        store.ingest(
            Event(
                "codex",
                codex_session,
                "model_call",
                utc((dt_cx + timedelta(seconds=1)).isoformat()),
                f"{cx_turn}-call-1",
                cx_turn,
                data={
                    "model": "gpt-5-codex",
                    "input_uncached": 1400,
                    "input_cache_read": 18000,
                    "output": 890,
                    "cost_reported": 0.028,
                    "duration_ms": 2100,
                },
            )
        )
        store.ingest(
            Event(
                "codex",
                codex_session,
                "turn_end",
                utc((dt_cx + timedelta(seconds=3)).isoformat()),
                cx_turn,
                cx_turn,
                data={"status": "completed"},
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
    store.close()


def run_server(db_dir: str, port: int) -> None:
    config = Config(root=Path(db_dir), port=port)
    store = Store(config.database)
    app = create_api(store, config, adapters())
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def main() -> None:
    port = 19876
    with tempfile.TemporaryDirectory(prefix="tracebook-demo-") as tmp_dir:
        config = Config(root=Path(tmp_dir), port=port)
        print("Building rich synthetic demo database...")
        build_synthetic_store(config)

        print(f"Starting API server on port {port}...")
        proc = multiprocessing.Process(target=run_server, args=(tmp_dir, port), daemon=True)
        proc.start()

        # Wait for server ready
        time.sleep(1.5)

        print("Executing Playwright screenshot capture...")
        env = os.environ.copy()
        env["PORT"] = str(port)
        result = subprocess.run(["node", "scripts/take_screenshots.js"], env=env)

        proc.terminate()
        proc.join(timeout=2)

        if result.returncode != 0:
            print("Screenshot capture failed with code:", result.returncode)
            sys.exit(result.returncode)

        print("Screenshots captured successfully in docs/images/")


if __name__ == "__main__":
    main()
