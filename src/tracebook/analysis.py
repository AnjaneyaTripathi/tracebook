"""Explainable rules over canonical tables only. No vendor-specific branches."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict

from tracebook.model import Json, digest
from tracebook.store import Store, encode


def analyze(store: Store) -> int:
    findings: list[Json] = []

    def add(
        detector: str,
        row: Json,
        title: str,
        recommendation: str,
        evidence: Json,
        severity: str = "info",
    ) -> None:
        findings.append(
            {
                "id": digest(
                    detector, row.get("agent"), row.get("session_id"), row.get("turn_id"), evidence
                ),
                "detector": detector,
                "severity": severity,
                "agent": row.get("agent"),
                "session_id": row.get("session_id"),
                "turn_id": row.get("turn_id"),
                "ts": row.get("ts", "1970-01-01T00:00:00.000Z"),
                "title": title,
                "evidence": encode(evidence),
                "recommendation": recommendation,
            }
        )

    repeated: dict[tuple[str, str, str], list[Json]] = defaultdict(list)
    for tool in store.rows("SELECT * FROM tool_calls ORDER BY ts,id"):
        if tool["turn_id"] and tool["input"] not in {None, "null", "{}"}:
            repeated[(tool["turn_id"], tool["tool"], tool["input"])].append(tool)
        if tool["duration_ms"] and tool["duration_ms"] > 120000:
            add(
                "slow_tool",
                tool,
                "Tool took over two minutes",
                "Inspect the command and its output; consider a narrower operation or an explicit timeout.",
                {
                    "tool_ids": [tool["id"]],
                    "duration_ms": tool["duration_ms"],
                    "threshold_ms": 120000,
                },
                "warning",
            )
    for (_, tool_kind, _), rows in repeated.items():
        failed = [r for r in rows if r["status"] == "error"]
        if len(failed) >= 3:
            add(
                "failure_loop",
                rows[-1],
                "Repeated tool failures",
                "Inspect the first failure before retrying the same input. Fix its cause or change the approach.",
                {"tool_ids": [r["id"] for r in failed], "count": len(failed), "threshold": 3},
                "warning",
            )
        elif len(rows) >= 4 and tool_kind in {
            "shell",
            "file_read",
            "file_search",
            "web_fetch",
            "web_search",
        }:
            add(
                "repeated_tool",
                rows[-1],
                "Same tool input repeated",
                "Check whether the underlying state changed. Reuse earlier results when it has not.",
                {"tool_ids": [r["id"] for r in rows], "count": len(rows), "threshold": 4},
            )
    calls = store.rows("SELECT * FROM accounted_model_calls ORDER BY ts,id")
    histories: dict[str, list[float]] = defaultdict(list)
    unknown: dict[str, list[Json]] = defaultdict(list)
    turns: dict[str, list[Json]] = defaultdict(list)
    for call in calls:
        if call["turn_id"]:
            turns[call["turn_id"]].append(call)
        if call["cost_reported"] is None and call["cost_estimated"] is None:
            unknown[str(call["model"] or "unknown")].append(call)
        inp, cache = call["input_uncached"], call["input_cache_read"]
        if (
            inp is not None
            and cache is not None
            and inp >= 16000
            and cache / max(1, inp + cache) < 0.05
        ):
            add(
                "cache_opportunity",
                call,
                "Large uncached input",
                "Inspect changing prompt prefixes and cache eligibility. A first request or a changed context can explain this.",
                {
                    "model_call_ids": [call["id"]],
                    "input_uncached": inp,
                    "input_cache_read": cache,
                    "threshold_tokens": 16000,
                },
            )
        duration = json.loads(call["data"]).get("duration_ms")
        if isinstance(duration, (int, float)):
            history = histories[str(call["model"])]
            if len(history) >= 20:
                baseline = statistics.median(history[-200:])
                if duration > max(10000, baseline * 3):
                    add(
                        "latency_outlier",
                        call,
                        "Model response slower than its baseline",
                        "Inspect retries, context size and provider latency before changing the model.",
                        {
                            "model_call_ids": [call["id"]],
                            "duration_ms": duration,
                            "baseline_median_ms": baseline,
                            "baseline_samples": min(len(history), 200),
                            "multiplier": 3,
                        },
                        "warning",
                    )
            history.append(float(duration))
    for model, rows in unknown.items():
        add(
            "unknown_price",
            rows[-1],
            f"No price for {model}",
            "Add verified rates and aliases to pricing.toml, then run reanalyze. Unknown cost remains null.",
            {
                "model": model,
                "call_count": len(rows),
                "model_call_ids": [r["id"] for r in rows[:20]],
            },
        )
    for rows in turns.values():
        inputs = [(r["input_uncached"] or 0) + (r["input_cache_read"] or 0) for r in rows]
        if (
            len(inputs) >= 5
            and min(inputs) > 0
            and max(inputs) > min(inputs) * 3
            and max(inputs) >= 32000
        ):
            add(
                "context_growth",
                rows[-1],
                "Context grew substantially within one prompt",
                "Inspect large tool responses and repeated file reads. Narrow the scope of subsequent requests.",
                {
                    "model_call_ids": [r["id"] for r in rows],
                    "min_input": min(inputs),
                    "max_input": max(inputs),
                    "multiplier": 3,
                },
            )
    for row in store.rows(
        "SELECT agent,session_id,turn_id,MAX(ts) ts,COUNT(*) count FROM events WHERE kind='compaction' GROUP BY agent,session_id,turn_id HAVING COUNT(*)>=3"
    ):
        add(
            "compaction_churn",
            row,
            "Repeated context compaction",
            "Inspect large inputs and consider splitting the task into smaller independent prompts.",
            {"count": row["count"], "threshold": 3},
            "warning",
        )
    with store.transaction():
        store.db.execute("DELETE FROM findings")
        for finding in findings:
            store.db.execute(
                "INSERT INTO findings VALUES(:id,:detector,:severity,:agent,:session_id,:turn_id,:ts,:title,:evidence,:recommendation)",
                finding,
            )
    return len(findings)
