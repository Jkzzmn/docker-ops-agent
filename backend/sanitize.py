from __future__ import annotations

import re


SENSITIVE_KEY = r"[\w.-]*(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w.-]*"
# key=value, key: value, "key": "value" (JSON) and quoted values; only the value is replaced.
KEY_VALUE = re.compile(rf"""(?i)((?:["']?){SENSITIVE_KEY}(?:["']?)\s*[=:]\s*)(\[REDACTED\]|"[^"\n]*"|'[^'\n]*'|[^\s,;&"'}}\]]+)""")
BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
URL_CREDENTIALS = re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(@)")
TOKEN_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
)


def _mask_value(match: re.Match) -> str:
    value = match.group(2)
    quote = value[0] if value[0] in "\"'" else ""
    if value == "[REDACTED]":
        return match.group(0)
    return f"{match.group(1)}{quote}[REDACTED]{quote}"


def redact(value: str, limit: int = 12000) -> str:
    text = value[:limit]
    text = KEY_VALUE.sub(_mask_value, text)
    text = BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", text)
    for pattern in TOKEN_SHAPES:
        text = pattern.sub("[REDACTED]", text)
    return text
