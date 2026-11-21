"""Network and DNS diagnostics (Build 12, module 5). No real network is used."""

import socket
from pathlib import Path
from typing import Any

import pytest

from agent.registry import ToolRegistry
from hostplatform import netdiag
from tools.network import (
    DEFAULT_ROUTE,
    DNS_LOOKUP,
    RESOLVER_CONFIG,
    TCP_CHECK,
    register_network_tools,
)


def fake_resolve(table: dict[str, list[str]]) -> Any:
    def resolve(host: str, port: Any, **_kw: Any) -> list[Any]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(0, 0, 0, "", (a, 0)) for a in table[host]]

    return resolve


class FakeSock:
    closed = False

    def getpeername(self) -> tuple[str, int]:
        return ("203.0.113.9", 443)

    def close(self) -> None:
        self.closed = True


def make(resolve: Any = None, connect: Any = None) -> ToolRegistry:
    reg = ToolRegistry()
    register_network_tools(
        reg, resolve or fake_resolve({}), connect or (lambda *a, **k: FakeSock())
    )
    return reg


def test_tools_are_read_only() -> None:
    reg = make()
    assert set(reg.names()) == {DNS_LOOKUP, TCP_CHECK, RESOLVER_CONFIG, DEFAULT_ROUTE}
    assert not any(reg.is_operation(n) for n in reg.names())


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "a-b.example.org",
        "localhost",
        "10.0.0.1",
        "::1",
        "my_host.local.",
    ],
)
def test_valid_hosts(host: str) -> None:
    assert netdiag.validate_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "-x",
        "a b",
        "a;b",
        "$(id)",
        "http://x",
        "a/b",
        "x" * 300,
        "a..b",
        "-oProxyCommand=x",
    ],
)
def test_invalid_hosts(host: str) -> None:
    with pytest.raises(netdiag.HostError):
        netdiag.validate_host(host)


@pytest.mark.parametrize("port", [0, 65536, -1, True, "80", None, 1.5])
def test_invalid_ports(port: Any) -> None:
    with pytest.raises(netdiag.HostError):
        netdiag.validate_port(port)


def test_dns_resolves_and_counts_families() -> None:
    reg = make(
        fake_resolve(
            {"example.com": ["93.184.216.34", "2606:2800::1", "93.184.216.34"]}
        )
    )
    data = reg.call(DNS_LOOKUP, {"host": "example.com"})
    assert data["status"] == "resolved" and data["ipv4"] == 1 and data["ipv6"] == 1
    assert len(data["addresses"]) == 2


def test_dns_failure_is_reported_not_raised() -> None:
    data = make().call(DNS_LOOKUP, {"host": "nope.invalid"})
    assert data["status"] == "failed" and "does not resolve" in data["error"]


def test_dns_temporary_failure_names_resolver_problem() -> None:
    def broken(*_a: Any, **_k: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_AGAIN, "x")

    assert "resolver" in make(broken).call(DNS_LOOKUP, {"host": "a.example"})["error"]


def test_dns_address_list_is_bounded() -> None:
    many = [f"10.0.{i // 250}.{i % 250}" for i in range(100)]
    data = make(fake_resolve({"big.example": many})).call(
        DNS_LOOKUP, {"host": "big.example"}
    )
    assert len(data["addresses"]) == netdiag.MAX_ADDRESSES


def test_hostile_host_never_reaches_resolver() -> None:
    seen: list[str] = []

    def spy(host: str, *_a: Any, **_k: Any) -> list[Any]:
        seen.append(host)
        return []

    assert "error" in make(spy).call(DNS_LOOKUP, {"host": "-x; rm -rf /"})
    assert seen == []


def test_tcp_open_closes_the_socket() -> None:
    sock = FakeSock()
    data = make(connect=lambda *a, **k: sock).call(
        TCP_CHECK, {"host": "example.com", "port": 443}
    )
    assert data["status"] == "open" and data["peer"] == "203.0.113.9" and sock.closed


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ConnectionRefusedError(), "refused"),
        (TimeoutError(), "timeout"),
        (OSError(101, "Network is unreachable"), "unreachable"),
        (socket.gaierror(socket.EAI_NONAME, "x"), "dns_failed"),
    ],
)
def test_tcp_failure_modes(exc: Exception, status: str) -> None:
    def fail(*_a: Any, **_k: Any) -> Any:
        raise exc

    assert (
        make(connect=fail).call(TCP_CHECK, {"host": "example.com", "port": 80})[
            "status"
        ]
        == status
    )


def test_tcp_timeout_is_clamped() -> None:
    seen: dict[str, Any] = {}

    def spy(addr: Any, timeout: float) -> FakeSock:
        seen["t"] = timeout
        return FakeSock()

    reg = make(connect=spy)
    assert "error" in reg.call(
        TCP_CHECK, {"host": "a.example", "port": 80, "timeout_seconds": 999}
    )
    netdiag.tcp_check("a.example", 80, 999, spy)
    assert seen["t"] == netdiag.MAX_TIMEOUT


def test_tcp_rejects_bad_port_via_schema() -> None:
    assert "error" in make().call(TCP_CHECK, {"host": "a.example", "port": 70000})


def test_resolver_config_parsing(tmp_path: Path) -> None:
    f = tmp_path / "resolv.conf"
    f.write_text(
        "# c\nnameserver 127.0.0.53\nnameserver bad;value\nsearch lan example.com\noptions edns0\n"
    )
    data = netdiag.resolver_config(f)
    assert data["nameservers"] == ["127.0.0.53"] and data["local_stub_resolver"] is True
    assert data["search_domains"] == ["lan", "example.com"]


def test_resolver_config_missing(tmp_path: Path) -> None:
    assert netdiag.resolver_config(tmp_path / "none")["status"] == "unreadable"


def test_default_route_parsing(tmp_path: Path) -> None:
    f = tmp_path / "route"
    f.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\n"
        "eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\n"
        "eth0\t0002A8C0\t00000000\t0001\t0\t0\t100\n"
    )
    data = netdiag.default_route(f)
    assert (
        data["has_default_route"]
        and data["default_routes"][0]["gateway"] == "192.168.2.1"
    )


def test_no_default_route(tmp_path: Path) -> None:
    f = tmp_path / "route"
    f.write_text("Iface\tDestination\tGateway\tFlags\n")
    assert netdiag.default_route(f)["has_default_route"] is False


def test_real_host_files_do_not_crash() -> None:
    assert netdiag.resolver_config()["status"] in ("ok", "unreadable")
    assert netdiag.default_route()["status"] in ("ok", "unreadable")
