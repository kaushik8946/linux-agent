"""Package-manager adapter contract.

An adapter answers three questions with fixed, validated commands:
what is the state of a package, what would installing it do, and did the
install achieve the expected state. Removal is deliberately absent.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

# Debian policy: lowercase alnum start, then alnum and + - . ; at least 2 chars.
# Other managers are similar; adapters may narrow this further.
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]{1,99}")


class PackageNameError(ValueError):
    pass


def validate_package_name(name: str) -> str:
    if not isinstance(name, str) or not PACKAGE_NAME.fullmatch(name):
        raise PackageNameError(
            "package name must be 2-100 characters: lowercase letters, digits, + - ."
        )
    return name


class PackageStatus(str, Enum):
    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"  # known to the manager, not installed
    UNAVAILABLE = "unavailable"  # the manager has no such package
    UNKNOWN = "unknown"  # could not determine


@dataclass(frozen=True)
class PackageState:
    name: str
    status: PackageStatus
    installed_version: str | None = None
    candidate_version: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class InstallPlan:
    package: str
    feasible: bool
    reason: str
    argv: tuple[str, ...] = ()  # exact command that would run (display + exec)
    needs_privilege: bool = True
    new_packages: tuple[str, ...] = ()  # everything that would be installed
    candidate_version: str | None = None
    already_installed: bool = False


class InstallStatus(str, Enum):
    INSTALLED = "installed"
    ALREADY_INSTALLED = "already_installed"
    FAILED = "failed"
    PERMISSION_DENIED = "permission_denied"
    LOCKED = "locked"  # another package manager holds the lock
    UNAVAILABLE = "unavailable"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class InstallResult:
    package: str
    status: InstallStatus
    message: str
    before: PackageState | None = None
    after: PackageState | None = None
    stderr_excerpt: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (InstallStatus.INSTALLED, InstallStatus.ALREADY_INSTALLED)


class PackageAdapter(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool:
        """True if this manager's executables were found on this host."""

    @abstractmethod
    def remove_hint(self, package: str) -> str:
        """The command the *user* could run to undo an install. Never run by us."""

    @abstractmethod
    def query(self, package: str) -> PackageState: ...

    @abstractmethod
    def plan_install(self, package: str) -> InstallPlan: ...

    @abstractmethod
    def install(self, plan: InstallPlan) -> InstallResult:
        """Run ``plan.argv`` exactly. Callers must have local approval."""
