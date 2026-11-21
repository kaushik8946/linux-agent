"""Risk policy: what each risk class requires before it may run.

This is the single place that maps a risk class to a requirement. The
command classifier (``agent.command_policy``) says what a command *is*; this
module says what the user must do before it *runs*. Model-supplied approval
flags never reach this module as input.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.command_policy import RiskClass


class Requirement(str, Enum):
    AUTO = "auto"  # may run without asking
    CONFIRM = "confirm"  # local yes/no prompt, default No
    TYPED = "typed"  # local prompt requiring an exact typed phrase
    BLOCKED = "blocked"  # never runs in this version


@dataclass(frozen=True)
class Decision:
    risk: RiskClass
    requirement: Requirement
    reason: str

    @property
    def needs_approval(self) -> bool:
        return self.requirement in (Requirement.CONFIRM, Requirement.TYPED)


# Order matters for combining: a plan takes the strictest requirement.
_STRICTNESS = [
    Requirement.AUTO,
    Requirement.CONFIRM,
    Requirement.TYPED,
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
    RiskClass.PRIVILEGED: (
        Requirement.TYPED,
        "needs elevated privileges; requires typing a confirmation phrase",
    ),
    RiskClass.UNKNOWN: (
        Requirement.BLOCKED,
        "command could not be classified, so it is not run",
    ),
    RiskClass.UNSUPPORTED: (
        Requirement.BLOCKED,
        "shells and command wrappers are never run",
    ),
    RiskClass.SENSITIVE: (
        Requirement.BLOCKED,
        "touches credentials or secret material, so it is not run",
    ),
}


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


def command_decision(name: str, risk: RiskClass) -> Decision:
    """Decision for one command, including the never-approvable list."""
    base = decide(risk)
    if name in NEVER_APPROVABLE or name.startswith("mkfs"):
        return Decision(
            risk,
            Requirement.BLOCKED,
            "disk, firewall, account, boot and power changes are never run",
        )
    return base
