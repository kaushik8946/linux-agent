"""Local approval prompts.

Approval comes only from the person at this terminal. An approval is bound
to the fingerprint of the exact plan shown, so any change to commands or
arguments invalidates it, and it expires after a short time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agent.command_policy import RiskClass
from agent.policy import Requirement

if TYPE_CHECKING:
    from agent.planner import ActionPlan

DEFAULT_APPROVAL_TTL = 300.0
YES_ANSWERS = frozenset({"y", "yes"})


class Approvable(Protocol):
    """Anything the user can approve: a command plan or a typed operation."""

    @property
    def fingerprint(self) -> str: ...

    @property
    def requirement(self) -> Requirement: ...

    def confirmation_phrase(self) -> str: ...


@dataclass(frozen=True)
class Approval:
    fingerprint: str
    granted_at: float
    requirement: Requirement

    def valid_for(
        self, plan: Approvable, now: float, ttl: float = DEFAULT_APPROVAL_TTL
    ) -> bool:
        return (
            self.fingerprint == plan.fingerprint
            and 0 <= now - self.granted_at <= ttl
            and self.requirement is plan.requirement
        )


Ask = Callable[[str], str]


class ApprovalService:
    """Asks the local user to approve a plan.

    ``ask`` is the terminal prompt function. It is never connected to model
    output. Any exception while asking (Ctrl+C, closed input) means No.
    """

    def __init__(
        self,
        ask: Ask,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = DEFAULT_APPROVAL_TTL,
    ) -> None:
        self._ask = ask
        self._clock = clock
        self.ttl = ttl

    def set_prompt(self, ask: Ask) -> None:
        self._ask = ask

    def request(self, plan: Approvable, rendered: str) -> Approval | None:
        """Show ``rendered`` and ask. Returns an Approval, or None for No."""
        requirement = plan.requirement
        if requirement is Requirement.AUTO:
            return Approval(plan.fingerprint, self._clock(), requirement)
        if requirement is Requirement.BLOCKED:
            return None
        try:
            if requirement is Requirement.OVERRIDE:
                phrase = plan.confirmation_phrase()
                answer = self._ask(
                    f"{rendered}\n\nThis plan contains a command the agent normally "
                    f"refuses.\nType exactly '{phrase}' to run it once (anything "
                    "else cancels): "
                )
                ok = answer.strip() == phrase
            elif requirement is Requirement.TYPED:
                phrase = plan.confirmation_phrase()
                answer = self._ask(
                    f"{rendered}\n\nThis plan can destroy data.\nType exactly '{phrase}' to proceed (anything "
                    "else cancels): "
                )
                ok = answer.strip() == phrase
            else:
                answer = self._ask(f"{rendered}\n\nApprove this plan? [y/N]: ")
                ok = answer.strip().lower() in YES_ANSWERS
        except (EOFError, KeyboardInterrupt, OSError):
            return None
        if not ok:
            return None
        return Approval(plan.fingerprint, self._clock(), requirement)

    def is_valid(self, approval: Approval | None, plan: Approvable) -> bool:
        return approval is not None and approval.valid_for(
            plan, self._clock(), self.ttl
        )


def render_plan(plan: ActionPlan) -> str:
    """Plain-text description of a plan for the approval prompt."""
    lines = [
        f"Proposed plan ({len(plan.steps)} step(s), requires: {plan.requirement.value})"
    ]
    for n, step in enumerate(plan.steps, 1):
        lines.append(f"  {n}. {step.command}")
        lines.append(
            f"     risk: {step.decision.risk.value}  needs: "
            f"{step.decision.requirement.value}  in: {step.working_dir}"
        )
        lines.append(f"     purpose: {step.purpose}")
        if step.blocked_reason:
            lines.append(f"     BLOCKED: {step.blocked_reason}")
        elif step.decision.requirement is Requirement.BLOCKED:
            lines.append(f"     BLOCKED: {step.decision.reason}")
        elif step.decision.requirement is Requirement.OVERRIDE:
            lines.append(
                f"     NORMALLY BLOCKED: {step.decision.reason}. You can run it "
                "once by typing the phrase below."
            )
            if step.decision.risk is RiskClass.PRIVILEGED:
                lines.append(
                    "     Runs as root. sudo may ask for your password itself; "
                    "the agent never sees it."
                )
        elif step.decision.risk is RiskClass.UNKNOWN:
            lines.append(
                "     NOT RECOGNISED: the agent cannot tell what this command "
                "does. Approve only if you know it is safe."
            )
    ctx = plan.context
    if ctx:
        lines.append(f"  Expected end state: {ctx.expected_end_state}")
        if ctx.baseline:
            lines.append(f"  Baseline: {ctx.baseline}")
        if ctx.affected_resources:
            lines.append(f"  Affects: {', '.join(ctx.affected_resources)}")
        if ctx.verification:
            lines.append(f"  Verification: {'; '.join(ctx.verification)}")
        if ctx.recovery:
            lines.append(f"  Recovery: {ctx.recovery}")
    for err in plan.errors:
        lines.append(f"  PROBLEM: {err}")
    return "\n".join(lines)
