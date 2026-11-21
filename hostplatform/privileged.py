"""Fixed-argv process boundary for adapters.

Adapters build complete argument arrays from validated names; nothing here
accepts a model-supplied command. Privileged operations go through ``sudo``
with a fixed, absolute executable path, so the user's normal sudo password
prompt (and policy) still applies. The agent never caches or sees the
password, and never configures passwordless sudo.

Everything is injectable (``Run``) so adapter logic is tested without
touching the host.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from agent.redaction import redact

DEFAULT_TIMEOUT = 600.0
MAX_CAPTURE = 64 * 1024


@dataclass(frozen=True)
class ProcResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None  # could not start

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


Run = Callable[[Sequence[str], float, Mapping[str, str] | None], ProcResult]


def run_fixed(
    argv: Sequence[str],
    timeout: float = DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> ProcResult:
    """Run a fixed argv with ``shell=False`` and bounded, redacted output.

    stdin is inherited so ``sudo`` can ask the user for a password on the
    terminal; it is never fed data by the agent.
    """
    try:
        proc = subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ProcResult(tuple(argv), None, _text(exc.stdout), _text(exc.stderr), True)
    except OSError as exc:
        return ProcResult(tuple(argv), None, "", "", False, type(exc).__name__)
    return ProcResult(
        tuple(argv), proc.returncode, _text(proc.stdout), _text(proc.stderr)
    )


def _text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return redact(text[:MAX_CAPTURE])


def as_root(argv: Sequence[str], sudo: str | None, is_root: bool) -> list[str] | None:
    """Prefix ``sudo`` when needed. Returns None if elevation is impossible."""
    if is_root:
        return list(argv)
    if sudo is None:
        return None
    return [sudo, "--", *argv]


def is_permission_error(result: ProcResult) -> bool:
    text = (result.stderr + result.stdout).lower()
    return any(
        marker in text
        for marker in (
            "permission denied",
            "are you root",
            "a password is required",
            "authentication is required",
            "not in the sudoers",
            "incorrect password",
            "access denied",
            "interactive authentication",
        )
    )
