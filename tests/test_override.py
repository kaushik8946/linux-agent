"""Per-command user override for sudo and shells.

The rules under test:

* sudo/doas/run0/pkexec and shells/wrappers are refused unless the user
  types an exact override phrase for that exact plan.
* "y", "yes", Enter, Ctrl+C or a near-miss phrase never runs anything.
* Credential access, the never-approvable list and dangerous sudo forms stay
  blocked even with a forged override approval.
* The model is never told an override exists: a declined override returns
  the same payload as the old fixed refusal, and the prompt says nothing.
* A privileged step keeps the terminal (so sudo can ask for a password);
  every other step stays detached with no stdin.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent.approval import Approval, render_plan
from agent.command_policy import RiskClass, classify
from agent.llm_client import EXECUTE_COMMAND_PLAN
from agent.policy import Requirement, command_decision
from agent.prompts import SYSTEM_PROMPT
from hostplatform.command_runner import CommandRunner
from tests.test_executor import Env, orchestrator, tool_payload
from tests.test_llm_client import completion, tool_call
from tests.test_policy import raw

SHELL = raw(("a", "bash", ["-c", "echo hi"]))
SUDO = raw(("a", "sudo", ["-n", "true"]))


def decision(name: str, *args: str) -> Any:
    return command_decision(name, classify(name, list(args)).risk, list(args))


# -- policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("sudo", ["true"]),
        ("sudo", ["apt-get", "update"]),
        ("sudo", ["-u", "root", "id"]),
        ("sudo", ["-n", "--", "systemctl", "restart", "x"]),
        ("doas", ["id"]),
        ("run0", ["id"]),
        ("pkexec", ["id"]),
        ("bash", ["-c", "echo hi"]),
        ("sh", ["-c", "echo hi"]),
        ("env", ["FOO=1", "id"]),
        ("xargs", ["echo"]),
        ("timeout", ["5", "id"]),
        ("ssh", ["host", "uptime"]),
        ("bash", ["-c", "ls -la /tmp && df -h"]),
        ("bash", ["-c", "unterminated 'quote"]),
        ("sudo", ["systemctl", "restart", "nginx"]),
    ],
)
def test_overridable_commands(name: str, args: list[str]) -> None:
    assert decision(name, *args).requirement is Requirement.OVERRIDE


@pytest.mark.parametrize(
    ("name", "args"),
    [
        # never-approvable, directly or through sudo
        ("sudo", ["fdisk", "/dev/sda"]),
        ("sudo", ["/usr/sbin/mkfs.ext4", "/dev/sdb1"]),
        ("sudo", ["-u", "root", "reboot"]),
        ("sudo", ["iptables", "-F"]),
        ("sudo", ["useradd", "eve"]),
        ("sudo", ["dd", "if=/dev/zero", "of=/dev/sda"]),
        ("doas", ["shutdown", "now"]),
        ("pkexec", ["passwd", "root"]),
        ("reboot", []),
        ("iptables", ["-F"]),
        # credentials, directly or through sudo
        ("sudo", ["cat", "/etc/shadow"]),
        ("sudo", ["cp", "/root/.ssh/id_rsa", "/tmp/k"]),
        ("cat", ["/etc/shadow"]),
        # root shells and password/env tricks
        ("sudo", []),
        ("sudo", ["-i"]),
        ("sudo", ["-s"]),
        ("sudo", ["--login"]),
        ("sudo", ["-S", "true"]),
        ("sudo", ["--stdin", "true"]),
        ("sudo", ["-A", "true"]),
        ("sudo", ["-E", "true"]),
        ("sudo", ["--preserve-env=PATH", "true"]),
        ("sudo", ["-nE", "true"]),
        ("sudo", ["-e", "/etc/hosts"]),
        ("sudo", ["-u", "root"]),
        ("su", []),
        ("su", ["-c", "id"]),
        ("pkexec", []),
        # nested elevation or shells under sudo
        ("sudo", ["sudo", "id"]),
        ("sudo", ["su"]),
        ("sudo", ["bash", "-c", "id"]),
        ("sudo", ["env", "id"]),
        ("sudo", ["timeout", "5", "fdisk"]),
        ("sudo", ["sudoedit", "/etc/hosts"]),
        ("sudoedit", ["/etc/hosts"]),
        ("sudo", ["-uroot", "reboot"]),
        ("sudo", ["--user=root", "reboot"]),
        ("sudo", ["--prompt=x", "fdisk"]),
        ("sudo", ["-sn", "true"]),
        ("sudo", ["-l"]),
        ("sudo", ["visudo"]),
        ("sudo", ["tee", "/etc/sudoers.d/x"]),
        ("pkexec", ["/bin/sh"]),
        ("doas", ["-s"]),
        ("run0", []),
        # wrappers and shell scripts that name a forbidden command
        ("env", ["sudo", "reboot"]),
        ("xargs", ["fdisk"]),
        ("timeout", ["5", "reboot"]),
        ("nice", ["/sbin/mkfs.ext4", "/dev/x"]),
        ("bash", ["-c", "sudo reboot"]),
        ("bash", ["-c", "echo x; /sbin/reboot"]),
        ("bash", ["-c", "ls && iptables -F"]),
        ("bash", ["-c", "cat ~/.ssh/id_rsa"]),
        ("sh", ["-c", "$(sudo id)"]),
        ("sh", ["-c", "`doas id`"]),
        ("bash", ["-c", "true\nreboot"]),
    ],
)
def test_never_runnable_even_with_override(name: str, args: list[str]) -> None:
    assert decision(name, *args).requirement is Requirement.BLOCKED


def test_override_sits_between_typed_and_blocked(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, mixed = env.plan(raw(("a", "rm", ["-rf", str(tmp_path / "x")]),
                            ("b", "bash", ["-c", "id"])))  # fmt: skip
    assert mixed.requirement is Requirement.OVERRIDE
    _, worse = env.plan(raw(("a", "bash", ["-c", "id"]), ("b", "cat", ["/etc/shadow"])))
    assert worse.requirement is Requirement.BLOCKED


# -- prompt and phrase -------------------------------------------------------------


def test_preview_explains_the_override(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(SUDO)
    text = render_plan(action)
    assert "NORMALLY BLOCKED" in text and "typing the phrase" in text
    assert "never sees it" in text


def test_phrase_names_the_command_and_plan(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(SHELL)
    assert action.confirmation_phrase() == f"override bash {action.fingerprint[:8]}"


@pytest.mark.parametrize(
    "answer", ["y", "Y", "yes", "YES", "", "override", "override bash", "OVERRIDE BASH {fp}",
               "override bash {fp}x", "override sh {fp}", "yes, run 1 step(s) {fp}"],
)  # fmt: skip
def test_anything_but_the_exact_phrase_runs_nothing(
    tmp_path: Path, answer: str
) -> None:
    marker = tmp_path / "ran"
    data = raw(("a", "bash", ["-c", f"touch {marker}"]))
    env = Env(tmp_path)
    _, action = env.plan(data)
    env.prompt.answers.append(answer.format(fp=action.fingerprint[:8]))
    outcome = env.approve_and_run(data)
    assert outcome.status == "rejected" and not marker.exists()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_interrupt_at_the_prompt_runs_nothing(tmp_path: Path, exc: type) -> None:
    marker = tmp_path / "ran"
    env = Env(tmp_path, exc)
    outcome = env.approve_and_run(raw(("a", "bash", ["-c", f"touch {marker}"])))
    assert outcome.status == "rejected" and not marker.exists()


def approve_with_phrase(env: Env, data: dict[str, Any]) -> Any:
    _, action = env.plan(data)
    env.prompt.answers.append("  " + action.confirmation_phrase() + "  ")
    return env.approve_and_run(data)


def test_exact_phrase_runs_the_shell_step(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    env = Env(tmp_path)
    outcome = approve_with_phrase(env, raw(("a", "bash", ["-c", f"touch {marker}"])))
    assert outcome.status == "completed" and marker.exists()


def test_override_is_journalled(tmp_path: Path) -> None:
    env = Env(tmp_path)
    outcome = approve_with_phrase(env, SHELL)
    first = env.journal.task(outcome.task_id)[0]
    assert first["override"] is True and first["approved"] is True
    assert first["requirement"] == "override"


def test_ordinary_change_is_not_journalled_as_override(tmp_path: Path) -> None:
    env = Env(tmp_path, "y")
    outcome = env.approve_and_run(raw(("a", "mkdir", [str(tmp_path / "d")])))
    assert env.journal.task(outcome.task_id)[0]["override"] is False


def test_override_is_bound_to_the_exact_plan(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(SHELL)
    env.prompt.answers.append(action.confirmation_phrase())
    approval = env.approvals.request(action, "x")
    assert approval is not None
    marker = tmp_path / "evil"
    other, _ = env.plan(raw(("a", "bash", ["-c", f"touch {marker}"])))
    assert env.executor.execute(other, action, approval).status == "rejected"
    assert not marker.exists()


def test_override_expires(tmp_path: Path) -> None:
    env = Env(tmp_path, ttl=60)
    plan, action = env.plan(SHELL)
    env.prompt.answers.append(action.confirmation_phrase())
    approval = env.approvals.request(action, "x")
    env.now[0] += 61
    assert env.executor.execute(plan, action, approval).status == "rejected"


@pytest.mark.parametrize(
    "level", [Requirement.CONFIRM, Requirement.TYPED, Requirement.AUTO]
)
def test_lower_approvals_cannot_unlock_an_override(
    tmp_path: Path, level: Requirement
) -> None:
    marker = tmp_path / "ran"
    env = Env(tmp_path)
    plan, action = env.plan(raw(("a", "bash", ["-c", f"touch {marker}"])))
    forged = Approval(action.fingerprint, env.now[0], level)
    assert env.executor.execute(plan, action, forged).status == "rejected"
    assert not marker.exists()


def test_forged_override_cannot_run_blocked_steps(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for data in (raw(("a", "sudo", ["fdisk", "/dev/sda"])), raw(("a", "cat", ["/etc/shadow"])),
                 raw(("a", "sudo", ["-S", "true"])), raw(("a", "reboot", []))):  # fmt: skip
        plan, action = env.plan(data)
        forged = Approval(action.fingerprint, env.now[0], Requirement.OVERRIDE)
        outcome = env.executor.execute(plan, action, forged)
        assert outcome.status == "rejected" and not outcome.executed_anything


def test_runner_alone_never_runs_override_steps(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    env = Env(tmp_path)
    plan, _ = env.plan(raw(("a", "bash", ["-c", f"touch {marker}"])))
    assert not env.runner.run_plan(plan).executed and not marker.exists()


# -- hidden from the model ------------------------------------------------------------


def test_system_prompt_does_not_mention_override() -> None:
    assert "override" not in SYSTEM_PROMPT.lower()


def test_tool_schema_does_not_mention_override(tmp_path: Path) -> None:
    env = Env(tmp_path)
    orch, _ = orchestrator(env)
    assert "override" not in json.dumps(orch.tools).lower()


def _ask(
    tmp_path: Path, answer: Any, data: dict[str, Any]
) -> tuple[dict[str, Any], Env]:
    env = Env(tmp_path)
    if answer is not None:
        env.prompt.answers.append(answer)
    orch, sdk = orchestrator(
        env, completion(None, [tool_call(EXECUTE_COMMAND_PLAN, data)]), completion("ok")
    )
    orch.ask("do it")
    return tool_payload(sdk), env


def test_declined_override_looks_like_the_old_refusal(tmp_path: Path) -> None:
    payload, _ = _ask(tmp_path, "y", SUDO)
    assert payload["status"] == "blocked" and payload["executed"] is False
    assert payload["requirement"] == "blocked"
    assert payload["message"] == "This plan contains steps that are never run."
    step = payload["steps"][0]
    assert step["requirement"] == "blocked"
    assert step["reason"] == (
        "privilege escalation is not run from a command plan; it needs a "
        "dedicated reviewed workflow"
    )
    assert "override" not in json.dumps(payload).lower()


def test_declined_shell_override_looks_like_the_old_refusal(tmp_path: Path) -> None:
    payload, _ = _ask(tmp_path, "", SHELL)
    assert payload["status"] == "blocked"
    assert payload["steps"][0]["reason"] == "shells and command wrappers are never run"
    assert "override" not in json.dumps(payload).lower()


def test_accepted_override_result_does_not_mention_override(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _, action = env.plan(SHELL)
    payload, _ = _ask(tmp_path, action.confirmation_phrase(), SHELL)
    assert payload["status"] == "completed"
    assert "override" not in json.dumps(payload).lower()


def test_user_sees_the_override_prompt(tmp_path: Path) -> None:
    _, env = _ask(tmp_path, "n", SHELL)
    assert (
        env.prompt.questions
        and "Type exactly 'override bash" in env.prompt.questions[0]
    )


# -- terminal handling ------------------------------------------------------------------


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    real = subprocess.Popen

    def spy(argv: Any, **kw: Any) -> Any:
        seen.append({"argv": argv, **kw})
        return real(["true"], **kw)  # never actually run sudo

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def test_privileged_step_keeps_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch)
    calls: list[bool] = []
    env = Env(tmp_path)
    env.runner.on_terminal = calls.append
    outcome = approve_with_phrase(env, SUDO)
    assert outcome.status == "completed"
    kw = seen[-1]
    assert kw["argv"][0].endswith("sudo") and kw["shell"] is False
    assert kw["stdin"] is None and kw["start_new_session"] is False
    assert calls == [True, False]  # spinner paused, then resumed


def test_shell_step_stays_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch)
    env = Env(tmp_path)
    approve_with_phrase(env, SHELL)
    kw = seen[-1]
    assert kw["stdin"] is subprocess.DEVNULL and kw["start_new_session"] is True
    assert kw["shell"] is False and isinstance(kw["argv"], list)


def test_ordinary_steps_stay_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch)
    env = Env(tmp_path, "y")
    env.approve_and_run(raw(("a", "mkdir", [str(tmp_path / "d")])))
    assert seen[-1]["stdin"] is subprocess.DEVNULL and seen[-1]["start_new_session"]


def test_cancelling_a_privileged_step_never_signals_the_agents_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    groups: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: groups.append(pid))
    from hostplatform import command_runner as cr

    class FakeProc:
        pid = os.getpid()
        returncode = None

        def __init__(self) -> None:
            self.signals: list[int] = []

        def poll(self) -> None:
            return None

        def send_signal(self, sig: int) -> None:
            self.signals.append(sig)

    proc = FakeProc()
    cr._kill(proc, 15, group=False)  # type: ignore[arg-type]
    assert groups == [] and proc.signals == [15]


def test_classify_is_unchanged_for_wrappers() -> None:
    assert classify("sudo", ["id"]).risk is RiskClass.PRIVILEGED
    assert classify("bash", ["-c", "id"]).risk is RiskClass.UNSUPPORTED


def test_runner_default_has_no_terminal_hook(tmp_path: Path) -> None:
    assert (
        CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path).on_terminal
        is None
    )


def test_override_plan_needs_no_plan_context(tmp_path: Path) -> None:
    # The model is never told about overrides, so it cannot be required to
    # describe one; a plan without plan_context must still reach the prompt.
    env = Env(tmp_path)
    data = raw(("a", "bash", ["-c", "echo hi"]))
    data.pop("plan_context", None)
    _, action = env.plan(data)
    assert action.errors == [] and action.requirement is Requirement.OVERRIDE


def test_override_plan_without_context_runs_end_to_end(tmp_path: Path) -> None:
    data = raw(("a", "bash", ["-c", "echo hi"]))
    data.pop("plan_context", None)
    env = Env(tmp_path)
    _, action = env.plan(data)
    payload, _ = _ask(tmp_path, action.confirmation_phrase(), data)
    assert payload["status"] == "completed"


def test_known_change_mixed_with_override_still_needs_context(tmp_path: Path) -> None:
    env = Env(tmp_path)
    data = raw(("a", "mkdir", [str(tmp_path / "d")]), ("b", "bash", ["-c", "echo hi"]))
    data.pop("plan_context", None)
    _, action = env.plan(data)
    assert action.errors
