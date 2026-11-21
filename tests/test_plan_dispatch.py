import json
from pathlib import Path
from typing import Any

from agent.llm_client import EXECUTE_COMMAND_PLAN, EXECUTE_COMMAND_PLAN_TOOL, LLMClient
from agent.plan_dispatch import make_dispatcher
from hostplatform.command_runner import CommandRunner
from tests.test_llm_client import FakeSDK, completion, tool_call


def client(*script: Any) -> tuple[LLMClient, FakeSDK]:
    sdk = FakeSDK(*script)
    return LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 6, sdk), sdk


def step(sid: str, exe: str, *args: str, **kw: Any) -> dict[str, Any]:
    return {"id": sid, "purpose": "test", "executable": exe, "args": list(args), **kw}


def test_model_plan_runs_and_adapts_to_results(tmp_path: Path) -> None:
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    first = {"steps": [step("a", "uname", "-s"), step("b", "echo", "$(id)")]}
    second = {
        "steps": [step("c", "wc", "-c", stdin_from_step="x")]
    }  # invalid reference
    c, sdk = client(
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, first, "1")]),
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, second, "2")]),
        completion("done"),
    )
    result = c.run_conversation(
        [{"role": "user", "content": "check"}],
        [EXECUTE_COMMAND_PLAN_TOOL],
        make_dispatcher(runner),
    )
    assert result.text == "done"
    r1 = json.loads(sdk.requests[1]["messages"][-1]["content"])
    assert r1["executed"] is True
    assert [x["stdout"] for x in r1["results"]] == ["Linux\n", "$(id)\n"]
    r2 = json.loads(sdk.requests[2]["messages"][-1]["content"])
    assert r2["executed"] is False and r2["error"] == "invalid command plan"


def test_policy_blocked_plan_is_reported_not_run(tmp_path: Path) -> None:
    marker = tmp_path / "m"
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    plan = {"steps": [step("a", "touch", str(marker)), step("b", "bash", "-c", "id")]}
    c, sdk = client(
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, plan)]),
        completion("ok"),
    )
    c.run_conversation(
        [{"role": "user", "content": "x"}], None, make_dispatcher(runner)
    )
    data = json.loads(sdk.requests[1]["messages"][-1]["content"])
    assert data["executed"] is False
    assert not marker.exists()
    assert "UNSUPPORTED" in {r.get("risk") for r in data["results"]}
