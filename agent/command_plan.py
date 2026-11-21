"""Structured command-plan schema and validation.

A plan is data proposed by the model. Parsing it here never executes anything
and never trusts it: unknown fields, wrong types, NUL bytes, duplicate step
IDs and forward stdin references are all rejected. Error messages name the
offending field but never echo the offending value.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_STEPS = 10
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096
MAX_ENV_VARS = 16
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 262_144
MAX_ERROR_LENGTH = 200

STEP_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    purpose: str = Field(min_length=1, max_length=300)
    # A command name resolved locally, or an absolute path that the runner
    # verifies against its trusted directories.
    executable: str = Field(min_length=1, max_length=256)
    args: list[str] = Field(max_length=MAX_ARGS)
    working_dir: str | None = Field(default=None, min_length=1, max_length=1024)
    timeout_seconds: int | None = Field(default=None, ge=1, le=MAX_TIMEOUT_SECONDS)
    max_output_bytes: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_BYTES)
    env_allowlist: list[str] = Field(default_factory=list, max_length=MAX_ENV_VARS)
    stdin_from_step: str | None = None
    expected_result: str | None = Field(default=None, max_length=500)
    classification_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> Step:
        if not STEP_ID.fullmatch(self.id):
            raise ValueError("id must be 1-32 characters of letters, digits, _ or -")
        strings = [
            self.purpose,
            self.executable,
            self.working_dir or "",
            self.expected_result or "",
            self.classification_reason or "",
            self.stdin_from_step or "",
            *self.args,
            *self.env_allowlist,
        ]
        if any("\x00" in s for s in strings):
            raise ValueError("NUL bytes are not allowed")
        if any(len(a) > MAX_ARG_LENGTH for a in self.args):
            raise ValueError(f"an argument exceeds {MAX_ARG_LENGTH} characters")
        return self


class CommandPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    # "batch": run all steps, then return all results.
    # "stepwise": run only the first step, return its result, and report the
    # remaining step IDs as pending. The model must send them in a later call
    # after reviewing the result, so it adapts between steps.
    mode: Literal["batch", "stepwise"] = "batch"

    @model_validator(mode="after")
    def _check(self) -> CommandPlan:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError("step IDs must be unique")
            if step.stdin_from_step is not None and step.stdin_from_step not in seen:
                raise ValueError(
                    "stdin_from_step must name an earlier step in the same plan"
                )
            seen.add(step.id)
        return self


def parse_plan(data: Any) -> tuple[CommandPlan | None, list[str]]:
    """Validate raw model output. Returns ``(plan, [])`` or ``(None, errors)``."""
    if not isinstance(data, dict):
        return None, ["plan must be a JSON object with a 'steps' array"]
    try:
        return CommandPlan.model_validate(data), []
    except ValidationError as exc:
        errors: list[str] = []
        for err in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(part) for part in err["loc"]) or "plan"
            message = str(err["msg"]).removeprefix("Value error, ")
            errors.append(f"{where}: {message}"[:MAX_ERROR_LENGTH])
        return None, errors[:10]
