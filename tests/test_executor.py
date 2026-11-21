import json
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from agent.approval import Approval, ApprovalService
from agent.command_plan import CommandPlan, parse_plan
from agent.executor import ChangeExecutor
from agent.llm_client import EXECUTE_COMMAND_PLAN, LLMClient
from agent.orchestrator import Orchestrator
from agent.planner import ActionPlan, build_action_plan
from agent.policy import Requirement
from agent.task_store import JournalError, TaskJournal, new_task_id
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from tests.test_llm_client import FakeSDK, completion, tool_call
from tests.test_policy import CONTEXT, Prompt, raw
from tools.system import build_registry

SECRET = "sk-journal-secret-key-123456"


class Env:
    def __init__(self, tmp_path: Path, *answers: Any, ttl: float = 300) -> None:
        self.tmp = tmp_path
        self.now = [1000.0]
        self.prompt = Prompt(*answers)
        self.approvals = ApprovalService(
            self.prompt, clock=lambda: self.now[0], ttl=ttl
        )
        self.runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
        self.journal = TaskJournal(tmp_path / "logs" / "tasks.jsonl", secrets=[SECRET])
        self.executor = ChangeExecutor(self.runner, self.approvals, self.journal)

    def plan(self, data: dict[str, Any]) -> tuple[CommandPlan, ActionPlan]:
        plan, errors = parse_plan(data)
        assert plan is not None, errors
        action = build_action_plan(
            plan, self.runner.preview_steps(plan, gate=False), str(self.tmp)
        )
        return plan, action

    def approve_and_run(self, data: dict[str, Any], **kw: Any) -> Any:
        plan, action = self.plan(data)
        approval = self.approvals.request(action, "preview")
        return self.executor.execute(plan, action, approval, **kw)


def mk(path: Path) -> tuple[str, str, list[str]]:
    return ("a", "mkdir", [str(path)])


# -- approved and rejected ----------------------------------------------------------


def test_approved_action_executes(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    target = tmp_path / "made"
    outcome = env.approve_and_run(raw(mk(target)))
    assert outcome.status == "completed" and target.is_dir()
    assert outcome.plan_result is not None and outcome.executed_anything


def test_rejected_action_does_not_execute(tmp_path: Path) -> None:
    env = Env(tmp_path, "n")
    target = tmp_path / "made"
    outcome = env.approve_and_run(raw(mk(target)))
    assert outcome.status == "rejected" and not target.exists()
    assert not outcome.executed_anything


def test_missing_approval_object_blocks_execution(tmp_path: Path) -> None:
    env = Env(tmp_path)
    plan, action = env.plan(raw(mk(tmp_path / "made")))
    outcome = env.executor.execute(plan, action, None)
    assert outcome.status == "rejected" and not (tmp_path / "made").exists()


def test_expired_approval_is_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, "y", ttl=60)
    plan, action = env.plan(raw(mk(tmp_path / "made")))
    approval = env.approvals.request(action, "x")
    env.now[0] += 120
    outcome = env.executor.execute(plan, action, approval)
    assert outcome.status == "rejected" and "expired" in " ".join(outcome.problems)
    assert not (tmp_path / "made").exists()


def test_approval_for_a_different_plan_is_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    _plan_a, action_a = env.plan(raw(mk(tmp_path / "a")))
    approval = env.approvals.request(action_a, "x")
    plan_b, action_b = env.plan(raw(("a", "mkdir", [str(tmp_path / "other")])))
    outcome = env.executor.execute(plan_b, action_b, approval)
    assert outcome.status == "rejected" and not (tmp_path / "other").exists()
    assert not (tmp_path / "a").exists()


def test_arguments_changed_after_approval_are_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    _plan, action = env.plan(raw(mk(tmp_path / "approved")))
    approval = env.approvals.request(action, "x")
    tampered, _ = parse_plan(raw(("a", "mkdir", [str(tmp_path / "tampered")])))
    assert tampered is not None
    outcome = env.executor.execute(tampered, action, approval)
    assert outcome.status == "rejected"
    assert "changed after it was approved" in " ".join(outcome.problems)
    assert not (tmp_path / "tampered").exists() and not (tmp_path / "approved").exists()


def test_risk_escalated_after_approval_is_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    _plan, action = env.plan(raw(mk(tmp_path / "x")))
    approval = env.approvals.request(action, "x")
    escalated, _ = parse_plan(raw(("a", "rm", ["-rf", str(tmp_path)])))
    assert escalated is not None
    outcome = env.executor.execute(escalated, action, approval)
    assert outcome.status == "rejected" and tmp_path.exists()


def test_destructive_plan_runs_only_with_typed_phrase(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    data = raw(("a", "rm", ["-r", str(victim)]))
    env = Env(tmp_path, "yes")
    assert env.approve_and_run(data).status == "rejected" and victim.exists()
    _plan, action = env.plan(data)
    env2 = Env(tmp_path, action.confirmation_phrase())
    outcome = env2.approve_and_run(data)
    assert outcome.status == "completed" and not victim.exists()


def test_blocked_steps_never_run_even_with_approval(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    plan, action = env.plan(raw(mk(tmp_path / "x"), ("b", "bash", ["-c", "id"])))
    forged = Approval(action.fingerprint, env.now[0], action.requirement)
    outcome = env.executor.execute(plan, action, forged)
    assert outcome.status == "rejected" and not (tmp_path / "x").exists()


@pytest.mark.parametrize("exe", ["fdisk", "dd", "iptables", "useradd", "reboot"])
def test_never_approvable_commands_cannot_run(tmp_path: Path, exe: str) -> None:
    env = Env(tmp_path, "y")
    data = raw(("a", exe, []))
    plan, errors = parse_plan(data)
    assert plan is not None, errors
    action = build_action_plan(
        plan, env.runner.preview_steps(plan, gate=False), str(tmp_path)
    )
    forged = Approval(action.fingerprint, env.now[0], action.requirement)
    outcome = env.executor.execute(plan, action, forged)
    assert outcome.status == "rejected"


# -- capabilities -------------------------------------------------------------------


def test_unavailable_command_blocks_execution(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    outcome = env.approve_and_run(raw(("a", "no-such-binary-xyz", [])))
    assert outcome.status == "rejected" and not outcome.executed_anything


def test_capability_checked_against_discovered_host(tmp_path: Path) -> None:
    from tests.test_discovery import UBUNTU, fake_which, make_root

    root = make_root(tmp_path / "h", UBUNTU, systemd_running=False)
    host = discover(root=root, which=fake_which("systemctl"), euid=lambda: 1000,
                    uname=lambda: ("Linux", "6", "x86_64"))  # fmt: skip
    env = Env(tmp_path, "y")
    env.runner = CommandRunner.for_host(
        host, allowed_roots=[tmp_path], default_cwd=tmp_path
    )
    env.executor = ChangeExecutor(env.runner, env.approvals, env.journal)
    outcome = env.approve_and_run(raw(("a", "systemctl", ["restart", "ssh"])))
    assert outcome.status == "rejected"


# -- partial completion, cancellation, duplicates ---------------------------------------


def test_partial_failure_is_recorded(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    first = tmp_path / "first"
    outcome = env.approve_and_run(
        raw(
            ("a", "mkdir", [str(first)]),
            ("b", "mkdir", [str(tmp_path / "no" / "parent" / "x")]),
            ("c", "mkdir", [str(tmp_path / "third")]),
        )
    )
    assert outcome.status == "partial" and first.is_dir()
    assert not (tmp_path / "third").exists()
    events = env.journal.task(outcome.task_id)
    finished = next(e for e in events if e["event"] == "task_finished")
    assert finished["outcome"] == "partial"
    statuses = {
        e["step_id"]: e["status"] for e in events if e["event"] == "step_finished"
    }
    assert statuses == {"a": "ok", "b": "failed"}
    assert "partly changed" in outcome.message


def test_first_step_failure_is_failed_not_partial(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    outcome = env.approve_and_run(raw(("a", "mkdir", [str(tmp_path / "no" / "x")])))
    assert outcome.status == "failed"


def test_cancellation_prevents_subsequent_operations(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    cancel = threading.Event()
    seen: list[str] = []

    def on_step(result: Any) -> None:
        seen.append(result.step_id)
        cancel.set()  # cancel after the first step finishes

    outcome = env.approve_and_run(
        raw(("a", "mkdir", [str(tmp_path / "one")]), ("b", "mkdir", [str(tmp_path / "two")])),
        cancel=cancel, on_step=on_step,
    )  # fmt: skip
    assert outcome.status == "cancelled"
    assert (tmp_path / "one").is_dir() and not (tmp_path / "two").exists()
    assert seen == ["a"]


def test_repeated_task_is_not_run_twice(tmp_path: Path) -> None:
    env = Env(tmp_path, "y", "y")
    data = raw(mk(tmp_path / "made"))
    assert env.approve_and_run(data).status == "completed"
    second = env.approve_and_run(data)
    assert second.status == "duplicate" and "already completed" in second.message
    assert not second.executed_anything


def test_repeat_allowed_only_when_explicit(tmp_path: Path) -> None:
    env = Env(tmp_path, "y", "y")
    data = raw(("a", "touch", [str(tmp_path / "f")]))
    assert env.approve_and_run(data).status == "completed"
    assert env.approve_and_run(data, allow_repeat=True).status == "completed"


def test_failed_task_may_be_retried(tmp_path: Path) -> None:
    env = Env(tmp_path, "y", "y")
    data = raw(("a", "mkdir", [str(tmp_path / "later" / "x")]))
    assert env.approve_and_run(data).status == "failed"
    (tmp_path / "later").mkdir()
    assert env.approve_and_run(data).status == "completed"


def test_concurrent_identical_runs_are_prevented(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    plan, action = env.plan(raw(("a", "touch", [str(tmp_path / "f")])))
    approval = env.approvals.request(action, "x")
    with env.executor._lock:
        env.executor._running.add(action.fingerprint)
    outcome = env.executor.execute(plan, action, approval, allow_repeat=True)
    assert outcome.status == "rejected" and "already running" in outcome.message


# -- the journal --------------------------------------------------------------------


def test_journal_records_the_whole_task(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    outcome = env.approve_and_run(raw(mk(tmp_path / "made")))
    events = env.journal.task(outcome.task_id)
    kinds = [e["event"] for e in events]
    assert kinds == ["task_started", "step_planned", "step_finished", "task_finished"]
    started = events[0]
    assert started["approved"] is True and started["requirement"] == "confirm"
    assert started["steps"][0]["command"].startswith("mkdir ")
    assert started["expected_end_state"] == CONTEXT["expected_end_state"]
    assert events[2]["exit_code"] == 0 and "time" in events[0]


def test_rejected_task_is_journalled_as_not_approved(tmp_path: Path) -> None:
    env = Env(tmp_path, "n")
    outcome = env.approve_and_run(raw(mk(tmp_path / "made")))
    events = env.journal.task(outcome.task_id)
    assert events[0]["approved"] is False
    assert events[-1]["outcome"] == "rejected"


def test_journal_is_append_only_and_private(tmp_path: Path) -> None:
    env = Env(tmp_path, "y", "y")
    env.approve_and_run(raw(mk(tmp_path / "a")))
    size = env.journal.path.stat().st_size
    env.approve_and_run(raw(("a", "mkdir", [str(tmp_path / "b")])))
    assert env.journal.path.stat().st_size > size
    assert stat.S_IMODE(env.journal.path.stat().st_mode) == 0o600
    assert len(list(env.journal.events())) >= 8


def test_journal_redacts_secrets(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    f = tmp_path / "f"
    f.write_text(f"password=hunter2hunter2 {SECRET}\n")
    plan = raw(
        ("a", "touch", [str(tmp_path / f"{SECRET}.txt")]), ("b", "cat", [str(f)])
    )
    env.approve_and_run(plan)
    text = env.journal.path.read_text()
    assert SECRET not in text and "hunter2" not in text


def test_torn_journal_line_is_ignored(tmp_path: Path) -> None:
    journal = TaskJournal(tmp_path / "j.jsonl")
    journal.record("t-1", "task_started")
    with journal.path.open("a") as handle:
        handle.write('{"event": "task_fin')
    assert [e["event"] for e in journal.events()] == ["task_started"]


def test_unwritable_journal_stops_execution(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    env.journal = TaskJournal(tmp_path / "locked" / "tasks.jsonl")
    (tmp_path / "locked").write_text("a file, not a directory")
    env.executor = ChangeExecutor(env.runner, env.approvals, env.journal)
    outcome = env.approve_and_run(raw(mk(tmp_path / "made")))
    assert outcome.status == "rejected" and not (tmp_path / "made").exists()
    with pytest.raises(JournalError):
        env.journal.record("t", "x")


def test_task_ids_are_unique() -> None:
    assert len({new_task_id() for _ in range(500)}) == 500


def test_interrupted_run_leaves_planned_but_unfinished_record(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    plan, action = env.plan(raw(("a", "touch", [str(tmp_path / "f")])))
    approval = env.approvals.request(action, "x")

    def explode(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt

    env.runner.run_plan = explode  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        env.executor.execute(plan, action, approval)
    events = list(env.journal.events())
    assert [e["event"] for e in events][:2] == ["task_started", "step_planned"]
    assert events[-1]["outcome"] == "cancelled"


# -- through the orchestrator ----------------------------------------------------------


def orchestrator(env: Env, *script: Any) -> tuple[Orchestrator, FakeSDK]:
    sdk = FakeSDK(*script)
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 6, sdk)
    return (
        Orchestrator(
            client, build_registry(discover(), env.runner), env.runner,
            approvals=env.approvals, executor=env.executor,
        ),
        sdk,
    )  # fmt: skip


def tool_payload(sdk: FakeSDK) -> dict[str, Any]:
    tools = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    return json.loads(tools[-1]["content"])


def test_orchestrator_runs_approved_change_end_to_end(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    orch, sdk = orchestrator(
        env,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(mk(tmp_path / "made")))]),
        completion("Created it."),
    )
    result = orch.ask("make a directory")
    data = tool_payload(sdk)
    assert data["status"] == "completed" and data["task_id"].startswith("t-")
    assert (tmp_path / "made").is_dir() and result.text == "Created it."
    assert result.events[0].ok


def test_orchestrator_rejection_runs_nothing_and_is_journalled(tmp_path: Path) -> None:
    env = Env(tmp_path, "n")
    orch, sdk = orchestrator(
        env,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, raw(mk(tmp_path / "made")))]),
        completion("ok"),
    )
    orch.ask("make a directory")
    assert (
        tool_payload(sdk)["status"] == "rejected" and not (tmp_path / "made").exists()
    )


def test_model_cannot_force_a_repeat(tmp_path: Path) -> None:
    env = Env(tmp_path, "y", "y")
    data = raw(("a", "touch", [str(tmp_path / "f")]))
    orch, sdk = orchestrator(
        env,
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, data, "1")]),
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, data, "2")]),
        completion("done"),
    )
    orch.ask("x")
    assert tool_payload(sdk)["status"] == "duplicate"


def test_output_of_changes_is_untrusted_evidence(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    orch, sdk = orchestrator(
        env,
        completion(
            None,
            [
                tool_call(
                    EXECUTE_COMMAND_PLAN, raw(("a", "touch", [str(tmp_path / "f")]))
                )
            ],
        ),
        completion("ok"),
    )
    orch.ask("x")
    assert "untrusted" in tool_payload(sdk)["note"]


def test_no_shell_is_used_for_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    seen: list[Any] = []
    real = subprocess.Popen

    def spy(argv: Any, **kw: Any) -> Any:
        seen.append((argv, kw))
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    env = Env(tmp_path, "y")
    env.approve_and_run(raw(("a", "mkdir", [str(tmp_path / "$(id);x")])))
    assert (tmp_path / "$(id);x").is_dir()  # the metacharacters stayed literal
    assert all(kw["shell"] is False and isinstance(a, list) for a, kw in seen)


def test_privileges_are_not_elevated(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    plan, action = env.plan(raw(("a", "sudo", ["true"])))
    forged = Approval(action.fingerprint, env.now[0], action.requirement)
    outcome = env.executor.execute(plan, action, forged)
    assert outcome.status == "rejected"
    assert not outcome.executed_anything
    assert action.requirement is Requirement.BLOCKED


def test_even_a_forged_typed_approval_cannot_run_sudo(tmp_path: Path) -> None:
    env = Env(tmp_path)
    plan, action = env.plan(raw(("a", "sudo", ["true"])))
    forged = Approval(action.fingerprint, env.now[0], Requirement.TYPED)
    assert env.executor.execute(plan, action, forged).status == "rejected"
