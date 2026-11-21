"""Controlled executor for approved changes (Build 9).

Order of checks, all done immediately before running:

1. The plan is re-parsed from the original model arguments and must have
   the same fingerprint as the plan the user approved.
2. Every step is classified again and its requirement must still be covered
   by the approval; nothing blocked may appear.
3. The approval must be valid (same fingerprint, not expired, same
   requirement level).
4. Capabilities are checked again through the runner's resolution.
5. A plan already completed in this journal is not run again without the
   user knowing; the model is told it already ran.

The journal records the task and every step as planned before anything
runs, then each step's result as soon as it finishes, so a crash or
cancellation leaves an accurate record of partial completion.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.approval import Approval, ApprovalService
from agent.command_plan import CommandPlan
from agent.command_policy import RiskClass
from agent.operations import Operation
from agent.planner import ActionPlan, build_action_plan
from agent.policy import Requirement
from agent.task_store import JournalError, TaskJournal, excerpt, new_task_id
from agent.verifier import VerificationReport, verify
from hostplatform.command_runner import CommandRunner, PlanResult, Status, StepResult

# Risk classes an approval may unlock. Anything else stays blocked.
APPROVABLE = {
    Requirement.CONFIRM: frozenset(
        {RiskClass.USER_LEVEL_CHANGE, RiskClass.SYSTEM_CHANGE}
    ),
    Requirement.TYPED: frozenset(
        {RiskClass.USER_LEVEL_CHANGE, RiskClass.SYSTEM_CHANGE, RiskClass.DESTRUCTIVE}
    ),
}


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str  # completed | partial | failed | rejected | cancelled | duplicate
    message: str
    plan_result: PlanResult | None = None
    problems: list[str] = field(default_factory=list)
    operation_result: dict[str, Any] | None = None
    verification: VerificationReport | None = None

    @property
    def executed_anything(self) -> bool:
        if self.operation_result is not None:
            return True
        return self.plan_result is not None and any(
            r.status not in (Status.SKIPPED, Status.BLOCKED)
            for r in self.plan_result.results
        )


class ChangeExecutor:
    def __init__(
        self,
        runner: CommandRunner,
        approvals: ApprovalService,
        journal: TaskJournal,
    ) -> None:
        self.runner = runner
        self.approvals = approvals
        self.journal = journal
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def revalidate(
        self, plan: CommandPlan, approved: ActionPlan
    ) -> tuple[ActionPlan, list[str]]:
        """Rebuild the action plan now and compare it with what was approved."""
        current = build_action_plan(
            plan,
            self.runner.preview_steps(plan, gate=False),
            str(self.runner.default_cwd),
        )
        problems: list[str] = []
        if current.fingerprint != approved.fingerprint:
            problems.append("the plan changed after it was approved")
        if current.requirement is not approved.requirement:
            problems.append("the plan's risk changed after it was approved")
        if current.blocked:
            problems.append("a step is now blocked or its command is unavailable")
        problems += current.errors
        return current, problems

    def execute(
        self,
        plan: CommandPlan,
        approved: ActionPlan,
        approval: Approval | None,
        cancel: threading.Event | None = None,
        on_step: Any = None,
        allow_repeat: bool = False,
    ) -> ExecutionOutcome:
        task_id = new_task_id()
        cancel = cancel or threading.Event()

        current, problems = self.revalidate(plan, approved)
        if not self.approvals.is_valid(approval, current):
            problems.append("approval is missing, expired or does not match this plan")
        allowed = APPROVABLE.get(current.requirement, frozenset())
        for step in current.steps:
            risk = step.decision.risk
            if risk is not RiskClass.READ_ONLY and risk not in allowed:
                problems.append(
                    f"step {step.step_id} needs a risk level that was not approved"
                )

        try:
            self.journal.record(
                task_id,
                "task_started",
                fingerprint=current.fingerprint,
                requirement=current.requirement.value,
                approved=not problems,
                steps=[
                    {
                        "step_id": s.step_id,
                        "command": s.command,
                        "risk": s.decision.risk.value,
                    }
                    for s in current.steps
                ],
                expected_end_state=(
                    current.context.expected_end_state if current.context else None
                ),
            )
        except JournalError as exc:
            return ExecutionOutcome(task_id, "rejected", str(exc), problems=[str(exc)])

        if problems:
            self._finish(task_id, current, "rejected", problems=problems)
            return ExecutionOutcome(
                task_id,
                "rejected",
                "Not run: " + "; ".join(problems),
                problems=problems,
            )

        if (
            not allow_repeat
            and current.fingerprint in self.journal.completed_fingerprints()
        ):
            self._finish(task_id, current, "duplicate")
            return ExecutionOutcome(
                task_id,
                "duplicate",
                "This exact plan already completed earlier. It was not run again; "
                "check the current state instead of repeating it.",
            )

        with self._lock:
            if current.fingerprint in self._running:
                return ExecutionOutcome(
                    task_id, "rejected", "This plan is already running."
                )
            self._running.add(current.fingerprint)
        try:
            return self._run(task_id, plan, current, allowed, cancel, on_step)
        finally:
            with self._lock:
                self._running.discard(current.fingerprint)

    def _run(
        self,
        task_id: str,
        plan: CommandPlan,
        current: ActionPlan,
        allowed: frozenset[RiskClass],
        cancel: threading.Event,
        on_step: Any,
    ) -> ExecutionOutcome:
        ids = iter(s.step_id for s in current.steps)

        def record_step(result: StepResult) -> None:
            self.journal.record(
                task_id,
                "step_finished",
                step_id=result.step_id,
                command=result.preview,
                status=result.status.value,
                exit_code=result.exit_code,
                duration_seconds=round(result.duration_seconds, 3),
                stdout=excerpt(result.stdout),
                stderr=excerpt(result.stderr),
            )
            if on_step is not None:
                on_step(result)

        # Journal each step's start before it runs.
        for step_id in ids:
            self.journal.record(task_id, "step_planned", step_id=step_id)

        try:
            result = self.runner.run_plan(
                plan, cancel, extra_risks=allowed, on_step=record_step
            )
        except KeyboardInterrupt:
            self._finish(task_id, current, "cancelled")
            raise
        except JournalError as exc:
            cancel.set()
            self._finish(task_id, current, "failed", problems=[str(exc)])
            return ExecutionOutcome(task_id, "failed", str(exc))

        statuses = [r.status for r in result.results]
        if not result.executed:
            outcome = "rejected"
        elif all(s is Status.OK for s in statuses):
            outcome = "completed"
        elif any(s is Status.CANCELLED for s in statuses):
            outcome = "cancelled"
        elif any(s is Status.OK for s in statuses):
            outcome = "partial"
        else:
            outcome = "failed"
        self._finish(task_id, current, outcome, pending=result.pending)
        messages = {
            "completed": "All steps finished. Verify the end state before claiming success.",
            "partial": "Some steps ran and a later one failed. The system may be partly changed.",
            "failed": "The first step failed; later steps were not run.",
            "cancelled": "Cancelled; steps after the cancellation were not run.",
            "rejected": "The plan was rejected before running.",
        }
        return ExecutionOutcome(task_id, outcome, messages[outcome], plan_result=result)

    def execute_operation(
        self,
        operation: Operation,
        approval: Approval | None,
        recheck: Callable[[], Operation | dict[str, Any]],
        allow_repeat: bool = False,
    ) -> ExecutionOutcome:
        """Run an approved typed operation (package install, service start).

        ``recheck`` prepares the operation again from the same arguments just
        before running, so a change of state since approval is noticed.
        """
        task_id = new_task_id()
        problems: list[str] = []
        fresh = recheck()
        if isinstance(fresh, dict):
            problems.append(
                str(fresh.get("error", "operation could not be prepared again"))
            )
        elif fresh.fingerprint != operation.fingerprint:
            problems.append("the operation changed after it was approved")
        elif fresh.refused:
            problems.append(f"no longer possible: {fresh.refused}")
        if not self.approvals.is_valid(approval, operation):
            problems.append(
                "approval is missing, expired or does not match this operation"
            )
        if operation.requirement is Requirement.BLOCKED:
            problems.append("operation is blocked")

        try:
            self.journal.record(
                task_id,
                "task_started",
                kind="operation",
                tool=operation.tool,
                arguments=operation.arguments,
                fingerprint=operation.fingerprint,
                requirement=operation.requirement.value,
                approved=not problems,
                commands=operation.commands,
                baseline=operation.baseline,
                expected_end_state=operation.expected_end_state,
            )
        except JournalError as exc:
            return ExecutionOutcome(task_id, "rejected", str(exc), problems=[str(exc)])

        def finish(outcome: str, **data: Any) -> None:
            try:
                self.journal.record(
                    task_id,
                    "task_finished",
                    fingerprint=operation.fingerprint,
                    outcome=outcome,
                    **data,
                )
            except JournalError:
                pass

        if problems:
            finish("rejected", problems=problems)
            return ExecutionOutcome(
                task_id,
                "rejected",
                "Not run: " + "; ".join(problems),
                problems=problems,
            )
        if not isinstance(fresh, dict) and fresh.no_change_needed:
            finish("no_change_needed")
            return ExecutionOutcome(task_id, "no_change_needed", fresh.summary)
        if (
            not allow_repeat
            and operation.fingerprint in self.journal.completed_fingerprints()
        ):
            finish("duplicate")
            return ExecutionOutcome(
                task_id,
                "duplicate",
                "This exact operation already completed earlier; it was not run again.",
            )

        with self._lock:
            if operation.fingerprint in self._running:
                return ExecutionOutcome(
                    task_id, "rejected", "This operation is already running."
                )
            self._running.add(operation.fingerprint)
        try:
            self.journal.record(
                task_id,
                "step_planned",
                step_id="operation",
                commands=operation.commands,
            )
            try:
                result = operation.perform()
            except KeyboardInterrupt:
                finish("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - report, never crash
                finish("failed", error=type(exc).__name__)
                return ExecutionOutcome(
                    task_id, "failed", f"operation failed: {type(exc).__name__}"
                )
            ok = bool(result.get("ok"))
            self.journal.record(
                task_id,
                "step_finished",
                step_id="operation",
                status="ok" if ok else "failed",
                result=result,
            )
            # Verify the real end state; the adapter's own "ok" is not enough.
            report = verify(operation)
            stderr = result.get("stderr_excerpt")
            if not report.passed and isinstance(stderr, str) and stderr.strip():
                report.diagnostics.insert(0, f"command stderr: {excerpt(stderr)}")
            self.journal.record(
                task_id,
                "verification",
                fingerprint=operation.fingerprint,
                **report.to_dict(),
            )
            outcome = "completed" if ok and report.passed else "failed"
            if outcome == "failed" and report.partial_change:
                outcome = "partial"
            finish(outcome, verification=report.outcome.value)
            if outcome == "completed":
                message = "The operation finished and its end state was verified."
            elif ok:
                message = (
                    "The command reported success, but verification did not "
                    "pass. Stopped; no further change was made."
                )
            else:
                message = "The operation did not achieve its expected state."
            return ExecutionOutcome(
                task_id,
                outcome,
                message,
                operation_result=result,
                verification=report,
            )
        finally:
            with self._lock:
                self._running.discard(operation.fingerprint)

    def _finish(
        self, task_id: str, plan: ActionPlan, outcome: str, **data: Any
    ) -> None:
        try:
            self.journal.record(
                task_id,
                "task_finished",
                fingerprint=plan.fingerprint,
                outcome=outcome,
                **data,
            )
        except JournalError:
            pass  # already failing; the started record remains
