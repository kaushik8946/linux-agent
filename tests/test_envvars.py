"""Environment-variable management (Build 12, module 1)."""

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from agent.approval import ApprovalService
from agent.executor import ChangeExecutor
from agent.llm_client import LLMClient
from agent.orchestrator import Orchestrator
from agent.registry import ToolRegistry
from agent.task_store import TaskJournal
from agent.verifier import Outcome, verify
from hostplatform.command_runner import CommandRunner
from hostplatform.envstore import (
    BEGIN,
    EnvError,
    EnvStore,
    validate_name,
    validate_value,
)
from tests.test_llm_client import FakeSDK, completion, tool_call
from tests.test_policy import Prompt
from tools.envvars import (
    GET_ENV,
    LIST_ENV,
    SET_ENV,
    UNSET_ENV,
    register_env_tools,
)

SH = "/bin/sh" if os.path.exists("/bin/sh") else None


def store_in(tmp_path: Path) -> EnvStore:
    return EnvStore(tmp_path / "home")


def registry_for(store: EnvStore) -> ToolRegistry:
    reg = ToolRegistry()
    register_env_tools(reg, store, SH)
    return reg


# -- validation -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["EDITOR", "MY_VAR", "A1", "_X"])
def test_valid_names(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "lower", "a b", "A;B", "1A", "A-B", "X" * 65, "$(id)", "A=B", 5],
)
def test_invalid_names(name: Any) -> None:
    with pytest.raises(EnvError):
        validate_name(name)


@pytest.mark.parametrize(
    "name",
    ["PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "IFS", "PYTHONPATH",
     "HOME", "BASH_FUNC_x%%", "NODE_OPTIONS"],
)  # fmt: skip
def test_dangerous_names_refused(name: str) -> None:
    with pytest.raises(EnvError):
        validate_name(name)


@pytest.mark.parametrize(
    "name", ["API_TOKEN", "DB_PASSWORD", "AWS_SECRET_KEY", "MY_API_KEY"]
)
def test_secret_like_names_refused(name: str) -> None:
    with pytest.raises(EnvError, match="secret"):
        validate_name(name)


@pytest.mark.parametrize(
    "value", ["", "a\nb", "a\x00b", "x" * 1001, "ghp_" + "a" * 36, "password=hunter2xx"]
)
def test_invalid_values(value: str) -> None:
    with pytest.raises(EnvError):
        validate_value(value)


# -- store ------------------------------------------------------------------------


def test_store_round_trip_with_hostile_values(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    hostile = "a'b\"c $(id) `x` ;rm -rf / & #z"
    store.set("MY_VAR", hostile)
    assert store.get("MY_VAR").value == hostile
    assert stat.S_IMODE(store.file.stat().st_mode) == 0o600


def test_profile_hook_added_once_and_existing_content_kept(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.home.mkdir(parents=True)
    store.profile.write_text("echo hello")  # no trailing newline
    store.set("A_VAR", "1")
    store.set("B_VAR", "2")
    text = store.profile.read_text()
    assert text.startswith("echo hello\n")
    assert text.count(BEGIN) == 1
    assert sorted(store.read_all()) == ["A_VAR", "B_VAR"]


def test_unset_leaves_other_variables(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.set("A_VAR", "1")
    store.set("B_VAR", "2")
    store.unset("A_VAR")
    assert store.read_all() == {"B_VAR": "2"}


def test_unmanaged_lines_in_file_are_ignored(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.file.parent.mkdir(parents=True)
    store.file.write_text("rm -rf /\nexport GOOD='1'\nexport bad name=2\n")
    assert store.read_all() == {"GOOD": "1"}


# -- tools: read-only ---------------------------------------------------------------


def test_list_and_get_tools(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.set("EDITOR_X", "vim")
    reg = registry_for(store)
    listing = reg.call(LIST_ENV, {})
    assert listing["variables"] == ["EDITOR_X"] and listing["profile_sources_file"]
    assert reg.call(GET_ENV, {"name": "EDITOR_X"})["value"] == "vim"
    assert reg.call(GET_ENV, {"name": "OTHER"})["managed"] is False
    assert "error" in reg.call(GET_ENV, {"name": "bad name"})


def test_changing_tools_are_operations(tmp_path: Path) -> None:
    reg = registry_for(store_in(tmp_path))
    assert reg.is_operation(SET_ENV) and reg.is_operation(UNSET_ENV)
    assert not reg.is_operation(LIST_ENV)


# -- prepare: nothing changes before approval ------------------------------------------


def test_prepare_changes_nothing(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict)
    assert not store.file.exists() and not store.profile.exists()
    assert "EDITOR_X" in op.render() and str(store.file) in op.render()
    assert op.requirement.value == "confirm"


@pytest.mark.parametrize(
    "args",
    [
        {"name": "PATH", "value": "/tmp"},
        {"name": "LD_PRELOAD", "value": "/tmp/x.so"},
        {"name": "A_TOKEN", "value": "x"},
        {"name": "OK_NAME", "value": "a\nb"},
        {"name": "OK_NAME"},
        {"name": "OK_NAME", "value": "v", "extra": 1},
    ],
)
def test_prepare_rejects_bad_arguments(tmp_path: Path, args: dict[str, Any]) -> None:
    result = registry_for(store_in(tmp_path)).prepare(SET_ENV, args)
    assert isinstance(result, dict) and "error" in result


def test_same_value_needs_no_change(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.set("EDITOR_X", "vim")
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict) and op.no_change_needed


def test_unset_of_unmanaged_needs_no_change(tmp_path: Path) -> None:
    op = registry_for(store_in(tmp_path)).prepare(UNSET_ENV, {"name": "NOPE_X"})
    assert not isinstance(op, dict) and op.no_change_needed


# -- verification: positive and negative ---------------------------------------------------


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
def test_set_verification_positive(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "va m"})
    assert not isinstance(op, dict)
    assert op.perform()["ok"] is True
    report = verify(op)
    assert report.outcome is Outcome.PASSED
    assert [c.name for c in report.checks][1] == "new process sees it"


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
def test_set_verification_negative_when_nothing_written(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict)
    report = verify(op)  # perform() never ran
    assert report.outcome is Outcome.FAILED and report.partial_change is False


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
def test_set_verification_partial_when_profile_hook_missing(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict)
    store._write_vars({"EDITOR_X": "vim"})  # file written, profile not hooked
    report = verify(op)
    assert report.outcome is Outcome.PARTIALLY_PASSED
    assert report.partial_change is True


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
def test_fresh_process_check_catches_a_value_that_differs(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict)
    op.perform()
    store.file.write_text(store.file.read_text() + "export EDITOR_X='other'\n")
    assert verify(op).outcome is not Outcome.PASSED


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
def test_unset_verification_positive_and_negative(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.set("EDITOR_X", "vim")
    op = registry_for(store).prepare(UNSET_ENV, {"name": "EDITOR_X"})
    assert not isinstance(op, dict)
    assert verify(op).outcome is not Outcome.PASSED  # not removed yet
    op.perform()
    assert verify(op).outcome is Outcome.PASSED


def test_no_shell_means_fresh_check_not_tested(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    reg = ToolRegistry()
    register_env_tools(reg, store, None)
    op = reg.prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict)
    op.perform()
    report = verify(op)
    assert report.outcome is Outcome.PARTIALLY_PASSED
    assert report.checks[1].passed is None


def test_write_failure_is_reported_not_raised(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    store.home.mkdir(parents=True)
    (store.home / ".config").write_text("a file, not a directory")
    op = registry_for(store).prepare(SET_ENV, {"name": "EDITOR_X", "value": "vim"})
    assert not isinstance(op, dict)
    result = op.perform()
    assert result["ok"] is False and not store.profile.exists()


# -- end to end through the orchestrator -----------------------------------------------------


def run_flow(
    tmp_path: Path, answer: str, args: dict[str, Any]
) -> tuple[dict[str, Any], EnvStore, TaskJournal]:
    store = store_in(tmp_path)
    sdk = FakeSDK(completion(None, [tool_call(SET_ENV, args)]), completion("ok"))
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 8, sdk)
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    reg = registry_for(store)
    approvals = ApprovalService(Prompt(answer))
    journal = TaskJournal(tmp_path / "tasks.jsonl")
    orch = Orchestrator(
        client, reg, runner, approvals=approvals,
        executor=ChangeExecutor(runner, approvals, journal),
    )  # fmt: skip
    orch.ask("set it")
    msgs = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    return json.loads(msgs[-1]["content"]), store, journal


@pytest.mark.skipif(SH is None, reason="needs a POSIX shell")
def test_approved_set_end_to_end(tmp_path: Path) -> None:
    data, store, journal = run_flow(tmp_path, "y", {"name": "EDITOR_X", "value": "vim"})
    assert data["status"] == "completed"
    assert data["verification"]["outcome"] == "PASSED"
    assert store.get("EDITOR_X").value == "vim"
    assert journal.task(data["task_id"])[0]["approved"] is True


def test_rejected_set_changes_nothing(tmp_path: Path) -> None:
    data, store, _ = run_flow(tmp_path, "n", {"name": "EDITOR_X", "value": "vim"})
    assert data["status"] == "rejected"
    assert not store.file.exists() and not store.profile.exists()


def test_model_cannot_set_forbidden_variable(tmp_path: Path) -> None:
    data, store, _ = run_flow(tmp_path, "y", {"name": "LD_PRELOAD", "value": "/x.so"})
    assert data["status"] == "invalid" and not store.file.exists()
