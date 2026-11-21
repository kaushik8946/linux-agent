import io
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from agent.llm_client import EXECUTE_COMMAND_PLAN, LLMClient
from agent.orchestrator import Orchestrator, StopReason
from cli.session import Session
from config import load_config
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from tests.test_llm_client import FakeSDK, completion, status_error, tool_call
from tools.system import build_registry

KEY = "sk-session-secret-key-123456"


def make_session(
    tmp_path: Path, lines: list[Any], *script: Any, **runner_kw: Any
) -> tuple[Session, io.StringIO, FakeSDK]:
    cfg = load_config(
        {
            "OPENAI_API_KEY": KEY,
            "OPENAI_BASE_URL": "https://g.example/v1",
            "OPENAI_MODEL": "m",
            "AGENT_REPORT_DIR": str(tmp_path / "reports"),
        },
        base_dir=tmp_path,
    )
    sdk = FakeSDK(*script)
    host = discover()
    runner = CommandRunner.for_host(
        host, allowed_roots=[tmp_path], default_cwd=tmp_path, **runner_kw
    )
    orch = Orchestrator(
        LLMClient.from_config(cfg, client=sdk), build_registry(host, runner), runner,
        secrets=[KEY],
    )  # fmt: skip
    out = io.StringIO()
    feed: Iterator[Any] = iter(lines)

    def read(_prompt: str) -> str:
        item = next(feed)
        if isinstance(item, BaseException) or (
            isinstance(item, type) and issubclass(item, BaseException)
        ):
            raise item
        return str(item)

    session = Session(orch, cfg, host, Console(file=out, width=120, force_terminal=False),
                      read=read, secrets=[KEY])  # fmt: skip
    return session, out, sdk


def plan(*steps: tuple[str, str, list[str]]) -> dict[str, Any]:
    return {
        "steps": [
            {"id": s, "purpose": "p", "executable": e, "args": a} for s, e, a in steps
        ]
    }


def test_help_inspect_history_and_exit(tmp_path: Path) -> None:
    session, out, _ = make_session(tmp_path, ["/help", "/inspect", "/history", "/exit"])
    assert session.run() == 0
    text = out.getvalue()
    assert "/report" in text and "Service manager" in text
    assert "No requests yet" in text and "Goodbye" in text


def test_eof_exits_cleanly(tmp_path: Path) -> None:
    session, out, _ = make_session(tmp_path, [EOFError])
    assert session.run() == 0 and "Goodbye" in out.getvalue()


def test_ctrl_c_at_prompt_does_not_exit(tmp_path: Path) -> None:
    session, out, _ = make_session(tmp_path, [KeyboardInterrupt, "/exit"])
    assert session.run() == 0
    assert "Type /exit to leave" in out.getvalue()


def test_unknown_command(tmp_path: Path) -> None:
    session, out, _ = make_session(tmp_path, ["/frobnicate", "/exit"])
    session.run()
    assert "Unknown command /frobnicate" in out.getvalue()


def test_blank_lines_ignored(tmp_path: Path) -> None:
    session, _, sdk = make_session(tmp_path, ["", "   ", "/exit"])
    session.run()
    assert sdk.requests == []


def test_request_shows_plan_progress_and_answer(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path,
        ["check memory", "/history", "/exit"],
        completion(
            None, [tool_call(EXECUTE_COMMAND_PLAN, plan(("a", "echo", ["mem ok"])))]
        ),
        completion("Memory is fine."),
    )
    session.run()
    text = out.getvalue()
    assert "Command plan" in text and "[READ_ONLY] echo 'mem ok'" in text
    assert "ok echo 'mem ok'" in text
    assert "1 command step(s)" in text and "Memory is fine." in text
    assert "1. check memory" in text


def test_blocked_command_is_shown(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path,
        ["delete stuff", "/exit"],
        completion(
            None, [tool_call(EXECUTE_COMMAND_PLAN, plan(("a", "rm", ["-rf", "x"])))]
        ),
        completion("I can't do that in read-only mode."),
    )
    session.run()
    text = out.getvalue()
    assert "rm -rf x" in text and "DESTRUCTIVE" in text
    assert "Plan needs review" in text and "(invalid)" in text


def test_report_saved_after_request(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path,
        ["check", "/report", "/report", "/exit"],
        completion(
            None, [tool_call(EXECUTE_COMMAND_PLAN, plan(("a", "echo", [f"key {KEY}"])))]
        ),
        completion(f"Done. The key was {KEY}."),
    )
    session.run()
    assert len(session.reports) == 2 and session.reports[0] != session.reports[1]
    content = Path(session.reports[0]).read_text()
    assert "## Final result" in content and KEY not in content
    assert "Report saved" in out.getvalue()


def test_report_before_any_request(tmp_path: Path) -> None:
    session, out, _ = make_session(tmp_path, ["/report", "/exit"])
    session.run()
    assert "Nothing to report yet" in out.getvalue()
    assert not (tmp_path / "reports").exists()


def test_gateway_error_is_shown_and_session_continues(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path,
        ["hello", "hello again", "/exit"],
        status_error(500),
        completion("Back online."),
    )
    session.run()
    text = out.getvalue()
    assert "Gateway error" in text and "Back online." in text


def test_secret_never_printed(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path,
        ["x", "/exit"],
        completion(None, [tool_call(EXECUTE_COMMAND_PLAN, plan(("a", "echo", [KEY])))]),
        completion(f"token={KEY}"),
    )
    session.run()
    assert KEY not in out.getvalue()


def test_terminal_escape_sequences_from_output_are_stripped(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path, ["x", "/exit"], completion("Answer \x1b[2J\x1b]0;pwned\x07 done")
    )
    session.run()
    assert "\x1b" not in out.getvalue() and "Answer" in out.getvalue()


def test_rich_markup_in_model_text_is_not_interpreted(tmp_path: Path) -> None:
    session, out, _ = make_session(
        tmp_path, ["x", "/exit"], completion("[red]fake alert[/red]")
    )
    session.run()
    assert "[red]fake alert[/red]" in out.getvalue()


def test_ctrl_c_during_request_cancels_without_corrupting_state(tmp_path: Path) -> None:
    session, _out, _ = make_session(
        tmp_path,
        ["slow", "/report", "/exit"],
        completion(
            None, [tool_call(EXECUTE_COMMAND_PLAN, plan(("a", "uname", ["-a"])))]
        ),
    )
    orch = session.orchestrator
    real = orch.runner.run_plan

    def interrupted(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt

    object.__setattr__(orch.runner, "run_plan", interrupted)
    session.run()
    object.__setattr__(orch.runner, "run_plan", real)
    turn = orch.turns[-1]
    assert turn.stop_reason is StopReason.CANCELLED
    for i, m in enumerate(orch.messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in orch.messages[i + 1 :]}
            assert {c["id"] for c in m["tool_calls"]} <= answered
    reports = list((tmp_path / "reports").glob("report-*.md"))
    assert len(reports) == 1 and "cancelled" in reports[0].read_text()
    assert not list((tmp_path / "reports").glob(".report-*"))


def test_ctrl_c_kills_running_command(tmp_path: Path) -> None:
    """A real Ctrl+C during a running command stops it promptly."""
    import os
    import signal
    import time

    from agent.command_policy import Classification, RiskClass

    # sleep is not read-only-allowlisted, so use a permissive test policy to
    # get a real long-running process.
    session, _, _ = make_session(
        tmp_path,
        ["slow", "/exit"],
        completion(
            None, [tool_call(EXECUTE_COMMAND_PLAN, plan(("a", "sleep", ["30"])))]
        ),
        policy=lambda n, a: Classification(RiskClass.READ_ONLY, "test"),
    )
    timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT))
    start = time.monotonic()
    timer.start()
    try:
        session.run()
    finally:
        timer.cancel()
    assert time.monotonic() - start < 10
    assert session.orchestrator.turns[-1].stop_reason is StopReason.CANCELLED


@pytest.mark.parametrize("cmd", ["/exit", "/quit", "/EXIT"])
def test_exit_aliases(tmp_path: Path, cmd: str) -> None:
    session, _, _ = make_session(tmp_path, [cmd])
    assert session.run() == 0
