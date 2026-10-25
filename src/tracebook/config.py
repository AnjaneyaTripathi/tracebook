from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    metadata_env: list[str] = field(default_factory=list)

    @property
    def database(self) -> Path:
        return self.root / "tracebook.sqlite3"

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        result = cls(root=root or home())
        path = result.root / "config.toml"
        if path.exists():
            data = tomllib.loads(path.read_text())
            for key in (
                "port",
                "otlp_port",
                "poll_seconds",
                "scan_seconds",
                "billing_mode",
                "capture_content",
                "redact_patterns",
                "report_hour",
                "timezone",
                "delivery",
                "metadata_env",
            ):
                if key in data:
                    setattr(result, key, data[key])
        if result.billing_mode not in {"api", "subscription", "mixed"}:
            raise ValueError("billing_mode must be api, subscription or mixed")
        if type(result.report_hour) is not int or not 0 <= result.report_hour <= 23:
            raise ValueError("Invalid report hour or poll interval")
        for port in (result.port, result.otlp_port):
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError("Ports must be integers between 1024 and 65535")
        if result.port == result.otlp_port:
            raise ValueError("Dashboard and OTLP need different ports")
        for interval in (result.poll_seconds, result.scan_seconds):
            if (
                not isinstance(interval, (int, float))
                or not math.isfinite(interval)
                or interval < 0.25
            ):
                raise ValueError("Poll and scan intervals must be finite and at least 0.25 seconds")
        if type(result.capture_content) is not bool or not isinstance(result.redact_patterns, list):
            raise ValueError("Invalid privacy configuration")
        for pattern in result.redact_patterns:
            re.compile(pattern)
        ZoneInfo(result.timezone)
        if not isinstance(result.delivery, dict):
            raise ValueError("Delivery must be a table of configured channels")
        if not isinstance(result.metadata_env, list) or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            for key in result.metadata_env
        ):
            raise ValueError("metadata_env must list environment variable names")
        return result
