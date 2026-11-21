"""Environment-variable tools (Build 12, module 1).

Read-only: ``list_managed_env``, ``get_managed_env``.
Changing (approval required): ``set_env_var``, ``unset_env_var``.

Only the agent's own managed file is edited. Verification starts a fresh
process that sources the file and reads the variable back.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any

from agent.command_policy import RiskClass
from agent.operations import Operation, OperationSpec
from agent.registry import ToolRegistry, ToolSpec
from agent.verifier import Check, CheckResult
from hostplatform.envstore import EnvError, EnvStore, validate_name, validate_value
from hostplatform.privileged import ProcResult, run_fixed

LIST_ENV = "list_managed_env"
GET_ENV = "get_managed_env"
SET_ENV = "set_env_var"
UNSET_ENV = "unset_env_var"

# Constant script: file and name arrive as positional parameters, never
# interpolated, so a value cannot become shell syntax.
_PROBE = '. "$1" >/dev/null 2>&1; printenv -- "$2"'

Runner = Callable[..., ProcResult]


def fresh_process_value(
    store: EnvStore, name: str, shell: str | None, run: Runner
) -> str | None:
    """Value seen by a brand-new process that sources the managed file."""
    if shell is None or not store.file.exists():
        return None
    result = run(
        [shell, "-c", _PROBE, "sh", str(store.file), name],
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "HOME": str(store.home)},
    )
    if not result.ok:
        return None
    return result.stdout.rstrip("\n")


def register_env_tools(
    registry: ToolRegistry,
    store: EnvStore,
    shell: str | None = "/bin/sh",
    run: Runner = run_fixed,
) -> None:
    def listing(_args: dict[str, Any]) -> dict[str, Any]:
        values = store.read_all()
        return {
            "file": str(store.file),
            "variables": sorted(values),
            "count": len(values),
            "profile_sources_file": store.profile_hooked(),
        }

    def getter(args: dict[str, Any]) -> dict[str, Any]:
        try:
            state = store.get(validate_name(args["name"]))
        except EnvError as exc:
            return {"error": str(exc)}
        return {"name": state.name, "managed": state.managed, "value": state.value}

    name_schema = {"type": "string", "maxLength": 64}
    registry.register(
        ToolSpec(
            name=LIST_ENV,
            description=(
                "List the environment variables this agent manages (names "
                "only). Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            handler=listing,
            risk=RiskClass.READ_ONLY,
        )
    )
    registry.register(
        ToolSpec(
            name=GET_ENV,
            description="Show one managed environment variable. Read-only.",
            input_schema={
                "type": "object",
                "properties": {"name": name_schema},
                "required": ["name"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            handler=getter,
            risk=RiskClass.READ_ONLY,
        )
    )

    def checks(name: str, want: str | None) -> list[Check]:
        def in_file() -> CheckResult:
            got = store.get(name).value
            return CheckResult(
                f"{name} {'removed from' if want is None else 'set in'} the file",
                got == want,
                "value matches" if got == want else "value does not match",
            )

        def fresh() -> CheckResult:
            got = fresh_process_value(store, name, shell, run)
            if shell is None:
                return CheckResult("new process sees it", None, "no shell available")
            return CheckResult(
                "new process sees it",
                got == want,
                "as expected" if got == want else "differs in a new process",
            )

        found = [Check("file", in_file), Check("fresh process", fresh)]
        if want is not None:
            found.append(
                Check(
                    "profile sources it",
                    lambda: CheckResult(
                        "profile sources it",
                        store.profile_hooked(),
                        str(store.profile),
                    ),
                )
            )
        return found

    def build(
        tool: str, name: str, want: str | None, summary: str, command: str
    ) -> Operation:
        current = store.get(name)
        baseline = (
            f"{name}={current.value!r} (managed)"
            if current.managed
            else f"{name} not managed by the agent"
        )
        return Operation(
            tool=tool,
            arguments={"name": name, **({"value": want} if want is not None else {})},
            summary=summary,
            commands=[command],
            risk=RiskClass.USER_LEVEL_CHANGE,
            baseline=baseline,
            expected_end_state=(
                f"{name}={want!r} in {store.file}"
                if want is not None
                else f"{name} removed from {store.file}"
            ),
            affected=[str(store.file)]
            + ([str(store.profile)] if want is not None else []),
            verification=[
                "file holds the expected value",
                "a new process that sources the file sees it",
            ],
            recovery=(
                f"Edit or delete {store.file}; the agent only changes that file "
                "and one marked block in ~/.profile."
            ),
            perform=lambda: apply(tool, name, want),
            checks=checks(name, want),
            probe_state=lambda: describe(name),
            diagnose=lambda: [
                f"file: {store.file} exists={store.file.exists()}",
                f"profile hook present: {store.profile_hooked()}",
            ],
            no_change_needed=(
                current.value == want if want is not None else not current.managed
            ),
        )

    def describe(name: str) -> str:
        current = store.get(name)
        return (
            f"{name}={current.value!r} (managed)"
            if current.managed
            else f"{name} not managed by the agent"
        )

    def apply(tool: str, name: str, want: str | None) -> dict[str, Any]:
        try:
            if want is None:
                store.unset(name)
            else:
                store.set(name, want)
        except OSError as exc:
            return {"ok": False, "status": "failed", "error": type(exc).__name__}
        return {"ok": True, "status": "written", "file": str(store.file)}

    def prepare_set(args: dict[str, Any]) -> Operation:
        name = validate_name(args["name"])
        value = validate_value(args["value"])
        return build(
            SET_ENV,
            name,
            value,
            f"set {name} for future login shells",
            f"write export {name}={shlex.quote(value)} to {store.file}",
        )

    def prepare_unset(args: dict[str, Any]) -> Operation:
        name = validate_name(args["name"])
        return build(
            UNSET_ENV,
            name,
            None,
            f"stop managing {name}",
            f"remove {name} from {store.file}",
        )

    registry.register_operation(
        OperationSpec(
            name=SET_ENV,
            description=(
                "Persist an environment variable for future login shells in the "
                "agent's own managed file. Needs local approval. Secrets, PATH "
                "and loader variables are refused. Running shells are not "
                "changed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": name_schema,
                    "value": {"type": "string", "maxLength": 1000},
                },
                "required": ["name", "value"],
                "additionalProperties": False,
            },
            prepare=prepare_set,
            risk=RiskClass.USER_LEVEL_CHANGE,
        )
    )
    registry.register_operation(
        OperationSpec(
            name=UNSET_ENV,
            description=(
                "Remove a variable the agent previously set. Only the agent's "
                "managed file is edited. Needs local approval."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": name_schema},
                "required": ["name"],
                "additionalProperties": False,
            },
            prepare=prepare_unset,
            risk=RiskClass.USER_LEVEL_CHANGE,
        )
    )
