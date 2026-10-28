from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tracebook import hook
from tracebook.adapters.antigravity import AntigravityAdapter
from tracebook.testing import conform


def test_antigravity_metadata_and_doctor(monkeypatch: object) -> None:
    adapter = AntigravityAdapter()
    assert adapter.id == "antigravity"
    assert adapter.binary == "agy"
    caps = adapter.capabilities()
    assert caps.transcripts == "yes"
    assert caps.token_input == "yes"
    assert caps.token_output == "yes"

    checks = {c.name: c for c in adapter.doctor()}
    assert "installed" in checks
    assert "transcripts" in checks
    assert "verification" in checks


def test_antigravity_conformance() -> None:
    adapter = AntigravityAdapter()
    fixture = Path("tests/fixtures/antigravity/1.4.8/observed.jsonl")
    events = conform(adapter, [fixture], complete=True)

    kinds = {e.kind for e in events}
    assert {"session", "turn_start", "model_call", "tool_call", "tool_result", "turn_end"} <= kinds

    calls = [e for e in events if e.kind == "model_call"]
    assert len(calls) == 4
    assert calls[0].data["input_uncached"] == 1200
    assert calls[0].data["input_cache_read"] == 300
    assert calls[0].data["output"] == 85

    tools = [e for e in events if e.kind == "tool_call"]
    assert len(tools) == 2
    assert tools[0].data["tool"] == "file_read"
    assert tools[0].data["native_name"] == "read_file"
    assert tools[0].data["input"]["file_path"] == "/workspace/example/file.py"

    assert tools[1].data["tool"] == "shell"
    assert tools[1].data["native_name"] == "run_command"
    assert tools[1].data["input"]["command"] == "pytest"

    results = [e for e in events if e.kind == "tool_result"]
    assert len(results) == 2
    assert results[0].data["status"] == "completed"
    assert results[1].data["status"] == "error"


def test_antigravity_hook_install_and_uninstall(tmp_path: Path, monkeypatch: object) -> None:
    adapter = AntigravityAdapter()
    config_file = tmp_path / ".gemini" / "config" / "hooks.json"
    adapter.hook_config_path = lambda: config_file  # type: ignore[method-assign]

    diff = adapter.install_hooks(dry_run=True)
    assert diff.changed
    assert not config_file.exists()

    diff_applied = adapter.install_hooks(dry_run=False)
    assert diff_applied.changed
    assert config_file.exists()

    data = json.loads(config_file.read_text())
    assert "tracebook" in data
    assert "PreToolUse" in data["tracebook"]
    assert "PostToolUse" in data["tracebook"]
    assert "SessionStart" in data["tracebook"]
    assert "Stop" in data["tracebook"]

    diff_idempotent = adapter.install_hooks(dry_run=False)
    assert not diff_idempotent.changed

    diff_uninstall = adapter.uninstall_hooks(dry_run=False)
    assert diff_uninstall.changed
    remaining = config_file.read_text().strip()
    assert remaining == "" or json.loads(remaining) == {}


def test_antigravity_parse_hook() -> None:
    adapter = AntigravityAdapter()

    pre_tool = list(
        adapter.parse_hook(
            {
                "conversationId": "conv-123",
                "stepIdx": 5,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "ls -la"}},
            },
            argv=["--agent", "antigravity", "--event", "PreToolUse"],
        )
    )
    assert len(pre_tool) == 1
    assert pre_tool[0].kind == "tool_call"
    assert pre_tool[0].data["tool"] == "shell"
    assert pre_tool[0].data["input"]["command"] == "ls -la"

    post_tool = list(
        adapter.parse_hook(
            {
                "conversationId": "conv-123",
                "stepIdx": 5,
                "error": "command not found",
            },
            argv=["--agent", "antigravity", "--event", "PostToolUse"],
        )
    )
    assert len(post_tool) == 1
    assert post_tool[0].kind == "tool_result"
    assert post_tool[0].data["status"] == "error"


def test_antigravity_hook_subprocess(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-S",
        hook.__file__,
        "--agent",
        "antigravity",
        "--event",
        "PostToolUse",
        "--home",
        str(tmp_path),
    ]
    payload = json.dumps({"conversationId": "test-conv", "stepIdx": 1})
    result = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    assert result.stderr == ""
