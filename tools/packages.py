"""Package and service tools backed by the first adapters (Build 10).

Read-only: ``query_package``, ``service_status``.
Changing (approval required): ``install_package``, ``start_service``.

Package removal, service stop/disable/mask and repository changes are
deliberately not offered.
"""

from __future__ import annotations

import shlex
from typing import Any

from agent.command_policy import RiskClass
from agent.operations import Operation, OperationSpec
from agent.registry import ToolRegistry, ToolSpec
from hostplatform.packages.base import (
    InstallResult,
    PackageAdapter,
    PackageState,
    validate_package_name,
)
from hostplatform.services.base import (
    ServiceAdapter,
    ServiceState,
    StartResult,
    validate_service_name,
)

QUERY_PACKAGE = "query_package"
INSTALL_PACKAGE = "install_package"
SERVICE_STATUS = "service_status"
START_SERVICE = "start_service"

_NAME_SCHEMA = {"type": "string", "maxLength": 128}


def package_state_dict(state: PackageState) -> dict[str, Any]:
    return {
        "package": state.name,
        "status": state.status.value,
        "installed_version": state.installed_version,
        "candidate_version": state.candidate_version,
        "detail": state.detail,
    }


def install_result_dict(result: InstallResult) -> dict[str, Any]:
    return {
        "package": result.package,
        "status": result.status.value,
        "ok": result.ok,
        "message": result.message,
        "before": package_state_dict(result.before) if result.before else None,
        "after": package_state_dict(result.after) if result.after else None,
        "stderr_excerpt": result.stderr_excerpt,
    }


def service_state_dict(state: ServiceState) -> dict[str, Any]:
    return {
        "unit": state.unit,
        "status": state.status.value,
        "sub_state": state.sub_state,
        "enabled": state.enabled,
        "main_pid": state.main_pid,
        "detail": state.detail,
    }


def start_result_dict(result: StartResult) -> dict[str, Any]:
    return {
        "unit": result.unit,
        "status": result.status.value,
        "ok": result.ok,
        "message": result.message,
        "before": service_state_dict(result.before) if result.before else None,
        "after": service_state_dict(result.after) if result.after else None,
        "stderr_excerpt": result.stderr_excerpt,
    }


def register_package_tools(
    registry: ToolRegistry, adapter: PackageAdapter | None
) -> None:
    if adapter is None or not adapter.available():
        return

    def query(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return package_state_dict(adapter.query(args["package"]))
        except ValueError as exc:
            return {"error": str(exc)}

    registry.register(
        ToolSpec(
            name=QUERY_PACKAGE,
            description=(
                f"Check whether a {adapter.name} package is installed and which "
                "version is installed or available. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {"package": _NAME_SCHEMA},
                "required": ["package"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            handler=query,
        )
    )

    def prepare_install(args: dict[str, Any]) -> Operation:
        name = validate_package_name(args["package"])
        plan = adapter.plan_install(name)
        state = adapter.query(name)
        version = f" ({state.installed_version})" if state.installed_version else ""

        def op(
            summary: str,
            commands: list[str],
            perform: Any,
            expected: str,
            **flags: Any,
        ) -> Operation:
            return Operation(
                tool=INSTALL_PACKAGE,
                arguments={"package": name},
                summary=summary,
                commands=commands,
                risk=RiskClass.SYSTEM_CHANGE,
                baseline=f"{name}: {state.status.value}{version}",
                expected_end_state=expected,
                affected=[f"package {p}" for p in (plan.new_packages or (name,))],
                verification=[
                    f"{name} reports status installed",
                    "installed version is recorded",
                ],
                recovery=(
                    f"If unwanted, remove it yourself with: sudo apt-get remove "
                    f"{name}. This agent does not remove packages."
                ),
                perform=perform,
                **flags,
            )

        if plan.already_installed:
            return op(
                f"{name} is already installed; nothing to do",
                [],
                lambda: {"status": "already_installed"},
                f"{name} installed",
                no_change_needed=True,
            )
        if not plan.feasible:
            return op(
                f"cannot install {name}",
                [],
                lambda: {"status": "not_attempted"},
                f"{name} installed",
                refused=plan.reason,
            )
        extra = [p for p in plan.new_packages if p != name]
        summary = f"install {name} {plan.candidate_version or ''}".strip()
        if extra:
            summary += f" plus {len(extra)} dependency package(s)"
        return op(
            summary,
            [shlex.join(plan.argv)],
            lambda: install_result_dict(adapter.install(plan)),
            f"{name} installed at version {plan.candidate_version}",
            needs_privilege=plan.needs_privilege,
        )

    registry.register_operation(
        OperationSpec(
            name=INSTALL_PACKAGE,
            description=(
                f"Install one named package with {adapter.name}. The user sees "
                "exactly what would be installed and must approve it locally. "
                "Removal is not supported."
            ),
            input_schema={
                "type": "object",
                "properties": {"package": _NAME_SCHEMA},
                "required": ["package"],
                "additionalProperties": False,
            },
            prepare=prepare_install,
        )
    )


def adapter_systemctl(adapter: ServiceAdapter) -> str | None:
    return getattr(adapter, "systemctl", None)


def register_service_tools(
    registry: ToolRegistry, adapter: ServiceAdapter | None
) -> None:
    if adapter is None or not adapter.available():
        return

    def status(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return service_state_dict(adapter.status(args["service"]))
        except ValueError as exc:
            return {"error": str(exc)}

    registry.register(
        ToolSpec(
            name=SERVICE_STATUS,
            description=f"Show a {adapter.name} service's state. Read-only.",
            input_schema={
                "type": "object",
                "properties": {"service": _NAME_SCHEMA},
                "required": ["service"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            handler=status,
        )
    )

    def prepare_start(args: dict[str, Any]) -> Operation:
        unit = validate_service_name(args["service"])
        state = adapter.status(unit)
        argv = adapter.start_argv(unit)
        sub = f" ({state.sub_state})" if state.sub_state else ""

        def op(
            summary: str, commands: list[str], perform: Any, **flags: Any
        ) -> Operation:
            return Operation(
                tool=START_SERVICE,
                arguments={"service": unit},
                summary=summary,
                commands=commands,
                risk=RiskClass.SYSTEM_CHANGE,
                baseline=f"{unit}: {state.status.value}{sub}",
                expected_end_state=f"{unit} active",
                affected=[f"service {unit}"],
                verification=[
                    f"{unit} ActiveState=active",
                    "it stays active after starting",
                ],
                recovery=f"If unwanted, stop it yourself with: sudo systemctl stop {unit}.",
                perform=perform,
                **flags,
            )

        if state.status.value == "active":
            return op(
                f"{unit} is already active; nothing to do",
                [],
                lambda: {"status": "already_active"},
                no_change_needed=True,
            )
        if state.status.value == "not_found" or argv is None:
            return op(
                f"cannot start {unit}",
                [],
                lambda: {"status": "not_attempted"},
                refused=state.detail or "service cannot be started on this host",
            )
        return op(
            f"start {unit}",
            [shlex.join(argv)],
            lambda: start_result_dict(adapter.start(unit)),
            needs_privilege=argv[0] != adapter_systemctl(adapter),
        )

    registry.register_operation(
        OperationSpec(
            name=START_SERVICE,
            description=(
                f"Start one {adapter.name} service. Requires local approval. "
                "Stopping, disabling and masking are not supported."
            ),
            input_schema={
                "type": "object",
                "properties": {"service": _NAME_SCHEMA},
                "required": ["service"],
                "additionalProperties": False,
            },
            prepare=prepare_start,
        )
    )
