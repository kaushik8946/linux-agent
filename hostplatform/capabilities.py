"""Typed host profile and its plain-text rendering.

An ``Optional`` field set to ``None`` means the value is unknown. Discovery
never guesses: a missing or unreadable source is reported as unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

# Diagnostic utilities the agent may propose. Missing ones are listed, not fatal.
DIAGNOSTIC_UTILITIES: tuple[str, ...] = (
    "free",
    "df",
    "ps",
    "uptime",
    "lscpu",
    "lsblk",
    "ip",
    "ss",
    "lsof",
    "dig",
    "journalctl",
    "systemctl",
    "docker",
    "git",
    "curl",
)

PACKAGE_MANAGERS: tuple[str, ...] = ("apt-get", "dnf", "yum", "pacman", "zypper", "apk")


@dataclass(frozen=True)
class OSInfo:
    """Distribution metadata from os-release. Context only, not proof of
    which package or service manager is usable."""

    distro_id: str | None = None
    id_like: tuple[str, ...] = ()
    version_id: str | None = None
    pretty_name: str | None = None
    source: str | None = None  # file the metadata came from; None if none read


@dataclass(frozen=True)
class ServiceManager:
    # "systemd", "openrc", or None when no known manager is identified.
    name: str | None
    # "running", "installed_not_running", "absent", or "unknown".
    state: str


@dataclass(frozen=True)
class Privileges:
    is_root: bool | None  # None if the effective UID could not be read
    sudo_available: bool
    doas_available: bool


@dataclass(frozen=True)
class HostProfile:
    os: OSInfo
    kernel: str | None
    architecture: str | None
    python_version: str
    package_managers: tuple[str, ...]
    service_manager: ServiceManager
    privileges: Privileges
    # None means no container source could be read; () means none found.
    container_indicators: tuple[str, ...] | None
    available_utilities: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def missing_utilities(self) -> tuple[str, ...]:
        return tuple(
            u for u in DIAGNOSTIC_UTILITIES if u not in self.available_utilities
        )


def _value(value: object) -> str:
    return "unknown" if value is None or value == "" else str(value)


def render_profile(profile: HostProfile) -> str:
    os_info = profile.os
    if os_info.pretty_name:
        os_line = os_info.pretty_name
    elif os_info.distro_id:
        os_line = os_info.distro_id
    else:
        os_line = None
    os_line = _value(os_line)
    if os_info.source:
        os_line += f" (from {os_info.source})"

    priv = profile.privileges
    root = "unknown" if priv.is_root is None else ("yes" if priv.is_root else "no")
    sm = profile.service_manager
    sm_name = _value(sm.name)

    if profile.container_indicators is None:
        containers = "unknown (no source could be read)"
    elif profile.container_indicators:
        containers = ", ".join(profile.container_indicators)
    else:
        containers = "none detected"

    lines = [
        "Host",
        f"  OS:            {os_line}",
        f"  ID-like:       {', '.join(os_info.id_like) or 'unknown'}",
        f"  Version:       {_value(os_info.version_id)}",
        f"  Kernel:        {_value(profile.kernel)}",
        f"  Architecture:  {_value(profile.architecture)}",
        f"  Python:        {profile.python_version}",
        "",
        "Package managers (found on PATH; usability not verified)",
        f"  {', '.join(profile.package_managers) or 'none found'}",
        "",
        "Service manager",
        f"  {sm_name}: {sm.state}",
        "",
        "Privileges",
        "  root: {}   sudo: {}   doas: {}".format(
            root,
            "yes" if priv.sudo_available else "no",
            "yes" if priv.doas_available else "no",
        ),
        "",
        "Containers / restricted environments",
        f"  {containers}",
        "",
        "Diagnostic utilities",
        f"  available: {', '.join(profile.available_utilities) or 'none'}",
        f"  missing:   {', '.join(profile.missing_utilities) or 'none'}",
    ]
    if profile.warnings:
        lines += ["", "Warnings"] + [f"  - {w}" for w in profile.warnings]
    return "\n".join(lines)
