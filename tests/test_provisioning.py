"""Provisioning profiles (Build 12, module 9), on the apt fake."""

import json
from pathlib import Path

import pytest

from agent.registry import ToolRegistry
from tests.test_adapters import FakeHost, apt, last_payload, make_orch
from tests.test_llm_client import completion, tool_call
from tools import devtools
from tools.devtools import INSTALL_DEV_TOOL, register_dev_tools
from tools.packages import register_package_tools
from tools.provisioning import (
    CHECK_PROFILE,
    LIST_PROFILES,
    PLAN_PROFILE,
    PROFILES,
    register_provisioning_tools,
)


def make(host: FakeHost) -> ToolRegistry:
    reg = ToolRegistry()
    adapter = apt(host)
    register_package_tools(reg, adapter)
    register_dev_tools(reg, adapter)
    register_provisioning_tools(reg)
    return reg


@pytest.fixture(autouse=True)
def which_follows_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devtools.shutil, "which", lambda n: f"/usr/bin/{n}")


def test_tools_are_read_only() -> None:
    reg = make(FakeHost())
    for name in (LIST_PROFILES, PLAN_PROFILE, CHECK_PROFILE):
        assert name in reg and not reg.is_operation(name)


def test_profiles_only_use_registered_operations() -> None:
    reg = make(FakeHost())
    for profile in PROFILES.values():
        for step in profile.steps:
            assert reg.is_operation(step.tool), step


def test_list_profiles() -> None:
    names = {p["name"] for p in make(FakeHost()).call(LIST_PROFILES, {})["profiles"]}
    assert names == set(PROFILES)


def test_unknown_profile_rejected() -> None:
    assert "error" in make(FakeHost()).call(PLAN_PROFILE, {"profile": "everything"})


def test_plan_changes_nothing_and_classifies_steps() -> None:
    host = FakeHost()
    host.installed["git"] = "2.4"
    host.available["curl"] = "8.0"
    data = make(host).call(PLAN_PROFILE, {"profile": "minimal"})
    states = {s["arguments"]["tool"]: s["state"] for s in data["steps"]}
    assert states == {"git": "done", "curl": "pending"}
    assert data["pending"] == 1 and "one at a time" in data["next"]
    assert "curl" not in host.installed
    assert not any(
        "install" in c and "--simulate" not in c for c in host.calls if "-y" in c
    )


def test_plan_reports_unavailable_steps() -> None:
    data = make(FakeHost()).call(PLAN_PROFILE, {"profile": "minimal"})
    assert data["cannot_run"] == 2 and data["pending"] == 0


def test_check_profile_positive() -> None:
    host = FakeHost()
    host.installed.update({"git": "2.4", "curl": "8.0"})
    data = make(host).call(CHECK_PROFILE, {"profile": "minimal"})
    assert data["outcome"] == "PASSED"


def test_check_profile_partial() -> None:
    host = FakeHost()
    host.installed["git"] = "2.4"
    host.available["curl"] = "8.0"
    data = make(host).call(CHECK_PROFILE, {"profile": "minimal"})
    assert data["outcome"] == "PARTIALLY PASSED"


def test_check_profile_failed() -> None:
    host = FakeHost()
    host.available.update({"git": "2.4", "curl": "8.0"})
    assert make(host).call(CHECK_PROFILE, {"profile": "minimal"})["outcome"] == "FAILED"


def test_each_step_is_approved_separately(tmp_path: Path) -> None:
    host = FakeHost()
    host.available.update({"git": "2.4", "curl": "8.0"})
    orch, sdk, _journal = make_orch(
        tmp_path, host, "y", "n",
        script=[
            completion(None, [tool_call(PLAN_PROFILE, {"profile": "minimal"})]),
            completion(None, [tool_call(INSTALL_DEV_TOOL, {"tool": "git"})]),
            completion(None, [tool_call(INSTALL_DEV_TOOL, {"tool": "curl"})]),
            completion(None, [tool_call(CHECK_PROFILE, {"profile": "minimal"})]),
            completion("done"),
        ],
    )  # fmt: skip
    register_dev_tools(orch.registry, apt(host))
    register_provisioning_tools(orch.registry)
    orch.ask("set up a minimal workstation")
    tool_msgs = [
        json.loads(m["content"])
        for m in sdk.requests[-1]["messages"]
        if m["role"] == "tool"
    ]
    assert tool_msgs[1]["status"] == "completed"  # git approved
    assert tool_msgs[2]["status"] == "rejected"  # curl declined
    assert last_payload(sdk)["outcome"] == "PARTIALLY PASSED"
    assert "git" in host.installed and "curl" not in host.installed
