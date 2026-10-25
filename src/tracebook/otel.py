"""OTLP decoding and generic semantic event mapping; vendor fields live in adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import nullcontext
from typing import Any

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from tracebook.adapters.base import Adapter, tool_input, tool_kind
from tracebook.model import Event, Json, digest, number, obj, utc
from tracebook.store import Store


def decode(body: bytes, signal: str, protobuf: bool) -> Json:
    if protobuf:
        message = ExportLogsServiceRequest() if signal == "logs" else ExportMetricsServiceRequest()
        message.ParseFromString(body)
        return dict(MessageToDict(message))
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("OTLP body must be an object")
    return result


def value(data: object) -> Any:
    item = obj(data)
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in item:
            return item[key]
    if "arrayValue" in item:
        return [value(v) for v in obj(item["arrayValue"]).get("values", [])]
    if "kvlistValue" in item:
        return attributes(obj(item["kvlistValue"]).get("values", []))
    return None


def attributes(items: object) -> Json:
    return (
        {str(i["key"]): value(i.get("value")) for i in items if isinstance(i, dict) and "key" in i}
        if isinstance(items, list)
        else {}
    )


def records(document: Json, signal: str) -> Iterable[tuple[Json, Json]]:
    for resource in document.get("resourceLogs" if signal == "logs" else "resourceMetrics", []):
        if not isinstance(resource, dict):
            continue
        common = attributes(obj(resource.get("resource")).get("attributes"))
        for scope in resource.get("scopeLogs" if signal == "logs" else "scopeMetrics", []):
            if not isinstance(scope, dict):
                continue
            for record in scope.get("logRecords" if signal == "logs" else "metrics", []):
                if not isinstance(record, dict):
                    continue
                if signal == "logs":
                    attrs = {**common, **attributes(record.get("attributes"))}
                    attrs["_body"] = value(record.get("body"))
                    attrs["_time_ns"] = record.get(
                        "timeUnixNano", record.get("observedTimeUnixNano", "0")
                    )
                    attrs["_event_name"] = record.get("eventName")
                    yield common, attrs
                else:
                    for typ in ("sum", "gauge", "histogram", "exponentialHistogram", "summary"):
                        metric = obj(record.get(typ))
                        for point in metric.get("dataPoints", []):
                            yield (
                                common,
                                {
                                    **common,
                                    **attributes(point.get("attributes")),
                                    "_metric": record.get("name"),
                                    "unit": record.get("unit"),
                                    "temporality": metric.get("aggregationTemporality"),
                                    "point": point,
                                    "_time_ns": point.get("timeUnixNano", "0"),
                                },
                            )


def map_records(adapter: Adapter, logs: list[Json], metrics: list[Json]) -> Iterable[Event]:
    for item in [*logs, *metrics]:
        session = str(
            item.get(
                "session.id",
                item.get("conversation.id", item.get("gen_ai.conversation.id", "uncorrelated")),
            )
        )
        turn = item.get("prompt.id", item.get("prompt_id", item.get("turn.id")))
        timestamp = item.get("event.timestamp") or (int(item.get("_time_ns", 0)) / 1e9)
        native = item.get(
            "request_id", item.get("response_id", item.get("tool_use_id", item.get("call_id")))
        )
        name = str(
            item.get("_event_name") or item.get("event.name") or item.get("_body") or "unknown"
        )
        suffix = name.split(".")[-1]
        data: Json = {
            "model": item.get("model", item.get("gen_ai.response.model")),
            "duration_ms": item.get("duration_ms"),
            "native_event": name,
        }
        if "_metric" in item:
            kind, data = "metric", dict(item)
        elif suffix in {"user_prompt", "prompt"}:
            kind = "turn_start"
            native = item.get("message.uuid") or turn
            data.update(
                prompt=item.get("prompt_text", item.get("prompt")),
                kind="command" if item.get("command_name") else "prompt",
                aliases=[turn, item.get("message.uuid")],
            )
        elif suffix in {"api_response", "api_request", "sse_event", "websocket_event"} and any(
            field in item for field in adapter.otel_token_fields.values()
        ):
            kind = "model_call"
            data.update(
                {key: number(item.get(field)) for key, field in adapter.otel_token_fields.items()}
            )
            if adapter.input_includes_cache and data.get("input_uncached") is not None:
                data["input_uncached"] = max(
                    0,
                    data["input_uncached"]
                    - (data.get("input_cache_read") or 0)
                    - (data.get("input_cache_write") or 0),
                )
            if adapter.output_includes_reasoning and data.get("output") is not None:
                data["output"] = max(0, data["output"] - (data.get("reasoning") or 0))
            data["token_semantics_confidence"] = "adapter_documented"
            data["cost_reported"] = (
                float(item["cost_usd"]) if item.get("cost_usd") is not None else None
            )
            data["cost_semantics"] = "agent_reported_api_value"
            data["aliases"] = [native, item.get("message.id")]
        elif suffix in {"tool_result", "tool_call"}:
            kind = "tool_result"
            tool = str(item.get("tool_name", item.get("function_name", "unknown")))
            data.update(
                tool=tool_kind(tool),
                native_name=tool,
                input=tool_input(item.get("tool_input")),
                output=item.get("output"),
                status="error" if item.get("success") in (False, "false") else "completed",
            )
        elif suffix in {"tool_decision", "permission"}:
            kind = "permission"
            data["decision"] = item.get("decision")
        elif suffix == "api_error":
            kind = "lifecycle"
            data.update(error=item.get("error.message", item.get("error")), failure=True)
        elif suffix in {"chat_compression", "compaction"}:
            kind = "compaction"
        else:
            kind = "warning"
            data["reason"] = "unmapped_telemetry"
        yield Event(
            adapter.id,
            session,
            kind,
            utc(timestamp),
            str(native or digest(item)),
            str(turn) if turn else None,
            "otel",
            data,
        )


def ingest_otlp(
    store: Store,
    adapters: dict[str, Adapter],
    document: Json,
    signal: str,
    own_transaction: bool = True,
) -> int:
    count = 0
    with store.transaction() if own_transaction else nullcontext():
        for common, record in records(document, signal):
            service = str(common.get("service.name", ""))
            adapter = next(
                (a for a in adapters.values() if service in a.otel_services), adapters["generic"]
            )
            adapter.state_root = store.path.parent
            store.raw(adapter.id, "otel", record)
            events = adapter.map_otel(
                [record] if signal == "logs" else [], [record] if signal == "metrics" else []
            )
            try:
                for event in events:
                    store.ingest(event)
                    count += 1
            except (ValueError, TypeError, OverflowError, AttributeError):
                store.ingest(
                    Event(
                        adapter.id,
                        "uncorrelated",
                        "warning",
                        utc(),
                        digest(record),
                        source="otel",
                        data={"reason": "malformed_telemetry"},
                    )
                )
    return count
