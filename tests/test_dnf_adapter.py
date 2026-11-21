"""DNF adapter and the shared package-adapter contract (Build 12, module 8).

Every contract test runs against both adapters, so a new adapter must meet
the same behaviour. dnf is exercised through a scripted fake only."""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from agent.registry import ToolRegistry
from agent.verifier import Outcome, verify
from hostplatform.packages.base import (
    InstallStatus,
    PackageAdapter,
    PackageNameError,
    PackageStatus,
)
from hostplatform.packages.dnf import DnfAdapter, parse_transaction
from hostplatform.privileged import ProcResult
from tests.test_adapters import FakeHost, apt
from tools.packages import INSTALL_PACKAGE, QUERY_PACKAGE, register_package_tools

BIN = {"rpm": "/usr/bin/rpm", "dnf": "/usr/bin/dnf", "sudo": "/usr/bin/sudo"}

SUMMARY = """Dependencies resolved.
Installing:
 cowsay    noarch  3.8-1  fedora  40 k
Installing dependencies:
 perl-Foo  noarch  1.0-1  fedora  10 k

Transaction Summary
Install  2 Packages
"""


class FakeDnf:
    def __init__(self) -> None:
        self.installed: dict[str, str] = {}
        self.available: dict[str, str] = {}
        self.plan_text: str | None = None
        self.install_error: str | None = None
        self.install_noop = False
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None
    ) -> ProcResult:
        argv = tuple(argv)
        self.calls.append(argv)
        cmd = argv[2:] if argv[0] == BIN["sudo"] else argv
        exe, rest = cmd[0], list(cmd[1:])
        name = rest[-1]
        if exe == BIN["rpm"]:
            if name in self.installed:
                return ProcResult(argv, 0, self.installed[name], "")
            return ProcResult(argv, 1, f"package {name} is not installed", "")
        if exe == BIN["dnf"] and "repoquery" in rest:
            cand = self.available.get(name) or self.installed.get(name)
            return ProcResult(argv, 0, (cand or "") + ("\n" if cand else ""), "")
        if exe == BIN["dnf"] and "--assumeno" in rest:
            text = self.plan_text or SUMMARY.replace("cowsay", name)
            return ProcResult(argv, 1, text, "Operation aborted.")
        if exe == BIN["dnf"] and rest[0] == "install":
            if argv[0] != BIN["sudo"]:
                return ProcResult(argv, 1, "", "This command has to be run with superuser privileges")  # fmt: skip
            if self.install_error:
                return ProcResult(argv, 1, "", self.install_error)
            if not self.install_noop:
                self.installed[name] = self.available.pop(name)
            return ProcResult(argv, 0, "Complete!", "")
        raise AssertionError(f"unexpected {argv}")


def dnf(fake: FakeDnf, root: bool = False, missing: tuple[str, ...] = ()) -> DnfAdapter:
    return DnfAdapter(lambda n: None if n in missing else BIN.get(n), root, run=fake)


# -- contract: both adapters ------------------------------------------------------


class Case:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.host: Any = FakeHost() if kind == "apt" else FakeDnf()

    @property
    def adapter(self) -> PackageAdapter:
        return apt(self.host) if self.kind == "apt" else dnf(self.host)

    def offer(self, name: str, version: str) -> None:
        self.host.available[name] = version

    def install_ok(self, name: str) -> bool:
        return name in self.host.installed


@pytest.fixture(params=["apt", "dnf"])
def case(request: pytest.FixtureRequest) -> Case:
    return Case(request.param)


def test_contract_available_flag() -> None:
    assert apt(FakeHost()).available() and dnf(FakeDnf()).available()
    assert not apt(FakeHost(), "apt-get").available()
    assert not dnf(FakeDnf(), missing=("dnf",)).available()


def test_contract_rejects_hostile_names(case: Case) -> None:
    for bad in ("-y", "--force", "a b", "x;y", "$(id)", "", "A"):
        with pytest.raises(PackageNameError):
            case.adapter.query(bad)
        with pytest.raises(PackageNameError):
            case.adapter.plan_install(bad)


def test_contract_query_states(case: Case) -> None:
    case.host.installed["have"] = "1.0"
    case.offer("want", "2.0")
    a = case.adapter
    assert a.query("have").status is PackageStatus.INSTALLED
    assert a.query("have").installed_version == "1.0"
    st = a.query("want")
    assert st.status is PackageStatus.NOT_INSTALLED and st.candidate_version == "2.0"
    assert a.query("ghost").status is PackageStatus.UNAVAILABLE


def test_contract_plan_install(case: Case) -> None:
    case.offer("cowsay", "3.8-1")
    plan = case.adapter.plan_install("cowsay")
    assert plan.feasible and plan.needs_privilege
    assert plan.argv[0] == "/usr/bin/sudo" and plan.argv[1] == "--"
    assert plan.argv[-1] == "cowsay" and "cowsay" in plan.new_packages
    assert not case.install_ok("cowsay")  # planning changes nothing


def test_contract_plan_for_installed_and_unavailable(case: Case) -> None:
    case.host.installed["have"] = "1.0"
    assert case.adapter.plan_install("have").already_installed
    plan = case.adapter.plan_install("ghost")
    assert not plan.feasible and plan.reason


def test_contract_install_and_verify(case: Case) -> None:
    case.offer("cowsay", "3.8-1")
    a = case.adapter
    result = a.install(a.plan_install("cowsay"))
    assert result.ok and result.status is InstallStatus.INSTALLED
    assert case.install_ok("cowsay")


def test_contract_install_that_does_nothing_is_a_failure(case: Case) -> None:
    case.offer("cowsay", "3.8-1")
    case.host.install_noop = True
    a = case.adapter
    result = a.install(a.plan_install("cowsay"))
    assert result.status is InstallStatus.FAILED and not result.ok


def test_contract_install_rejects_tampered_plan(case: Case) -> None:
    case.offer("cowsay", "3.8-1")
    a = case.adapter
    plan = a.plan_install("cowsay")
    bad = type(plan)(**{**plan.__dict__, "argv": (*plan.argv[:-1], "other")})
    assert a.install(bad).status is InstallStatus.NOT_ATTEMPTED
    assert not case.install_ok("cowsay")


def test_contract_remove_hint_is_text_only(case: Case) -> None:
    hint = case.adapter.remove_hint("cowsay")
    assert "remove" in hint and hint.endswith("cowsay")
    assert not any("remove" in " ".join(c) for c in case.host.calls)


def test_contract_tools_and_operation_workflow(case: Case) -> None:
    case.offer("cowsay", "3.8-1")
    reg = ToolRegistry()
    register_package_tools(reg, case.adapter)
    assert set(reg.names()) == {QUERY_PACKAGE, INSTALL_PACKAGE}
    assert reg.call(QUERY_PACKAGE, {"package": "cowsay"})["status"] == "not_installed"
    op = reg.prepare(INSTALL_PACKAGE, {"package": "cowsay"})
    assert not isinstance(op, dict) and op.requirement.value == "confirm"
    assert case.adapter.name in op.recovery or "remove" in op.recovery
    assert verify(op).outcome is Outcome.FAILED  # nothing installed yet
    assert op.perform()["ok"]
    assert verify(op).outcome is Outcome.PASSED


# -- dnf specifics ------------------------------------------------------------------


def test_parse_transaction_lists_installs() -> None:
    added, refused = parse_transaction(SUMMARY)
    assert added == ["cowsay", "perl-Foo"] and refused == []


@pytest.mark.parametrize("section", ["Removing", "Downgrading", "Replacing"])
def test_plan_that_changes_other_packages_is_refused(section: str) -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    fake.plan_text = SUMMARY.replace("\nTransaction", f"\n{section}:\n old-pkg  x86_64  1  @a  1 k\n\nTransaction")  # fmt: skip
    plan = dnf(fake).plan_install("cowsay")
    assert not plan.feasible and "refused" in plan.reason


def test_unparseable_plan_is_refused_not_guessed() -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    fake.plan_text = "something entirely different"
    plan = dnf(fake).plan_install("cowsay")
    assert not plan.feasible and "could not be understood" in plan.reason


def test_install_command_is_fixed_argv_with_sudo_separator() -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    plan = dnf(fake).plan_install("cowsay")
    assert plan.argv == (
        "/usr/bin/sudo",
        "--",
        "/usr/bin/dnf",
        "install",
        "-y",
        "cowsay",
    )


def test_root_needs_no_sudo() -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    plan = dnf(fake, root=True).plan_install("cowsay")
    assert plan.argv[0] == "/usr/bin/dnf" and not plan.needs_privilege


def test_no_sudo_and_not_root_is_infeasible() -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    plan = dnf(fake, missing=("sudo",)).plan_install("cowsay")
    assert not plan.feasible and "sudo" in plan.reason


@pytest.mark.parametrize(
    ("stderr", "status"),
    [
        ("Waiting for process with pid 123 to finish.", InstallStatus.LOCKED),
        (
            "Error: This command has to be run with superuser privileges",
            InstallStatus.PERMISSION_DENIED,
        ),
        ("Error: Unable to find a match: cowsay", InstallStatus.UNAVAILABLE),
        ("Error: Failed to download metadata", InstallStatus.FAILED),
    ],
)
def test_install_failures_are_classified(stderr: str, status: InstallStatus) -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    a = dnf(fake)
    plan = a.plan_install("cowsay")
    fake.install_error = stderr
    assert a.install(plan).status is status
    assert "cowsay" not in fake.installed


def test_only_read_and_install_subcommands_ever_run() -> None:
    fake = FakeDnf()
    fake.available["cowsay"] = "3.8-1"
    a = dnf(fake)
    a.install(a.plan_install("cowsay"))
    used = {c[3] if c[0].endswith("sudo") else c[1] for c in fake.calls}
    flat = " ".join(" ".join(c) for c in fake.calls)
    assert " remove" not in flat and " erase" not in flat and "autoremove" not in flat
    assert used <= {"-q", "--quiet", "install"}


def test_registered_through_host_adapters(tmp_path: Any) -> None:
    import dataclasses

    from hostplatform.discovery import discover
    from tools import adapters

    host = dataclasses.replace(discover(), package_managers=("dnf",))
    reg = ToolRegistry()
    enabled = adapters.register_host_adapters(reg, host, home=tmp_path)
    # dnf is not installed on this machine, so it must NOT be enabled.
    assert "dnf" not in enabled or DnfAdapter(adapters.shutil.which, False).available()
