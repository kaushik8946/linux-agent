import json
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from agent import llm_client
from agent.llm_client import (
    EXECUTE_COMMAND_PLAN,
    GET_AGENT_STATUS,
    LLMAuthError,
    LLMClient,
    LLMConnectionError,
    LLMGatewayError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    MaxToolRoundsExceeded,
    ToolCall,
    check_gateway,
    default_dispatch,
    parse_response,
)

SECRET = "sk-super-secret-value-12345"
REQUEST = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")


# -- fakes ------------------------------------------------------------------


def tool_call(name: str, arguments: Any, call_id: str = "call_1") -> SimpleNamespace:
    if not isinstance(arguments, str) and arguments is not None:
        arguments = json.dumps(arguments)
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def completion(
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )


class FakeSDK:
    """Stands in for ``openai.OpenAI``; yields scripted responses or raises."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(*script: Any, rounds: int = 6) -> tuple[LLMClient, FakeSDK]:
    sdk = FakeSDK(*script)
    client = LLMClient(
        api_key=SECRET,
        base_url="https://gateway.example/v1",
        model="test-model",
        timeout=5,
        max_tool_rounds=rounds,
        client=sdk,
    )
    return client, sdk


def status_error(code: int, body: str = "oops") -> openai.APIStatusError:
    response = httpx2.Response(code, request=REQUEST)
    cls = {
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        429: openai.RateLimitError,
        400: openai.BadRequestError,
        500: openai.InternalServerError,
    }[code]
    return cls(body, response=response, body=None)


USER = [{"role": "user", "content": "hi"}]


# -- text and tool-call parsing ---------------------------------------------


def test_text_response() -> None:
    client, sdk = make_client(completion("hello"))
    response = client.chat(USER)
    assert response.content == "hello"
    assert response.tool_calls == []
    assert sdk.requests[0]["model"] == "test-model"
    assert "tools" not in sdk.requests[0]


def test_tool_call_response_is_parsed() -> None:
    client, sdk = make_client(completion(None, [tool_call(GET_AGENT_STATUS, {})]))
    response = client.chat(USER, [llm_client.GET_AGENT_STATUS_TOOL])
    assert [c.name for c in response.tool_calls] == [GET_AGENT_STATUS]
    assert response.tool_calls[0].error is None
    assert sdk.requests[0]["tools"]
    assert response.assistant_message["tool_calls"][0]["id"] == "call_1"


def test_command_plan_is_parsed_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("an OS command must not run in Build 2")

    import os
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    plan = {
        "steps": [{"id": "s1", "purpose": "mem", "executable": "free", "args": ["-h"]}]
    }
    client, _ = make_client(completion(None, [tool_call(EXECUTE_COMMAND_PLAN, plan)]))
    call = client.chat(USER).tool_calls[0]
    assert call.arguments == plan
    result = default_dispatch(call)
    assert result["status"] == "not_executed"
    assert result["steps_received"] == 1


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"', "42", "null"])
def test_invalid_json_arguments_are_flagged(raw: str) -> None:
    client, _ = make_client(completion(None, [tool_call(GET_AGENT_STATUS, raw)]))
    call = client.chat(USER).tool_calls[0]
    assert call.error
    assert call.arguments == {}


def test_empty_arguments_mean_no_arguments() -> None:
    client, _ = make_client(completion(None, [tool_call(GET_AGENT_STATUS, None)]))
    call = client.chat(USER).tool_calls[0]
    assert call.error is None
    assert call.arguments == {}


@pytest.mark.parametrize(
    "bad",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=None),
        SimpleNamespace(choices=[SimpleNamespace(message=None, finish_reason="stop")]),
        completion(None, None),
        completion("", []),
        completion(["not", "text"]),  # type: ignore[arg-type]
    ],
)
def test_malformed_responses_are_rejected(bad: Any) -> None:
    with pytest.raises(LLMResponseError):
        parse_response(bad)


def test_tool_call_without_name_or_id_is_rejected() -> None:
    with pytest.raises(LLMResponseError):
        parse_response(completion(None, [tool_call("", {})]))
    with pytest.raises(LLMResponseError):
        parse_response(completion(None, [tool_call(GET_AGENT_STATUS, {}, call_id="")]))


# -- gateway errors ---------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(401), LLMAuthError),
        (status_error(403), LLMAuthError),
        (status_error(429), LLMRateLimitError),
        (status_error(500), LLMGatewayError),
        (status_error(400), LLMGatewayError),
        (openai.APITimeoutError(REQUEST), LLMTimeoutError),
        (openai.APIConnectionError(request=REQUEST), LLMConnectionError),
    ],
)
def test_gateway_errors_are_translated(error: Exception, expected: type) -> None:
    client, _ = make_client(error)
    with pytest.raises(expected):
        client.chat(USER)


def test_timeout_is_not_reported_as_generic_connection_error() -> None:
    client, _ = make_client(openai.APITimeoutError(REQUEST))
    with pytest.raises(LLMTimeoutError):
        client.chat(USER)


def test_status_code_is_preserved() -> None:
    client, _ = make_client(status_error(429))
    with pytest.raises(LLMRateLimitError) as exc:
        client.chat(USER)
    assert exc.value.status_code == 429


def test_secret_never_appears_in_errors() -> None:
    err = status_error(500, body=f"upstream said key {SECRET} is bad")
    client, _ = make_client(err)
    with pytest.raises(LLMGatewayError) as exc:
        client.chat(USER)
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value)


def test_unexpected_sdk_error_is_wrapped() -> None:
    client, _ = make_client(openai.OpenAIError("boom"))
    with pytest.raises(LLMGatewayError):
        client.chat(USER)


# -- the bounded tool loop ---------------------------------------------------


def test_loop_returns_final_text_without_tools() -> None:
    client, _ = make_client(completion("done"))
    result = client.run_conversation(USER)
    assert result.text == "done"
    assert result.rounds == 1
    assert result.tool_calls == []


def test_loop_dispatches_tool_and_feeds_result_back() -> None:
    client, sdk = make_client(
        completion(None, [tool_call(GET_AGENT_STATUS, {})]),
        completion("status received"),
    )
    result = client.run_conversation(USER, [llm_client.GET_AGENT_STATUS_TOOL])
    assert result.text == "status received"
    assert result.rounds == 2
    second_request = sdk.requests[1]["messages"]
    assert second_request[-1]["role"] == "tool"
    assert second_request[-1]["tool_call_id"] == "call_1"
    assert json.loads(second_request[-1]["content"])["name"] == "linux-agent"


def test_iteration_limit_is_enforced() -> None:
    looping = [
        completion(None, [tool_call(GET_AGENT_STATUS, {}, f"c{i}")]) for i in range(10)
    ]
    client, sdk = make_client(*looping, rounds=3)
    with pytest.raises(MaxToolRoundsExceeded):
        client.run_conversation(USER)
    assert len(sdk.requests) == 3


def test_final_round_tool_calls_are_not_dispatched() -> None:
    dispatched: list[ToolCall] = []

    def dispatch(call: ToolCall) -> dict[str, Any]:
        dispatched.append(call)
        return {}

    client, _ = make_client(
        completion(None, [tool_call(GET_AGENT_STATUS, {}, "a")]),
        completion(None, [tool_call(GET_AGENT_STATUS, {}, "b")]),
        rounds=2,
    )
    with pytest.raises(MaxToolRoundsExceeded):
        client.run_conversation(USER, dispatch=dispatch)
    assert [c.id for c in dispatched] == ["a"]


def test_invalid_arguments_reported_to_model_not_dispatched() -> None:
    called: list[ToolCall] = []

    def record(call: ToolCall) -> dict[str, Any]:
        called.append(call)
        return {}

    client, sdk = make_client(
        completion(None, [tool_call(GET_AGENT_STATUS, "{bad")]),
        completion("recovered"),
    )
    result = client.run_conversation(USER, dispatch=record)
    assert result.text == "recovered"
    assert called == []
    assert "invalid tool call" in sdk.requests[1]["messages"][-1]["content"]


def test_failing_tool_does_not_crash_loop() -> None:
    def broken(call: ToolCall) -> dict[str, Any]:
        raise RuntimeError(f"secret detail {SECRET}")

    client, sdk = make_client(
        completion(None, [tool_call(GET_AGENT_STATUS, {})]),
        completion("handled"),
    )
    result = client.run_conversation(USER, dispatch=broken)
    assert result.text == "handled"
    tool_msg = sdk.requests[1]["messages"][-1]["content"]
    assert "RuntimeError" in tool_msg
    assert SECRET not in tool_msg


def test_unknown_tool_is_reported_not_run() -> None:
    assert (
        "unknown tool" in default_dispatch(ToolCall("1", "rm_everything", {}))["error"]
    )


def test_input_messages_are_not_mutated() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    client, _ = make_client(completion("ok"))
    client.run_conversation(msgs)
    assert msgs == [{"role": "user", "content": "hi"}]


# -- schema and configuration ------------------------------------------------


def test_command_plan_schema_shape() -> None:
    fn = llm_client.EXECUTE_COMMAND_PLAN_TOOL["function"]
    assert fn["name"] == EXECUTE_COMMAND_PLAN
    item = fn["parameters"]["properties"]["steps"]["items"]
    assert set(item["required"]) == {"id", "purpose", "executable", "args"}
    assert item["additionalProperties"] is False
    assert item["properties"]["args"]["items"] == {"type": "string"}


def test_from_config_uses_config_values(tmp_path: Any) -> None:
    from config import load_config

    cfg = load_config(
        {
            "OPENAI_API_KEY": SECRET,
            "OPENAI_BASE_URL": "https://gateway.example/v1",
            "OPENAI_MODEL": "m",
            "AGENT_MAX_TOOL_ROUNDS": "4",
        },
        base_dir=tmp_path,
    )
    sdk = FakeSDK()
    client = LLMClient.from_config(cfg, client=sdk)
    assert client.model == "m"
    assert client.max_tool_rounds == 4


def test_real_sdk_receives_timeout_and_base_url() -> None:
    client = LLMClient(SECRET, "https://gateway.example/v1", "m", 12.5, 3)
    sdk = client._client
    assert str(sdk.base_url).startswith("https://gateway.example/v1")
    assert sdk.timeout == 12.5


# -- gateway check -----------------------------------------------------------


def test_check_gateway_all_pass() -> None:
    plan = {"steps": [{"id": "s", "purpose": "p", "executable": "free", "args": []}]}
    client, _ = make_client(
        completion("ok"),
        completion(None, [tool_call(GET_AGENT_STATUS, {})]),
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, plan)]),
    )
    results = check_gateway(client)
    assert [r.ok for r in results] == [True, True, True]


def test_check_gateway_reports_unsupported_tool_calling() -> None:
    client, _ = make_client(completion("ok"), completion("I cannot"), completion("no"))
    results = check_gateway(client)
    assert [r.ok for r in results] == [True, False, False]


def test_check_gateway_reports_gateway_errors() -> None:
    client, _ = make_client(status_error(401), status_error(401), status_error(401))
    results = check_gateway(client)
    assert not any(r.ok for r in results)
    assert SECRET not in " ".join(r.detail for r in results)
