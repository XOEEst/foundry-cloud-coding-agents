import re
from collections.abc import Iterable


_SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?\s*"
        r"(?:bearer|basic)\s+)"
        r"(?!\[REDACTED\])[^\s,;\"'&}\]]+"
    ),
    re.compile(
        r"(?i)(\b(?:x[-_]?api[-_]?key|api[-_]?key)\b[\"']?\s*[:=]\s*"
        r"[\"']?\s*)"
        r"(?!\[REDACTED\])[^\s,;\"'&}\]]+"
    ),
    re.compile(
        r'(?i)(?<![?&])(\b(?:accountkey|sharedaccesskey|'
        r'sharedaccesssignature|password|client[-_]?secret)'
        r'\b\s*=\s*")(?!\[REDACTED\])[^"]+'
    ),
    re.compile(
        r"(?i)(?<![?&])(\b(?:accountkey|sharedaccesskey|"
        r"sharedaccesssignature|password|client[-_]?secret)"
        r"\b\s*=\s*')(?!\[REDACTED\])[^']+"
    ),
    re.compile(
        r"(?i)(?<![?&])(\b(?:accountkey|sharedaccesskey|"
        r"sharedaccesssignature|password|client[-_]?secret)\b\s*=\s*)"
        r"(?!\[REDACTED\])[^;\s,\"'}\]]+"
    ),
    re.compile(
        r"(?i)([?&](?:sig|token|code|key|client[-_]?secret|api[-_]?key|"
        r"access[-_]?token|password)=)(?!\[REDACTED\])"
        r"[^&#;,\s\"'}\]]+"
    ),
)


def redact(text: str | None, secrets: Iterable[str] = ()) -> str | None:
    if text is None:
        return None

    redacted = text
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted
