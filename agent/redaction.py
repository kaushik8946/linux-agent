"""Best-effort secret redaction for text that leaves the process.

Used before command output reaches logs, reports or the model. Patterns are
deliberately bounded (no unbounded repeats around alternations) so a huge
single-line output cannot cause catastrophic backtracking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"
MIN_LITERAL_SECRET_LENGTH = 6
# A literal secret this long, or one that is not a plain run of letters, is
# replaced wherever it occurs. Shorter plain-word values (for example "ubuntu"
# or "linux") are matched only as whole words, so ordinary output such as
# "16ubuntu1" is not mangled by an unrelated environment value.
_PLAIN_WORD_MAX = 12
_PLAIN_WORD = re.compile(r"[A-Za-z]+")

_KEY_WORDS = (
    r"(?:password|passwd|passphrase|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|credential|client[_-]?secret)"
)

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,30}PRIVATE KEY-----"
)
_PRIVATE_KEY_PARTIAL = re.compile(
    r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[A-Za-z0-9+/=\s]*"
)
_TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]
_AUTH_HEADER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(r"(://[^/\s:@]{1,64}:)[^/\s@]{1,128}(@)")
_KEY_VALUE = re.compile(
    r"([\w.-]{0,40}" + _KEY_WORDS + r"[\w.-]{0,40}[\"']?\s*[=:]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with likely secrets replaced by ``[REDACTED]``."""
    if not text:
        return text
    for literal in secrets:
        if not literal or len(literal) < MIN_LITERAL_SECRET_LENGTH:
            continue
        if _PLAIN_WORD.fullmatch(literal) and len(literal) <= _PLAIN_WORD_MAX:
            pattern = re.compile(
                r"(?<![A-Za-z0-9])" + re.escape(literal) + r"(?![A-Za-z0-9])"
            )
            text = pattern.sub(REDACTED, text)
        else:
            text = text.replace(literal, REDACTED)
    text = _PRIVATE_KEY_BLOCK.sub(REDACTED, text)
    text = _PRIVATE_KEY_PARTIAL.sub(REDACTED, text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(2)}", text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
