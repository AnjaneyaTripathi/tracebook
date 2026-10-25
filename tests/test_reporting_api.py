from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from tracebook.adapters import adapters
from tracebook.analysis import analyze
from tracebook.api import create_api, create_otlp
from tracebook.config import Config
from tracebook.model import Event, utc
from tracebook.otel import attributes, decode, ingest_otlp, map_records
from tracebook.pricing import PriceBook
from tracebook.reports import deliver, nightly_window, report
from tracebook.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state/db.sqlite3")
    yield value
    value.close()


def seed(store: Store, model: str = "test-model") -> None:
    with store.transaction():
        store.ingest(
            Event(
                "codex", "session", "session", utc(1), "session", data={"cwd": "/workspace/project"}
            )
        )
        store.ingest(
            Event(
                "codex",
                "session",
                "turn_start",
                utc(1),
                "turn",
                "turn",
                data={"prompt": "Fix a failing test"},
            )
        )
        for index in range(5):
            store.ingest(
                Event(
                    "codex",
                    "session",
                    "model_call",
                    utc(2 + index),
                    str(index),
                    "turn",
                    data={
                        "model": model,
                        "input_uncached": 20000 + 12000 * index,
                        "input_cache_read": 0,
                        "output": 100,
                        "reasoning": 20,
                        "duration_ms": 500,
                    },
                )
            )
            store.ingest(
                Event(
                    "codex",
                    "session",
                    "tool_call",
                    utc(2 + index),
                    str(index),
                    "turn",
                    data={"tool": "shell", "native_name": "Bash", "input": {"command": "pytest"}},
                )
            )
            store.ingest(
                Event(
                    "codex",
                    "session",
                    "tool_result",
                    utc(3 + index),
                    str(index),
                    "turn",
                    data={"status": "error", "output": "AssertionError", "duration_ms": 150000},
                )
            )
        store.ingest(
            Event("codex", "session", "turn_end", utc(9), "turn", "turn", data={"status": "error"})
        )


def test_pricing_aliases_tiers_and_missing(tmp_path: Path, store: Store):
    path = tmp_path / "pricing.toml"
    path.write_text(
        'as_of="2026-10-24"\n[models."test/model"]\naliases=["test-model"]\ninput=2\noutput=10\ncache_read=0.2\ncache_write=3\n[[models."test/model".tiers]]\nabove_input_tokens=200000\ninput=4\n'
    )
    book = PriceBook(path)
    assert book.estimate({"model": "unknown"}) == (None, "unknown_model")
    assert book.estimate({"model": "test-model"}) == (None, "missing_usage")
    cost, reason = book.estimate(
        {
            "model": "test-model",
            "input_uncached": 300000,
            "output": 1000,
            "reasoning": 200,
            "input_cache_read": 10000,
        }
    )
    assert cost == pytest.approx(1.214)
    assert reason == "estimated_api_value"
    seed(store)
    book.apply(store)
    assert store.overview()["unpriced_calls"] == 0
    assert (
        json.loads(store.rows("SELECT data FROM model_calls")[0]["data"])["pricing_as_of"]
        == "2026-10-24"
    )
    path.write_text("[models.bad]\ninput=-1\noutput=1\n")
    with pytest.raises(ValueError):
        PriceBook(path).estimate({"model": "bad", "input_uncached": 1, "output": 1})
    path.write_text("[models.bad]\ninput=1\noutput=1\n")
    assert PriceBook(path).estimate(
        {"model": "bad", "input_uncached": 1, "output": 1, "input_cache_write": 5}
    ) == (None, "missing_rate")


def test_rules_are_explainable_and_replay_stable(store: Store):
    seed(store)
    with store.transaction():
        for i in range(3):
            store.ingest(Event("codex", "session", "compaction", utc(10 + i), str(i), "turn"))
        for i in range(4):
            store.ingest(
                Event(
                    "generic",
                    "other",
                    "tool_call",
                    utc(i),
                    str(i),
                    "t",
                    data={"tool": "file_read", "input": {"file_path": "same.py"}},
                )
            )
        for i in range(22):
            store.ingest(
                Event(
                    "generic",
                    "other",
                    "model_call",
                    utc(i),
                    str(i),
                    "t",
                    data={
                        "model": "slow",
                        "duration_ms": 20000 if i == 21 else 1000,
                        "input_uncached": 100,
                        "output": 10,
                    },
                )
            )
    first = analyze(store)
    before = store.rows("SELECT * FROM findings ORDER BY id")
    assert analyze(store) == first
    assert before == store.rows("SELECT * FROM findings ORDER BY id")
    detected = {f["detector"] for f in before}
    assert {
        "failure_loop",
        "slow_tool",
        "cache_opportunity",
        "unknown_price",
        "context_growth",
        "compaction_churn",
        "latency_outlier",
        "repeated_tool",
    } <= detected
    for finding in before:
        assert json.loads(finding["evidence"])
        assert finding["recommendation"]


def test_reports_window_idempotency_and_local_outbox(store: Store, tmp_path):
    seed(store)
    analyze(store)
    config = Config(
        root=tmp_path / "state",
        delivery={"file": {"enabled": True, "directory": str(tmp_path / "delivered")}},
    )
    first = report(store, config, adapters(), utc(1), utc(8))
    second = report(store, config, adapters(), utc(1), utc(8))
    assert first["id"] == second["id"]
    assert len(store.rows("SELECT * FROM reports")) == len(store.rows("SELECT * FROM outbox")) == 1
    assert "not billed spend" in first["body"]
    assert "unverified" in first["body"]
    assert "Reported value: unavailable" in first["body"]
    assert deliver(store, config) == 1
    assert deliver(store, config) == 0
    assert (tmp_path / "delivered" / (first["id"] + ".md")).read_text() == first["body"]
    with pytest.raises(ValueError):
        report(store, config, adapters(), utc(8), utc(1))


def test_outbox_failure_retries_no_secret_error(store: Store, tmp_path):
    config = Config(
        root=tmp_path, delivery={"webhook": {"enabled": True, "url": "http://secret-token.example"}}
    )
    report(store, config, adapters(), utc(1), utc(8))
    assert deliver(store, config) == 0
    row = store.rows("SELECT * FROM outbox")[0]
    assert row["attempts"] == 1 and row["status"] == "pending"
    assert row["last_error"] == "ValueError"
    assert "secret-token" not in str(row)
    config.delivery["webhook"]["enabled"] = False
    assert deliver(store, config, at="9999") == 0


def test_timezone_window_dst_and_sleep():
    config = Config(timezone="America/New_York", report_hour=23)
    start, end = nightly_window(config, datetime(2026, 3, 9, 4, 0, tzinfo=UTC))
    assert start == "2026-03-08T04:00:00.000Z"
    assert end == "2026-03-09T03:00:00.000Z"
    assert (
        nightly_window(
            Config(timezone="Asia/Kolkata", report_hour=23), datetime(2026, 10, 24, 12, tzinfo=UTC)
        )[1]
        == "2026-10-23T17:30:00.000Z"
    )


def otlp_document(service="claude-code", event="claude_code.api_request"):
    attrs = {
        "session.id": "session",
        "prompt.id": "turn",
        "event.name": event,
        "model": "test-model",
        "request_id": "request",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 20,
        "cost_usd": 0.05,
    }
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1000000000",
                                "attributes": [
                                    {
                                        "key": k,
                                        "value": {
                                            "stringValue"
                                            if isinstance(v, str)
                                            else "doubleValue"
                                            if isinstance(v, float)
                                            else "intValue": str(v) if isinstance(v, int) else v
                                        },
                                    }
                                    for k, v in attrs.items()
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_otlp_json_protobuf_gzip_and_bounds(store: Store):
    client = TestClient(create_otlp(store, adapters()), base_url="http://127.0.0.1:4318")
    document = otlp_document()
    response = client.post(
        "/v1/logs",
        content=gzip.compress(json.dumps(document).encode()),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert store.overview()["calls"] == 1
    assert store.overview()["cost_reported"] == 0.05
    response = client.post("/v1/logs", json=document)
    assert response.status_code == 200 and store.overview()["calls"] == 1
    message = ExportLogsServiceRequest()
    resource = message.resource_logs.add()
    resource.resource.attributes.add(key="service.name").value.string_value = "claude-code"
    log = resource.scope_logs.add().log_records.add()
    log.time_unix_nano = 2_000_000_000
    log.body.string_value = "new_event"
    response = client.post(
        "/v1/logs",
        content=message.SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert response.status_code == 200 and response.content == b""
    assert client.post("/v1/traces", json={}).status_code == 200
    assert client.post("/v1/nope", json={}).status_code == 404
    assert (
        client.post(
            "/v1/logs", content=b"bad", headers={"Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert client.post("/v1/logs", content=b"bad").status_code == 415
    assert (
        client.post(
            "/v1/logs",
            content=b"bad",
            headers={"Content-Type": "application/json", "Content-Encoding": "br"},
        ).status_code
        == 415
    )
    bomb = gzip.compress(b"x" * (17 * 1024 * 1024))
    assert (
        client.post(
            "/v1/logs",
            content=bomb,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/v1/logs",
            content=b"bad",
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        ).status_code
        == 400
    )


def test_otlp_metrics_and_mapping_variants(store: Store):
    registry = adapters()
    document = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "gemini-cli"}}]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "gemini_cli.token.usage",
                                "unit": "tokens",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [{"asInt": "100", "timeUnixNano": "1000000000"}],
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    assert ingest_otlp(store, registry, document, "metrics") == 1
    assert store.overview()["calls"] == 0  # Aggregate metrics never duplicate call usage.
    base = {"session.id": "s", "prompt.id": "p", "_time_ns": "1000000000", "model": "x"}
    names = [
        "user_prompt",
        "tool_result",
        "tool_decision",
        "api_error",
        "chat_compression",
        "future",
    ]
    assert {
        e.kind
        for e in map_records(
            registry["claude_code"], [{**base, "event.name": n} for n in names], []
        )
    } == {"turn_start", "tool_result", "permission", "lifecycle", "compaction", "warning"}
    gemini = list(
        map_records(
            registry["gemini_cli"],
            [
                {
                    **base,
                    "event.name": "gemini_cli.api_response",
                    "input_token_count": 100,
                    "cached_content_token_count": 40,
                    "output_token_count": 20,
                    "thoughts_token_count": 5,
                }
            ],
            [],
        )
    )[0]
    assert gemini.data["input_uncached"] == 60 and gemini.data["output"] == 20
    codex = list(
        map_records(
            registry["codex"],
            [
                {
                    **base,
                    "event.name": "codex.sse_event",
                    "input_token_count": 100,
                    "cached_input_token_count": 40,
                    "output_token_count": 20,
                    "reasoning_output_token_count": 5,
                }
            ],
            [],
        )
    )[0]
    assert codex.data["output"] == 15
    assert attributes(
        [
            {
                "key": "nested",
                "value": {
                    "kvlistValue": {
                        "values": [
                            {
                                "key": "items",
                                "value": {"arrayValue": {"values": [{"boolValue": True}, {}]}},
                            }
                        ]
                    }
                },
            }
        ]
    ) == {"nested": {"items": [True, None]}}
    with pytest.raises(ValueError):
        decode(b"[]", "logs", False)


def test_api_filters_details_and_origin_protection(store: Store, tmp_path):
    seed(store)
    analyze(store)
    client = TestClient(
        create_api(store, Config(root=tmp_path), adapters()), base_url="http://127.0.0.1:8742"
    )
    assert client.get("/api/health").json()["ok"]
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403
    assert (
        client.get("/api/health", headers={"Origin": "https://attacker.example"}).status_code == 403
    )
    assert client.get("/api/health", headers={"Origin": "http://127.0.0.1:1234"}).status_code == 403
    assert client.get("/api/overview?agent=codex").json()["calls"] == 5
    assert client.get("/api/overview?agent=gemini_cli").json()["tools"] == 0
    assert client.get("/api/overview?start=2026").json()["turns"] == 0
    assert client.get("/api/adapters").json()
    project = client.get("/api/projects").json()[0]["id"]
    sessions = client.get(f"/api/sessions?agent=codex&project={project}&q=failing").json()
    assert len(sessions) == 1
    sid = sessions[0]["id"]
    detail = client.get(f"/api/sessions/{sid}").json()
    assert len(detail["tools"]) == 5
    turn = detail["turns"][0]["id"]
    assert len(client.get(f"/api/turns/{turn}").json()["calls"]) == 5
    assert client.get(f"/api/events?session_id={sid}&turn_id={turn}&limit=2").json()[0][
        "provenance"
    ]
    assert client.get("/api/findings?agent=codex").json()
    assert client.get("/api/sessions/missing").status_code == 404
    assert client.get("/api/turns/missing").status_code == 404
    assert client.get("/api/reports/missing").status_code == 404
    assert client.get("/api/sessions?limit=10000").status_code == 422
    generated = client.post("/api/reports", json={"start": utc(1), "end": utc(10)})
    assert generated.status_code == 200
    assert client.get("/api/reports").json()
    rid = generated.json()["id"]
    assert client.get(f"/api/reports/{rid}").json()["body"]
    assert client.post("/api/reports", json={"start": utc(10), "end": utc(1)}).status_code == 400
    assert client.get("/").status_code in {200, 503}
