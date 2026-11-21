"""Connects model-proposed command plans to the controlled executor."""

from __future__ import annotations

from typing import Any

from agent.command_plan import parse_plan
from agent.llm_client import EXECUTE_COMMAND_PLAN, ToolCall, default_dispatch
from hostplatform.command_runner import CommandRunner


def make_dispatcher(runner: CommandRunner) -> Any:
    """Return a tool dispatcher that validates and runs ``execute_command_plan``.

    Other tools fall through to the default handlers. Invalid plans are
    reported back to the model as errors; nothing runs.
    """

    def dispatch(call: ToolCall) -> dict[str, Any]:
        if call.name != EXECUTE_COMMAND_PLAN:
            return default_dispatch(call)
        plan, errors = parse_plan(call.arguments)
        if plan is None:
            return {
                "executed": False,
                "error": "invalid command plan",
                "details": errors,
            }
        return runner.run_plan(plan).to_model_dict()

    return dispatch
