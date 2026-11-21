from typing import Any

import pytest

from agent.command_plan import MAX_STEPS, parse_plan
from agent.redaction import redact


def step(sid: str = "s1", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": sid,
        "purpose": "check memory",
        "executable": "free",
        "args": ["-h"],
    }
    base.update(over)
    return base


def parse(*steps: dict[str, Any]) -> tuple[Any, list[str]]:
    return parse_plan({"steps": list(steps)})


def test_valid_multi_step_plan() -> None:
    plan, errors = parse(step("a"), step("b", stdin_from_step="a"))
    assert errors == []
    assert [s.id for s in plan.steps] == ["a", "b"]
    assert plan.steps[0].args == ["-h"]


@pytest.mark.parametrize(
    "bad", [None, [], "text", 5, {"steps": []}, {"nosteps": 1}, {}]
)
def test_non_plan_input_rejected(bad: Any) -> None:
    plan, errors = parse_plan(bad)
    assert plan is None and errors


def test_unknown_fields_rejected() -> None:
    plan, errors = parse(step(sudo=True))
    assert plan is None
    assert any("sudo" in e for e in errors)


def test_unknown_top_level_field_rejected() -> None:
    plan, _ = parse_plan({"steps": [step()], "approved": True})
    assert plan is None


@pytest.mark.parametrize(
    "args", ["ls -la", [1, 2], [["nested"]], None, [None], [{"a": 1}], "$(id)"]
)
def test_args_must_be_a_list_of_strings(args: Any) -> None:
    plan, errors = parse(step(args=args))
    assert plan is None and errors


@pytest.mark.parametrize("field", ["purpose", "executable", "id"])
def test_missing_required_field_rejected(field: str) -> None:
    s = step()
    del s[field]
    assert parse(s)[0] is None


def test_missing_args_rejected() -> None:
    s = step()
    del s["args"]
    assert parse(s)[0] is None


@pytest.mark.parametrize(
    "over",
    [
        {"args": ["a\x00b"]},
        {"executable": "ls\x00"},
        {"purpose": "x\x00"},
        {"working_dir": "/tmp\x00"},
        {"env_allowlist": ["A\x00"]},
    ],
)
def test_nul_bytes_rejected(over: dict[str, Any]) -> None:
    assert parse(step(**over))[0] is None


@pytest.mark.parametrize("sid", ["", "a b", "a;b", "../x", "x" * 33, "é"])
def test_bad_step_ids_rejected(sid: str) -> None:
    assert parse(step(sid))[0] is None


def test_duplicate_ids_rejected() -> None:
    plan, errors = parse(step("a"), step("a"))
    assert plan is None and any("unique" in e for e in errors)


def test_stdin_must_reference_earlier_step() -> None:
    assert parse(step("a", stdin_from_step="b"), step("b"))[0] is None
    assert parse(step("a", stdin_from_step="a"))[0] is None
    assert parse(step("a", stdin_from_step="zzz"))[0] is None


def test_step_count_limits() -> None:
    assert parse()[0] is None
    assert parse(*[step(f"s{i}") for i in range(MAX_STEPS + 1)])[0] is None
    assert parse(*[step(f"s{i}") for i in range(MAX_STEPS)])[0] is not None


@pytest.mark.parametrize(
    "over",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"timeout_seconds": "5"},
        {"timeout_seconds": 2.5},
        {"timeout_seconds": True},
        {"max_output_bytes": 0},
        {"max_output_bytes": 10**9},
    ],
)
def test_limits_are_bounded(over: dict[str, Any]) -> None:
    assert parse(step(**over))[0] is None


def test_oversized_argument_rejected() -> None:
    assert parse(step(args=["x" * 5000]))[0] is None


def test_errors_never_echo_values() -> None:
    secret = "sk-supersecretvalue1234567890"
    _, errors = parse(step(args=[secret, 5]))
    assert errors and secret not in " ".join(errors)


def test_strictness_no_type_coercion() -> None:
    assert parse(step(id=5))[0] is None


# -- redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2hunter2",
        "API_KEY: abcdef123456",
        'token = "abc def"',
        "Authorization: Bearer abcdefghijklmnop",
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "AKIAIOSFODNN7EXAMPLE",
        "postgres://user:s3cretpw@db.example/x",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijkl",
    ],
)
def test_redaction_removes_secrets(text: str) -> None:
    out = redact(text)
    assert "[REDACTED]" in out
    for leak in ("hunter2", "abcdef123456", "abc def", "s3cretpw", "MIIabc", "EXAMPLE"):
        assert leak not in out


def test_redaction_keeps_ordinary_text() -> None:
    text = "Mem: 16Gi used 4Gi free 8Gi\nLinux 6.1 x86_64"
    assert redact(text) == text


def test_redaction_of_known_literals() -> None:
    assert "topsecretvalue" not in redact("x topsecretvalue y", ["topsecretvalue"])


def test_redaction_is_fast_on_hostile_input() -> None:
    import time

    start = time.monotonic()
    redact("password" + "=" * 50000 + "a" * 50000)
    redact("a" * 200000)
    redact("-----BEGIN PRIVATE KEY-----" + "A" * 200000)
    assert time.monotonic() - start < 2
