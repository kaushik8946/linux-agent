import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent.registry import validate
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from tests.test_discovery import UBUNTU, fake_which, make_root
from tools.system import (
    COMMAND_ENVIRONMENT_OUTPUT,
    GET_COMMAND_ENVIRONMENT,
    GET_HOST_SUMMARY,
    HOST_SUMMARY_OUTPUT,
    build_registry,
    command_environment,
    host_summary,
    read_memory_total_mb,
    read_uptime_seconds,
)

MEMINFO = "MemTotal:       16384000 kB\nMemFree:  100 kB\n"


def fixture_root(tmp_path: Path, **kw: Any) -> Path:
    files = {"proc/meminfo": MEMINFO, "proc/uptime": "12345.67 999.0\n"}
    return make_root(tmp_path, UBUNTU, systemd_running=True, files=files, **kw)


def profile(root: Path, *present: str) -> Any:
    return discover(
        root=root,
        which=fake_which(*present),
        euid=lambda: 1000,
        uname=lambda: ("Linux", "6.1.0", "x86_64"),
    )


def test_host_summary_matches_output_schema(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    data = host_summary(profile(root, "apt-get", "sudo", "free"), root, lambda: 8)
    assert validate(HOST_SUMMARY_OUTPUT, data, "out") == []
    assert data["os"]["id"] == "ubuntu"
    assert data["hardware"] == {
        "cpu_count": 8,
        "memory_total_mb": 16000,
        "uptime_seconds": 12345,
    }
    assert data["package_managers"] == ["apt-get"]
    assert data["privileges"]["sudo_available"] is True
    assert data["service_manager"] == {"name": "systemd", "state": "running"}


def test_host_summary_unknowns_are_null_with_warnings(tmp_path: Path) -> None:
    root = make_root(tmp_path, None, cgroup=None)
    data = host_summary(profile(root), root, lambda: None)
    assert validate(HOST_SUMMARY_OUTPUT, data, "out") == []
    assert data["os"]["id"] is None
    assert data["hardware"] == {
        "cpu_count": None,
        "memory_total_mb": None,
        "uptime_seconds": None,
    }
    assert data["containers"]["detected"] is None
    joined = " ".join(data["warnings"])
    assert "memory size unavailable" in joined and "uptime unavailable" in joined


def test_host_summary_is_json_serialisable(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    json.dumps(host_summary(profile(root), root))


@pytest.mark.parametrize("text", ["", "garbage", "MemTotal: lots kB", None])
def test_bad_meminfo_gives_none(tmp_path: Path, text: str | None) -> None:
    root = tmp_path / "r"
    (root / "proc").mkdir(parents=True)
    if text is not None:
        (root / "proc/meminfo").write_text(text)
    assert read_memory_total_mb(root) is None


@pytest.mark.parametrize("text", ["", "abc", None])
def test_bad_uptime_gives_none(tmp_path: Path, text: str | None) -> None:
    root = tmp_path / "r"
    (root / "proc").mkdir(parents=True)
    if text is not None:
        (root / "proc/uptime").write_text(text)
    assert read_uptime_seconds(root) is None


def test_command_environment_matches_schema_and_flags_problems(tmp_path: Path) -> None:
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    data = command_environment(runner, ["uname", "no-such-command-xyz", "bash", "a;b"])
    assert validate(COMMAND_ENVIRONMENT_OUTPUT, data, "out") == []
    ex = data["executables"]
    assert ex["uname"]["path"].endswith("/uname") and not ex["uname"]["never_runnable"]
    assert ex["no-such-command-xyz"]["path"] is None
    assert ex["no-such-command-xyz"]["problem"]
    assert ex["bash"]["never_runnable"] is True
    assert "a;b" not in ex
    assert any("never run" in w for w in data["warnings"])
    assert data["constraints"]["shell_syntax_supported"] is False
    assert data["constraints"]["allowed_risk_classes"] == ["READ_ONLY"]


def test_command_environment_never_exposes_secrets(tmp_path: Path) -> None:
    secret = "sk-supersecret-value-9999"
    runner = CommandRunner(
        environ={"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": secret, "MY_TOKEN": secret},
        allowed_roots=[tmp_path],
        default_cwd=tmp_path,
    )
    text = json.dumps(command_environment(runner, ["uname"]))
    assert secret not in text
    assert "OPENAI_API_KEY" not in text


def test_command_environment_without_trusted_dirs_warns(tmp_path: Path) -> None:
    runner = CommandRunner(path_dirs=["relative/bin"], allowed_roots=[tmp_path])
    data = command_environment(runner, ["uname"])
    assert any("no trusted directories" in w for w in data["warnings"])


def test_registry_has_both_tools_registered_read_only(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    reg = build_registry(profile(root), CommandRunner(allowed_roots=[tmp_path]), root)
    assert reg.names() == [GET_COMMAND_ENVIRONMENT, GET_HOST_SUMMARY]
    for name in reg.names():
        spec = reg.get(name)
        assert spec is not None
        assert spec.risk.value == "READ_ONLY"
        assert spec.input_schema["additionalProperties"] is False
        assert spec.description and spec.output_schema


def test_registered_tools_validate_arguments(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    reg = build_registry(profile(root), CommandRunner(allowed_roots=[tmp_path]), root)
    assert reg.call(GET_HOST_SUMMARY, {"x": 1})["error"] == "invalid arguments"
    assert (
        reg.call(GET_COMMAND_ENVIRONMENT, {"names": "lsof"})["error"]
        == "invalid arguments"
    )
    assert (
        reg.call(GET_COMMAND_ENVIRONMENT, {"names": ["a"] * 21})["error"]
        == "invalid arguments"
    )
    assert "error" not in reg.call(GET_HOST_SUMMARY, {})


def test_tools_do_not_modify_state_or_start_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixture_root(tmp_path)
    reg = build_registry(profile(root), CommandRunner(allowed_roots=[tmp_path]), root)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))

    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("context tools must not start processes")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    reg.call(GET_HOST_SUMMARY, {})
    reg.call(GET_COMMAND_ENVIRONMENT, {"names": ["uname", "lsof"]})
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before


def test_real_host_tools_return_valid_structures() -> None:
    reg = build_registry()
    assert validate(HOST_SUMMARY_OUTPUT, reg.call(GET_HOST_SUMMARY, {}), "out") == []
    env = reg.call(GET_COMMAND_ENVIRONMENT, {"names": ["uname"]})
    assert validate(COMMAND_ENVIRONMENT_OUTPUT, env, "out") == []


def test_tool_schemas_are_usable_by_the_gateway_client() -> None:
    from agent.llm_client import LLMClient
    from tests.test_llm_client import FakeSDK, completion, tool_call

    reg = build_registry()
    sdk = FakeSDK(
        completion(None, [tool_call(GET_HOST_SUMMARY, {})]),
        completion("done"),
    )
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 6, sdk)
    result = client.run_conversation(
        [{"role": "user", "content": "hi"}],
        reg.openai_tools(),
        lambda call: reg.call(call.name, call.arguments),
    )
    assert result.text == "done"
    tool_msg = json.loads(sdk.requests[1]["messages"][-1]["content"])
    assert tool_msg["os"]["id"]
