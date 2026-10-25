"""Explicit, local API-equivalent pricing; no network refreshes or guessed rates."""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path

from tracebook.model import Json, obj
from tracebook.store import Store, encode


class PriceBook:
    def __init__(self, path: Path) -> None:
        self.data = tomllib.loads(path.read_text()) if path.exists() else {}
        self.as_of = str(self.data.get("as_of", "unspecified"))
        self.models = obj(self.data.get("models"))

    def estimate(self, call: Json) -> tuple[float | None, str]:
        model = str(call.get("model", ""))
        rates = obj(self.models.get(model))
        if not rates:
            rates = next(
                (obj(v) for v in self.models.values() if model in obj(v).get("aliases", [])), {}
            )
        if not rates:
            return None, "unknown_model"
        total_input = sum(
            call.get(k) or 0 for k in ("input_uncached", "input_cache_read", "input_cache_write")
        )
        for tier in sorted(rates.get("tiers", []), key=lambda t: t.get("above_input_tokens", 0)):
            if total_input > tier.get("above_input_tokens", 0):
                rates = {**rates, **tier}
        if call.get("input_uncached") is None or call.get("output") is None:
            return None, "missing_usage"
        cost = 0.0
        for token, rate in {
            "input_uncached": "input",
            "input_cache_read": "cache_read",
            "input_cache_write": "cache_write",
            "output": "output",
            "reasoning": "reasoning",
        }.items():
            count = call.get(token) or 0
            price = rates.get(rate, rates.get("output") if rate == "reasoning" else None)
            if count and price is None:
                return None, "missing_rate"
            if price is not None:
                price = float(price)
                if not math.isfinite(price) or price < 0:
                    raise ValueError("Pricing rates must be finite and non-negative")
                cost += count * price / 1_000_000
        return cost, "estimated_api_value"

    def apply(self, store: Store) -> int:
        count = 0
        with store.transaction():
            for call in store.iter_rows("SELECT * FROM model_calls"):
                cost, reason = self.estimate(call)
                data = json.loads(call["data"])
                data.update(pricing_as_of=self.as_of, pricing_status=reason)
                store.db.execute(
                    "UPDATE model_calls SET cost_estimated=?,data=? WHERE id=?",
                    (cost, encode(data), call["id"]),
                )
                count += 1
        return count
