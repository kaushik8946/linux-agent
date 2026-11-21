"""Changing operations offered as typed tools (Build 10+).

A changing tool never runs directly. Its ``prepare`` step validates the
arguments, inspects the current state and returns an ``Operation``: a fixed
command plus the baseline and expected end state. The orchestrator shows the
operation to the user through the same approval prompt as command plans,
records it in the same journal, and only then calls ``perform``. Each
operation also declares how its result is verified (Build 11).

The model chooses *which* operation and its arguments; local code chooses
the exact command, so a package or service name cannot smuggle in options.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.command_policy import RiskClass
from agent.policy import Requirement, decide


@dataclass
class Operation:
    tool: str
    arguments: dict[str, Any]
    summary: str  # one line: what will happen
    commands: list[str]  # exact display strings of what will run
    risk: RiskClass
    baseline: str
    expected_end_state: str
    affected: list[str]
    verification: list[str]
    recovery: str
    perform: Callable[[], dict[str, Any]] = field(repr=False)
    # Already in the requested state: nothing needs to run or be approved.
    no_change_needed: bool = False
    # Could not be prepared (unavailable, invalid); nothing will run.
    refused: str | None = None
    needs_privilege: bool = False

    @property
    def requirement(self) -> Requirement:
        if self.refused:
            return Requirement.BLOCKED
        if self.no_change_needed:
            return Requirement.AUTO
        return decide(self.risk).requirement

    def confirmation_phrase(self) -> str:
        return f"yes, {self.tool} {self.fingerprint[:8]}"

    def render(self) -> str:
        lines = [
            f"Proposed operation: {self.summary} (requires: {self.requirement.value})"
        ]
        for command in self.commands:
            lines.append(f"  command: {command}")
        if self.needs_privilege:
            lines.append(
                "  needs administrator rights: sudo will ask for your password"
            )
        lines.append(f"  risk: {self.risk.value}")
        lines.append(f"  baseline: {self.baseline}")
        lines.append(f"  expected end state: {self.expected_end_state}")
        if self.affected:
            lines.append(f"  affects: {', '.join(self.affected)}")
        if self.verification:
            lines.append(f"  verification: {'; '.join(self.verification)}")
        if self.recovery:
            lines.append(f"  recovery: {self.recovery}")
        if self.refused:
            lines.append(f"  BLOCKED: {self.refused}")
        return "\n".join(lines)

    @property
    def fingerprint(self) -> str:
        material = {
            "tool": self.tool,
            "args": self.arguments,
            "commands": self.commands,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


Preparer = Callable[[dict[str, Any]], Operation]


@dataclass(frozen=True)
class OperationSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    prepare: Preparer
    risk: RiskClass = RiskClass.SYSTEM_CHANGE

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }
