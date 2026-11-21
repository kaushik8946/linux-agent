import json
from pathlib import Path
from typing import Any

import pytest

from agent.approval import Approval, ApprovalService, render_plan
from agent.command_plan import parse_plan
from agent.command_policy import RiskClass
from agent.llm_client import EXECUTE_COMMAND_PLAN, LLMClient
from agent.orchestrator import Orchestrator
from agent.planner import ActionPlan, build_action_plan, fingerprint
from agent.policy import (
    NEVER_APPROVABLE,
    Requirement,
    command_decision,
    decide,
    strictest,
)
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from tests.test_llm_client import FakeSDK, completion, tool_call
from tools.system import build_registry

CONTEXT = {
    "expected_end_state": "directory exists",
    "baseline": "directory absent",
    "affected_resources": ["/tmp/x"],
    "verification": ["test -d /tmp/x"],
    "recovery": "rmdir /tmp/x",
}


def raw(*steps: tuple[str, str, list[str]], context: Any = CONTEXT) -> dict[str, Any]:
    data: dict[str, Any] = {
        "steps": [
            {"id": sid, "purpose": "p", "executable": exe, "args": args}
            for sid, exe, args in steps
        ]
    }
    if context is not None:
        data["plan_context"] = context
    return data


def action(tmp_path: Path, data: dict[str, Any]) -> ActionPlan:
    plan, errors = parse_plan(data)
    assert plan is not None, errors
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    return build_action_plan(
        plan, runner.preview_steps(plan, gate=False), str(tmp_path)
    )


class Prompt:
    """Records prompts and answers from a script, like a user at a terminal."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException) or (
            isinstance(answer, type) and issubclass(answer, BaseException)
        ):
            raise answer
        return str(answer)


# -- risk policy ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("risk", "requirement"),
    [
        (RiskClass.READ_ONLY, Requirement.AUTO),
        (RiskClass.USER_LEVEL_CHANGE, Requirement.CONFIRM),
        (RiskClass.SYSTEM_CHANGE, Requirement.CONFIRM),
        (RiskClass.DESTRUCTIVE, Requirement.TYPED),
        (RiskClass.PRIVILEGED, Requirement.BLOCKED),
        (RiskClass.UNKNOWN, Requirement.CONFIRM),
        (RiskClass.UNSUPPORTED, Requirement.BLOCKED),
        (RiskClass.SENSITIVE, Requirement.BLOCKED),
    ],
)
def test_every_risk_class_has_a_requirement(
    risk: RiskClass, requirement: Requirement
) -> None:
    assert decide(risk).requirement is requirement


def test_unknown_risk_classification_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown risk"):
        decide("ROOT_EVERYTHING")  # type: ignore[arg-type]


def test_strictest_requirement_wins() -> None:
    assert (
        strictest([Requirement.AUTO, Requirement.TYPED, Requirement.CONFIRM])
        is Requirement.TYPED
    )
    assert strictest([]) is Requirement.AUTO
    assert strictest([Requirement.CONFIRM, Requirement.BLOCKED]) is Requirement.BLOCKED


@pytest.mark.parametrize("name", sorted(NEVER_APPROVABLE) + ["mkfs.ext4"])
def test_never_approvable_commands_are_blocked(name: str) -> None:
    assert (
        command_decision(name, RiskClass.SYSTEM_CHANGE).requirement
        is Requirement.BLOCKED
    )


# -- action plans -------------------------------------------------------------------


def test_read_only_plan_is_not_modifying(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "uname", ["-a"]), context=None))
    assert not plan.is_modifying and plan.requirement is Requirement.AUTO
    assert plan.errors == []


def test_modifying_plan_requires_confirmation(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", [str(tmp_path / "x")])))
    assert plan.is_modifying and plan.requirement is Requirement.CONFIRM


def test_destructive_plan_requires_typed_confirmation(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", ["x"]), ("b", "rm", ["-rf", "x"])))
    assert plan.requirement is Requirement.TYPED


def test_blocked_step_blocks_the_whole_plan(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", ["x"]), ("b", "bash", ["-c", "id"])))
    assert plan.blocked


def test_unresolvable_executable_blocks_the_plan(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "no-such-binary-xyz", [])))
    assert plan.blocked and plan.steps[0].blocked_reason


def test_modifying_plan_without_context_is_invalid(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", ["x"]), context=None))
    assert any("expected end state" in e for e in plan.errors)


def test_plan_records_everything_the_user_needs(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", [str(tmp_path / "x")])))
    step = plan.steps[0]
    assert step.command == f"mkdir {tmp_path / 'x'}"
    assert step.working_dir == str(tmp_path)
    assert step.decision.risk is RiskClass.USER_LEVEL_CHANGE
    text = render_plan(plan)
    for item in ("mkdir", "USER_LEVEL_CHANGE", "confirm", "directory exists",
                 "directory absent", "/tmp/x", "test -d /tmp/x", "rmdir /tmp/x"):  # fmt: skip
        assert item in text


@pytest.mark.parametrize(
    "bad_context",
    [
        {},
        {"expected_end_state": ""},
        {"expected_end_state": "x", "approved": True},
        {"expected_end_state": "x", "affected_resources": "not a list"},
        "just text",
    ],
)
def test_invalid_plan_context_rejected(bad_context: Any) -> None:
    plan, errors = parse_plan(raw(("a", "mkdir", ["x"]), context=bad_context))
    assert plan is None and errors


def test_changed_arguments_change_the_fingerprint() -> None:
    a, _ = parse_plan(raw(("a", "mkdir", ["x"])))
    b, _ = parse_plan(raw(("a", "mkdir", ["y"])))
    c, _ = parse_plan(raw(("a", "mkdir", ["x"])))
    assert a and b and c
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) == fingerprint(c)


def test_fingerprint_ignores_descriptive_text() -> None:
    a, _ = parse_plan(raw(("a", "mkdir", ["x"])))
    data = raw(("a", "mkdir", ["x"]))
    data["steps"][0]["purpose"] = "different wording"
    b, _ = parse_plan(data)
    assert a and b and fingerprint(a) == fingerprint(b)


# -- approval prompts --------------------------------------------------------------


def test_read_only_plan_does_not_prompt(tmp_path: Path) -> None:
    prompt = Prompt()
    plan = action(tmp_path, raw(("a", "uname", []), context=None))
    assert ApprovalService(prompt).request(plan, "x") is not None
    assert prompt.questions == []


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_yes_approves(tmp_path: Path, answer: str) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", ["x"])))
    assert ApprovalService(Prompt(answer)).request(plan, "x") is not None


@pytest.mark.parametrize("answer", ["", "n", "no", "nope", "sure", "yes please", "1"])
def test_enter_or_anything_else_means_no(tmp_path: Path, answer: str) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", ["x"])))
    prompt = Prompt(answer)
    assert ApprovalService(prompt).request(plan, "x") is None
    assert "[y/N]" in prompt.questions[0]


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt, OSError])
def test_interrupted_prompt_means_no(tmp_path: Path, error: type) -> None:
    plan = action(tmp_path, raw(("a", "mkdir", ["x"])))
    assert ApprovalService(Prompt(error)).request(plan, "x") is None


def test_destructive_requires_exact_typed_phrase(tmp_path: Path) -> None:
    plan = action(tmp_path, raw(("a", "rm", ["-rf", "x"])))
    phrase = plan.confirmation_phrase()
    assert ApprovalService(Prompt("yes")).request(plan, "x") is None
    assert ApprovalService(Prompt("y")).request(plan, "x") is None
    assert ApprovalService(Prompt(phrase.upper())).request(plan, "x") is None
    prompt = Prompt(phrase)
    assert ApprovalService(prompt).request(plan, "x") is not None
    assert phrase in prompt.questions[0]


def test_blocked_plan_never_prompts(tmp_path: Path) -> None:
    prompt = Prompt("y")
    plan = action(tmp_path, raw(("a", "bash", ["-c", "id"])))
    assert ApprovalService(prompt).request(plan, "x") is None
    assert prompt.questions == []


def test_approval_is_bound_to_the_exact_plan(tmp_path: Path) -> None:
    svc = ApprovalService(Prompt("y"))
    plan = action(tmp_path, raw(("a", "mkdir", ["x"])))
    other = action(tmp_path, raw(("a", "mkdir", ["y"])))
    approval = svc.request(plan, "x")
    assert svc.is_valid(approval, plan)
    assert not svc.is_valid(approval, other)
    assert not svc.is_valid(None, plan)


def test_approval_expires(tmp_path: Path) -> None:
    now = [100.0]
    svc = ApprovalService(Prompt("y"), clock=lambda: now[0], ttl=60)
    plan = action(tmp_path, raw(("a", "mkdir", ["x"])))
    approval = svc.request(plan, "x")
    now[0] = 150.0
    assert svc.is_valid(approval, plan)
    now[0] = 161.0
    assert not svc.is_valid(approval, plan)


def test_forged_approval_with_wrong_requirement_is_invalid(tmp_path: Path) -> None:
    svc = ApprovalService(Prompt(), clock=lambda: 0.0)
    plan = action(tmp_path, raw(("a", "rm", ["-rf", "x"])))
    forged = Approval(plan.fingerprint, 0.0, Requirement.CONFIRM)
    assert not svc.is_valid(forged, plan)


# -- orchestrator integration ---------------------------------------------------------


def make(
    tmp_path: Path, prompt: Prompt | None, *script: Any
) -> tuple[Orchestrator, FakeSDK]:
    sdk = FakeSDK(*script)
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 6, sdk)
    host = discover()
    runner = CommandRunner.for_host(
        host, allowed_roots=[tmp_path], default_cwd=tmp_path
    )
    approvals = ApprovalService(prompt) if prompt is not None else None
    return Orchestrator(
        client, build_registry(host, runner), runner, approvals=approvals
    ), sdk


def payload(sdk: FakeSDK) -> dict[str, Any]:
    tools = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    return json.loads(tools[-1]["content"])


def test_model_approval_flag_cannot_authorise_anything(tmp_path: Path) -> None:
    data = raw(("a", "mkdir", [str(tmp_path / "x")]))
    data["approved"] = True
    orch, sdk = make(
        tmp_path,
        Prompt(),
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, data)]),
        completion("ok"),
    )
    orch.ask("make x")
    assert payload(sdk)["error"] == "invalid command plan"
    assert not (tmp_path / "x").exists()


def test_user_rejection_is_reported_and_nothing_runs(tmp_path: Path) -> None:
    prompt = Prompt("n")
    orch, sdk = make(
        tmp_path, prompt,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(("a", "mkdir", [str(tmp_path / "x")])))]),
        completion("ok"),
    )  # fmt: skip
    orch.ask("make x")
    data = payload(sdk)
    assert data["status"] == "rejected" and "Do not retry" in data["message"]
    assert len(prompt.questions) == 1
    assert not (tmp_path / "x").exists()


def test_approved_change_is_still_not_executed_in_this_build(tmp_path: Path) -> None:
    orch, sdk = make(
        tmp_path, Prompt("y"),
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(("a", "mkdir", [str(tmp_path / "x")])))]),
        completion("ok"),
    )  # fmt: skip
    orch.ask("make x")
    assert payload(sdk)["status"] == "approved_not_executed"
    assert not (tmp_path / "x").exists()


def test_read_only_plans_still_run_without_prompting(tmp_path: Path) -> None:
    prompt = Prompt()
    orch, sdk = make(
        tmp_path, prompt,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(("a", "echo", ["hi"]), context=None))]),
        completion("ok"),
    )  # fmt: skip
    orch.ask("x")
    assert payload(sdk)["results"][0]["stdout"] == "hi\n"
    assert prompt.questions == []


def test_no_approval_channel_refuses_changes(tmp_path: Path) -> None:
    orch, sdk = make(
        tmp_path, None,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(("a", "mkdir", ["x"])))]),
        completion("ok"),
    )  # fmt: skip
    orch.ask("x")
    assert payload(sdk)["status"] == "approval_unavailable"


def test_blocked_plan_is_refused_without_asking(tmp_path: Path) -> None:
    prompt = Prompt()
    orch, sdk = make(
        tmp_path, prompt,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(("a", "mkdir", ["x"]), ("b", "dd", ["if=/dev/zero"])))]),
        completion("ok"),
    )  # fmt: skip
    orch.ask("x")
    assert payload(sdk)["status"] == "blocked" and prompt.questions == []
