from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from tracebook.cli import app
from tracebook.config import Config
from tracebook.service import install_service


def test_cli_private_lifecycle_import_wrapper_and_report(tmp_path, monkeypatch):
    for env in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME"):
        monkeypatch.setenv(env, str(tmp_path / "agents"))
    runner = CliRunner()
    prefix = ["--home", str(tmp_path / "state")]

    def run(*args):
        result = runner.invoke(app, [*prefix, *args])
        assert result.exit_code == 0, (result.output, result.exception)
        return result.output

    run("init")
    run("init")
    assert "zero_model_tokens" in run("doctor")
    for agent in ("claude_code", "codex", "gemini_cli"):
        assert "127.0.0.1" in run("otel-env", "--agent", agent)
        run("install-hooks", "--agent", agent)
        run("install-hooks", "--agent", agent, "--apply")
        run("uninstall-hooks", "--agent", agent, "--apply")
    mapping = tmp_path / "mapping.toml"
    mapping.write_text(
        '[fields]\nsession_id="session"\nkind="event"\nturn_id="turn"\nnative_id="id"\nts="timestamp"\nprompt="prompt"\n'
    )
    source = tmp_path / "source.jsonl"
    source.write_text(
        json.dumps(
            {
                "session": "example",
                "event": "turn_start",
                "turn": "t",
                "id": "t",
                "timestamp": "2026-01-01T00:00:00Z",
                "prompt": "Sample",
            }
        )
        + "\n"
    )
    run("import", str(source), "--map", str(mapping))
    assert "process" not in run("run", "--", sys.executable, "-c", "print('child-output')")
    run("backfill", "--agent", "codex", "--since", "2026-01-01", "--reset")
    run("reanalyze")
    assert "Capture coverage" in run(
        "report", "--from", "2026-01-01", "--to", "2026-01-02", "--send"
    )
    assert '"billing_mode"' in run("report", "--format", "json")
    output = tmp_path / "matrix.md"
    run("adapter", "capabilities", "--output", str(output))
    assert "claude_code" in output.read_text()
    run("adapter", "new", "test_agent", "--directory", str(tmp_path))
    assert (tmp_path / "tracebook-test_agent/pyproject.toml").exists()
    assert runner.invoke(app, [*prefix, "adapter", "new", "../bad"]).exit_code != 0
    assert runner.invoke(app, [*prefix, "run"]).exit_code != 0
    assert runner.invoke(app, [*prefix, "backfill", "--agent", "bad"]).exit_code != 0
    assert runner.invoke(app, [*prefix, "otel-env", "--agent", "bad"]).exit_code != 0


def test_service_platforms_are_user_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = Config(root=tmp_path / "state")
    monkeypatch.setattr(sys, "platform", "darwin")
    result = install_service(config)
    assert "LaunchAgents" in result.path and not Path(result.path).exists()
    install_service(config, dry_run=False)
    assert not install_service(config).changed
    monkeypatch.setattr(sys, "platform", "linux")
    result = install_service(config, dry_run=False)
    assert "systemd/user" in result.path and "127.0.0.1" not in result.text
    monkeypatch.setattr(sys, "platform", "win32")
    assert not install_service(config).changed
