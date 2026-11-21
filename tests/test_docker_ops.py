"""Docker safe operations (Build 12, module 3), against a stateful fake."""

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
from agent.verifier import Outcome, verify
from hostplatform.command_runner import CommandRunner
from hostplatform.docker import DockerClient, DockerNameError, validate_image
from hostplatform.privileged import ProcResult
from tests.test_llm_client import FakeSDK, completion, tool_call
from tests.test_policy import Prompt
from tools.docker_ops import (
    PULL_IMAGE,
    RESTART_CONTAINER,
    START_CONTAINER,
    STOP_CONTAINER,
    register_docker_operations,
)

DOCKER = "/usr/bin/docker"


class StatefulDocker:
    def __init__(self) -> None:
        self.containers: dict[str, str] = {"web": "exited", "db": "running"}
        self.images: set[str] = {"nginx:1"}
        self.daemon = True
        self.start_dies = False  # start "succeeds" then the container exits
        self.fail_with: str | None = None
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None
    ) -> ProcResult:
        argv = tuple(argv)
        self.calls.append(argv)
        cmd, last = argv[1], argv[-1]
        if not self.daemon:
            return ProcResult(argv, 1, "", "Cannot connect to the Docker daemon")
        if cmd == "version":
            return ProcResult(argv, 0, json.dumps({"Client": {"Version": "27"}, "Server": {"Version": "27"}}), "")  # fmt: skip
        if cmd == "inspect":
            if last not in self.containers:
                return ProcResult(argv, 1, "", "Error: No such container: " + last)
            st = self.containers[last]
            data = [{"Name": "/" + last, "Config": {}, "HostConfig": {},
                     "State": {"Status": st, "Running": st == "running"}}]  # fmt: skip
            return ProcResult(argv, 0, json.dumps(data), "")
        if cmd == "image":
            return ProcResult(argv, 0 if last in self.images else 1,
                              "sha256:x" if last in self.images else "", "")  # fmt: skip
        if self.fail_with:
            return ProcResult(argv, 1, "", self.fail_with)
        if cmd in ("start", "restart"):
            if last not in self.containers:
                return ProcResult(argv, 1, "", "Error: No such container")
            self.containers[last] = "exited" if self.start_dies else "running"
            return ProcResult(argv, 0, last, "")
        if cmd == "stop":
            self.containers[last] = "exited"
            return ProcResult(argv, 0, last, "")
        if cmd == "pull":
            self.images.add(last)
            return ProcResult(argv, 0, "Pulled", "")
        raise AssertionError(f"unexpected {argv}")


def make() -> tuple[ToolRegistry, StatefulDocker]:
    fake = StatefulDocker()
    reg = ToolRegistry()
    register_docker_operations(reg, DockerClient(lambda _n: DOCKER, run=fake))
    return reg, fake


def prepared(reg: ToolRegistry, tool: str, args: dict[str, Any]) -> Any:
    op = reg.prepare(tool, args)
    assert not isinstance(op, dict), op
    return op


def test_only_safe_operations_exist() -> None:
    reg, _ = make()
    assert set(reg.names()) == {
        START_CONTAINER,
        STOP_CONTAINER,
        RESTART_CONTAINER,
        PULL_IMAGE,
    }
    assert not any(
        w in " ".join(reg.names()) for w in ("rm", "remove", "prune", "volume", "kill")
    )


def test_prepare_runs_nothing_changing() -> None:
    reg, fake = make()
    op = prepared(reg, START_CONTAINER, {"container": "web"})
    assert fake.containers["web"] == "exited"
    assert {c[1] for c in fake.calls} <= {"version", "inspect"}
    assert op.commands == [f"{DOCKER} start -- web"]
    assert op.risk.value == "SYSTEM_CHANGE" and op.requirement.value == "confirm"


# -- positive and negative verification for each operation -------------------------


def test_start_positive() -> None:
    reg, _ = make()
    op = prepared(reg, START_CONTAINER, {"container": "web"})
    assert op.perform()["ok"]
    assert verify(op).outcome is Outcome.PASSED


def test_start_negative_container_exits_after_start() -> None:
    reg, fake = make()
    fake.start_dies = True
    op = prepared(reg, START_CONTAINER, {"container": "web"})
    assert op.perform()["ok"]  # docker said OK...
    report = verify(op)  # ...but it is not running
    assert report.outcome is Outcome.FAILED
    assert report.recovery and "docker stop web" in report.recovery


def test_stop_positive_and_negative() -> None:
    reg, fake = make()
    op = prepared(reg, STOP_CONTAINER, {"container": "db"})
    assert verify(op).outcome is not Outcome.PASSED  # before stopping
    op.perform()
    assert verify(op).outcome is Outcome.PASSED
    assert "db" in fake.containers  # stopped, not removed


def test_restart_positive_and_negative() -> None:
    reg, fake = make()
    op = prepared(reg, RESTART_CONTAINER, {"container": "db"})
    assert not op.no_change_needed  # restart always does something
    op.perform()
    assert verify(op).outcome is Outcome.PASSED
    fake.start_dies = True
    op2 = prepared(reg, RESTART_CONTAINER, {"container": "db"})
    op2.perform()
    assert verify(op2).outcome is Outcome.FAILED


def test_pull_positive_and_negative() -> None:
    reg, _ = make()
    op = prepared(reg, PULL_IMAGE, {"image": "redis:7"})
    assert verify(op).outcome is Outcome.FAILED  # not pulled yet
    op.perform()
    assert verify(op).outcome is Outcome.PASSED
    assert "does not delete images" in op.recovery


# -- refusals and no-change ---------------------------------------------------------


def test_already_running_needs_no_change() -> None:
    reg, _ = make()
    op = prepared(reg, START_CONTAINER, {"container": "db"})
    assert op.no_change_needed and op.commands == []


def test_unknown_container_refused() -> None:
    reg, _ = make()
    op = prepared(reg, START_CONTAINER, {"container": "nope"})
    assert op.refused and op.requirement.value == "blocked"
    assert verify(op).outcome is Outcome.BLOCKED


def test_daemon_down_refuses_everything() -> None:
    reg, fake = make()
    fake.daemon = False
    for tool, args in [
        (START_CONTAINER, {"container": "web"}),
        (PULL_IMAGE, {"image": "x"}),
    ]:
        op = prepared(reg, tool, args)
        assert (
            op.refused and "daemon" in op.refused.lower() or "unreachable" in op.refused
        )


@pytest.mark.parametrize("name", ["-d", "--rm", "a b", "$(id)", "x;y", ""])
def test_hostile_names_rejected(name: str) -> None:
    reg, fake = make()
    assert isinstance(reg.prepare(START_CONTAINER, {"container": name}), dict)
    assert not any(c[1] == "start" for c in fake.calls)


@pytest.mark.parametrize(
    "ref", ["nginx", "nginx:1.25", "library/nginx:latest", "ghcr.io/o/r:v1",
            "localhost:5000/x", "r@sha256:" + "a" * 64],
)  # fmt: skip
def test_valid_images(ref: str) -> None:
    assert validate_image(ref) == ref


@pytest.mark.parametrize(
    "ref", ["--help", "Nginx", "a b", "x:$(id)", "-q", "x" * 300, "a//b"]
)
def test_invalid_images(ref: str) -> None:
    with pytest.raises(DockerNameError):
        validate_image(ref)


def test_failure_reports_status_and_verification_fails() -> None:
    reg, fake = make()
    fake.fail_with = "permission denied while trying to connect"
    op = prepared(reg, START_CONTAINER, {"container": "web"})
    result = op.perform()
    assert result["ok"] is False and result["status"] == "permission_denied"
    assert verify(op).outcome is Outcome.FAILED


# -- end to end ---------------------------------------------------------------------


def run_flow(tmp_path: Path, answer: str) -> tuple[dict[str, Any], StatefulDocker]:
    reg, fake = make()
    sdk = FakeSDK(completion(None, [tool_call(START_CONTAINER, {"container": "web"})]),
                  completion("ok"))  # fmt: skip
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 8, sdk)
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    approvals = ApprovalService(Prompt(answer))
    journal = TaskJournal(tmp_path / "t.jsonl")
    Orchestrator(client, reg, runner, approvals=approvals,
                 executor=ChangeExecutor(runner, approvals, journal)).ask("start web")  # fmt: skip
    msgs = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    return json.loads(msgs[-1]["content"]), fake


def test_approved_start_end_to_end(tmp_path: Path) -> None:
    data, fake = run_flow(tmp_path, "y")
    assert data["status"] == "completed" and data["verification"]["outcome"] == "PASSED"
    assert fake.containers["web"] == "running"


def test_rejected_start_changes_nothing(tmp_path: Path) -> None:
    data, fake = run_flow(tmp_path, "n")
    assert data["status"] == "rejected" and fake.containers["web"] == "exited"
    assert not any(c[1] == "start" for c in fake.calls)
