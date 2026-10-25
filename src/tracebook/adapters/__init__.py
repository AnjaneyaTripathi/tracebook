"""Adapters are discovered through installed Python entry points."""
from __future__ import annotations

from importlib.metadata import entry_points

from tracebook.adapters.base import Adapter


def adapters() -> dict[str, Adapter]:
    result: dict[str, Adapter] = {}
    for entry in entry_points(group="tracebook.adapters"):
        instance: Adapter = entry.load()()
        if instance.id in result:
            raise ValueError(f"Duplicate adapter id: {instance.id}")
        result[instance.id] = instance
    return result
