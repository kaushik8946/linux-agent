"""Development-tool installation (Build 12, module 6), on the apt fake."""

from pathlib import Path
from typing import Any

import pytest

from agent.registry import ToolRegistry
from agent.verifier import Outcome, verify
from tests.test_adapters import FakeHost, apt, last_payload, make_orch
from tests.test_llm_client import completion, tool_call
from tools import devtools
from tools.devtools import (
    DEV_TOOLS,
    INSTALL_DEV_TOOL,
    LIST_DEV_TOOLS,
    register_dev_tools,
)
from tools.packages import register_package_tools


def make(host: FakeHost) -> ToolRegistry:
    reg = ToolRegistry()
    adapter = apt(host)
    register_package_tools(reg, adapter)
    register_dev_tools(reg, adapter)
    return reg


def prepared(reg: ToolRegistry, tool: str) -> Any:
    op = reg.prepare(INSTALL_DEV_TOOL, {"tool": tool})
    assert not isinstance(op, dict), op
    return op


def test_absent_without_adapter() -> None:
    reg = ToolRegistry()
    register_dev_tools(reg, None)
    register_dev_tools(reg, apt(FakeHost(), "apt-get"))
    assert reg.names() == []


def test_registered_with_adapter() -> None:
    reg = make(FakeHost())
    assert reg.is_operation(INSTALL_DEV_TOOL) and not reg.is_operation(LIST_DEV_TOOLS)


def test_only_allowlisted_tools_accepted() -> None:
    reg = make(FakeHost())
    for bad in ("rm", "git; id", "", "../x", "nginx"):
        assert isinstance(reg.prepare(INSTALL_DEV_TOOL, {"tool": bad}), dict)
    assert isinstance(
        reg.prepare(INSTALL_DEV_TOOL, {"tool": "git", "package": "x"}), dict
    )


def test_listing_reports_installed_state() -> None:
    host = FakeHost()
    host.installed["git"] = "2.4"
    rows = {r["tool"]: r for r in make(host).call(LIST_DEV_TOOLS, {})["tools"]}
    assert rows["git"]["installed"] is True and rows["jq"]["installed"] is False
    assert set(rows) == set(DEV_TOOLS)


def test_prepare_runs_no_install() -> None:
    host = FakeHost()
    host.available["jq"] = "1.7"
    op = prepared(make(host), "jq")
    assert "jq" not in host.installed
    assert op.commands and op.commands[0].endswith("-- jq")
    assert op.requirement.value == "confirm" and op.needs_privilege


def test_already_installed_needs_no_change() -> None:
    host = FakeHost()
    host.installed["git"] = "2.4"
    op = prepared(make(host), "git")
    assert op.no_change_needed and op.commands == []


def test_unavailable_package_is_refused() -> None:
    op = prepared(make(FakeHost()), "jq")
    assert op.refused and verify(op).outcome is Outcome.BLOCKED


def test_verification_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    host = FakeHost()
    host.available["jq"] = "1.7"
    op = prepared(make(host), "jq")
    assert op.perform()["ok"]
    monkeypatch.setattr(devtools.shutil, "which", lambda _n: "/usr/bin/jq")
    assert verify(op).outcome is Outcome.PASSED


def test_verification_negative_package_installed_but_executable_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = FakeHost()
    host.available["jq"] = "1.7"
    op = prepared(make(host), "jq")
    op.perform()
    monkeypatch.setattr(devtools.shutil, "which", lambda _n: None)
    report = verify(op)
    assert report.outcome is Outcome.PARTIALLY_PASSED and report.partial_change is True


def test_verification_negative_nothing_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = FakeHost()
    host.available["jq"] = "1.7"
    host.install_noop = True  # command "succeeds", nothing happens
    op = prepared(make(host), "jq")
    op.perform()
    monkeypatch.setattr(devtools.shutil, "which", lambda _n: None)
    assert verify(op).outcome is Outcome.FAILED


def test_end_to_end_rejection_changes_nothing(tmp_path: Path) -> None:
    host = FakeHost()
    host.available["jq"] = "1.7"
    orch, sdk, _ = make_orch(
        tmp_path, host, "n",
        script=[completion(None, [tool_call(INSTALL_DEV_TOOL, {"tool": "jq"})]), completion("ok")],
    )  # fmt: skip
    register_dev_tools(orch.registry, apt(host))
    orch.ask("install jq")
    assert last_payload(sdk)["status"] == "rejected" and "jq" not in host.installed
