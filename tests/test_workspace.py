"""Project initialisation (Build 12, module 4)."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent.approval import ApprovalService
from agent.executor import ChangeExecutor
from agent.llm_client import LLMClient
from agent.orchestrator import Orchestrator
from agent.registry import ToolRegistry
from agent.task_store import TaskJournal
from agent.verifier import Outcome, verify
from config import load_config
from hostplatform.command_runner import CommandRunner
from hostplatform.workspace import (
    WorkspaceError,
    create,
    plan_project,
    validate_project_name,
    workspace_problem,
)
from tests.test_llm_client import FakeSDK, completion, tool_call
from tests.test_policy import Prompt
from tools.workspace import INIT_PROJECT, register_workspace_tools


def reg_for(root: Path | None) -> ToolRegistry:
    reg = ToolRegistry()
    register_workspace_tools(reg, root)
    return reg


def op_for(root: Path, **args: Any) -> Any:
    return reg_for(root).prepare(INIT_PROJECT, {"kind": "python", **args})


@pytest.mark.parametrize("name", ["demo", "my-app", "a_b", "x1"])
def test_valid_project_names(name: str) -> None:
    assert validate_project_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "..", ".", "../x", "a/b", "-x", "Ab", "a b", "x" * 65, "~"]
)
def test_invalid_project_names(name: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_project_name(name)


def test_tool_absent_without_workspace() -> None:
    assert reg_for(None).names() == []


def test_prepare_writes_nothing(tmp_path: Path) -> None:
    op = op_for(tmp_path, name="demo")
    assert not isinstance(op, dict) and not (tmp_path / "demo").exists()
    assert op.requirement.value == "confirm" and "pyproject.toml" in op.commands[0]


def test_python_template_contents(tmp_path: Path) -> None:
    plan = plan_project(tmp_path, "my-app", "python")
    create(plan)
    assert (tmp_path / "my-app/my_app/__init__.py").is_file()
    assert 'name = "my-app"' in (tmp_path / "my-app/pyproject.toml").read_text()


def test_text_template_is_minimal(tmp_path: Path) -> None:
    create(plan_project(tmp_path, "notes", "text"))
    assert sorted(p.name for p in (tmp_path / "notes").iterdir()) == [
        ".gitignore",
        "README.md",
    ]


def test_existing_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "README.md").write_text("mine")
    op = op_for(tmp_path, name="demo")
    assert op.refused and "already exists" in op.refused
    assert (tmp_path / "demo" / "README.md").read_text() == "mine"


def test_create_never_overwrites(tmp_path: Path) -> None:
    plan = plan_project(tmp_path, "demo", "python")
    (tmp_path / "demo").mkdir()
    with pytest.raises(WorkspaceError):
        create(plan)


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    assert isinstance(
        reg_for(tmp_path).prepare(INIT_PROJECT, {"name": "x1", "kind": "rust"}), dict
    )


@pytest.mark.parametrize("name", ["../escape", "/abs", "a/../../b"])
def test_paths_cannot_escape_workspace(tmp_path: Path, name: str) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    result = op_for(root, name=name)
    assert isinstance(result, dict)
    assert list(tmp_path.iterdir()) == [root]


def test_workspace_problems(tmp_path: Path) -> None:
    assert workspace_problem(tmp_path) is None
    assert workspace_problem(tmp_path / "missing") == "workspace root does not exist"
    assert workspace_problem(Path("relative")) is not None
    assert workspace_problem(Path("/etc")) is not None
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    assert "symbolic link" in str(workspace_problem(link))


def test_missing_workspace_refuses_operation(tmp_path: Path) -> None:
    op = op_for(tmp_path / "nope", name="demo")
    assert op.refused and verify(op).outcome is Outcome.BLOCKED


# -- verification ---------------------------------------------------------------


def test_verification_positive(tmp_path: Path) -> None:
    op = op_for(tmp_path, name="demo")
    assert op.perform()["ok"]
    assert verify(op).outcome is Outcome.PASSED


def test_verification_negative_not_created(tmp_path: Path) -> None:
    op = op_for(tmp_path, name="demo")
    report = verify(op)
    assert report.outcome is not Outcome.PASSED and report.partial_change is False


def test_verification_negative_file_missing(tmp_path: Path) -> None:
    op = op_for(tmp_path, name="demo")
    op.perform()
    (tmp_path / "demo" / "README.md").unlink()
    report = verify(op)
    assert report.outcome is Outcome.PARTIALLY_PASSED and report.partial_change is True


def test_verification_negative_extra_file(tmp_path: Path) -> None:
    op = op_for(tmp_path, name="demo")
    op.perform()
    (tmp_path / "demo" / "surprise.sh").write_text("x")
    assert verify(op).outcome is Outcome.PARTIALLY_PASSED


def test_perform_reports_failure_when_dir_appears(tmp_path: Path) -> None:
    op = op_for(tmp_path, name="demo")
    (tmp_path / "demo").mkdir()
    assert op.perform()["ok"] is False


def test_workspace_root_config(tmp_path: Path) -> None:
    env = {"OPENAI_API_KEY": "sk-abcdefgh1234", "OPENAI_BASE_URL": "https://g.example/v1",
           "OPENAI_MODEL": "m", "AGENT_WORKSPACE_ROOT": str(tmp_path / "ws")}  # fmt: skip
    cfg = load_config(env, base_dir=tmp_path)
    assert cfg.workspace_root == tmp_path / "ws" and not cfg.workspace_root.exists()


# -- end to end -----------------------------------------------------------------


def run_flow(tmp_path: Path, answer: str) -> dict[str, Any]:
    sdk = FakeSDK(completion(None, [tool_call(INIT_PROJECT, {"name": "demo", "kind": "python"})]),
                  completion("ok"))  # fmt: skip
    client = LLMClient("sk-test-key-123456", "https://g.example/v1", "m", 5, 8, sdk)
    runner = CommandRunner(allowed_roots=[tmp_path], default_cwd=tmp_path)
    approvals = ApprovalService(Prompt(answer))
    journal = TaskJournal(tmp_path / "t.jsonl")
    Orchestrator(client, reg_for(tmp_path), runner, approvals=approvals,
                 executor=ChangeExecutor(runner, approvals, journal)).ask("new project")  # fmt: skip
    msgs = [m for m in sdk.requests[-1]["messages"] if m["role"] == "tool"]
    return json.loads(msgs[-1]["content"])


def test_approved_init_end_to_end(tmp_path: Path) -> None:
    data = run_flow(tmp_path, "y")
    assert data["status"] == "completed" and data["verification"]["outcome"] == "PASSED"


def test_rejected_init_creates_nothing(tmp_path: Path) -> None:
    data = run_flow(tmp_path, "n")
    assert data["status"] == "rejected" and not (tmp_path / "demo").exists()
