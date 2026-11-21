"""Markdown investigation reports.

A report records what was asked, what ran, what the tools returned (as short,
redacted excerpts) and what the model concluded. It never stores secrets or
full unfiltered command output.

Files are written atomically and never overwrite an existing report: content
goes to a private temporary file, which is then hard-linked to a unique final
name, so an interrupted write leaves no partial report.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.orchestrator import StopReason, ToolEvent, TurnResult
from agent.redaction import redact
from agent.textutil import excerpt, fence, strip_control

EXCERPT_CHARS = 600
MAX_REQUEST_CHARS = 2000
MAX_NAME_ATTEMPTS = 1000


FACTS_NOTE = (
    "- **Facts:** the commands, statuses and excerpts listed above. They are "
    "recorded results of commands that ran on this host."
)
HYPOTHESES_NOTE = (
    "- **Hypotheses:** any cause, explanation or recommendation in the final "
    "result below is the model's interpretation. Treat it as unverified "
    "unless a listed result supports it."
)


class ReportError(Exception):
    """The report could not be written. The message is safe to display."""


def _clean(text: str, secrets: Sequence[str]) -> str:
    return strip_control(redact(text, secrets))


def _host_lines(host: dict[str, Any] | None) -> list[str]:
    if not host:
        return ["Host facts were not available for this session."]
    os_info = host.get("os") or {}
    hw = host.get("hardware") or {}
    priv = host.get("privileges") or {}
    sm = host.get("service_manager") or {}
    containers = host.get("containers") or {}

    def val(v: Any) -> str:
        return "unknown" if v is None or v == "" else str(v)

    detected = containers.get("detected")
    container_text = (
        "unknown"
        if detected is None
        else (
            "yes: " + ", ".join(containers.get("indicators") or [])
            if detected
            else "no"
        )
    )
    return [
        f"- OS: {val(os_info.get('pretty_name') or os_info.get('id'))}",
        f"- Kernel: {val(host.get('kernel'))}",
        f"- Architecture: {val(host.get('architecture'))}",
        f"- CPUs: {val(hw.get('cpu_count'))}; memory (MB): {val(hw.get('memory_total_mb'))}",
        f"- Privileges: root={val(priv.get('is_root'))}, sudo={val(priv.get('sudo_available'))}",
        f"- Service manager: {val(sm.get('name'))} ({val(sm.get('state'))})",
        f"- Package managers found: {', '.join(host.get('package_managers') or []) or 'none'}",
        f"- Container indicators: {container_text}",
    ]


def _step_lines(event: ToolEvent, secrets: Sequence[str]) -> list[str]:
    lines: list[str] = []
    if event.plan is None:
        status = "ok" if event.ok else "failed"
        lines.append(
            f"- `{event.name}`: {status} - {_clean(event.summary, secrets)[:200]}"
        )
        return lines
    plan = event.plan
    header = "executed" if plan.executed else "rejected before running"
    lines.append(f"- Command plan ({header}):")
    for step in plan.results:
        bits = [f"status={step.status.value}"]
        if step.risk:
            bits.append(f"risk={step.risk}")
        if step.exit_code is not None:
            bits.append(f"exit={step.exit_code}")
        if step.timed_out:
            bits.append("timed out")
        if step.stdout_truncated or step.stderr_truncated:
            bits.append("output truncated")
        lines.append(f"  - `{_clean(step.preview, secrets)}` ({', '.join(bits)})")
        if step.reason:
            lines.append(f"    - note: {_clean(step.reason, secrets)[:200]}")
    return lines


def _evidence(events: list[ToolEvent], secrets: Sequence[str]) -> list[str]:
    blocks: list[str] = []
    for event in events:
        if event.plan is None:
            continue
        for step in event.plan.results:
            if not (step.stdout or step.stderr):
                continue
            body = _clean(step.stdout or step.stderr, secrets)
            shown, omitted = excerpt(body, EXCERPT_CHARS)
            label = f"`{_clean(step.preview, secrets)}` ({'stdout' if step.stdout else 'stderr'})"
            blocks.append(label)
            blocks.append(fence(shown.rstrip("\n")))
            if omitted:
                blocks.append(f"_{omitted} more characters omitted from this report._")
    return blocks


def build_report(
    *,
    session_id: str,
    turn: TurnResult,
    host: dict[str, Any] | None,
    secrets: Sequence[str] = (),
    generated_at: datetime | None = None,
) -> str:
    stamp = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")
    request, req_omitted = excerpt(_clean(turn.request, secrets), MAX_REQUEST_CHARS)
    lines = [
        "# Investigation report",
        "",
        f"- Session: `{session_id}`",
        f"- Generated: {stamp}",
        f"- Outcome: {turn.stop_reason.value}",
        f"- Model rounds: {turn.rounds}; duration: {turn.duration_seconds:.1f}s",
        "",
        "## Request",
        "",
        fence(request)
        + (f"\n\n_{req_omitted} more characters omitted._" if req_omitted else ""),
        "",
        "## Host facts",
        "",
        *_host_lines(host),
        "",
        "## Tools used",
        "",
    ]
    if turn.events:
        for event in turn.events:
            lines += _step_lines(event, secrets)
    else:
        lines.append("No tools or commands were used.")

    lines += ["", "## Key observations (evidence excerpts)", ""]
    evidence = _evidence(turn.events, secrets)
    if evidence:
        lines.append(
            f"Excerpts are redacted and limited to {EXCERPT_CHARS} characters each; "
            "full output is not stored."
        )
        lines.append("")
        for block in evidence:
            lines += [block, ""]
    else:
        lines += ["No command output was captured.", ""]

    lines += ["## Warnings and missing capabilities", ""]
    warnings = list((host or {}).get("warnings") or [])
    missing = ((host or {}).get("utilities") or {}).get("missing") or []
    if missing:
        warnings.append("Diagnostic utilities not found: " + ", ".join(missing))
    for event in turn.events:
        if not event.ok:
            warnings.append(f"{event.name} did not fully succeed: {event.summary}")
    if turn.error:
        warnings.append(f"Error: {turn.error}")
    if turn.stop_reason is not StopReason.ANSWERED:
        warnings.append(f"The request stopped early ({turn.stop_reason.value}).")
    if warnings:
        lines += [f"- {_clean(w, secrets)[:300]}" for w in warnings]
    else:
        lines.append("None recorded.")

    lines += [
        "",
        "## Facts versus hypotheses",
        "",
        FACTS_NOTE,
        HYPOTHESES_NOTE,
        "",
        "## Final result",
        "",
        _clean(turn.text, secrets).strip() or "_No final answer was produced._",
        "",
    ]
    return "\n".join(lines)


def report_filename(now: datetime, session_id: str, attempt: int) -> str:
    return f"report-{now:%Y%m%d-%H%M%S}-{session_id}-{attempt:02d}.md"


def write_report(
    report_dir: Path,
    content: str,
    session_id: str,
    now: datetime | None = None,
) -> Path:
    """Write ``content`` to a new, uniquely named file and return its path."""
    now = now or datetime.now(UTC)
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=report_dir, prefix=".report-", suffix=".tmp"
        )
    except OSError as exc:
        raise ReportError(
            f"cannot create report directory or file: {type(exc).__name__}"
        ) from None
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            final = report_dir / report_filename(now, session_id, attempt)
            try:
                os.link(tmp, final)  # fails if the name exists: never overwrites
            except FileExistsError:
                continue
            return final
        raise ReportError("could not find an unused report filename")
    except OSError as exc:
        raise ReportError(f"cannot write report: {type(exc).__name__}") from None
    finally:
        with suppress(OSError):
            tmp.unlink()
