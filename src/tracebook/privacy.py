"""One redaction boundary for raw evidence and every canonical source."""

from __future__ import annotations

import re
from typing import Any

SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|password|passwd|secret|authorization|credential|access[_-]?token|refresh[_-]?token|private[_-]?key)"
)
PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16})\b"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(
        r"(?i)(?:api[_-]?key|password|secret|access[_-]?token)[\"']?\s*[=:]\s*[\"']?[^\s\"',;}]+"
    ),
    re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+@"),
)


class Redactor:
    def __init__(
        self,
        extra_patterns: list[str] | None = None,
        max_text: int = 32768,
        capture_content: bool = True,
    ) -> None:
        self.extra_patterns = tuple(re.compile(p) for p in (extra_patterns or []))
        self.max_text = max_text
        self.capture_content = capture_content
        self._text_cache: dict[str, str] = {}

    def begin_record(self) -> None:
        """Share redacted strings between a raw record and its canonical projection."""
        self._text_cache.clear()

    def clean(self, value: Any, key: str = "", depth: int = 0) -> Any:
        if depth > 40:
            return "[depth-limited]"
        if SECRET_KEY.search(key):
            return "[REDACTED]"
        if (
            not self.capture_content
            and isinstance(value, (str, list, dict))
            and key
            in {
                "prompt",
                "content",
                "text",
                "output",
                "input",
                "arguments",
                "command",
                "response",
                "tool_input",
                "tool_response",
                "prompt_text",
                "aggregated_output",
                "last-assistant-message",
                "unparsed",
                "payload_raw",
                "summary",
                "prompt_response",
            }
        ):
            return "[content disabled]"
        if isinstance(value, dict):
            return {str(k): self.clean(v, str(k), depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v, key, depth + 1) for v in value[:10000]]
        if isinstance(value, str):
            original = value
            cacheable = 256 <= len(value) <= self.max_text
            if cacheable and value in self._text_cache:
                return self._text_cache[value]
            # Necessary literal prefixes gate costly regex scans without weakening them.
            lowered = value.lower()
            gates = (
                "PRIVATE KEY" in value,
                "sk-" in value or "gh" in value or "AKIA" in value,
                "bearer" in lowered or not value.isascii(),
                any(marker in lowered for marker in ("api", "password", "secret", "access"))
                or not value.isascii(),
                "://" in value and "@" in value,
            )
            for enabled, pattern in zip(gates, PATTERNS, strict=True):
                if enabled:
                    value = pattern.sub("[REDACTED]", value)
            for pattern in self.extra_patterns:
                value = pattern.sub("[REDACTED]", value)
            result = value[: self.max_text] + ("…[truncated]" if len(value) > self.max_text else "")
            if cacheable and len(self._text_cache) < 8:
                self._text_cache[original] = result
            return result
        return value
