"""Post-change verification (Build 11).

Every changing operation declares its checks before it runs. After it runs,
the checks re-inspect the real system state; an exit code of zero or a
"success" message from the adapter is never enough on its own.

Outcomes: PASSED, PARTIALLY_PASSED, FAILED, NOT_TESTED, BLOCKED. When
verification does not pass, the verifier stops (it changes nothing itself),
collects safe diagnostics, compares the current state with the baseline,
decides whether the system was partly changed, and proposes a recovery for
the user to approve. After a recovery, verifying again is another call to
``verify``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from agent.redaction import redact

if TYPE_CHECKING:
    from agent.operations import Operation

MAX_DETAIL = 300
MAX_DIAGNOSTICS = 20


class Outcome(str, Enum):
    PASSED = "PASSED"
    PARTIALLY_PASSED = "PARTIALLY PASSED"
    FAILED = "FAILED"
    NOT_TESTED = "NOT TESTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool | None  # None: the check could not be carried out
    detail: str = ""


@dataclass(frozen=True)
class Check:
    """One declared verification: a name and a read-only probe."""

    name: str
    run: Callable[[], CheckResult] = field(repr=False)


@dataclass
class VerificationReport:
    outcome: Outcome
    checks: list[CheckResult]
    baseline: str = ""
    current: str | None = None
    # True: state differs from baseline although the goal was not reached.
    # False: unchanged. None: cannot tell.
    partial_change: bool | None = None
    diagnostics: list[str] = field(default_factory=list)
    recovery: str | None = None
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "baseline": self.baseline,
            "current": self.current,
            "partial_change": self.partial_change,
            "diagnostics": self.diagnostics,
            "recovery": self.recovery,
            "note": self.note,
        }

    def summary(self) -> str:
        lines = [f"Verification: {self.outcome.value}"]
        for c in self.checks:
            mark = {True: "pass", False: "FAIL", None: "not tested"}[c.passed]
            lines.append(f"  [{mark}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        if self.note:
            lines.append(f"  {self.note}")
        if self.partial_change:
            lines.append("  The system was changed but did not reach the goal.")
        for d in self.diagnostics:
            lines.append(f"  diagnostic: {d}")
        if self.recovery:
            lines.append(f"  suggested recovery (needs your approval): {self.recovery}")
        return "\n".join(lines)


def _safe(text: str) -> str:
    return redact(text)[:MAX_DETAIL]


def run_check(check: Check) -> CheckResult:
    try:
        result = check.run()
    except Exception as exc:  # noqa: BLE001 - a broken probe must not crash
        return CheckResult(
            check.name, None, f"check could not run: {type(exc).__name__}"
        )
    return CheckResult(result.name, result.passed, _safe(result.detail))


def classify(results: list[CheckResult]) -> Outcome:
    if not results:
        return Outcome.NOT_TESTED
    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    untested = len(results) - passed - failed
    if passed == len(results):
        return Outcome.PASSED
    if untested == len(results):
        return Outcome.NOT_TESTED
    if failed and not passed:
        return Outcome.FAILED
    return Outcome.PARTIALLY_PASSED


def verify(operation: Operation) -> VerificationReport:
    """Run the operation's declared checks and build a report. Read-only."""
    if operation.refused:
        return VerificationReport(
            Outcome.BLOCKED,
            [],
            baseline=operation.baseline,
            note=f"Nothing was run, so nothing can be verified: {operation.refused}",
        )
    if not operation.checks:
        return VerificationReport(
            Outcome.NOT_TESTED,
            [],
            baseline=operation.baseline,
            note="This operation declared no verification checks.",
        )

    results = [run_check(c) for c in operation.checks]
    outcome = classify(results)
    report = VerificationReport(outcome, results, baseline=operation.baseline)
    if outcome is Outcome.PASSED:
        return report

    # Not verified: stop, gather evidence, compare with baseline, propose recovery.
    if operation.probe_state is not None:
        try:
            report.current = _safe(operation.probe_state())
        except Exception as exc:  # noqa: BLE001
            report.note = f"Current state could not be read: {type(exc).__name__}"
    if report.current is not None:
        report.partial_change = report.current != operation.baseline
    if operation.diagnose is not None:
        try:
            report.diagnostics = [
                _safe(d) for d in operation.diagnose()[:MAX_DIAGNOSTICS]
            ]
        except Exception as exc:  # noqa: BLE001
            report.diagnostics = [f"diagnostics unavailable: {type(exc).__name__}"]
    if outcome is not Outcome.NOT_TESTED:
        report.recovery = operation.recovery or None
    return report
