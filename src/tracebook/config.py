from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracebook.platform import home


@dataclass(slots=True)
class Config:
    root: Path = field(default_factory=home)
    port: int = 8742
    otlp_port: int = 4318
    poll_seconds: float = 2.0
    scan_seconds: float = 30.0
    billing_mode: str = "mixed"
    capture_content: bool = True
    redact_patterns: list[str] = field(default_factory=list)
    report_hour: int = 23
    timezone: str = "UTC"
    delivery: dict[str, Any] = field(default_factory=dict)

    @property
    def database(self) -> Path:
        return self.root / "tracebook.sqlite3"

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        result = cls(root=root or home())
        path = result.root / "config.toml"
        if path.exists():
            data = tomllib.loads(path.read_text())
            for key in ("port", "otlp_port", "poll_seconds", "scan_seconds", "billing_mode", "capture_content", "redact_patterns", "report_hour", "timezone", "delivery"):
                if key in data:
                    setattr(result, key, data[key])
        if result.billing_mode not in {"api", "subscription", "mixed"}:
            raise ValueError("billing_mode must be api, subscription or mixed")
        if not 0 <= result.report_hour <= 23 or result.poll_seconds < 0.25:
            raise ValueError("Invalid report hour or poll interval")
        return result
