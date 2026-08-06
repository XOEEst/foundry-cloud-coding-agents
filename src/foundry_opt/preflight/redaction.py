import re
from collections.abc import Iterable


_SENSITIVE_PATTERNS = (
    re.compile(
        r"(?is)-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
        r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    ),
    re.compile(
        r"(?i)\b(?:github_pat_[A-Za-z0-9_]{20,}|"
        r"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
    ),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(
        r"(?i)(://)(?!\[REDACTED\]@)[^/\s@]+(?=@)"
    ),
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
    re.compile(
        r"(?i)(?<![?&])(\b(?:token|access[-_]?token|api[-_]?key|"
        r"client[-_]?secret|password)\b\s*[:=]\s*[\"']?)"
        r"(?!\[REDACTED\])[^\s,;\"'&}\]]+"
    ),
)


def redact(text: str | None, secrets: Iterable[str] = ()) -> str | None:
    if text is None:
        return None

    redacted = text
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _SENSITIVE_PATTERNS:
        replacement = (
            r"\1[REDACTED]"
            if pattern.groups
            else "[REDACTED]"
        )
        redacted = pattern.sub(replacement, redacted)
    return redacted
