"""Network and DNS diagnostics (Build 12, module 5).

Read-only. A TCP check only opens and closes a connection (no bytes are
sent); a DNS check only asks the system resolver. Hostnames are validated,
timeouts and result counts are bounded, and resolver configuration is
returned as nameserver addresses only.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOST_RE = re.compile(r"(?=.{1,253}$)([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*\.?")  # fmt: skip
MAX_ADDRESSES = 20
MAX_TIMEOUT = 10.0


class HostError(ValueError):
    pass


def validate_host(host: object) -> str:
    if not isinstance(host, str) or not host:
        raise HostError("host must be a hostname or IP address")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if host.startswith("-") or not HOST_RE.fullmatch(host):
        raise HostError("host is not a valid hostname or IP address")
    return host


def validate_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise HostError("port must be 1-65535")
    return port


Resolver = Callable[..., list[Any]]
Connector = Callable[..., Any]


def dns_lookup(host: str, resolve: Resolver = socket.getaddrinfo) -> dict[str, Any]:
    name = validate_host(host)
    start = time.monotonic()
    try:
        infos = resolve(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {
            "host": name,
            "status": "failed",
            "error": _gai_text(exc),
            "seconds": round(time.monotonic() - start, 3),
        }
    except OSError as exc:
        return {"host": name, "status": "failed", "error": type(exc).__name__}
    addresses = sorted({info[4][0] for info in infos})
    return {
        "host": name,
        "status": "resolved",
        "addresses": addresses[:MAX_ADDRESSES],
        "ipv4": sum(1 for a in addresses if ":" not in a),
        "ipv6": sum(1 for a in addresses if ":" in a),
        "seconds": round(time.monotonic() - start, 3),
    }


def _gai_text(exc: socket.gaierror) -> str:
    code = exc.args[0] if exc.args else None
    if code == socket.EAI_NONAME:
        return "name does not resolve"
    if code == socket.EAI_AGAIN:
        return "temporary resolver failure (no DNS server reachable?)"
    return "resolution failed"


def tcp_check(
    host: str,
    port: int,
    timeout: float = 5.0,
    connect: Connector = socket.create_connection,
) -> dict[str, Any]:
    name = validate_host(host)
    port = validate_port(port)
    timeout = max(0.5, min(float(timeout), MAX_TIMEOUT))
    start = time.monotonic()
    try:
        sock = connect((name, port), timeout=timeout)
    except socket.gaierror as exc:
        return {
            "host": name,
            "port": port,
            "status": "dns_failed",
            "error": _gai_text(exc),
        }
    except TimeoutError:
        return {
            "host": name,
            "port": port,
            "status": "timeout",
            "timeout_seconds": timeout,
        }
    except ConnectionRefusedError:
        return {"host": name, "port": port, "status": "refused"}
    except OSError as exc:
        return {"host": name, "port": port, "status": "unreachable", "error": exc.strerror or type(exc).__name__}  # fmt: skip
    try:
        peer = sock.getpeername()[0]
    finally:
        sock.close()
    return {
        "host": name,
        "port": port,
        "status": "open",
        "peer": peer,
        "seconds": round(time.monotonic() - start, 3),
    }


def resolver_config(path: Path = Path("/etc/resolv.conf")) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"status": "unreadable"}
    servers: list[str] = []
    search: list[str] = []
    for line in text.splitlines()[:50]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                servers.append(str(ipaddress.ip_address(parts[1].split("%")[0])))
            except ValueError:
                continue
        elif parts and parts[0] in ("search", "domain"):
            search = [p for p in parts[1:7] if HOST_RE.fullmatch(p)]
    stub = servers == ["127.0.0.53"]
    return {
        "status": "ok",
        "nameservers": servers[:6],
        "search_domains": search,
        "local_stub_resolver": stub,
    }


def default_route(path: Path = Path("/proc/net/route")) -> dict[str, Any]:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return {"status": "unreadable"}
    routes: list[dict[str, Any]] = []
    for row in rows[:200]:
        f = row.split()
        if len(f) >= 4 and f[1] == "00000000" and int(f[3], 16) & 2:
            gw = socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
            routes.append({"interface": f[0], "gateway": gw, "metric": int(f[6]) if len(f) > 6 else None})  # fmt: skip
    return {
        "status": "ok",
        "has_default_route": bool(routes),
        "default_routes": routes[:5],
    }
