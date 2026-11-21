"""Registry of typed Python tools the model may call.

Tools here return structured data for things local code can know reliably
(host facts, command environment). They are not a catalogue of diagnostic
commands: for CPU, memory, disk and so on the model proposes commands through
``execute_command_plan``.

Arguments are validated against each tool's input schema before the handler
runs; unknown tools and invalid arguments are rejected, and a failing handler
returns a short error instead of raising.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.command_policy import RiskClass

Handler = Callable[[dict[str, Any]], dict[str, Any]]

_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[expected])


def validate(schema: Mapping[str, Any], value: Any, path: str = "value") -> list[str]:
    """Validate ``value`` against a small JSON-Schema subset.

    Supported: type (or list of types), enum, properties, required,
    additionalProperties (False or a schema), items, maxItems, minItems,
    maxLength, minimum, maximum. Messages name fields, never echo values.
    """
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(t, value) for t in types):
            return [f"{path}: expected {' or '.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not one of the allowed values")
    if isinstance(value, str) and len(value) > schema.get("maxLength", 10**9):
        errors.append(f"{path}: too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            errors.append(f"{path}: below minimum")
        if value > schema.get("maximum", value):
            errors.append(f"{path}: above maximum")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 10**9):
            errors.append(f"{path}: too many items")
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(schema["items"], item, f"{path}[{i}]")
    if isinstance(value, dict):
        props: Mapping[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: is required")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in props:
                errors += validate(props[name], item, f"{path}.{name}")
            elif extra is False:
                errors.append(f"{path}.{name}: unknown field")
            elif isinstance(extra, dict):
                errors += validate(extra, item, f"{path}.{name}")
    return errors


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: Handler
    risk: RiskClass = RiskClass.READ_ONLY

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        if spec.risk is not RiskClass.READ_ONLY:
            raise ValueError("only READ_ONLY tools can be registered before Build 8")
        if spec.input_schema.get("additionalProperties") is not False:
            raise ValueError("tool input schemas must set additionalProperties=false")
        self._tools[spec.name] = spec

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [self._tools[n].to_openai_tool() for n in self.names()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and run a tool. Never raises for model-caused problems."""
        spec = self._tools.get(name)
        if spec is None:
            return {"error": f"unknown tool: {name[:64]}"}
        problems = validate(spec.input_schema, dict(arguments), "arguments")
        if problems:
            return {"error": "invalid arguments", "details": problems[:10]}
        try:
            return spec.handler(dict(arguments))
        except Exception as exc:  # noqa: BLE001 - a tool failure must not crash the session
            return {"error": f"tool failed: {type(exc).__name__}"}
