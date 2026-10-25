import asyncio
import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from tracebook import hook
from tracebook.adapters import adapters
from tracebook.config import Config
from tracebook.model import Event, utc
from tracebook.platform import child_tree
from tracebook.privacy import Redactor
from tracebook.reports import deliver_async, report_due, send_delivery
from tracebook.store import Store


def test_scheduler_catches_up_once_and_async_outbox_retries(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3")
    config = Config(
        root=tmp_path, delivery={"file": {"enabled": True}}, timezone="America/New_York"
    )
    date = datetime(2026, 3, 7, 12, tzinfo=UTC)
    assert report_due(store, config, adapters(), date) == 1
    assert report_due(store, config, adapters(), date) == 0
    assert report_due(store, config, adapters(), date + timedelta(days=4)) == 4
    assert len(store.rows("SELECT * FROM reports")) == 5
    assert asyncio.run(deliver_async(store, config)) == 5
    assert asyncio.run(deliver_async(store, config)) == 0
    assert report_due(store, config, adapters(), date + timedelta(days=5)) == 1

    def fail(*args):
        raise OSError("private error detail")

    monkeypatch.setattr("tracebook.reports.send_delivery", fail)
    assert asyncio.run(deliver_async(store, config)) == 0
    pending = store.rows("SELECT * FROM outbox WHERE status='pending'")[0]
    assert pending["last_error"] == "OSError" and pending["attempts"] == 1
    store.close()


def test_explicit_webhook_and_smtp_use_idempotency_tls_and_environment(tmp_path, monkeypatch):
    item = {
        "channel": "webhook",
        "id": "delivery-id",
        "report_id": "report-id",
        "body": "Private report",
    }
    config = Config(
        root=tmp_path,
        delivery={
            "webhook": {"enabled": True, "url": "https://example.invalid/report"},
            "smtp": {
                "enabled": True,
                "host": "smtp.example.invalid",
                "from": "from@example.invalid",
                "to": "to@example.invalid",
                "username": "test",
            },
        },
    )
    response = MagicMock()
    response.__enter__.return_value.status = 204
    network = MagicMock(return_value=response)
    opener = MagicMock()
    opener.open = network
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    monkeypatch.setenv("TRACEBOOK_WEBHOOK_TOKEN", "test-token")
    send_delivery(item, config)
    request = network.call_args.args[0]
    assert request.get_header("Idempotency-key") == "delivery-id"
    assert request.get_header("Authorization") == "Bearer test-token"
    from tracebook.reports import NoRedirect

    assert (
        NoRedirect().redirect_request(
            request, None, 302, "redirect", {}, "https://elsewhere.invalid"
        )
        is None
    )
    smtp = MagicMock()
    monkeypatch.setattr("smtplib.SMTP", smtp)
    monkeypatch.setenv("TRACEBOOK_SMTP_PASSWORD", "test-password")
    send_delivery({**item, "channel": "smtp"}, config)
    client = smtp.return_value.__enter__.return_value
    client.starttls.assert_called_once()
    client.login.assert_called_once_with("test", "test-password")
    assert client.send_message.call_args.args[0]["Message-ID"].endswith("@tracebook.local>")


def test_child_tree_and_custom_spool_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, "1 0 init\n2 1 agent\n3 2 tool\n4 3 worker\n9 1 unrelated\n", ""
        ),
    )
    assert [row["pid"] for row in child_tree(2)] == [3, 4]
    monkeypatch.setattr(sys, "argv", ["shim", "--home", str(tmp_path), "--agent", "claude_code"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"test"}'))
    hook.capture()
    assert (
        json.loads(next((tmp_path / "spool").glob("*.jsonl")).read_text())["agent"] == "claude_code"
    )
    assert capsys.readouterr() == ("", "")


def test_content_disabled_preserves_numeric_usage_and_rejects_bad_cost(tmp_path):
    store = Store(tmp_path / "db.sqlite3", Redactor(capture_content=False))
    with store.transaction():
        store.raw(
            "generic", "import", {"prompt_text": "private", "output": 9, "tool_response": "private"}
        )
        store.ingest(
            Event(
                "generic",
                "s",
                "model_call",
                utc(1),
                "call",
                data={
                    "output": 9,
                    "input_uncached": 20,
                    "response": "private",
                    "cost_reported": "NaN",
                },
            )
        )
    assert store.overview()["output"] == 9
    assert store.overview()["cost_reported"] is None
    assert "private" not in str(store.rows("SELECT * FROM raw_payloads"))
    assert "private" not in str(store.rows("SELECT * FROM model_calls"))
    store.close()


def test_outbox_claim_excludes_concurrent_sender_and_recovers_expired_lease(tmp_path):
    from tracebook.reports import claim, pending

    path = tmp_path / "db.sqlite3"
    first, second = Store(path), Store(path)
    config = Config(root=tmp_path, delivery={"file": {"enabled": True}})
    report_due(first, config, adapters())
    item = pending(first, config)[0]
    assert claim(first, item)
    assert not claim(second, item)
    assert pending(second, config) == []
    first.db.execute("UPDATE outbox SET next_attempt=?", (utc(1),))
    recovered = pending(second, config)
    assert len(recovered) == 1 and claim(second, recovered[0])
    first.close()
    second.close()
