"""Text safety helpers for anything shown in a terminal or written to a file."""

from __future__ import annotations

import re

# C0 controls except tab/newline, DEL, and C1 controls. ESC (0x1b) starts
# terminal escape sequences, so removing it neutralises them.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_control(text: str) -> str:
    """Remove control characters so output cannot drive the terminal."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def fence(text: str, lang: str = "") -> str:
    """Wrap text in a Markdown code fence that the text cannot close."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def excerpt(text: str, limit: int) -> tuple[str, int]:
    """Return ``(text[:limit], omitted_chars)``."""
    if len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit
