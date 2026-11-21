"""Append-only task journal.

Each line of the journal is one JSON event. A task's events are written as
they happen (before a step starts, after it ends), so an interrupted run
still shows what was started and what finished. Writes are line-buffered,
flushed and fsynced; the file is created with mode 0600 because it records
commands that ran on this machine.

Recorded output is limited to short redacted excerpts.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.redaction import redact
from hostplatform.identity import as_user

EXCERPT_CHARS = 300
JOURNAL_NAME = "tasks.jsonl"


class JournalError(Exception):
    """The journal could not be written. Execution must not proceed."""


def new_task_id() -> str:
    return "t-" + secrets.token_hex(6)


@dataclass
class TaskJournal:
    path: Path
    secrets: Sequence[str] = ()

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def record(self, task_id: str, event: str, **data: Any) -> None:
        entry = {
            "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "task_id": task_id,
            "event": event,
            **data,
        }
        line = redact(json.dumps(entry, default=str, sort_keys=True), self.secrets)
        with self._lock, as_user():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise JournalError(
                    f"cannot write task journal: {type(exc).__name__}"
                ) from None

    def events(self) -> Iterator[dict[str, Any]]:
        try:
            with as_user():
                text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalError(
                f"cannot read task journal: {type(exc).__name__}"
            ) from None
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a torn final line from a crash is ignored
            if isinstance(event, dict):
                yield event

    def task(self, task_id: str) -> list[dict[str, Any]]:
        return [e for e in self.events() if e.get("task_id") == task_id]

    def completed_fingerprints(self) -> set[str]:
        """Fingerprints of plans whose every step finished successfully."""
        return {
            str(e["fingerprint"])
            for e in self.events()
            if e.get("event") == "task_finished" and e.get("outcome") == "completed"
        }


def excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "..."
