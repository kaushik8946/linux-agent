"""Commands the policy does not recognise ask the user instead of being
refused. The agent cannot ship a rule for every tool, so the user decides.

What must NOT change: shells, wrappers, privilege escalation, credential
access and the never-approvable list stay blocked whatever the user answers.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agent.approval import Approval, render_plan
from agent.command_policy import RiskClass, classify
from agent.llm_client import EXECUTE_COMMAND_PLAN
from agent.policy import Requirement
from tests.test_executor import Env, orchestrator, tool_payload
from tests.test_llm_client import completion, tool_call
from tests.test_policy import raw


def unk(tmp_path: Path, code: str = "print('hi')") -> dict[str, Any]:
    return raw(("a", "python3", ["-c", code]))


def test_python_is_unrecognised_not_read_only() -> None:
    assert classify("python3", ["-c", "1"]).risk is RiskClass.UNKNOWN


def test_unrecognised_plan_needs_confirmation_not_a_block(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(unk(tmp_path))
    assert action.requirement is Requirement.CONFIRM
    assert not action.blocked and action.has_unrecognised


def test_unrecognised_plan_needs_no_plan_context(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(unk(tmp_path))
    assert action.errors == []  # no "must describe its expected end state"


def test_known_change_still_needs_plan_context(tmp_path: Path) -> None:
    env = Env(tmp_path)
    data = raw(("a", "mkdir", [str(tmp_path / "d")]))
    data.pop("plan_context", None)
    _, action = env.plan(data)
    assert action.errors and action.has_known_change


def test_preview_warns_the_user(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(unk(tmp_path))
    text = render_plan(action)
    assert "NOT RECOGNISED" in text and "Approve only if you know" in text
    assert "BLOCKED" not in text


def test_approved_unrecognised_command_runs(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'w').write('x')"
    outcome = env.approve_and_run(unk(tmp_path, code))
    assert outcome.status == "completed" and marker.exists()


def test_rejected_unrecognised_command_does_not_run(tmp_path: Path) -> None:
    env = Env(tmp_path, "n")
    marker = tmp_path / "ran"
    outcome = env.approve_and_run(unk(tmp_path, f"open({str(marker)!r}, 'w')"))
    assert outcome.status == "rejected" and not marker.exists()


def test_default_answer_is_no(tmp_path: Path) -> None:
    env = Env(tmp_path, "")
    marker = tmp_path / "ran"
    outcome = env.approve_and_run(unk(tmp_path, f"open({str(marker)!r}, 'w')"))
    assert outcome.status == "rejected" and not marker.exists()


def test_changing_the_command_after_approval_invalidates_it(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    _, action = env.plan(unk(tmp_path, "print(1)"))
    approval = env.approvals.request(action, "preview")
    other, _ = env.plan(unk(tmp_path, f"open({str(tmp_path / 'evil')!r}, 'w')"))
    outcome = env.executor.execute(other, action, approval)
    assert outcome.status == "rejected" and not (tmp_path / "evil").exists()


def test_unapproved_unrecognised_command_never_runs_automatically(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    marker = tmp_path / "ran"
    plan, action = env.plan(unk(tmp_path, f"open({str(marker)!r}, 'w')"))
    assert env.executor.execute(plan, action, None).status == "rejected"
    assert not marker.exists()


def test_plain_runner_still_refuses_unrecognised_commands(tmp_path: Path) -> None:
    # Without an approval, the runner's own gate stays closed.
    env = Env(tmp_path)
    plan, _ = env.plan(unk(tmp_path))
    result = env.runner.run_plan(plan)
    assert not result.executed


def test_unrecognised_mixed_with_blocked_still_blocks_everything(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, "y")
    marker = tmp_path / "ran"
    data = raw(
        ("a", "python3", ["-c", f"open({str(marker)!r}, 'w')"]),
        ("b", "cat", ["/etc/shadow"]),
    )
    plan, action = env.plan(data)
    assert action.blocked
    forged = Approval(action.fingerprint, env.now[0], Requirement.OVERRIDE)
    assert env.executor.execute(plan, action, forged).status == "rejected"
    assert not marker.exists()


@pytest.mark.parametrize(
    "step",
    [
        ("a", "sudo", ["fdisk", "/dev/sda"]),
        ("a", "sudo", ["cat", "/etc/shadow"]),
        ("a", "sudo", []),
        ("a", "su", ["-c", "id"]),
        ("a", "cat", ["/etc/shadow"]),
        ("a", "fdisk", ["/dev/sda"]),
        ("a", "reboot", []),
    ],
)
def test_never_runnable_commands_are_still_blocked(
    tmp_path: Path, step: tuple[str, str, list[str]]
) -> None:
    env = Env(tmp_path, "y")
    plan, action = env.plan(raw(step))
    assert action.blocked
    for level in (Requirement.TYPED, Requirement.OVERRIDE):
        forged = Approval(action.fingerprint, env.now[0], level)
        assert env.executor.execute(plan, action, forged).status == "rejected"


def test_unrecognised_command_output_is_untrusted_and_journalled(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, "y")
    outcome = env.approve_and_run(unk(tmp_path))
    events = env.journal.task(outcome.task_id)
    assert events[0]["steps"][0]["risk"] == "UNKNOWN" and events[0]["approved"] is True


def test_orchestrator_asks_then_runs_an_unrecognised_command(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    orch, sdk = orchestrator(
        env,
        completion(
            None, [tool_call(EXECUTE_COMMAND_PLAN, unk(tmp_path, "print('hi')"))]
        ),
        completion("done"),
    )
    orch.ask("run it")
    data = tool_payload(sdk)
    assert data["status"] == "completed"
    assert env.prompt.questions and "NOT RECOGNISED" in env.prompt.questions[0]


def test_orchestrator_rejection_tells_the_model_not_to_retry(tmp_path: Path) -> None:
    env = Env(tmp_path, "n")
    orch, sdk = orchestrator(
        env,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, unk(tmp_path))]),
        completion("ok"),
    )
    orch.ask("run it")
    data = tool_payload(sdk)
    assert data["status"] == "rejected" and "Do not retry" in data["message"]
    assert json.dumps(data).count("python3") >= 1
