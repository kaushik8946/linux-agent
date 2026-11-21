"""Docker read-only tools (Build 12, module 2)."""

from __future__ import annotations

from typing import Any

from agent.command_policy import RiskClass
from agent.registry import ToolRegistry, ToolSpec
from hostplatform.docker import (
    DockerClient,
    DockerInfo,
    DockerNameError,
    DockerStatus,
)

DOCKER_STATUS = "docker_status"
LIST_CONTAINERS = "list_containers"
LIST_IMAGES = "list_images"
INSPECT_CONTAINER = "inspect_container"
CONTAINER_LOGS = "container_logs"

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_NAME = {"type": "string", "maxLength": 128}


def info_dict(info: DockerInfo) -> dict[str, Any]:
    return {
        "status": info.status.value,
        "client_version": info.client_version,
        "server_version": info.server_version,
        "detail": info.detail,
    }


def register_docker_tools(registry: ToolRegistry, client: DockerClient | None) -> None:
    if client is None or not client.available():
        return

    def status(_a: dict[str, Any]) -> dict[str, Any]:
        return info_dict(client.info())

    def containers(a: dict[str, Any]) -> dict[str, Any]:
        rows, err = client.containers(bool(a.get("all", True)))
        if err is not None:
            return info_dict(err)
        return {"status": "ok", "count": len(rows), "containers": rows}

    def images(_a: dict[str, Any]) -> dict[str, Any]:
        rows, err = client.images()
        if err is not None:
            return info_dict(err)
        return {"status": "ok", "count": len(rows), "images": rows}

    def inspect(a: dict[str, Any]) -> dict[str, Any]:
        try:
            return client.inspect(a["container"])
        except DockerNameError as exc:
            return {"error": str(exc)}

    def logs(a: dict[str, Any]) -> dict[str, Any]:
        try:
            return client.logs(a["container"], a.get("lines", 50))
        except DockerNameError as exc:
            return {"error": str(exc)}

    def reg(name: str, desc: str, schema: dict[str, Any], fn: Any) -> None:
        registry.register(
            ToolSpec(
                name=name,
                description=desc + " Read-only.",
                input_schema=schema,
                output_schema={"type": "object"},
                handler=fn,
                risk=RiskClass.READ_ONLY,
            )
        )

    reg(
        DOCKER_STATUS,
        "Report whether Docker is installed and its daemon reachable.",
        _EMPTY,
        status,
    )
    reg(
        LIST_CONTAINERS,
        "List Docker containers (name, image, state, ports).",
        {
            "type": "object",
            "properties": {"all": {"type": "boolean"}},
            "additionalProperties": False,
        },
        containers,
    )
    reg(LIST_IMAGES, "List local Docker images.", _EMPTY, images)
    reg(
        INSPECT_CONTAINER,
        "Show one container's state, restart policy, ports and health. "
        "Environment variables are never returned.",
        {
            "type": "object",
            "properties": {"container": _NAME},
            "required": ["container"],
            "additionalProperties": False,
        },
        inspect,
    )
    reg(
        CONTAINER_LOGS,
        "Show the last lines of a container's logs (redacted, bounded).",
        {
            "type": "object",
            "properties": {
                "container": _NAME,
                "lines": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["container"],
            "additionalProperties": False,
        },
        logs,
    )


__all__ = ["DockerStatus", "register_docker_tools"]
