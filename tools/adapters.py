"""Choose and register the adapters this host can actually use.

An adapter is enabled only when discovery shows its manager is present
(and, for services, running). Distribution metadata alone is not enough.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agent.registry import ToolRegistry
from hostplatform.capabilities import HostProfile
from hostplatform.docker import DockerClient
from hostplatform.envstore import EnvStore
from hostplatform.identity import current
from hostplatform.packages.apt import AptAdapter
from hostplatform.packages.base import PackageAdapter
from hostplatform.packages.dnf import DnfAdapter
from hostplatform.services.systemd import SystemdAdapter
from tools.devtools import register_dev_tools
from tools.docker import register_docker_tools
from tools.docker_ops import register_docker_operations
from tools.envvars import register_env_tools
from tools.network import register_network_tools
from tools.packages import register_package_tools, register_service_tools
from tools.provisioning import register_provisioning_tools
from tools.research import register_research_tools
from tools.workspace import register_workspace_tools


def register_host_adapters(
    registry: ToolRegistry,
    host: HostProfile,
    home: Path | None = None,
    workspace_root: Path | None = None,
) -> list[str]:
    enabled: list[str] = []
    register_env_tools(registry, EnvStore(home or current().home), shutil.which("sh"))
    enabled.append("env")
    register_network_tools(registry)
    enabled.append("network")
    if workspace_root is not None:
        register_workspace_tools(registry, workspace_root)
        enabled.append("workspace")
    is_root = bool(host.privileges.is_root)
    docker = DockerClient(shutil.which)
    if docker.available():
        register_docker_tools(registry, docker)
        register_docker_operations(registry, docker)
        enabled.append("docker")
    packages: PackageAdapter | None = None
    if "apt-get" in host.package_managers:
        apt = AptAdapter(shutil.which, is_root)
        if apt.available():
            packages = apt
            register_package_tools(registry, apt)
            register_dev_tools(registry, apt)  # package names are apt-specific
            enabled.append("apt")
    elif "dnf" in host.package_managers:
        dnf = DnfAdapter(shutil.which, is_root)
        if dnf.available():
            packages = dnf
            register_package_tools(registry, dnf)
            enabled.append("dnf")
    register_research_tools(registry, packages)
    enabled.append("research")
    sm = host.service_manager
    if sm.name == "systemd" and sm.state == "running":
        systemd = SystemdAdapter(shutil.which, is_root, running=True)
        if systemd.available():
            register_service_tools(registry, systemd)
            enabled.append("systemd")
    register_provisioning_tools(registry)
    enabled.append("provisioning")
    return enabled
