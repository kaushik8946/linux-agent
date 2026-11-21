"""Service-manager adapter contract. Stopping, disabling and masking are
deliberately absent."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

# systemd unit names: letters, digits, : _ . @ - ; optional .service suffix.
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,127}")


class ServiceNameError(ValueError):
    pass


def validate_service_name(name: str) -> str:
    if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name) or ".." in name:
        raise ServiceNameError("service name must be letters, digits and : _ . @ -")
    if "." in name and not name.endswith(".service"):
        raise ServiceNameError("only .service units are supported")
    return name if name.endswith(".service") else f"{name}.service"


class ServiceStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"
    ACTIVATING = "activating"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ServiceState:
    unit: str
    status: ServiceStatus
    sub_state: str | None = None
    enabled: str | None = None
    main_pid: int | None = None
    detail: str | None = None


class StartStatus(str, Enum):
    STARTED = "started"
    ALREADY_ACTIVE = "already_active"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class StartResult:
    unit: str
    status: StartStatus
    message: str
    before: ServiceState | None = None
    after: ServiceState | None = None
    stderr_excerpt: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (StartStatus.STARTED, StartStatus.ALREADY_ACTIVE)


class ServiceAdapter(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def status(self, service: str) -> ServiceState: ...

    @abstractmethod
    def start_argv(self, service: str) -> list[str] | None:
        """Exact command that would start the service, or None if impossible."""

    @abstractmethod
    def start(self, service: str) -> StartResult:
        """Start the service. Callers must have local approval."""
