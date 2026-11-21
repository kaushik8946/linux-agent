"""Read-only host discovery.

Reads os-release, /proc and /run markers, and PATH lookups. It never runs a
probe command, never executes sudo, and never reads environment variables
other than what the standard library needs.

Every source is injectable (``root``, ``which``, ``euid``, ``uname``) so tests
can use fixture trees instead of the real host.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from hostplatform.capabilities import (
    DIAGNOSTIC_UTILITIES,
    PACKAGE_MANAGERS,
    HostProfile,
    OSInfo,
    Privileges,
    ServiceManager,
)

Which = Callable[[str], str | None]
CONTAINER_CGROUP_MARKERS = ("docker", "kubepods", "containerd", "libpod", "lxc")
_OS_RELEASE_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_OS_RELEASE_PATHS = ("etc/os-release", "usr/lib/os-release")


def _current_euid() -> int | None:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid else None


def _current_uname() -> tuple[str, str, str]:
    return platform.system(), platform.release(), platform.machine()


def parse_os_release(text: str) -> dict[str, str]:
    """Parse os-release ``KEY=value`` lines, following its quoting rules."""
    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _OS_RELEASE_KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = re.sub(r"\\([\"'\\$`])", r"\1", value)
        data[key] = value
    return data


def read_os_release(root: Path) -> tuple[dict[str, str] | None, str | None]:
    for rel in _OS_RELEASE_PATHS:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        return parse_os_release(text), f"/{rel}"
    return None, None


def _os_info(root: Path) -> tuple[OSInfo, list[str]]:
    data, source = read_os_release(root)
    if data is None:
        return OSInfo(), ["no readable os-release file; distribution is unknown"]
    warnings: list[str] = []
    distro_id = data.get("ID") or None
    if distro_id is None:
        warnings.append("os-release does not define ID")
    info = OSInfo(
        distro_id=distro_id,
        id_like=tuple(data.get("ID_LIKE", "").split()),
        version_id=data.get("VERSION_ID") or None,
        pretty_name=data.get("PRETTY_NAME") or data.get("NAME") or None,
        source=source,
    )
    return info, warnings


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def detect_service_manager(root: Path, which: Which) -> ServiceManager:
    """Identify the running init system, not merely an installed binary.

    A ``systemctl`` binary can exist on a host where systemd is not PID 1
    (e.g. a container). The systemd runtime directory is the marker for a
    booted systemd.
    """
    if _is_dir(root / "run/systemd/system"):
        return ServiceManager("systemd", "running")
    if which("systemctl"):
        return ServiceManager("systemd", "installed_not_running")
    if _is_dir(root / "run/openrc"):
        return ServiceManager("openrc", "running")
    return ServiceManager(None, "absent")


def detect_containers(root: Path) -> tuple[str, ...] | None:
    """Return container/restriction indicators, ``()`` if none were found, or
    ``None`` when nothing conclusive could be read.

    Marker files prove a container when present, but their absence proves
    nothing. ``/proc/1/cgroup`` is the source whose absence makes the answer
    unknowable, so ``None`` requires both no marker and no readable cgroup.
    """
    indicators: list[str] = []

    for marker in (".dockerenv", "run/.containerenv"):
        if (root / marker).exists():
            indicators.append(f"/{marker} present")

    try:
        container = (root / "run/systemd/container").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        container = ""
    if container.strip():
        indicators.append(f"systemd reports container={container.strip()}")

    cgroup_readable = True
    try:
        cgroup = (root / "proc/1/cgroup").read_text(encoding="utf-8").lower()
    except (OSError, UnicodeDecodeError):
        cgroup_readable = False
    else:
        for marker in CONTAINER_CGROUP_MARKERS:
            if marker in cgroup:
                indicators.append(f"PID 1 cgroup mentions '{marker}'")
                break

    try:
        status = (root / "proc/self/status").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        status = ""
    if re.search(r"^NoNewPrivs:\s*1\b", status, re.MULTILINE):
        indicators.append("NoNewPrivs is set (privilege escalation blocked)")

    if not indicators and not cgroup_readable:
        return None
    return tuple(indicators)


def discover(
    *,
    root: Path = Path("/"),
    which: Which = shutil.which,
    euid: Callable[[], int | None] = _current_euid,
    uname: Callable[[], tuple[str, str, str]] = _current_uname,
) -> HostProfile:
    """Build a :class:`HostProfile`. Never raises for a missing optional source."""
    warnings: list[str] = []

    system, release, machine = uname()
    if system != "Linux":
        warnings.append(f"host reports {system or 'unknown'}, not Linux")

    os_info, os_warnings = _os_info(root)
    warnings += os_warnings

    service = detect_service_manager(root, which)
    if service.name == "systemd" and service.state == "installed_not_running":
        warnings.append(
            "systemctl is installed but systemd is not running; "
            "service checks will not work"
        )

    uid = euid()
    privileges = Privileges(
        is_root=None if uid is None else uid == 0,
        sudo_available=which("sudo") is not None,
        doas_available=which("doas") is not None,
    )
    if uid is None:
        warnings.append("could not determine the effective user ID")
    elif uid != 0 and not (privileges.sudo_available or privileges.doas_available):
        warnings.append("no sudo or doas found; privileged steps will be unavailable")

    containers = detect_containers(root)
    if containers is None:
        warnings.append("container detection incomplete: no source could be read")

    package_managers = tuple(name for name in PACKAGE_MANAGERS if which(name))
    utilities = tuple(name for name in DIAGNOSTIC_UTILITIES if which(name))

    return HostProfile(
        os=os_info,
        kernel=release or None,
        architecture=machine or None,
        python_version=".".join(str(p) for p in sys.version_info[:3]),
        package_managers=package_managers,
        service_manager=service,
        privileges=privileges,
        container_indicators=containers,
        available_utilities=utilities,
        warnings=tuple(warnings),
    )
