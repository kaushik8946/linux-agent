"""Fresh workstation provisioning profiles (Build 12, module 9).

A profile is a named list of steps, each an existing typed operation with
fixed arguments (for example ``install_dev_tool`` with ``tool=git``). This
module never changes anything itself:

* ``plan_profile`` prepares every step (read-only) and reports which are
  already done, which would change the system, and which cannot run here.
* The model then calls each pending step's own tool. Every step goes through
  the usual approval, executor, journal and verification, one at a time, so
  the user can approve some steps and decline others.
* ``check_profile`` runs each step's declared verification checks and
  summarises PASSED / PARTIALLY PASSED / FAILED for the whole profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.command_policy import RiskClass
from agent.registry import ToolRegistry, ToolSpec
from agent.verifier import CheckResult as VCheck
from agent.verifier import Outcome, classify, verify


@dataclass(frozen=True)
class Step:
    tool: str
    arguments: dict[str, Any]
    why: str


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    steps: tuple[Step, ...]


def _dev(tool: str, why: str) -> Step:
    return Step("install_dev_tool", {"tool": tool}, why)


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile(
            "python-dev",
            "Python development workstation",
            (
                _dev("git", "version control"),
                _dev("curl", "download tools and test HTTP"),
                _dev("make", "common build entry point"),
                _dev("python3-venv", "isolated Python environments"),
                _dev("jq", "inspect JSON"),
            ),
        ),
        Profile(
            "shell-tools",
            "Terminal productivity tools",
            (
                _dev("tmux", "terminal multiplexer"),
                _dev("shellcheck", "lint shell scripts"),
                _dev("jq", "inspect JSON"),
                _dev("sqlite3", "inspect SQLite databases"),
            ),
        ),
        Profile(
            "minimal",
            "Minimal developer basics",
            (_dev("git", "version control"), _dev("curl", "download tools")),
        ),
    )
}

PLAN_PROFILE = "plan_profile"
CHECK_PROFILE = "check_profile"
LIST_PROFILES = "list_profiles"


def register_provisioning_tools(registry: ToolRegistry) -> None:
    def step_ops(profile: Profile) -> list[tuple[Step, Any]]:
        return [(s, registry.prepare(s.tool, s.arguments)) for s in profile.steps]

    def listing(_a: dict[str, Any]) -> dict[str, Any]:
        return {
            "profiles": [
                {
                    "name": p.name,
                    "description": p.description,
                    "steps": [f"{s.tool} {s.arguments}" for s in p.steps],
                }
                for p in PROFILES.values()
            ]
        }

    def plan(a: dict[str, Any]) -> dict[str, Any]:
        profile = PROFILES[a["profile"]]
        rows = []
        for step, op in step_ops(profile):
            row: dict[str, Any] = {
                "tool": step.tool,
                "arguments": step.arguments,
                "why": step.why,
            }
            if isinstance(op, dict):
                row["state"] = "unavailable"
                row["reason"] = str(op.get("error"))[:200]
            elif op.refused:
                row["state"] = "blocked"
                row["reason"] = op.refused[:200]
            elif op.no_change_needed:
                row["state"] = "done"
            else:
                row["state"] = "pending"
                row["summary"] = op.summary
                row["requirement"] = op.requirement.value
            rows.append(row)
        pending = [r for r in rows if r["state"] == "pending"]
        return {
            "profile": profile.name,
            "steps": rows,
            "pending": len(pending),
            "done": sum(1 for r in rows if r["state"] == "done"),
            "cannot_run": sum(
                1 for r in rows if r["state"] in ("blocked", "unavailable")
            ),
            "next": (
                "Call each pending step's tool with exactly the arguments shown, "
                "one at a time. Each is approved separately by the user; a "
                "declined step is skipped, not retried. Then call check_profile."
            )
            if pending
            else "Nothing to change. Call check_profile to confirm.",
        }

    def check(a: dict[str, Any]) -> dict[str, Any]:
        profile = PROFILES[a["profile"]]
        rows = []
        summary: list[VCheck] = []
        for step, op in step_ops(profile):
            if isinstance(op, dict):
                outcome = Outcome.NOT_TESTED
                detail = str(op.get("error"))[:200]
            elif op.refused:
                outcome, detail = Outcome.BLOCKED, op.refused[:200]
            else:
                report = verify(op)
                outcome = report.outcome
                detail = "; ".join(
                    f"{c.name}: {'pass' if c.passed else 'fail' if c.passed is False else 'n/a'}"
                    for c in report.checks
                )
            passed = {Outcome.PASSED: True, Outcome.FAILED: False}.get(outcome)
            if outcome is Outcome.PARTIALLY_PASSED:
                passed = False
            summary.append(VCheck(f"{step.tool} {step.arguments}", passed, detail))
            rows.append(
                {
                    "tool": step.tool,
                    "arguments": step.arguments,
                    "outcome": outcome.value,
                    "detail": detail,
                }
            )
        overall = classify(summary)
        return {"profile": profile.name, "outcome": overall.value, "steps": rows}

    names = sorted(PROFILES)
    schema = {
        "type": "object",
        "properties": {"profile": {"type": "string", "enum": names}},
        "required": ["profile"],
        "additionalProperties": False,
    }
    for name, desc, input_schema, fn in (
        (LIST_PROFILES, "List workstation provisioning profiles and their steps.",
         {"type": "object", "properties": {}, "additionalProperties": False}, listing),
        (PLAN_PROFILE, ("Preview a provisioning profile: which steps are done, "
         "pending (need approval) or cannot run here. Changes nothing."), schema, plan),
        (CHECK_PROFILE, ("Verify every step of a provisioning profile and give "
         "an overall PASSED / PARTIALLY PASSED / FAILED result."), schema, check),
    ):  # fmt: skip
        registry.register(
            ToolSpec(
                name=name,
                description=desc + " Read-only.",
                input_schema=input_schema,
                output_schema={"type": "object"},
                handler=fn,
                risk=RiskClass.READ_ONLY,
            )
        )
