from typing import Any

import pytest

from agent.command_policy import RiskClass
from agent.registry import ToolRegistry, ToolSpec, validate

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": 5},
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def spec(name: str = "t", handler: Any = None, **kw: Any) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        input_schema=kw.pop("input_schema", SCHEMA),
        output_schema={"type": "object"},
        handler=handler or (lambda a: {"ok": True, "args": a}),
        **kw,
    )


def test_registered_tool_runs() -> None:
    reg = ToolRegistry()
    reg.register(spec())
    assert "t" in reg and reg.names() == ["t"]
    assert reg.call("t", {"name": "x"}) == {"ok": True, "args": {"name": "x"}}


def test_unknown_tool_rejected() -> None:
    result = ToolRegistry().call("rm_everything", {})
    assert "unknown tool" in result["error"]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"name": 5},
        {"name": "toolong"},
        {"name": "x", "extra": 1},
        {"name": "x", "count": 0},
        {"name": "x", "count": 4},
        {"name": "x", "count": True},
        {"name": "x", "count": 1.5},
        {"name": "x", "mode": "c"},
        {"name": "x", "tags": ["a", "b", "c"]},
        {"name": "x", "tags": [1]},
    ],
)
def test_invalid_arguments_rejected_and_handler_not_called(
    args: dict[str, Any],
) -> None:
    called: list[Any] = []

    def record(a: dict[str, Any]) -> dict[str, Any]:
        called.append(a)
        return {}

    reg = ToolRegistry()
    reg.register(spec(handler=record))
    result = reg.call("t", args)
    assert result["error"] == "invalid arguments"
    assert called == []


def test_valid_edge_arguments_accepted() -> None:
    reg = ToolRegistry()
    reg.register(spec())
    assert "error" not in reg.call(
        "t", {"name": "x", "count": 3, "mode": "b", "tags": ["a"]}
    )


def test_validation_errors_do_not_echo_values() -> None:
    secret = "sk-secret-value-123456"
    errors = validate(SCHEMA, {"name": secret, "extra": secret}, "arguments")
    assert errors and secret not in " ".join(errors)


def test_handler_failure_is_returned_safely() -> None:
    def boom(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("secret sk-leak-123456")

    reg = ToolRegistry()
    reg.register(spec(handler=boom))
    result = reg.call("t", {"name": "x"})
    assert result == {"error": "tool failed: RuntimeError"}


def test_duplicate_registration_rejected() -> None:
    reg = ToolRegistry()
    reg.register(spec())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(spec())


def test_only_read_only_tools_before_policy_build() -> None:
    with pytest.raises(ValueError, match="READ_ONLY"):
        ToolRegistry().register(spec(risk=RiskClass.SYSTEM_CHANGE))


def test_open_input_schema_rejected() -> None:
    loose = {"type": "object", "properties": {}}
    with pytest.raises(ValueError, match="additionalProperties"):
        ToolRegistry().register(spec(input_schema=loose))


def test_openai_tool_format() -> None:
    reg = ToolRegistry()
    reg.register(spec())
    tool = reg.openai_tools()[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "t"
    assert tool["function"]["parameters"] is SCHEMA


def test_non_object_arguments_do_not_crash() -> None:
    assert validate(SCHEMA, [1, 2]) == ["value: expected object"]
