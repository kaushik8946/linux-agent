"""Turn a validated command plan into a reviewable action plan.

An ``ActionPlan`` carries everything the user needs to judge a change before
approving it: the exact commands, working directory, risk and requirement of
each step, the resources touched, the expected end state, the baseline,
verification checks and recovery notes. The model supplies the descriptive
fields; local code supplies the classification and requirement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from agent.command_plan import CommandPlan, PlanContext
from agent.command_policy import RiskClass
from agent.policy import Decision, Requirement, command_decision, strictest


@dataclass(frozen=True)
class PlannedStep:
    step_id: str
    command: str  # display only; never executed
    working_dir: str
    purpose: str
    decision: Decision
    blocked_reason: str | None = None  # resolution or policy rejection


@dataclass(frozen=True)
class ActionPlan:
    steps: list[PlannedStep]
    context: PlanContext | None
    fingerprint: str  # identifies the exact commands and arguments
    errors: list[str] = field(default_factory=list)

    @property
    def requirement(self) -> Requirement:
        reqs = [s.decision.requirement for s in self.steps]
        if any(s.blocked_reason for s in self.steps):
            reqs.append(Requirement.BLOCKED)
        return strictest(reqs)

    @property
    def is_modifying(self) -> bool:
        return any(s.decision.requirement is not Requirement.AUTO for s in self.steps)

    @property
    def has_known_change(self) -> bool:
        """A step the policy knows changes something (not merely unrecognised)."""
        return any(
            s.decision.requirement is not Requirement.AUTO
            and s.decision.risk is not RiskClass.UNKNOWN
            for s in self.steps
        )

    @property
    def has_unrecognised(self) -> bool:
        return any(s.decision.risk is RiskClass.UNKNOWN for s in self.steps)

    @property
    def blocked(self) -> bool:
        return self.requirement is Requirement.BLOCKED

    def confirmation_phrase(self) -> str:
        """Phrase the user must type for a TYPED plan (names the target)."""
        return f"yes, run {len(self.steps)} step(s) {self.fingerprint[:8]}"


def fingerprint(plan: CommandPlan) -> str:
    """Hash of everything that affects what runs. Changes invalidate approval."""
    material = [
        {
            "id": s.id,
            "executable": s.executable,
            "args": list(s.args),
            "working_dir": s.working_dir,
            "env": sorted(s.env_allowlist),
            "stdin": s.stdin_from_step,
            "timeout": s.timeout_seconds,
        }
        for s in plan.steps
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def build_action_plan(
    plan: CommandPlan, previews: list[Any], default_cwd: str
) -> ActionPlan:
    """Combine a plan with the runner's previews into an ``ActionPlan``.

    ``previews`` come from ``CommandRunner.preview_steps`` evaluated with every
    risk class permitted, so each step carries its true classification.
    """
    context = plan.plan_context
    steps: list[PlannedStep] = []
    for step, preview in zip(plan.steps, previews, strict=True):
        if preview.risk is None:
            risk = RiskClass.UNKNOWN
        else:
            risk = RiskClass(preview.risk)
        name = step.executable.rsplit("/", 1)[-1]
        steps.append(
            PlannedStep(
                step_id=step.id,
                command=preview.command,
                working_dir=step.working_dir or default_cwd,
                purpose=step.purpose,
                decision=command_decision(name, risk),
                blocked_reason=preview.blocked_reason,
            )
        )
    errors: list[str] = []
    plan_obj = ActionPlan(steps, context, fingerprint(plan), errors)
    # The model can only describe an end state for changes the policy knows
    # about. An unrecognised command is shown to the user with a warning
    # instead; the description is still welcome if the model has one.
    if plan_obj.has_known_change and context is None:
        errors.append(
            "a plan that changes the system must describe its expected end "
            "state (plan_context.expected_end_state)"
        )
    return plan_obj
