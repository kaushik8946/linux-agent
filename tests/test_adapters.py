"""Adapter contract tests. A scripted fake replaces every process call, so
nothing here touches apt, dpkg, sudo or systemd on the host."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent.approval import ApprovalService
from agent.executor import ChangeExecutor
from agent.llm_client import LLMClient
from agent.orchestrator import Orchestrator
from agent.registry import ToolRegistry
from agent.task_store import TaskJournal
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from hostplatform.packages.apt import AptAdapter
from hostplatform.packages.base import (
    InstallStatus,
    PackageNameError,
    PackageStatus,
    validate_package_name,
)
from hostplatform.privileged import ProcResult, as_root
from hostplatform.services.base import (
    ServiceNameError,
    ServiceStatus,
    StartStatus,
    validate_service_name,
)
from hostplatform.services.systemd import SystemdAdapter
from tests.test_llm_client import FakeSDK, completion, tool_call
from tests.test_policy import Prompt
from tools.packages import (
    INSTALL_PACKAGE,
    QUERY_PACKAGE,
    SERVICE_STATUS,
    START_SERVICE,
    register_package_tools,
    register_service_tools,
)
from tools.system import build_registry

BIN = {
    "dpkg-query": "/usr/bin/dpkg-query",
    "apt-cache": "/usr/bin/apt-cache",
    "apt-get": "/usr/bin/apt-get",
    "sudo": "/usr/bin/sudo",
    "systemctl": "/usr/bin/systemctl",
}


def which(*missing: str) -> Any:
    return lambda name: None if name in missing else BIN.get(name)


class FakeHost:
    """Simulates dpkg/apt/systemd state and records every argv run."""

    def __init__(self) -> None:
        self.installed: dict[str, str] = {}
        self.available: dict[str, str] = {}
        self.deps: dict[str, list[str]] = {}
        self.removes: dict[str, list[str]] = {}
        self.install_error: str | None = None
        self.install_noop = False
        self.units: dict[str, str] = {}  # unit -> ActiveState
        self.start_result: str = "active"
        self.start_error: str | None = None
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None
    ) -> ProcResult:
        argv = tuple(argv)
        self.calls.append(argv)
        cmd = argv[2:] if argv[0] == BIN["sudo"] else argv
        exe, rest = cmd[0], list(cmd[1:])
        name = rest[-1]
        if exe == BIN["dpkg-query"]:
            if name in self.installed:
                return ProcResult(
                    argv, 0, f"install ok installed\t{self.installed[name]}", ""
                )
            return ProcResult(
                argv, 1, "", f"dpkg-query: no packages found matching {name}"
            )
        if exe == BIN["apt-cache"]:
            cand = self.available.get(name) or self.installed.get(name)
            if cand is None:
                return ProcResult(argv, 0, "", "N: Unable to locate package")
            return ProcResult(
                argv, 0, f"{name}:\n  Installed: (none)\n  Candidate: {cand}\n", ""
            )
        if exe == BIN["apt-get"] and "--simulate" in rest:
            if name not in self.available:
                return ProcResult(argv, 100, "", f"E: Unable to locate package {name}")
            lines = [
                f"Inst {p} (1.0 Ubuntu:26.04 [amd64])"
                for p in [name, *self.deps.get(name, [])]
            ]
            lines += [f"Remv {p} [1.0]" for p in self.removes.get(name, [])]
            return ProcResult(argv, 0, "\n".join(lines), "")
        if exe == BIN["apt-get"] and rest[0] == "install":
            if argv[0] != BIN["sudo"]:
                return ProcResult(
                    argv, 100, "", "E: Could not open lock file - are you root?"
                )
            if self.install_error:
                return ProcResult(argv, 100, "", self.install_error)
            if not self.install_noop:
                self.installed[name] = self.available.pop(name)
            return ProcResult(argv, 0, "Setting up...", "")
        if exe == BIN["systemctl"] and rest[0] == "show":
            if name not in self.units:
                return ProcResult(
                    argv, 0, "LoadState=not-found\nActiveState=inactive\n", ""
                )
            state = self.units[name]
            return ProcResult(
                argv,
                0,
                f"LoadState=loaded\nActiveState={state}\nSubState=x\nUnitFileState=enabled\nMainPID=42\n",
                "",
            )
        if exe == BIN["systemctl"] and rest[0] == "start":
            if self.start_error:
                return ProcResult(argv, 1, "", self.start_error)
            if name not in self.units:
                return ProcResult(
                    argv, 5, "", f"Failed to start {name}: Unit {name} not found."
                )
            self.units[name] = self.start_result
            return ProcResult(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


def apt(host: FakeHost, *missing: str, root: bool = False) -> AptAdapter:
    return AptAdapter(which(*missing), is_root=root, run=host)


def systemd(host: FakeHost, running: bool = True, root: bool = False) -> SystemdAdapter:
    return SystemdAdapter(
        which(), root, running, run=host, sleep=lambda _s: None, settle_seconds=1
    )


# -- name validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["git", "python3", "libc6-dev", "g++", "openjdk-21-jdk", "lib.x"]
)
def test_valid_package_names(name: str) -> None:
    assert validate_package_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a",
        "-y",
        "--force-yes",
        "Git",
        "a b",
        "a;rm",
        "$(id)",
        "x" * 101,
        "../x",
        "pkg=1.0",
    ],
)
def test_invalid_package_names_rejected(name: str) -> None:
    with pytest.raises(PackageNameError):
        validate_package_name(name)


@pytest.mark.parametrize(
    "name", ["cron", "ssh.service", "getty@tty1.service", "docker"]
)
def test_valid_service_names(name: str) -> None:
    assert validate_service_name(name).endswith(".service")


@pytest.mark.parametrize(
    "name", ["", "-x", "a b", "x;y", "../etc", "cron.timer", "a..b", "$(id)"]
)
def test_invalid_service_names_rejected(name: str) -> None:
    with pytest.raises(ServiceNameError):
        validate_service_name(name)


def test_as_root_prefixes_sudo_with_separator() -> None:
    assert as_root(["/usr/bin/apt-get", "x"], "/usr/bin/sudo", False) == [
        "/usr/bin/sudo",
        "--",
        "/usr/bin/apt-get",
        "x",
    ]
    assert as_root(["a"], None, False) is None
    assert as_root(["a"], None, True) == ["a"]


# -- apt queries ----------------------------------------------------------------------


def test_query_installed_package() -> None:
    host = FakeHost()
    host.installed["git"] = "1:2.43"
    state = apt(host).query("git")
    assert (
        state.status is PackageStatus.INSTALLED and state.installed_version == "1:2.43"
    )


def test_query_not_installed_package() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    state = apt(host).query("cowsay")
    assert (
        state.status is PackageStatus.NOT_INSTALLED
        and state.candidate_version == "3.03"
    )


def test_query_unavailable_package() -> None:
    assert apt(FakeHost()).query("nopkgxyz").status is PackageStatus.UNAVAILABLE


def test_queries_do_not_use_sudo() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    adapter = apt(host)
    adapter.query("cowsay")
    adapter.plan_install("cowsay")
    assert all(c[0] != BIN["sudo"] for c in host.calls)


def test_missing_apt_is_unavailable() -> None:
    adapter = apt(FakeHost(), "apt-get")
    assert not adapter.available()
    assert adapter.query("git").status is PackageStatus.UNKNOWN
    assert not adapter.plan_install("git").feasible


# -- apt plans and installs --------------------------------------------------------------


def test_install_plan_uses_exact_fixed_argv() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.deps["cowsay"] = ["libtext-charwidth-perl"]
    plan = apt(host).plan_install("cowsay")
    assert plan.feasible and plan.needs_privilege
    assert plan.argv == (
        BIN["sudo"],
        "--",
        BIN["apt-get"],
        "install",
        "-y",
        "--no-remove",
        "--",
        "cowsay",
    )
    assert plan.new_packages == ("cowsay", "libtext-charwidth-perl")


def test_install_plan_as_root_has_no_sudo() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    assert apt(host, root=True).plan_install("cowsay").argv[0] == BIN["apt-get"]


def test_install_plan_without_sudo_is_infeasible() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    plan = apt(host, "sudo").plan_install("cowsay")
    assert not plan.feasible and "sudo" in plan.reason


def test_install_that_would_remove_packages_is_refused() -> None:
    host = FakeHost()
    host.available["evil"] = "1"
    host.removes["evil"] = ["libc6"]
    plan = apt(host).plan_install("evil")
    assert not plan.feasible and "remove" in plan.reason


def test_package_already_installed() -> None:
    host = FakeHost()
    host.installed["git"] = "2.43"
    adapter = apt(host)
    plan = adapter.plan_install("git")
    assert plan.already_installed and plan.argv == ()
    result = adapter.install(plan)
    assert result.status is InstallStatus.ALREADY_INSTALLED and result.ok
    assert not any(
        "install" in c and "--simulate" not in c
        for c in host.calls
        if BIN["apt-get"] in c
    )


def test_package_unavailable() -> None:
    plan = apt(FakeHost()).plan_install("nopkgxyz")
    assert not plan.feasible


def test_successful_install_is_verified() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    adapter = apt(host)
    result = adapter.install(adapter.plan_install("cowsay"))
    assert result.status is InstallStatus.INSTALLED and result.ok
    assert result.before and result.before.status is PackageStatus.NOT_INSTALLED
    assert result.after and result.after.installed_version == "3.03"


def test_exit_zero_without_installed_state_is_failure() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.install_noop = True
    adapter = apt(host)
    result = adapter.install(adapter.plan_install("cowsay"))
    assert result.status is InstallStatus.FAILED and "not installed" in result.message


def test_package_manager_lock() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.install_error = (
        "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 123"
    )
    adapter = apt(host)
    result = adapter.install(adapter.plan_install("cowsay"))
    assert result.status is InstallStatus.LOCKED


def test_permission_failure() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.install_error = "sudo: a password is required"
    adapter = apt(host)
    result = adapter.install(adapter.plan_install("cowsay"))
    assert result.status is InstallStatus.PERMISSION_DENIED


def test_generic_install_failure() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.install_error = "E: Sub-process /usr/bin/dpkg returned an error code (1)"
    adapter = apt(host)
    result = adapter.install(adapter.plan_install("cowsay"))
    assert result.status is InstallStatus.FAILED and not result.ok


def test_tampered_install_plan_is_not_run() -> None:
    from dataclasses import replace

    host = FakeHost()
    host.available["cowsay"] = "3.03"
    adapter = apt(host)
    plan = adapter.plan_install("cowsay")
    evil = replace(
        plan, argv=(BIN["sudo"], "--", BIN["apt-get"], "remove", "-y", "--", "cowsay")
    )
    result = adapter.install(evil)
    assert result.status is InstallStatus.NOT_ATTEMPTED
    assert not any("remove" in c for c in host.calls)


# -- systemd ----------------------------------------------------------------------------


def test_service_status() -> None:
    host = FakeHost()
    host.units["cron.service"] = "active"
    state = systemd(host).status("cron")
    assert (
        state.status is ServiceStatus.ACTIVE
        and state.main_pid == 42
        and state.enabled == "enabled"
    )


def test_service_not_found() -> None:
    adapter = systemd(FakeHost())
    assert adapter.status("nope").status is ServiceStatus.NOT_FOUND
    assert adapter.start("nope").status is StartStatus.NOT_FOUND


def test_service_start_success_is_verified() -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    result = systemd(host).start("cron")
    assert result.status is StartStatus.STARTED and result.ok
    assert (
        BIN["sudo"],
        "--",
        BIN["systemctl"],
        "start",
        "--",
        "cron.service",
    ) in host.calls


def test_service_already_active_is_not_restarted() -> None:
    host = FakeHost()
    host.units["cron.service"] = "active"
    result = systemd(host).start("cron")
    assert result.status is StartStatus.ALREADY_ACTIVE
    assert not any("start" in c for c in host.calls)


def test_service_start_failure() -> None:
    host = FakeHost()
    host.units["broken.service"] = "inactive"
    host.start_error = (
        "Job for broken.service failed because the control process exited"
    )
    result = systemd(host).start("broken")
    assert result.status is StartStatus.FAILED and not result.ok


def test_service_that_starts_then_fails_is_failure() -> None:
    host = FakeHost()
    host.units["flaky.service"] = "inactive"
    host.start_result = "failed"
    result = systemd(host).start("flaky")
    assert result.status is StartStatus.FAILED and "failed" in result.message


def test_service_start_permission_failure() -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    host.start_error = "Interactive authentication required."
    assert systemd(host).start("cron").status is StartStatus.PERMISSION_DENIED


def test_systemd_not_running_is_unavailable() -> None:
    adapter = systemd(FakeHost(), running=False)
    assert not adapter.available()
    assert adapter.status("cron").status is ServiceStatus.UNKNOWN
    assert adapter.start_argv("cron") is None


# -- tools, approval and journal -----------------------------------------------------------


def registry_for(host: FakeHost) -> ToolRegistry:
    registry = ToolRegistry()
    register_package_tools(registry, apt(host))
    register_service_tools(registry, systemd(host))
    return registry


def test_tools_registered_only_when_adapter_available() -> None:
    reg = ToolRegistry()
    register_package_tools(reg, apt(FakeHost(), "apt-get"))
    register_service_tools(reg, systemd(FakeHost(), running=False))
    assert reg.names() == []
    full = registry_for(FakeHost())
    assert set(full.names()) == {
        QUERY_PACKAGE,
        INSTALL_PACKAGE,
        SERVICE_STATUS,
        START_SERVICE,
    }
    assert full.is_operation(INSTALL_PACKAGE) and not full.is_operation(QUERY_PACKAGE)


def test_operation_arguments_are_validated() -> None:
    reg = registry_for(FakeHost())
    assert "error" in reg.prepare(INSTALL_PACKAGE, {"package": "-y"})  # type: ignore[operator]
    assert "error" in reg.prepare(INSTALL_PACKAGE, {"package": "git", "extra": 1})  # type: ignore[operator]
    assert "error" in reg.prepare(START_SERVICE, {"service": "x;y"})  # type: ignore[operator]


def test_prepare_never_changes_anything() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.units["cron.service"] = "inactive"
    reg = registry_for(host)
    reg.prepare(INSTALL_PACKAGE, {"package": "cowsay"})
    reg.prepare(START_SERVICE, {"service": "cron"})
    assert "cowsay" not in host.installed and host.units["cron.service"] == "inactive"
    assert all(c[0] != BIN["sudo"] for c in host.calls)


def make_orch(
    tmp_path: Path, host: FakeHost, *answers: Any, script: list[Any]
) -> tuple[Orchestrator, FakeSDK, TaskJournal]:
    sdk = FakeSDK(*script)
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 8, sdk)
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    registry = build_registry(discover(), runner)
    register_package_tools(registry, apt(host))
    register_service_tools(registry, systemd(host))
    approvals = ApprovalService(Prompt(*answers))
    journal = TaskJournal(tmp_path / "tasks.jsonl")
    orch = Orchestrator(client, registry, runner, approvals=approvals,
                        executor=ChangeExecutor(runner, approvals, journal))  # fmt: skip
    return orch, sdk, journal


def last_payload(sdk: FakeSDK) -> dict[str, Any]:
    tools = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    return json.loads(tools[-1]["content"])


def test_approved_install_workflow_end_to_end(tmp_path: Path) -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    orch, sdk, journal = make_orch(
        tmp_path, host, "y",
        script=[completion(None, [tool_call(INSTALL_PACKAGE, {"package": "cowsay"})]), completion("Installed.")],
    )  # fmt: skip
    result = orch.ask("install cowsay")
    data = last_payload(sdk)
    assert data["status"] == "completed" and data["result"]["status"] == "installed"
    assert host.installed["cowsay"] == "3.03" and result.events[0].ok
    events = journal.task(data["task_id"])
    assert events[0]["tool"] == INSTALL_PACKAGE and events[0]["approved"] is True
    assert events[-1]["outcome"] == "completed"


def test_rejected_install_changes_nothing(tmp_path: Path) -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    orch, sdk, _ = make_orch(
        tmp_path, host, "",
        script=[completion(None, [tool_call(INSTALL_PACKAGE, {"package": "cowsay"})]), completion("ok")],
    )  # fmt: skip
    orch.ask("install cowsay")
    assert last_payload(sdk)["status"] == "rejected" and "cowsay" not in host.installed
    assert not any(c[0] == BIN["sudo"] for c in host.calls)


def test_already_installed_needs_no_approval(tmp_path: Path) -> None:
    host = FakeHost()
    host.installed["git"] = "2.43"
    orch, sdk, _ = make_orch(
        tmp_path, host,
        script=[completion(None, [tool_call(INSTALL_PACKAGE, {"package": "git"})]), completion("ok")],
    )  # fmt: skip
    orch.ask("install git")
    assert last_payload(sdk)["status"] == "no_change_needed"


def test_unavailable_package_is_refused_without_prompt(tmp_path: Path) -> None:
    orch, sdk, _ = make_orch(
        tmp_path, FakeHost(),
        script=[completion(None, [tool_call(INSTALL_PACKAGE, {"package": "nopkgxyz"})]), completion("ok")],
    )  # fmt: skip
    orch.ask("install it")
    assert last_payload(sdk)["status"] == "blocked"


def test_state_change_between_approval_and_run_is_detected(tmp_path: Path) -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"

    class Racing(Prompt):
        def __call__(self, question: str) -> str:
            host.installed["cowsay"] = host.available.pop(
                "cowsay"
            )  # someone else installed it
            return "y"

    orch, sdk, _ = make_orch(
        tmp_path, host,
        script=[completion(None, [tool_call(INSTALL_PACKAGE, {"package": "cowsay"})]), completion("ok")],
    )  # fmt: skip
    assert orch.approvals is not None
    orch.approvals.set_prompt(Racing())
    orch.ask("install cowsay")
    assert last_payload(sdk)["status"] in ("rejected", "no_change_needed")
    assert not any(c[0] == BIN["sudo"] for c in host.calls)


def test_failed_install_is_reported_as_failed(tmp_path: Path) -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.install_error = "E: Sub-process /usr/bin/dpkg returned an error code (1)"
    orch, sdk, _ = make_orch(
        tmp_path, host, "y",
        script=[completion(None, [tool_call(INSTALL_PACKAGE, {"package": "cowsay"})]), completion("ok")],
    )  # fmt: skip
    orch.ask("install cowsay")
    data = last_payload(sdk)
    assert data["status"] == "failed" and data["result"]["status"] == "failed"


def test_approved_service_start_workflow(tmp_path: Path) -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    orch, sdk, _ = make_orch(
        tmp_path, host, "y",
        script=[completion(None, [tool_call(START_SERVICE, {"service": "cron"})]), completion("Started.")],
    )  # fmt: skip
    orch.ask("start cron")
    data = last_payload(sdk)
    assert data["status"] == "completed" and data["result"]["status"] == "started"
    assert host.units["cron.service"] == "active"


def test_service_start_failure_reaches_the_model(tmp_path: Path) -> None:
    host = FakeHost()
    host.units["flaky.service"] = "inactive"
    host.start_result = "failed"
    orch, sdk, _ = make_orch(
        tmp_path, host, "y",
        script=[completion(None, [tool_call(START_SERVICE, {"service": "flaky"})]), completion("ok")],
    )  # fmt: skip
    orch.ask("start flaky")
    data = last_payload(sdk)
    # The start command reported ok, but the unit is not active afterwards:
    # verification fails and the outcome is never "completed".
    assert data["status"] in ("failed", "partial")
    assert data["verification"]["outcome"] in ("FAILED", "PARTIALLY PASSED")
    assert "Verification" not in data["message"]


def test_operation_render_shows_everything() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    op = registry_for(host).prepare(INSTALL_PACKAGE, {"package": "cowsay"})
    assert not isinstance(op, dict)
    text = op.render()
    for item in ("install cowsay", "apt-get install -y --no-remove -- cowsay",
                 "sudo will ask for your password", "baseline", "expected end state",
                 "recovery", "does not remove packages"):  # fmt: skip
        assert item in text


def test_no_removal_operations_exist() -> None:
    names = registry_for(FakeHost()).names()
    assert not any(
        w in n for n in names for w in ("remove", "purge", "stop", "disable", "mask")
    )
