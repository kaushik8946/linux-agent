"""Development-tool installation (Build 12, module 6).

The model may ask for one of a fixed set of tools. Each maps to one package
name for the apt adapter, and installation goes through the normal approval
and verification path. Arbitrary package names are not accepted here; use
``install_package`` for those. Nothing is removed.
"""

from __future__ import annotations

import shutil
from typing import Any

from agent.command_policy import RiskClass
from agent.operations import Operation, OperationSpec
from agent.registry import ToolRegistry, ToolSpec
from agent.verifier import Check, CheckResult
from hostplatform.packages.base import PackageAdapter, PackageStatus
from tools.packages import INSTALL_PACKAGE, describe_package

INSTALL_DEV_TOOL = "install_dev_tool"
LIST_DEV_TOOLS = "list_dev_tools"

# tool id -> (apt package, executable that proves it works)
DEV_TOOLS: dict[str, tuple[str, str]] = {
    "git": ("git", "git"),
    "curl": ("curl", "curl"),
    "jq": ("jq", "jq"),
    "make": ("make", "make"),
    "python3-venv": ("python3-venv", "python3"),
    "shellcheck": ("shellcheck", "shellcheck"),
    "sqlite3": ("sqlite3", "sqlite3"),
    "tmux": ("tmux", "tmux"),
}


def register_dev_tools(registry: ToolRegistry, adapter: PackageAdapter | None) -> None:
    if adapter is None or not adapter.available():
        return

    def listing(_a: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for tool, (package, exe) in sorted(DEV_TOOLS.items()):
            state = adapter.query(package)
            rows.append(
                {
                    "tool": tool,
                    "package": package,
                    "installed": state.status is PackageStatus.INSTALLED,
                    "executable_on_path": shutil.which(exe) is not None,
                }
            )
        return {"tools": rows}

    registry.register(
        ToolSpec(
            name=LIST_DEV_TOOLS,
            description="List the supported development tools and whether each is installed. Read-only.",
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

    def prepare(args: dict[str, Any]) -> Operation:
        tool = args["tool"]
        package, exe = DEV_TOOLS[tool]
        # Delegate to the same validated package preparation as install_package.
        inner = registry.prepare(INSTALL_PACKAGE, {"package": package})
        if isinstance(inner, dict):
            return _refused(tool, str(inner.get("error")))
        return _wrap(tool, package, exe, inner)

    def _wrap(tool: str, package: str, exe: str, inner: Operation) -> Operation:
        def check_package() -> CheckResult:
            st = adapter.query(package)
            return CheckResult(
                f"{package} installed",
                st.status is PackageStatus.INSTALLED,
                st.status.value,
            )

        def check_exe() -> CheckResult:
            found = shutil.which(exe)
            return CheckResult(
                f"{exe} on PATH", found is not None, found or "not found"
            )

        return Operation(
            tool=INSTALL_DEV_TOOL,
            arguments={"tool": tool},
            summary=f"install development tool {tool} ({package})"
            if not inner.no_change_needed
            else f"{tool} already installed",
            commands=inner.commands,
            risk=inner.risk,
            baseline=inner.baseline,
            expected_end_state=f"{tool} available ({package} installed, {exe} on PATH)",
            affected=inner.affected,
            verification=[f"{package} installed", f"{exe} found on PATH"],
            recovery=inner.recovery,
            perform=inner.perform,
            checks=[Check("package", check_package), Check("executable", check_exe)],
            probe_state=lambda: describe_package(adapter.query(package)),
            diagnose=lambda: [describe_package(adapter.query(package))],
            no_change_needed=inner.no_change_needed,
            refused=inner.refused,
            needs_privilege=inner.needs_privilege,
        )

    def _refused(tool: str, reason: str) -> Operation:
        return Operation(
            tool=INSTALL_DEV_TOOL,
            arguments={"tool": tool},
            summary=f"cannot install {tool}",
            commands=[],
            risk=RiskClass.SYSTEM_CHANGE,
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
            name=INSTALL_DEV_TOOL,
            description=(
                "Install one supported development tool: "
                + ", ".join(sorted(DEV_TOOLS))
                + ". Needs local approval. Use list_dev_tools to see what is installed."
            ),
            input_schema={
                "type": "object",
                "properties": {"tool": {"type": "string", "enum": sorted(DEV_TOOLS)}},
                "required": ["tool"],
                "additionalProperties": False,
            },
            prepare=prepare,
        )
    )
