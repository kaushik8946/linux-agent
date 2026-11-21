"""Risk policy: what each risk class requires before it may run.

This is the single place that maps a risk class to a requirement. The
command classifier (``agent.command_policy``) says what a command *is*; this
module says what the user must do before it *runs*. Model-supplied approval
flags never reach this module as input.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from agent.command_policy import RiskClass


class Requirement(str, Enum):
    AUTO = "auto"  # may run without asking
    CONFIRM = "confirm"  # local yes/no prompt, default No
    TYPED = "typed"  # local prompt requiring an exact typed phrase
    # Normally refused (sudo, shells, wrappers). Runs only if the user types an
    # exact override phrase naming the command, once, for that exact step.
    OVERRIDE = "override"
    BLOCKED = "blocked"  # never runs


@dataclass(frozen=True)
class Decision:
    risk: RiskClass
    requirement: Requirement
    reason: str

    @property
    def needs_approval(self) -> bool:
        return self.requirement in (
            Requirement.CONFIRM,
            Requirement.TYPED,
            Requirement.OVERRIDE,
        )


# Order matters for combining: a plan takes the strictest requirement.
_STRICTNESS = [
    Requirement.AUTO,
    Requirement.CONFIRM,
    Requirement.TYPED,
    Requirement.OVERRIDE,
    Requirement.BLOCKED,
]

_RULES: dict[RiskClass, tuple[Requirement, str]] = {
    RiskClass.READ_ONLY: (Requirement.AUTO, "read-only commands run automatically"),
    RiskClass.USER_LEVEL_CHANGE: (
        Requirement.CONFIRM,
        "changes your files; requires your approval",
    ),
    RiskClass.SYSTEM_CHANGE: (
        Requirement.CONFIRM,
        "changes system state; requires your approval",
    ),
    RiskClass.DESTRUCTIVE: (
        Requirement.TYPED,
        "can destroy data; requires typing a confirmation phrase",
    ),
    # Privilege escalation and shells are never run on the model's say-so.
    # The user may still run one exact step by typing an override phrase.
    RiskClass.PRIVILEGED: (
        Requirement.OVERRIDE,
        "privilege escalation is blocked unless you override it for this step",
    ),
    # A command the classifier does not recognise may do anything, so it is
    # never run automatically. The user sees it and decides; new tools do not
    # need a policy rule before they can be used.
    RiskClass.UNKNOWN: (
        Requirement.CONFIRM,
        "not recognised by the agent's policy; runs only if you approve it",
    ),
    RiskClass.UNSUPPORTED: (
        Requirement.OVERRIDE,
        "shells and command wrappers are blocked unless you override them for this step",
    ),
    RiskClass.SENSITIVE: (
        Requirement.BLOCKED,
        "touches credentials or secret material, so it is not run",
    ),
}


# What the model is told about an OVERRIDE step. It is identical to the old
# fixed refusal, so the model never learns that a user override exists.
MODEL_FACING_REASON: dict[RiskClass, str] = {
    RiskClass.PRIVILEGED: (
        "privilege escalation is not run from a command plan; it needs a "
        "dedicated reviewed workflow"
    ),
    RiskClass.UNSUPPORTED: "shells and command wrappers are never run",
}


def model_view(decision: Decision) -> tuple[str, str]:
    """(requirement, reason) as reported to the model."""
    if decision.requirement is Requirement.OVERRIDE:
        return (
            Requirement.BLOCKED.value,
            MODEL_FACING_REASON.get(decision.risk, "this command is never run"),
        )
    return decision.requirement.value, decision.reason


def decide(risk: RiskClass) -> Decision:
    """Return the requirement for a risk class. Raises on an unmapped class."""
    if not isinstance(risk, RiskClass) or risk not in _RULES:
        raise ValueError("unknown risk classification")
    requirement, reason = _RULES[risk]
    return Decision(risk, requirement, reason)


def strictest(requirements: list[Requirement]) -> Requirement:
    if not requirements:
        return Requirement.AUTO
    return max(requirements, key=_STRICTNESS.index)


# Command names that may never be approved in this version, whatever the
# classifier says. These are the plan's "do not build yet" areas.
NEVER_APPROVABLE = frozenset(
    {
        "fdisk", "sfdisk", "parted", "wipefs", "mkswap", "shred", "dd",
        "iptables", "ip6tables", "nft", "ufw", "firewall-cmd",
        "useradd", "usermod", "userdel", "groupadd", "passwd", "chpasswd",
        "visudo", "update-grub", "setenforce", "reboot", "shutdown",
        "poweroff", "halt",
    }
)  # fmt: skip


PRIVILEGE_WRAPPERS = frozenset({"sudo", "sudoedit", "doas", "run0", "pkexec", "su"})
# sudo/doas options that take a value (the next argument).
_VALUE_OPTIONS = frozenset(
    {"-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir",
     "-h", "--host", "-p", "--prompt", "-r", "--role", "-t", "--type",
     "-T", "--command-timeout", "-U", "--other-user"}
)  # fmt: skip
# Never allowed even with an override: feeding a password, keeping the
# caller's environment, opening a root shell, or editing files as root.
_FORBIDDEN_OPTIONS = frozenset(
    {"-S", "--stdin", "-A", "--askpass", "-E", "--preserve-env", "-i",
     "--login", "-s", "--shell", "-e", "--edit", "-b", "--background"}
)  # fmt: skip

_NEVER = "disk, firewall, account, boot and power changes are never run"


def _never(name: str) -> bool:
    return name in NEVER_APPROVABLE or name.startswith("mkfs")


def _inner_command(
    name: str, args: Sequence[str]
) -> tuple[str | None, list[str], str | None]:
    """For a privilege wrapper, return (inner command, its args, problem)."""
    if name == "su":
        # su -c 'shell string' runs a shell; bare su opens a root shell.
        return None, [], "su opens a root shell; use sudo with an explicit command"
    if name == "sudoedit":
        return None, [], "editing files as root is not supported"
    if name == "pkexec":
        if not args:
            return None, [], "pkexec without a command opens a root shell"
        return args[0].rsplit("/", 1)[-1], list(args[1:]), None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-"):
            break
        flag = a.split("=", 1)[0]
        if flag in _FORBIDDEN_OPTIONS or (
            len(a) > 1
            and a[1] != "-"
            and any(f"-{c}" in _FORBIDDEN_OPTIONS for c in a[1:])
        ):
            return None, [], f"{flag} is never allowed with {name}"
        i += 2 if (flag in _VALUE_OPTIONS and "=" not in a) else 1
    if i >= len(args):
        return None, [], f"{name} needs an explicit command to run"
    return args[i].rsplit("/", 1)[-1], list(args[i + 1 :]), None


_SCRIPT_SPLIT = re.compile(r"[;&|()`$<>{}\n]+")


def _script_words(args: Sequence[str]) -> list[str]:
    """Best-effort words of a wrapper's arguments or a shell script.

    Not a parser: it splits on whitespace and shell punctuation, so it can
    only find names written plainly. It is a defensive check on top of the
    user's own review, never a guarantee about what a script does.
    """
    words: list[str] = []
    for arg in args:
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = arg.split()
        for part in parts or [arg]:
            words += [w for w in _SCRIPT_SPLIT.split(part) if w]
    return words


def _hidden_problem(args: Sequence[str]) -> str | None:
    """Why a wrapper/shell step must stay blocked, judged by what it names."""
    from agent.command_policy import is_sensitive_arg

    for word in _script_words(args):
        base = word.rsplit("/", 1)[-1]
        if _never(base):
            return _NEVER
        if base in PRIVILEGE_WRAPPERS:
            return "privilege escalation inside a shell or wrapper is not run"
        if is_sensitive_arg(word):
            return "touches credentials or secret material, so it is not run"
    return None


def command_decision(name: str, risk: RiskClass, args: Sequence[str] = ()) -> Decision:
    """Decision for one command, including the never-approvable list.

    For privilege wrappers the command being elevated is checked too, so
    ``sudo fdisk`` or ``sudo cat /etc/shadow`` stays blocked even though
    ``sudo`` itself can be overridden.
    """
    from agent.command_policy import classify, is_sensitive_arg

    if _never(name):
        return Decision(risk, Requirement.BLOCKED, _NEVER)
    if risk is RiskClass.UNSUPPORTED:
        problem = _hidden_problem(args)
        if problem:
            return Decision(risk, Requirement.BLOCKED, problem)
    if name in PRIVILEGE_WRAPPERS:
        inner, inner_args, problem = _inner_command(name, args)
        if problem:
            return Decision(risk, Requirement.BLOCKED, problem)
        assert inner is not None
        if _never(inner):
            return Decision(risk, Requirement.BLOCKED, _NEVER)
        if any(is_sensitive_arg(a) for a in inner_args):
            return Decision(
                RiskClass.SENSITIVE,
                Requirement.BLOCKED,
                "touches credentials or secret material, so it is not run",
            )
        problem = _hidden_problem(inner_args)
        if problem:
            return Decision(risk, Requirement.BLOCKED, problem)
        inner_risk = classify(inner, inner_args).risk
        if inner_risk in (
            RiskClass.PRIVILEGED,
            RiskClass.SENSITIVE,
            RiskClass.UNSUPPORTED,
        ):
            return Decision(risk, Requirement.BLOCKED, f"{name} {inner} is not run")
    return decide(risk)
