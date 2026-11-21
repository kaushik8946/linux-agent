"""APT/dpkg adapter (Debian, Ubuntu and derivatives).

Queries use ``dpkg-query`` and ``apt-cache`` (no root needed). The install
plan is computed with ``apt-get --simulate`` so the user sees every package
that would be added before approving. Install runs
``sudo -- /usr/bin/apt-get install -y --no-remove -- <name>`` exactly; the
``--no-remove`` flag makes apt refuse a plan that would remove anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from hostplatform.packages.base import (
    InstallPlan,
    InstallResult,
    InstallStatus,
    PackageAdapter,
    PackageState,
    PackageStatus,
    validate_package_name,
)
from hostplatform.privileged import (
    ProcResult,
    Run,
    as_root,
    is_permission_error,
    run_fixed,
)

QUERY_TIMEOUT = 30.0
INSTALL_TIMEOUT = 900.0
APT_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "LC_ALL": "C",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
}
_LOCK_MARKERS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "is another process using it",
)
_SIM_INST = re.compile(r"^Inst (\S+)(?: \[[^\]]*\])? \(([^ )]+)", re.MULTILINE)
_SIM_REMOVE = re.compile(r"^Remv (\S+)", re.MULTILINE)


class AptAdapter(PackageAdapter):
    name = "apt"

    def __init__(
        self,
        which: Callable[[str], str | None],
        is_root: bool,
        run: Run = run_fixed,
    ) -> None:
        self._which = which
        self._run = run
        self._is_root = is_root
        self.dpkg_query = which("dpkg-query")
        self.apt_cache = which("apt-cache")
        self.apt_get = which("apt-get")
        self.sudo = which("sudo")

    def available(self) -> bool:
        return bool(self.dpkg_query and self.apt_cache and self.apt_get)

    # -- queries -------------------------------------------------------------

    def query(self, package: str) -> PackageState:
        name = validate_package_name(package)
        if not self.available():
            return PackageState(
                name, PackageStatus.UNKNOWN, detail="apt is not available"
            )
        assert self.dpkg_query and self.apt_cache
        installed = self._run(
            [self.dpkg_query, "-W", "-f=${Status}\\t${Version}", "--", name],
            QUERY_TIMEOUT,
            APT_ENV,
        )
        version = None
        if installed.ok:
            status_field, _, ver = installed.stdout.partition("\t")
            if status_field.strip().endswith(" installed"):
                version = ver.strip() or None
        policy = self._run(
            [self.apt_cache, "policy", "--", name], QUERY_TIMEOUT, APT_ENV
        )
        candidate = _policy_field(policy.stdout, "Candidate")
        if version:
            return PackageState(name, PackageStatus.INSTALLED, version, candidate)
        if candidate and candidate != "(none)":
            return PackageState(name, PackageStatus.NOT_INSTALLED, None, candidate)
        if policy.ok or installed.exit_code == 1:
            return PackageState(
                name, PackageStatus.UNAVAILABLE, detail="no installation candidate"
            )
        return PackageState(
            name, PackageStatus.UNKNOWN, detail="package state could not be read"
        )

    def plan_install(self, package: str) -> InstallPlan:
        name = validate_package_name(package)
        if not self.available():
            return InstallPlan(name, False, "apt is not available on this host")
        state = self.query(name)
        if state.status is PackageStatus.INSTALLED:
            return InstallPlan(
                name, True, f"already installed ({state.installed_version})",
                already_installed=True, candidate_version=state.candidate_version,
            )  # fmt: skip
        if state.status is not PackageStatus.NOT_INSTALLED:
            return InstallPlan(name, False, state.detail or "package is not available")
        assert self.apt_get
        sim = self._run(
            [self.apt_get, "install", "--simulate", "--no-remove", "-y", "--", name],
            QUERY_TIMEOUT,
            APT_ENV,
        )
        if not sim.ok:
            return InstallPlan(
                name,
                False,
                "apt could not plan this install: " + _first_line(sim.stderr),
            )
        if _SIM_REMOVE.search(sim.stdout):
            return InstallPlan(
                name, False, "installing would remove other packages; refused"
            )
        new = tuple(m.group(1) for m in _SIM_INST.finditer(sim.stdout))
        argv = as_root(
            [self.apt_get, "install", "-y", "--no-remove", "--", name],
            self.sudo,
            self._is_root,
        )
        if argv is None:
            return InstallPlan(
                name, False, "installing needs root and sudo is not available"
            )
        return InstallPlan(
            name, True, f"will install {len(new)} package(s)", tuple(argv),
            needs_privilege=not self._is_root, new_packages=new,
            candidate_version=state.candidate_version,
        )  # fmt: skip

    # -- install -------------------------------------------------------------

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
        expected_tail = ("install", "-y", "--no-remove", "--", name)
        if tuple(plan.argv[-len(expected_tail) :]) != expected_tail:
            return InstallResult(
                name,
                InstallStatus.NOT_ATTEMPTED,
                "install command does not match the plan",
                before,
            )

        result = self._run(plan.argv, INSTALL_TIMEOUT, APT_ENV)
        after = self.query(name)
        return _install_outcome(name, result, before, after)


def _install_outcome(
    name: str, result: ProcResult, before: PackageState, after: PackageState
) -> InstallResult:
    stderr = result.stderr.strip()[-600:]
    if result.ok and after.status is PackageStatus.INSTALLED:
        return InstallResult(
            name, InstallStatus.INSTALLED, f"{name} {after.installed_version} installed",
            before, after, stderr,
        )  # fmt: skip
    lowered = (result.stderr + result.stdout).lower()
    if any(m in lowered for m in _LOCK_MARKERS):
        status, message = (
            InstallStatus.LOCKED,
            "another package manager is running; try again later",
        )
    elif is_permission_error(result):
        status, message = (
            InstallStatus.PERMISSION_DENIED,
            "permission denied (sudo was refused or not authorised)",
        )
    elif (
        "unable to locate package" in lowered
        or "has no installation candidate" in lowered
    ):
        status, message = InstallStatus.UNAVAILABLE, "package is not available"
    elif result.timed_out:
        status, message = (
            InstallStatus.FAILED,
            "install timed out; the package state may be incomplete",
        )
    elif result.ok:
        status, message = (
            InstallStatus.FAILED,
            "apt reported success but the package is not installed",
        )
    else:
        status, message = (
            InstallStatus.FAILED,
            f"install failed (exit {result.exit_code})",
        )
    return InstallResult(name, status, message, before, after, stderr)


def _policy_field(text: str, field_name: str) -> str | None:
    match = re.search(rf"^\s*{field_name}:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def _first_line(text: str) -> str:
    return (text.strip().splitlines() or ["(no output)"])[-1][:200]
