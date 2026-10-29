"""Explicit, local API-equivalent pricing; no network refreshes or guessed rates."""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path

from tracebook.model import Json, number, obj
from tracebook.store import Store, encode


class PriceBook:
    def __init__(self, path: Path) -> None:
        self.data = tomllib.loads(path.read_text()) if path.exists() else {}
        self.as_of = str(self.data.get("as_of", "unspecified"))
        self.models = obj(self.data.get("models"))

    def rates_for(self, call: Json) -> Json:
        model = str(call.get("model", ""))
        rates = obj(self.models.get(model))
        if not rates:
            rates = next(
                (obj(v) for v in self.models.values() if model in obj(v).get("aliases", [])), {}
            )
        if not rates:
            return {}
        total_input = sum(
            call.get(k) or 0 for k in ("input_uncached", "input_cache_read", "input_cache_write")
        )
        for tier in sorted(rates.get("tiers", []), key=lambda t: t.get("above_input_tokens", 0)):
            if total_input > tier.get("above_input_tokens", 0):
                rates = {**rates, **tier}
        return rates

    def estimate(self, call: Json) -> tuple[float | None, str]:
        rates = self.rates_for(call)
        if not rates:
            return None, "unknown_model"
        if call.get("input_uncached") is None or call.get("output") is None:
            return None, "missing_usage"
        data = call.get("data", {})
        metadata = obj(json.loads(data) if isinstance(data, str) else data)
        split = obj(
            call.get("cache_write_tokens_by_ttl", metadata.get("cache_write_tokens_by_ttl"))
        )
        ttl_counts = {ttl: number(value) for ttl, value in split.items()}
        if split and (
            any(value is None for value in ttl_counts.values())
            or sum(value or 0 for value in ttl_counts.values())
            != (call.get("input_cache_write") or 0)
        ):
            return None, "invalid_cache_write_usage"
        cost = 0.0
        for token, rate in {
            "input_uncached": "input",
            "input_cache_read": "cache_read",
            "input_cache_write": "cache_write",
            "output": "output",
            "reasoning": "reasoning",
        }.items():
            count = call.get(token) or 0
            if token == "input_cache_write" and split and rates.get("cache_write_by_ttl"):
                for ttl, ttl_count in ttl_counts.items():
                    price = obj(rates.get("cache_write_by_ttl")).get(ttl)
                    if ttl_count and price is None:
                        return None, "missing_rate"
                    if price is not None:
                        price = float(price)
                        if not math.isfinite(price) or price < 0:
                            raise ValueError("Pricing rates must be finite and non-negative")
                        cost += (ttl_count or 0) * price / 1_000_000
                continue
            price = rates.get(rate, rates.get("output") if rate == "reasoning" else None)
            if count and price is None:
                return None, "missing_rate"
            if price is not None:
                price = float(price)
                if not math.isfinite(price) or price < 0:
                    raise ValueError("Pricing rates must be finite and non-negative")
                cost += count * price / 1_000_000
        return cost, "estimated_api_value"

    def apply(self, store: Store, since: str | None = None) -> int:
        """Re-estimate costs; ``since`` limits the pass to calls at or after that timestamp."""
        count = 0
        with store.transaction():
            query = "SELECT * FROM model_calls" + (" WHERE ts>=?" if since else "")
            for call in store.iter_rows(query, (since,) if since else ()):
                cost, reason = self.estimate(call)
                data = json.loads(call["data"])
                data.update(
                    pricing_as_of=self.as_of,
                    pricing_status=reason,
                    pricing_basis=self.data.get("basis"),
                    pricing_source=self.rates_for(call).get("source"),
                )
                count += 1
                if cost == call["cost_estimated"] and data == json.loads(call["data"]):
                    continue
                store.db.execute(
                    "UPDATE model_calls SET cost_estimated=?,data=? WHERE id=?",
                    (cost, encode(data), call["id"]),
                )
                store.dirty_sessions.add(call["session_id"])
        return count
