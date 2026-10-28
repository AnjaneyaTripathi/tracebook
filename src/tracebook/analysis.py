"""Explainable rules over canonical tables only. No vendor-specific branches."""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from itertools import groupby

from tracebook.model import Json, digest
from tracebook.store import Store, encode


def analyze(store: Store) -> int:
    with store.transaction():
        return _analyze(store)


def _analyze(store: Store) -> int:
    count = 0

    def add(
        detector: str,
        row: Json,
        title: str,
        recommendation: str,
        evidence: Json,
        severity: str = "info",
    ) -> None:
        nonlocal count
        finding = {
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
        store.db.execute(
            "INSERT OR REPLACE INTO findings VALUES(:id,:detector,:severity,:agent,:session_id,:turn_id,:ts,:title,:evidence,:recommendation)",
            finding,
        )
        count += 1

    store.db.execute("DELETE FROM findings")

    fields = "id,agent,session_id,turn_id,ts,tool,status,duration_ms"
    for tool in store.iter_rows(
        f"SELECT {fields} FROM tool_calls WHERE duration_ms>120000 ORDER BY ts,id"
    ):
        if tool["duration_ms"] and tool["duration_ms"] > 120000:
            sec = round(tool["duration_ms"] / 1000, 1)
            add(
                "slow_tool",
                tool,
                "Tool took over two minutes",
                f"Tool execution took {sec}s (threshold: 120s). Consider narrowing operations, avoiding massive tree scans, or setting a timeout.",
                {
                    "tool_ids": [tool["id"]],
                    "duration_ms": tool["duration_ms"],
                    "threshold_ms": 120000,
                },
                "warning",
            )
    tools = store.iter_rows(
        f"SELECT {fields},input FROM tool_calls WHERE turn_id IS NOT NULL AND input IS NOT NULL AND input NOT IN ('null','{{}}') ORDER BY turn_id,tool,input,ts,id"
    )
    for (_, tool_kind, _), group in groupby(
        tools, lambda row: (row["turn_id"], row["tool"], row["input"])
    ):
        total, failures = 0, 0
        evidence_ids: list[str] = []
        failure_ids: list[str] = []
        latest: Json = {}
        for row in group:
            latest = row
            total += 1
            if len(evidence_ids) < 20:
                evidence_ids.append(row["id"])
            if row["status"] == "error":
                failures += 1
                if len(failure_ids) < 20:
                    failure_ids.append(row["id"])
        if failures >= 3:
            add(
                "failure_loop",
                latest,
                "Repeated tool failures",
                f"The agent attempted the same tool operation {failures} times consecutively and failed each time. Inspect the error in the session trace and provide a clarifying hint or correct the failing file path/command.",
                {"tool_ids": failure_ids, "count": failures, "threshold": 3},
                "warning",
            )
        elif total >= 4 and tool_kind in {
            "shell",
            "file_read",
            "file_search",
            "web_fetch",
            "web_search",
        }:
            add(
                "repeated_tool",
                latest,
                "Same tool input repeated",
                f"The agent executed the exact same tool input {total} times within this prompt. Unless external files changed, instruct the agent to reuse earlier results to save time and tokens.",
                {"tool_ids": evidence_ids, "count": total, "threshold": 4},
            )
    calls = store.iter_rows(
        "SELECT id,agent,session_id,turn_id,ts,model,input_uncached,input_cache_read,cost_reported,cost_estimated,json_extract(data,'$.duration_ms') duration_ms FROM accounted_model_calls ORDER BY ts,id"
    )
    histories: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
    unknown: dict[str, Json] = {}
    for call in calls:
        if call["cost_reported"] is None and call["cost_estimated"] is None:
            model = str(call["model"] or "unknown")
            group_info = unknown.setdefault(model, {"ids": [], "count": 0})
            group_info["latest"] = call
            group_info["count"] += 1
            if len(group_info["ids"]) < 20:
                group_info["ids"].append(call["id"])
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
                f"Model processed {inp:,} uncached tokens with only {cache:,} cached tokens. Ensure stable prompt prefixes and shared context to benefit from provider prompt-cache discounts.",
                {
                    "model_call_ids": [call["id"]],
                    "input_uncached": inp,
                    "input_cache_read": cache,
                    "threshold_tokens": 16000,
                },
            )
        duration = call["duration_ms"]
        if isinstance(duration, (int, float)):
            history = histories[str(call["model"])]
            if len(history) >= 20:
                baseline = statistics.median(history)
                if duration > max(10000, baseline * 3):
                    add(
                        "latency_outlier",
                        call,
                        "Model response slower than its baseline",
                        f"Model response took {round(duration / 1000, 1)}s, more than 3x the baseline median of {round(baseline / 1000, 1)}s. Check for provider latency, congestion, or unusually large context.",
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
    for model, info in unknown.items():
        add(
            "unknown_price",
            info["latest"],
            f"No price for {model}",
            f"{info['count']} model calls used '{model}' without a verified rate. Add rates to ~/.tracebook/pricing.toml and run 'tracebook reanalyze' to enable cost tracking.",
            {"model": model, "call_count": info["count"], "model_call_ids": info["ids"]},
        )
    for row in store.iter_rows(
        "SELECT agent,session_id,turn_id,MAX(ts) ts,COUNT(*) count,MIN(COALESCE(input_uncached,0)+COALESCE(input_cache_read,0)+COALESCE(input_cache_write,0)) min_input,MAX(COALESCE(input_uncached,0)+COALESCE(input_cache_read,0)+COALESCE(input_cache_write,0)) max_input FROM accounted_model_calls WHERE turn_id IS NOT NULL GROUP BY turn_id HAVING count>=5 AND min_input>0 AND max_input>min_input*3 AND max_input>=32000"
    ):
        ids = [
            r["id"]
            for r in store.rows(
                "SELECT id FROM accounted_model_calls WHERE turn_id=? ORDER BY ts,id LIMIT 20",
                (row["turn_id"],),
            )
        ]
        min_in = int(row["min_input"])
        max_in = int(row["max_input"])
        growth_pct = int(((max_in - min_in) / max(1, min_in)) * 100)
        add(
            "context_growth",
            row,
            "Context grew substantially within one prompt",
            f"Context surged from {min_in:,} to {max_in:,} tokens (+{growth_pct}%) across {row['count']} calls in this prompt. Tool outputs or files accumulated in memory. Subsequent prompts in this session will re-send this larger context. Consider starting a new session or compacting history once this sub-task is complete.",
            {
                "model_call_ids": ids,
                "call_count": row["count"],
                "min_input": min_in,
                "max_input": max_in,
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
            f"Context was compacted {row['count']} times in this prompt. The conversation is near the model context window limit. Consider splitting the task or starting a fresh session.",
            {"count": row["count"], "threshold": 3},
            "warning",
        )
    return count
