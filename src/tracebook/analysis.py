"""Explainable rules over canonical tables only. No vendor-specific branches."""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from datetime import datetime
from itertools import groupby

from tracebook.model import Json, digest
from tracebook.store import Store, encode


def _parse_ts(ts_str: object) -> float:
    if not isinstance(ts_str, str) or not ts_str:
        return 0.0
    try:
        clean = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean).timestamp()
    except Exception:
        return 0.0


def analyze(store: Store, pricing: bool = False) -> int:
    with store.transaction():
        return _analyze(store, pricing)


def _analyze(store: Store, pricing: bool) -> int:
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
    compaction_sessions: set[str] = {
        str(row["session_id"])
        for row in store.rows(
            "SELECT DISTINCT session_id FROM events WHERE kind='compaction' AND session_id IS NOT NULL"
        )
    }
    calls = store.iter_rows(
        "SELECT id,agent,session_id,turn_id,ts,model,input_uncached,input_cache_read,cost_reported,cost_estimated,json_extract(data,'$.duration_ms') duration_ms FROM accounted_model_calls ORDER BY ts,id"
    )
    histories: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
    unknown: dict[str, Json] = {}
    session_calls: dict[str, list[Json]] = defaultdict(list)
    session_had_cache: dict[str, bool] = defaultdict(bool)
    session_last_cached: dict[str, Json] = {}
    for call in calls:
        if pricing and call["cost_reported"] is None and call["cost_estimated"] is None:
            model = str(call["model"] or "unknown")
            group_info = unknown.setdefault(model, {"ids": [], "count": 0})
            group_info["latest"] = call
            group_info["count"] += 1
            if len(group_info["ids"]) < 20:
                group_info["ids"].append(call["id"])
        inp, cache = call["input_uncached"], call["input_cache_read"]
        session_id = str(call.get("session_id") or "")
        session_history = session_calls[session_id]
        prev_call = session_history[-1] if session_history else None
        call_seq = len(session_history) + 1
        had_cache = session_had_cache[session_id]

        if (
            isinstance(inp, int)
            and isinstance(cache, int)
            and inp >= 16000
            and cache / max(1, inp + cache) < 0.05
        ):
            delta_sec = (
                max(0.0, _parse_ts(call.get("ts")) - _parse_ts(prev_call.get("ts")))
                if prev_call
                else 0.0
            )
            if delta_sec >= 3600:
                hours = delta_sec / 3600
                elapsed_str = f"{hours:.1f} hours" if hours < 10 else f"{round(hours)} hours"
            elif delta_sec >= 60:
                mins = delta_sec / 60
                elapsed_str = f"{mins:.1f} minutes" if mins < 10 else f"{round(mins)} minutes"
            else:
                elapsed_str = f"{round(delta_sec)} seconds"

            if had_cache and delta_sec >= 300:
                cause = "cache_ttl_expired"
                title = f"Cache TTL expired after {elapsed_str} idle pause"
                rec = (
                    f"Provider prompt cache expired after an idle pause of {elapsed_str} between prompts. "
                    f"Model provider caches (e.g. Gemini/Claude) are ephemeral with a 5–60 minute inactivity TTL. "
                    f"Resuming after this pause forced all {inp:,} tokens to be re-read uncached to re-warm the provider cache."
                )
                prev_c = prev_call.get("input_cache_read") if prev_call else 0
                ev: Json = {
                    "model_call_ids": [call["id"]],
                    "input_uncached": inp,
                    "input_cache_read": cache,
                    "root_cause": cause,
                    "idle_seconds": round(delta_sec, 1),
                    "idle_formatted": elapsed_str,
                    "call_sequence": call_seq,
                    "previous_cache_read": prev_c if isinstance(prev_c, int) else 0,
                    "threshold_tokens": 16000,
                }
            elif not had_cache and call_seq <= 8:
                cause = "session_warmup"
                title = f"Uncached input during initial warm-up ({inp:,} tokens)"
                rec = (
                    f"Initial session warm-up: turn #{call_seq} processed {inp:,} uncached tokens. "
                    f"Model provider prefix caching (e.g. Gemini's ~32k token threshold) activates automatically once "
                    f"enough shared context accumulates. Subsequent turns with stable prefixes will benefit from cache discounts."
                )
                ev = {
                    "model_call_ids": [call["id"]],
                    "input_uncached": inp,
                    "input_cache_read": cache,
                    "root_cause": cause,
                    "call_sequence": call_seq,
                    "threshold_tokens": 16000,
                }
            elif had_cache and delta_sec < 300:
                cause = "transient_miss"
                title = f"Transient cache miss or prompt prefix shift ({inp:,} tokens)"
                prev_c = prev_call.get("input_cache_read") if prev_call else 0
                prev_cache_val = prev_c if isinstance(prev_c, int) else 0
                last_cached = session_last_cached.get(session_id)
                compaction_note = (
                    " Note: Context compaction was recorded in this session, which condenses earlier history into a summary and resets the prefix hash."
                    if session_id in compaction_sessions
                    else ""
                )
                if prev_cache_val > 0:
                    prior_note = f"only {elapsed_str} elapsed since prior turn with {prev_cache_val:,} cached tokens"
                elif last_cached:
                    lc_val = last_cached.get("input_cache_read") or 0
                    lc_tokens = lc_val if isinstance(lc_val, int) else 0
                    prior_note = f"earlier turns in this session cached up to {lc_tokens:,} tokens"
                else:
                    prior_note = f"only {elapsed_str} elapsed since prior turn"
                rec = (
                    f"Cache miss occurred despite active session history ({prior_note}). "
                    f"A dynamic prefix change (such as timestamps, variable instructions, or context summarization) or provider-side eviction "
                    f"invalidated the prefix cache. Ensure stable prompt prefixes to maximize cache reuse.{compaction_note}"
                )
                ev = {
                    "model_call_ids": [call["id"]],
                    "input_uncached": inp,
                    "input_cache_read": cache,
                    "root_cause": cause,
                    "elapsed_seconds": round(delta_sec, 1),
                    "elapsed_formatted": elapsed_str,
                    "call_sequence": call_seq,
                    "previous_cache_read": prev_cache_val,
                    "threshold_tokens": 16000,
                }
            else:
                cause = "unstable_prefix"
                title = f"Persistent uncached input: unstable prefix ({inp:,} tokens)"
                rec = (
                    f"Model processed {inp:,} uncached tokens with only {cache:,} cached tokens deep into the session (turn #{call_seq}). "
                    f"The agent or harness is likely mutating the prompt prefix on every turn. "
                    f"Ensure system instructions, tool definitions, and conversation prefixes remain immutable so the provider can cache them."
                )
                ev = {
                    "model_call_ids": [call["id"]],
                    "input_uncached": inp,
                    "input_cache_read": cache,
                    "root_cause": cause,
                    "call_sequence": call_seq,
                    "threshold_tokens": 16000,
                }
            add("cache_opportunity", call, title, rec, ev)

        if isinstance(cache, int) and cache > 0:
            session_had_cache[session_id] = True
            session_last_cached[session_id] = call
        session_calls[session_id].append(call)
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
