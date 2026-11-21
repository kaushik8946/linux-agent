"""Project initialisation tool (Build 12, module 4).

Changing (approval required): ``init_project``. It creates a fixed template
directory inside the configured workspace root. Verification checks that every
planned file exists afterwards and that nothing else was added.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.command_policy import RiskClass
from agent.operations import Operation, OperationSpec
from agent.registry import ToolRegistry
from agent.verifier import Check, CheckResult
from hostplatform.workspace import (
    KINDS,
    ProjectPlan,
    WorkspaceError,
    conflicts,
    create,
    plan_project,
    preview,
    workspace_problem,
)

INIT_PROJECT = "init_project"


def register_workspace_tools(registry: ToolRegistry, root: Path | None) -> None:
    if root is None:
        return

    def prepare(args: dict[str, Any]) -> Operation:
        problem = workspace_problem(root)
        if problem:
            return _refused(root, args, problem)
        plan: ProjectPlan = plan_project(root, args["name"], args["kind"])
        taken = conflicts(plan)
        if taken:
            return _refused(root, args, f"already exists: {', '.join(taken[:5])}")
        return _operation(plan)

    def _operation(plan: ProjectPlan) -> Operation:
        def perform() -> dict[str, Any]:
            try:
                written = create(plan)
            except (WorkspaceError, OSError) as exc:
                return {"ok": False, "status": "failed", "error": type(exc).__name__}
            return {"ok": True, "status": "created", "files": written}

        def present() -> CheckResult:
            missing = [rel for rel in plan.files if not (plan.path / rel).is_file()]
            return CheckResult(
                "all template files exist", not missing, f"missing {len(missing)}"
            )

        def extra() -> CheckResult:
            found = {
                str(p.relative_to(plan.path))
                for p in plan.path.rglob("*")
                if p.is_file()
            }
            surplus = found - set(plan.files)
            return CheckResult(
                "nothing extra was created",
                not surplus,
                f"{len(surplus)} unexpected file(s)",
            )

        def state() -> str:
            if not plan.path.exists():
                return f"{plan.path} does not exist"
            present_count = sum(1 for rel in plan.files if (plan.path / rel).is_file())
            return f"{plan.path} exists with {present_count}/{len(plan.files)} files"

        return Operation(
            tool=INIT_PROJECT,
            arguments={"name": plan.name, "kind": plan.kind},
            summary=f"create {plan.kind} project {plan.name} in {plan.root}",
            commands=[preview(plan).replace("\n", "; ")],
            risk=RiskClass.USER_LEVEL_CHANGE,
            baseline=f"{plan.path} does not exist",
            expected_end_state=f"{plan.path} with {len(plan.files)} template files",
            affected=[str(plan.path)],
            verification=["every template file exists", "no extra files"],
            recovery=(
                f"Delete it yourself if unwanted: rm -r {plan.path}. This agent "
                "does not delete project directories."
            ),
            perform=perform,
            checks=[Check("files", present), Check("extra", extra)],
            probe_state=state,
            diagnose=lambda: [f"workspace: {plan.root}"],
        )

    def _refused(root_: Path, args: dict[str, Any], reason: str) -> Operation:
        return Operation(
            tool=INIT_PROJECT,
            arguments=dict(args),
            summary="cannot create project",
            commands=[],
            risk=RiskClass.USER_LEVEL_CHANGE,
            baseline="unknown",
            expected_end_state="not attempted",
            affected=[],
            verification=[],
            recovery="",
            perform=lambda: {"ok": False, "status": "not_attempted"},
            refused=reason,
        )

    registry.register_operation(
        OperationSpec(
            name=INIT_PROJECT,
            description=(
                "Create a new project from a fixed template (python or text) "
                f"directly inside the approved workspace. Kinds: {', '.join(KINDS)}. "
                "Needs local approval. Existing paths are never overwritten."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 64},
                    "kind": {"type": "string", "enum": list(KINDS)},
                },
                "required": ["name", "kind"],
                "additionalProperties": False,
            },
            prepare=prepare,
            risk=RiskClass.USER_LEVEL_CHANGE,
        )
    )
