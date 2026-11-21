"""Verification engine (Build 11): outcome rules, the stop-and-diagnose path,
and positive and negative verification for every supported operation."""

import json
from pathlib import Path
from typing import Any

from agent.approval import ApprovalService
from agent.command_policy import RiskClass
from agent.executor import ChangeExecutor
from agent.llm_client import LLMClient
from agent.operations import Operation
from agent.orchestrator import Orchestrator
from agent.task_store import TaskJournal
from agent.verifier import Check, CheckResult, Outcome, classify, verify
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from hostplatform.packages.base import PackageState as PState
from hostplatform.packages.base import PackageStatus
from hostplatform.services.base import ServiceState, ServiceStatus
from tests.test_adapters import (
    FakeHost,
    apt,
    registry_for,
    systemd,
)
from tests.test_llm_client import FakeSDK, completion, tool_call
from tests.test_policy import Prompt
from tools.packages import (
    INSTALL_PACKAGE,
    START_SERVICE,
    describe_package,
    register_package_tools,
    register_service_tools,
)
from tools.system import build_registry


def op_with(
    checks: list[Check], baseline: str = "x: not_installed", **kw: Any
) -> Operation:
    defaults: dict[str, Any] = {
        "tool": "t",
        "arguments": {},
        "summary": "s",
        "commands": ["c"],
        "risk": RiskClass.SYSTEM_CHANGE,
        "baseline": baseline,
        "expected_end_state": "e",
        "affected": [],
        "verification": [],
        "recovery": "undo it yourself",
        "perform": lambda: {"ok": True},
        "checks": checks,
    }
    defaults.update(kw)
    return Operation(**defaults)


def ok(name: str) -> Check:
    return Check(name, lambda: CheckResult(name, True, "fine"))


def bad(name: str) -> Check:
    return Check(name, lambda: CheckResult(name, False, "wrong"))


def unknown(name: str) -> Check:
    return Check(name, lambda: CheckResult(name, None, "no data"))


# -- outcome rules -------------------------------------------------------------------


def test_all_checks_pass_is_passed() -> None:
    assert verify(op_with([ok("a"), ok("b")])).outcome is Outcome.PASSED


def test_some_pass_is_partially_passed() -> None:
    assert verify(op_with([ok("a"), bad("b")])).outcome is Outcome.PARTIALLY_PASSED


def test_all_fail_is_failed() -> None:
    assert verify(op_with([bad("a"), bad("b")])).outcome is Outcome.FAILED


def test_unknown_only_is_not_tested() -> None:
    assert verify(op_with([unknown("a")])).outcome is Outcome.NOT_TESTED


def test_no_declared_checks_is_not_tested_never_passed() -> None:
    report = verify(op_with([]))
    assert report.outcome is Outcome.NOT_TESTED
    assert "declared no verification" in report.note


def test_refused_operation_is_blocked() -> None:
    report = verify(op_with([ok("a")], refused="no such package"))
    assert report.outcome is Outcome.BLOCKED and report.checks == []


def test_exit_code_zero_alone_is_not_success() -> None:
    # The operation "succeeds" but its check finds nothing changed.
    op = op_with([bad("installed")], perform=lambda: {"ok": True})
    assert verify(op).outcome is not Outcome.PASSED


def test_broken_probe_is_not_tested_not_a_crash() -> None:
    def boom() -> CheckResult:
        raise RuntimeError("secret=abc123")

    report = verify(op_with([Check("probe", boom), ok("b")]))
    assert report.outcome is Outcome.PARTIALLY_PASSED
    assert report.checks[0].passed is None
    assert "abc123" not in report.checks[0].detail


def test_classify_with_empty_results() -> None:
    assert classify([]) is Outcome.NOT_TESTED


# -- stop, diagnose and propose recovery ---------------------------------------------


def test_failed_verification_collects_diagnostics_and_baseline_comparison() -> None:
    op = op_with(
        [bad("installed")],
        baseline="x: not_installed",
        probe_state=lambda: "x: installed (1.0)",
        diagnose=lambda: ["dpkg says half-configured"],
    )
    report = verify(op)
    assert report.outcome is Outcome.FAILED
    assert report.current == "x: installed (1.0)"
    assert report.partial_change is True
    assert report.diagnostics == ["dpkg says half-configured"]
    assert report.recovery == "undo it yourself"
    assert "suggested recovery (needs your approval)" in report.summary()


def test_unchanged_state_means_no_partial_change() -> None:
    op = op_with([bad("a")], probe_state=lambda: "x: not_installed")
    report = verify(op)
    assert report.partial_change is False


def test_diagnostics_and_probe_failures_do_not_crash() -> None:
    def no_state() -> str:
        raise OSError("gone")

    def no_diag() -> list[str]:
        raise OSError("gone")

    report = verify(op_with([bad("a")], probe_state=no_state, diagnose=no_diag))
    assert report.current is None and report.partial_change is None
    assert report.diagnostics == ["diagnostics unavailable: OSError"]


def test_diagnostics_are_redacted_and_bounded() -> None:
    op = op_with(
        [bad("a")],
        diagnose=lambda: ["token=supersecret " + "x" * 2000] * 50,
    )
    report = verify(op)
    assert len(report.diagnostics) <= 20
    assert all("supersecret" not in d and len(d) <= 300 for d in report.diagnostics)


def test_passed_report_has_no_recovery_or_diagnostics() -> None:
    op = op_with([ok("a")], diagnose=lambda: ["should not run"])
    report = verify(op)
    assert report.recovery is None and report.diagnostics == []


# -- supported operations: positive and negative ---------------------------------------


def registry_ops(host: FakeHost) -> Any:
    return registry_for(host)


def test_package_install_positive_verification() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    op = registry_ops(host).prepare(INSTALL_PACKAGE, {"package": "cowsay"})
    host.installed["cowsay"] = "3.03"  # as if the install had worked
    report = verify(op)
    assert report.outcome is Outcome.PASSED
    assert [c.name for c in report.checks] == [
        "cowsay is installed",
        "installed version",
    ]


def test_package_install_negative_wrong_version() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    op = registry_ops(host).prepare(INSTALL_PACKAGE, {"package": "cowsay"})
    host.installed["cowsay"] = "2.0"
    report = verify(op)
    assert report.outcome is Outcome.PARTIALLY_PASSED
    assert (
        report.checks[1].passed is False and "expected 3.03" in report.checks[1].detail
    )


def test_package_install_negative_not_installed() -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    op = registry_ops(host).prepare(INSTALL_PACKAGE, {"package": "cowsay"})
    report = verify(op)
    assert report.outcome is Outcome.FAILED
    assert report.partial_change is False


def test_service_start_positive_verification() -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    op = registry_ops(host).prepare(START_SERVICE, {"service": "cron"})
    host.units["cron.service"] = "active"
    report = verify(op)
    assert report.outcome is Outcome.PASSED


def test_service_start_negative_unit_dies_after_start() -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    op = registry_ops(host).prepare(START_SERVICE, {"service": "cron"})
    host.units["cron.service"] = "failed"
    report = verify(op)
    assert report.outcome is Outcome.FAILED
    assert report.current == "cron.service: failed (x)"


def test_service_start_negative_substate_failed_is_not_healthy() -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    op = registry_ops(host).prepare(START_SERVICE, {"service": "cron"})
    # ActiveState=active but the sub-state reports a failure.
    host.units["cron.service"] = "active"

    def failing_substate() -> CheckResult:
        return CheckResult("cron.service stays up", False, "sub-state failed")

    op.checks[1] = Check("healthy", failing_substate)
    report = verify(op)
    assert report.outcome is Outcome.PARTIALLY_PASSED


def test_service_diagnostics_include_detail() -> None:
    host = FakeHost()
    host.units["cron.service"] = "inactive"
    op = registry_ops(host).prepare(START_SERVICE, {"service": "cron"})
    host.units["cron.service"] = "failed"
    report = verify(op)
    assert any("enabled:" in d for d in report.diagnostics)


def test_verification_status_is_reported_to_model(tmp_path: Path) -> None:
    host = FakeHost()
    host.available["cowsay"] = "3.03"
    host.install_noop = True  # command succeeds but nothing gets installed
    sdk = FakeSDK(
        completion(None, [tool_call(INSTALL_PACKAGE, {"package": "cowsay"})]),
        completion("done"),
    )

    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 8, sdk)
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    registry = build_registry(discover(), runner)
    register_package_tools(registry, apt(host))
    register_service_tools(registry, systemd(host))
    approvals = ApprovalService(Prompt("y"))
    journal = TaskJournal(tmp_path / "tasks.jsonl")
    orch = Orchestrator(
        client, registry, runner, approvals=approvals,
        executor=ChangeExecutor(runner, approvals, journal),
    )  # fmt: skip
    orch.ask("install cowsay")
    tool_msgs = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    data = json.loads(tool_msgs[-1]["content"])
    assert data["status"] == "failed"
    assert data["verification"]["outcome"] == "FAILED"
    events = journal.task(data["task_id"])
    assert events[-1]["outcome"] == "failed"
    assert any(e.get("event") == "verification" for e in events)


def test_package_state_helper_used_for_baseline() -> None:
    state = PState("x", PackageStatus.INSTALLED, "1.0")
    assert describe_package(state) == "x: installed (1.0)"


def test_service_state_helper_used_for_baseline() -> None:
    from tools.packages import describe_service

    state = ServiceState("a.service", ServiceStatus.ACTIVE, "running")
    assert describe_service(state) == "a.service: active (running)"
