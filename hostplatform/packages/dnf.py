"""DNF adapter (Fedora, RHEL and derivatives) - Build 12, module 8.

Same contract as the apt adapter. Queries use ``rpm -q`` and
``dnf repoquery`` (no root). The plan comes from ``dnf install --assumeno``,
whose transaction summary is parsed conservatively: if the summary cannot be
understood, or it would remove or downgrade anything, the plan is refused.
Install runs ``sudo -- /usr/bin/dnf install -y <name>`` exactly.

Unlike apt there is no ``--no-remove`` switch, so the no-removal guarantee
comes from the simulated plan and the approval recheck, not from dnf itself.
Names are validated (lowercase, cannot start with ``-``), so ``dnf`` receives
no option-like argument.

This adapter has been tested against scripted fixtures only. Verify it on a
disposable Fedora VM before relying on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from hostplatform.packages.apt import _first_line
from hostplatform.packages.base import (
    InstallPlan,
    InstallResult,
    InstallStatus,
    PackageAdapter,
    PackageState,
    PackageStatus,
    validate_package_name,
)
from hostplatform.privileged import Run, as_root, is_permission_error, run_fixed

QUERY_TIMEOUT = 60.0
INSTALL_TIMEOUT = 900.0
DNF_ENV = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
_LOCK_MARKERS = (
    "waiting for process with pid",
    "another app is currently holding the",
    "cannot acquire lock",
    "could not acquire lock",
)
_NOT_FOUND = ("no match for argument", "unable to find a match", "no matching packages")
_SECTION = re.compile(r"^([A-Za-z][A-Za-z ]+):\s*$")
_ROW = re.compile(r"^ (\S+)")
_REFUSED_SECTIONS = ("removing", "downgrading", "replacing", "removing dependent packages")  # fmt: skip
_ADDED_SECTIONS = ("installing", "installing dependencies", "installing weak dependencies")  # fmt: skip


def parse_transaction(text: str) -> tuple[list[str], list[str]]:
    """Return (packages to install, section names that must be refused)."""
    section: str | None = None
    added: list[str] = []
    refused: list[str] = []
    for line in text.splitlines():
        if line.startswith("Transaction Summary"):
            break
        m = _SECTION.match(line)
        if m:
            section = m.group(1).strip().lower()
            if section in _REFUSED_SECTIONS and section not in refused:
                refused.append(section)
            continue
        row = _ROW.match(line)
        if row and section in _ADDED_SECTIONS:
            added.append(row.group(1))
    return added, refused


class DnfAdapter(PackageAdapter):
    name = "dnf"

    def __init__(
        self,
        which: Callable[[str], str | None],
        is_root: bool,
        run: Run = run_fixed,
    ) -> None:
        self._run = run
        self._is_root = is_root
        self.rpm = which("rpm")
        self.dnf = which("dnf")
        self.sudo = which("sudo")

    def available(self) -> bool:
        return bool(self.rpm and self.dnf)

    def remove_hint(self, package: str) -> str:
        return f"sudo dnf remove {validate_package_name(package)}"

    def query(self, package: str) -> PackageState:
        name = validate_package_name(package)
        if not self.available():
            return PackageState(
                name, PackageStatus.UNKNOWN, detail="dnf is not available"
            )
        assert self.rpm and self.dnf
        installed = self._run(
            [self.rpm, "-q", "--qf", "%{VERSION}-%{RELEASE}", "--", name],
            QUERY_TIMEOUT,
            DNF_ENV,
        )
        version = installed.stdout.strip() if installed.ok else None
        repo = self._run(
            [self.dnf, "--quiet", "repoquery", "--latest-limit", "1",
             "--qf", "%{version}-%{release}", name],
            QUERY_TIMEOUT,
            DNF_ENV,
        )  # fmt: skip
        candidate = None
        if repo.ok:
            lines = [ln.strip() for ln in repo.stdout.splitlines() if ln.strip()]
            candidate = lines[-1] if lines else None
        if version:
            return PackageState(name, PackageStatus.INSTALLED, version, candidate)
        if candidate:
            return PackageState(name, PackageStatus.NOT_INSTALLED, None, candidate)
        if repo.ok and installed.exit_code == 1:
            return PackageState(
                name, PackageStatus.UNAVAILABLE, detail="no installation candidate"
            )
        return PackageState(
            name, PackageStatus.UNKNOWN, detail="package state could not be read"
        )

    def plan_install(self, package: str) -> InstallPlan:
        name = validate_package_name(package)
        if not self.available():
            return InstallPlan(name, False, "dnf is not available on this host")
        state = self.query(name)
        if state.status is PackageStatus.INSTALLED:
            return InstallPlan(
                name, True, f"already installed ({state.installed_version})",
                already_installed=True, candidate_version=state.candidate_version,
            )  # fmt: skip
        if state.status is not PackageStatus.NOT_INSTALLED:
            return InstallPlan(name, False, state.detail or "package is not available")
        assert self.dnf
        sim = self._run(
            [self.dnf, "install", "--assumeno", name], QUERY_TIMEOUT, DNF_ENV
        )
        text = sim.stdout + "\n" + sim.stderr
        added, refused = parse_transaction(text)
        if refused:
            return InstallPlan(
                name,
                False,
                f"the plan would change other packages ({refused[0]}); refused",
            )
        if not added or name not in added:
            return InstallPlan(
                name, False, "dnf's plan could not be understood; refused to be safe"
            )
        argv = as_root([self.dnf, "install", "-y", name], self.sudo, self._is_root)
        if argv is None:
            return InstallPlan(
                name, False, "installing needs root and sudo is not available"
            )
        return InstallPlan(
            name, True, f"will install {len(added)} package(s)", tuple(argv),
            needs_privilege=not self._is_root, new_packages=tuple(added),
            candidate_version=state.candidate_version,
        )  # fmt: skip

    def install(self, plan: InstallPlan) -> InstallResult:
        name = validate_package_name(plan.package)
        before = self.query(name)
        if before.status is PackageStatus.INSTALLED:
            return InstallResult(
                name, InstallStatus.ALREADY_INSTALLED,
                f"{name} is already installed ({before.installed_version}); nothing changed",
                before, before,
            )  # fmt: skip
        if not plan.feasible or not plan.argv:
            return InstallResult(name, InstallStatus.NOT_ATTEMPTED, plan.reason, before)
        if before.status is PackageStatus.UNAVAILABLE:
            return InstallResult(
                name, InstallStatus.UNAVAILABLE, "package is not available", before
            )
        expected_tail = ("install", "-y", name)
        if tuple(plan.argv[-len(expected_tail) :]) != expected_tail:
            return InstallResult(
                name, InstallStatus.NOT_ATTEMPTED,
                "install command does not match the plan", before,
            )  # fmt: skip
        result = self._run(plan.argv, INSTALL_TIMEOUT, DNF_ENV)
        after = self.query(name)
        stderr = result.stderr.strip()[-600:]
        if result.ok and after.status is PackageStatus.INSTALLED:
            return InstallResult(
                name, InstallStatus.INSTALLED,
                f"{name} {after.installed_version} installed", before, after, stderr,
            )  # fmt: skip
        lowered = (result.stderr + result.stdout).lower()
        if any(m in lowered for m in _LOCK_MARKERS):
            status, message = (
                InstallStatus.LOCKED,
                "another package manager is running; try again later",
            )
        elif is_permission_error(result) or "superuser privileges" in lowered:
            status, message = (
                InstallStatus.PERMISSION_DENIED,
                "permission denied (sudo was refused or not authorised)",
            )
        elif any(m in lowered for m in _NOT_FOUND):
            status, message = InstallStatus.UNAVAILABLE, "package is not available"
        elif result.timed_out:
            status, message = (
                InstallStatus.FAILED,
                "install timed out; the package state may be incomplete",
            )
        elif result.ok:
            status, message = (
                InstallStatus.FAILED,
                "dnf reported success but the package is not installed",
            )
        else:
            status, message = (
                InstallStatus.FAILED,
                f"install failed (exit {result.exit_code}): {_first_line(result.stderr)}",
            )
        return InstallResult(name, status, message, before, after, stderr)
