"""OpenAI-compatible gateway client with tool-call parsing.

This module talks to the model and parses what comes back. It never executes
an operating-system command: ``execute_command_plan`` proposals are parsed and
returned as data only. The process executor arrives in Build 4.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import openai

from agent.version import get_version
from config import Config

MAX_ERROR_TEXT = 300

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for gateway failures. Messages never contain the API key."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMAuthError(LLMError):
    """The gateway rejected the credentials (HTTP 401/403)."""


class LLMRateLimitError(LLMError):
    """The gateway is rate limiting requests (HTTP 429)."""


class LLMTimeoutError(LLMError):
    """The request did not complete within the configured timeout."""


class LLMConnectionError(LLMError):
    """The gateway could not be reached."""


class LLMGatewayError(LLMError):
    """The gateway returned another error, such as 400 or 5xx."""


class LLMResponseError(LLMError):
    """The gateway answered, but the response is unusable."""


class MaxToolRoundsExceeded(LLMError):
    """The model still wanted tools after the configured round limit."""


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

GET_AGENT_STATUS = "get_agent_status"
EXECUTE_COMMAND_PLAN = "execute_command_plan"

GET_AGENT_STATUS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": GET_AGENT_STATUS,
        "description": (
            "Return static metadata about this agent: name, version and which "
            "capabilities are enabled. Read-only and harmless."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}

# Mirrors agent.command_plan.CommandPlan, which is the authoritative validator.
# The default dispatcher below never executes plans; agent.plan_dispatch
# connects them to the controlled executor.
EXECUTE_COMMAND_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EXECUTE_COMMAND_PLAN,
        "description": (
            "Propose an ordered list of Linux commands to gather evidence or "
            "achieve the user's outcome. Each step is an executable name plus "
            "a separate argument array. No shell syntax is supported: pipes, "
            "redirection, globbing, substitution and expansion are NOT "
            "interpreted. The local application validates and runs the steps "
            "and returns their results; you never run anything yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["batch", "stepwise"],
                    "description": (
                        "batch (default) runs every step and returns all "
                        "results. stepwise runs only the first step and "
                        "returns its result, so you can choose the next "
                        "command from what you observed."
                    ),
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Unique step ID within the plan.",
                            },
                            "purpose": {
                                "type": "string",
                                "description": "Why this step is needed.",
                            },
                            "executable": {
                                "type": "string",
                                "description": "Command name, resolved locally.",
                            },
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "One argument per element.",
                            },
                            "working_dir": {"type": "string"},
                            "timeout_seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 300,
                            },
                            "max_output_bytes": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 262144,
                            },
                            "env_allowlist": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "stdin_from_step": {
                                "type": "string",
                                "description": (
                                    "ID of an earlier step whose bounded "
                                    "output is passed as stdin."
                                ),
                            },
                            "expected_result": {"type": "string"},
                            "classification_reason": {
                                "type": "string",
                                "description": (
                                    "Why you consider this step read-only or modifying."
                                ),
                            },
                        },
                        "required": ["id", "purpose", "executable", "args"],
                        "additionalProperties": False,
                    },
                },
                "plan_context": {
                    "type": "object",
                    "description": (
                        "Required when any step changes the system. Shown to "
                        "the user, who decides locally whether to approve."
                    ),
                    "properties": {
                        "expected_end_state": {"type": "string"},
                        "baseline": {"type": "string"},
                        "affected_resources": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "verification": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "recovery": {"type": "string"},
                    },
                    "required": ["expected_end_state"],
                    "additionalProperties": False,
                },
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    },
}

DEFAULT_TOOLS: list[dict[str, Any]] = [GET_AGENT_STATUS_TOOL, EXECUTE_COMMAND_PLAN_TOOL]

# --------------------------------------------------------------------------
# Parsed response types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the model.

    If the arguments could not be parsed, ``error`` is set and ``arguments`` is
    empty. The call is then reported back to the model instead of executed.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    error: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    assistant_message: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ConversationResult:
    text: str
    messages: list[dict[str, Any]]
    rounds: int
    tool_calls: list[ToolCall]


Dispatcher = Callable[[ToolCall], Mapping[str, Any]]

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _parse_tool_call(raw: Any) -> ToolCall:
    function = getattr(raw, "function", None)
    call_id = getattr(raw, "id", None)
    name = getattr(function, "name", None)
    if not isinstance(call_id, str) or not call_id:
        raise LLMResponseError("Tool call is missing an id")
    if not isinstance(name, str) or not name:
        raise LLMResponseError("Tool call is missing a function name")

    raw_args = getattr(function, "arguments", None)
    if raw_args is None or raw_args == "":
        return ToolCall(call_id, name, {}, "")
    if not isinstance(raw_args, str):
        return ToolCall(call_id, name, {}, "", "arguments are not a JSON string")
    try:
        parsed = json.loads(raw_args)
    except ValueError:
        return ToolCall(call_id, name, {}, raw_args, "arguments are not valid JSON")
    if not isinstance(parsed, dict):
        return ToolCall(call_id, name, {}, raw_args, "arguments must be a JSON object")
    return ToolCall(call_id, name, parsed, raw_args)


def parse_response(raw: Any) -> LLMResponse:
    """Convert a chat-completion object into an :class:`LLMResponse`."""
    choices = getattr(raw, "choices", None)
    if not choices:
        raise LLMResponseError("Gateway response contained no choices")
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise LLMResponseError("Gateway response contained no message")

    content = getattr(message, "content", None)
    if content is not None and not isinstance(content, str):
        raise LLMResponseError("Gateway message content is not text")
    calls = [
        _parse_tool_call(tc) for tc in (getattr(message, "tool_calls", None) or [])
    ]
    if not content and not calls:
        raise LLMResponseError("Gateway response had neither text nor tool calls")

    assistant: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        assistant["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.raw_arguments or "{}"},
            }
            for c in calls
        ]
    return LLMResponse(
        content=content or None,
        tool_calls=calls,
        finish_reason=getattr(choice, "finish_reason", None),
        assistant_message=assistant,
    )


# --------------------------------------------------------------------------
# Default (non-executing) tool dispatch
# --------------------------------------------------------------------------


def agent_status() -> dict[str, Any]:
    return {
        "name": "linux-agent",
        "version": get_version(),
        "interface": "cli",
        "command_execution_enabled": False,
        "note": "Read-only proof-of-concept build; no commands are executed.",
    }


def default_dispatch(call: ToolCall) -> dict[str, Any]:
    """Handle tool calls for Build 2. Nothing here touches the OS."""
    if call.name == GET_AGENT_STATUS:
        return agent_status()
    if call.name == EXECUTE_COMMAND_PLAN:
        steps = call.arguments.get("steps")
        count = len(steps) if isinstance(steps, list) else 0
        return {
            "status": "not_executed",
            "reason": "Command execution is not enabled in this build.",
            "steps_received": count,
        }
    return {"error": f"unknown tool: {call.name}"}


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class LLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float,
        max_tool_rounds: int,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.max_tool_rounds = max_tool_rounds
        self._client = client or openai.OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout
        )

    @classmethod
    def from_config(cls, config: Config, client: Any | None = None) -> LLMClient:
        return cls(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            timeout=config.request_timeout,
            max_tool_rounds=config.max_tool_rounds,
            client=client,
        )

    # -- errors ----------------------------------------------------------

    def _safe_text(self, exc: BaseException) -> str:
        text = str(getattr(exc, "message", None) or exc)
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return text[:MAX_ERROR_TEXT]

    def _translate(self, exc: Exception) -> LLMError:
        text = self._safe_text(exc)
        if isinstance(exc, openai.APITimeoutError):
            return LLMTimeoutError("Gateway request timed out")
        if isinstance(exc, openai.APIConnectionError):
            return LLMConnectionError("Could not connect to the gateway")
        if isinstance(exc, openai.APIStatusError):
            code = exc.status_code
            if code in (401, 403):
                return LLMAuthError(f"Gateway rejected the credentials ({code})", code)
            if code == 429:
                return LLMRateLimitError("Gateway rate limit reached (429)", code)
            return LLMGatewayError(f"Gateway error {code}: {text}", code)
        return LLMGatewayError(f"Gateway request failed: {text}")

    # -- requests --------------------------------------------------------

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> LLMResponse:
        """Send one request and return the parsed response."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": list(messages)}
        if tools:
            kwargs["tools"] = list(tools)
        try:
            raw = self._client.chat.completions.create(**kwargs)
        except openai.OpenAIError as exc:
            raise self._translate(exc) from None
        return parse_response(raw)

    def run_conversation(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        dispatch: Dispatcher = default_dispatch,
        max_rounds: int | None = None,
    ) -> ConversationResult:
        """Run the bounded tool-calling loop.

        A round is one model request. If the model still asks for tools on the
        final allowed round, :class:`MaxToolRoundsExceeded` is raised and those
        calls are not dispatched.
        """
        limit = self.max_tool_rounds if max_rounds is None else max_rounds
        history: list[dict[str, Any]] = [dict(m) for m in messages]
        executed: list[ToolCall] = []

        for round_number in range(1, limit + 1):
            response = self.chat(history, tools)
            history.append(response.assistant_message)
            if not response.tool_calls:
                return ConversationResult(
                    response.content or "", history, round_number, executed
                )
            if round_number == limit:
                break
            for call in response.tool_calls:
                executed.append(call)
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            self._run_tool(call, dispatch), default=str
                        ),
                    }
                )
        raise MaxToolRoundsExceeded(
            f"Model still requested tools after {limit} round(s)"
        )

    @staticmethod
    def _run_tool(call: ToolCall, dispatch: Dispatcher) -> Mapping[str, Any]:
        if call.error:
            return {"error": f"invalid tool call: {call.error}"}
        try:
            return dispatch(call)
        except Exception as exc:  # noqa: BLE001 - a failing tool must not crash the session
            return {"error": f"tool failed: {type(exc).__name__}"}


# --------------------------------------------------------------------------
# Gateway capability check (used by ``linux-agent --check-gateway``)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_gateway(client: LLMClient) -> list[CheckResult]:
    """Verify text, tool calling and command-plan proposals against the model.

    Sends three small fixed prompts. Nothing returned by the model is executed.
    """
    results: list[CheckResult] = []

    def attempt(name: str, fn: Callable[[], str]) -> bool:
        try:
            results.append(CheckResult(name, True, fn()))
            return True
        except LLMError as exc:
            results.append(CheckResult(name, False, str(exc)))
            return False

    def text() -> str:
        r = client.chat([{"role": "user", "content": "Reply with the single word: ok"}])
        if not r.content:
            raise LLMResponseError("Response had no text")
        return f"model replied ({len(r.content)} chars)"

    def tool() -> str:
        r = client.chat(
            [{"role": "user", "content": "Call get_agent_status now."}],
            [GET_AGENT_STATUS_TOOL],
        )
        calls = [c for c in r.tool_calls if c.name == GET_AGENT_STATUS and not c.error]
        if not calls:
            raise LLMResponseError("Model did not make a valid tool call")
        return "model called get_agent_status"

    def plan() -> str:
        r = client.chat(
            [
                {
                    "role": "user",
                    "content": (
                        "Use execute_command_plan to propose one read-only step "
                        "that shows memory usage."
                    ),
                }
            ],
            [EXECUTE_COMMAND_PLAN_TOOL],
        )
        for c in r.tool_calls:
            steps = c.arguments.get("steps")
            if c.name == EXECUTE_COMMAND_PLAN and not c.error and steps:
                return f"model proposed a plan with {len(steps)} step(s) (not run)"
        raise LLMResponseError("Model did not propose a valid command plan")

    attempt("text response", text)
    attempt("tool calling", tool)
    attempt("command-plan proposal", plan)
    return results
