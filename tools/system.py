"""Host-context tools: facts local code can know without the model guessing.

Both tools are read-only and return structured data. They read /proc and
discovery results; they never run a command and never collect environment
variable values or other secrets.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.command_policy import EXECUTABLE_NAME, RiskClass, classify
from agent.registry import ToolRegistry, ToolSpec
from hostplatform.capabilities import HostProfile
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover

MAX_NAMES = 20

GET_HOST_SUMMARY = "get_host_summary"
GET_COMMAND_ENVIRONMENT = "get_command_environment"

_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

HOST_SUMMARY_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["os", "kernel", "architecture", "hardware", "privileges", "warnings"],
    "properties": {
        "os": {
            "type": "object",
            "properties": {
                "id": _NULLABLE_STR,
                "id_like": _STR_LIST,
                "version_id": _NULLABLE_STR,
                "pretty_name": _NULLABLE_STR,
            },
        },
        "kernel": _NULLABLE_STR,
        "architecture": _NULLABLE_STR,
        "python_version": {"type": "string"},
        "hardware": {
            "type": "object",
            "properties": {
                "cpu_count": _NULLABLE_INT,
                "memory_total_mb": _NULLABLE_INT,
                "uptime_seconds": _NULLABLE_INT,
            },
        },
        "privileges": {
            "type": "object",
            "properties": {
                "is_root": {"type": ["boolean", "null"]},
                "sudo_available": {"type": "boolean"},
                "doas_available": {"type": "boolean"},
            },
        },
        "service_manager": {
            "type": "object",
            "properties": {"name": _NULLABLE_STR, "state": {"type": "string"}},
        },
        "package_managers": _STR_LIST,
        "containers": {
            "type": "object",
            "properties": {
                "detected": {"type": ["boolean", "null"]},
                "indicators": _STR_LIST,
            },
        },
        "utilities": {
            "type": "object",
            "properties": {"available": _STR_LIST, "missing": _STR_LIST},
        },
        "warnings": _STR_LIST,
    },
}

COMMAND_ENVIRONMENT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["path_dirs", "executables", "constraints", "warnings"],
    "properties": {
        "path_dirs": _STR_LIST,
        "executables": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "path": _NULLABLE_STR,
                    "problem": _NULLABLE_STR,
                    "never_runnable": {"type": "boolean"},
                },
            },
        },
        "constraints": {"type": "object"},
        "warnings": _STR_LIST,
    },
}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_memory_total_mb(root: Path) -> int | None:
    text = _read(root / "proc/meminfo")
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text or "", re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def read_uptime_seconds(root: Path) -> int | None:
    text = _read(root / "proc/uptime")
    try:
        return int(float((text or "").split()[0]))
    except (IndexError, ValueError):
        return None


def host_summary(
    profile: HostProfile,
    root: Path = Path("/"),
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> dict[str, Any]:
    """Normalise a host profile and a few /proc facts into one dictionary."""
    warnings = list(profile.warnings)
    memory = read_memory_total_mb(root)
    uptime = read_uptime_seconds(root)
    if memory is None:
        warnings.append("memory size unavailable (/proc/meminfo unreadable)")
    if uptime is None:
        warnings.append("uptime unavailable (/proc/uptime unreadable)")
    containers = profile.container_indicators
    return {
        "os": {
            "id": profile.os.distro_id,
            "id_like": list(profile.os.id_like),
            "version_id": profile.os.version_id,
            "pretty_name": profile.os.pretty_name,
        },
        "kernel": profile.kernel,
        "architecture": profile.architecture,
        "python_version": profile.python_version,
        "hardware": {
            "cpu_count": cpu_count(),
            "memory_total_mb": memory,
            "uptime_seconds": uptime,
        },
        "privileges": {
            "is_root": profile.privileges.is_root,
            "sudo_available": profile.privileges.sudo_available,
            "doas_available": profile.privileges.doas_available,
        },
        "service_manager": {
            "name": profile.service_manager.name,
            "state": profile.service_manager.state,
        },
        "package_managers": list(profile.package_managers),
        "containers": {
            "detected": None if containers is None else bool(containers),
            "indicators": list(containers or ()),
        },
        "utilities": {
            "available": list(profile.available_utilities),
            "missing": list(profile.missing_utilities),
        },
        "warnings": warnings,
    }


def command_environment(
    runner: CommandRunner, names: list[str] | None = None
) -> dict[str, Any]:
    """Where commands resolve from, and the limits the executor enforces."""
    warnings: list[str] = []
    executables: dict[str, dict[str, Any]] = {}
    for name in names or []:
        if not EXECUTABLE_NAME.fullmatch(name):
            warnings.append("ignored a name that is not a plain command name")
            continue
        path, problem = runner.locate(name)
        # Shells, wrappers and privilege tools exist on the host but the
        # policy never runs them, whatever the arguments.
        never = classify(name, []).risk in (RiskClass.UNSUPPORTED, RiskClass.PRIVILEGED)
        executables[name] = {"path": path, "problem": problem, "never_runnable": never}
        if path is None:
            warnings.append(f"'{name}' is not available: {problem}")
        elif never:
            warnings.append(f"'{name}' exists but is never run by this agent")
    if not runner.trusted_dirs:
        warnings.append("no trusted directories found; no command can run")
    return {
        "path_dirs": [str(d) for d in runner.trusted_dirs],
        "executables": executables,
        "constraints": runner.constraints(),
        "warnings": warnings,
    }


def build_registry(
    profile: HostProfile | None = None,
    runner: CommandRunner | None = None,
    root: Path = Path("/"),
    adapters: bool = False,
    workspace_root: Path | None = None,
) -> ToolRegistry:
    """Register the Build 5 context tools."""
    host = profile or discover()
    cmd_runner = runner or CommandRunner.for_host(host)
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name=GET_HOST_SUMMARY,
            description=(
                "Return normalised facts about this machine: OS, kernel, "
                "architecture, CPU count, memory, uptime, privileges, service "
                "manager, package managers, container indicators and which "
                "diagnostic utilities exist. Unknown values are null. This is "
                "context; use execute_command_plan to inspect live state."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=HOST_SUMMARY_OUTPUT,
            handler=lambda _args: host_summary(host, root),
            risk=RiskClass.READ_ONLY,
        )
    )
    registry.register(
        ToolSpec(
            name=GET_COMMAND_ENVIRONMENT,
            description=(
                "Return the directories commands are resolved from, the "
                "location of any named executables (or why one cannot be used), "
                "and the limits the executor enforces. Use it to check a "
                "command exists before proposing it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 64},
                        "maxItems": MAX_NAMES,
                        "description": "Command names to locate, e.g. ['lsof'].",
                    }
                },
                "additionalProperties": False,
            },
            output_schema=COMMAND_ENVIRONMENT_OUTPUT,
            handler=lambda args: command_environment(cmd_runner, args.get("names")),
            risk=RiskClass.READ_ONLY,
        )
    )
    if adapters:
        from tools.adapters import register_host_adapters

        register_host_adapters(registry, host, workspace_root=workspace_root)
    return registry
