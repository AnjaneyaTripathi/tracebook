"""One redaction boundary for raw evidence and every canonical source."""
from __future__ import annotations

import re
from typing import Any

SECRET_KEY = re.compile(r"(?i)(api[_-]?key|password|passwd|secret|authorization|credential|access[_-]?token|refresh[_-]?token|private[_-]?key)")
PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*[\"']?[^\s\"',;}]+"),
    re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+@"),
)


class Redactor:
    def __init__(self, extra_patterns: list[str] | None = None, max_text: int = 32768, capture_content: bool = True) -> None:
        self.patterns = (*PATTERNS, *(re.compile(p) for p in (extra_patterns or [])))
        self.max_text = max_text
        self.capture_content = capture_content

    def clean(self, value: Any, key: str = "", depth: int = 0) -> Any:
        if depth > 40:
            return "[depth-limited]"
        if SECRET_KEY.search(key):
            return "[REDACTED]"
        if not self.capture_content and key in {"prompt", "content", "text", "output", "input", "arguments", "command", "response"}:
            return "[content disabled]"
        if isinstance(value, dict):
            return {str(k): self.clean(v, str(k), depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v, key, depth + 1) for v in value[:10000]]
        if isinstance(value, str):
            for pattern in self.patterns:
                value = pattern.sub("[REDACTED]", value)
            return value[:self.max_text] + ("…[truncated]" if len(value) > self.max_text else "")
        return value
