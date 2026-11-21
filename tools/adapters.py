"""Choose and register the adapters this host can actually use.

An adapter is enabled only when discovery shows its manager is present
(and, for services, running). Distribution metadata alone is not enough.
"""

from __future__ import annotations

import shutil

from agent.registry import ToolRegistry
from hostplatform.capabilities import HostProfile
from hostplatform.packages.apt import AptAdapter
from hostplatform.services.systemd import SystemdAdapter
from tools.packages import register_package_tools, register_service_tools


def register_host_adapters(registry: ToolRegistry, host: HostProfile) -> list[str]:
    enabled: list[str] = []
    is_root = bool(host.privileges.is_root)
    if "apt-get" in host.package_managers:
        apt = AptAdapter(shutil.which, is_root)
        if apt.available():
            register_package_tools(registry, apt)
            enabled.append("apt")
    sm = host.service_manager
    if sm.name == "systemd" and sm.state == "running":
        systemd = SystemdAdapter(shutil.which, is_root, running=True)
        if systemd.available():
            register_service_tools(registry, systemd)
            enabled.append("systemd")
    return enabled
