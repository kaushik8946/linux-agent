"""Network and DNS diagnostic tools (Build 12, module 5). All read-only."""

from __future__ import annotations

from typing import Any

from agent.command_policy import RiskClass
from agent.registry import ToolRegistry, ToolSpec
from hostplatform import netdiag

DNS_LOOKUP = "dns_lookup"
TCP_CHECK = "tcp_check"
RESOLVER_CONFIG = "resolver_config"
DEFAULT_ROUTE = "default_route"

_HOST = {"type": "string", "maxLength": 253}


def register_network_tools(
    registry: ToolRegistry, resolve: Any = None, connect: Any = None
) -> None:
    import socket

    resolve = resolve or socket.getaddrinfo
    connect = connect or socket.create_connection

    def safe(fn: Any) -> Any:
        def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return fn(args)
            except netdiag.HostError as exc:
                return {"error": str(exc)}

        return run

    def add(
        name: str, desc: str, props: dict[str, Any], required: list[str], fn: Any
    ) -> None:
        registry.register(
            ToolSpec(
                name=name,
                description=desc + " Read-only.",
                input_schema={
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                handler=safe(fn),
                risk=RiskClass.READ_ONLY,
            )
        )

    add(
        DNS_LOOKUP,
        "Resolve a hostname with the system resolver and report addresses and timing.",
        {"host": _HOST},
        ["host"],
        lambda a: netdiag.dns_lookup(a["host"], resolve),
    )
    add(
        TCP_CHECK,
        "Open and immediately close a TCP connection to host:port to test "
        "reachability. No data is sent.",
        {
            "host": _HOST,
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "timeout_seconds": {"type": "number", "minimum": 0.5, "maximum": 10},
        },
        ["host", "port"],
        lambda a: netdiag.tcp_check(
            a["host"], a["port"], a.get("timeout_seconds", 5.0), connect
        ),
    )
    add(
        RESOLVER_CONFIG,
        "Show configured DNS servers and search domains.",
        {},
        [],
        lambda _a: netdiag.resolver_config(),
    )
    add(
        DEFAULT_ROUTE,
        "Show whether a default route exists and through which interface.",
        {},
        [],
        lambda _a: netdiag.default_route(),
    )
