import os
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.orchestrator import StopReason, ToolEvent, TurnResult
from agent.report_writer import (
    EXCERPT_CHARS,
    ReportError,
    build_report,
    report_filename,
    write_report,
)
from agent.textutil import fence, strip_control
from hostplatform.command_runner import PlanResult, Status, StepResult

SECRET = "sk-supersecret-report-key-1234567"
NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)

HOST = {
    "os": {"id": "ubuntu", "pretty_name": "Ubuntu 26.04 LTS"},
    "kernel": "7.0.0",
    "architecture": "x86_64",
    "hardware": {"cpu_count": 8, "memory_total_mb": 16000},
    "privileges": {"is_root": False, "sudo_available": True},
    "service_manager": {"name": "systemd", "state": "running"},
    "package_managers": ["apt-get"],
    "containers": {"detected": False, "indicators": []},
    "utilities": {"available": ["free"], "missing": ["lsof"]},
    "warnings": ["example host warning"],
}


def step(sid: str, stdout: str = "", **kw: Any) -> StepResult:
    return StepResult(sid, kw.pop("status", Status.OK), kw.pop("preview", f"cmd-{sid}"),
                      kw.pop("risk", "READ_ONLY"), stdout=stdout, exit_code=0, **kw)  # fmt: skip


def turn(**kw: Any) -> TurnResult:
    plan = PlanResult(
        True, [step("mem", "Mem: 16Gi total\n"), step("disk", "/ 34% used\n")]
    )
    defaults: dict[str, Any] = {
        "request": "Check why my laptop is slow",
        "text": "Memory looks fine. **Likely** the browser.",
        "stop_reason": StopReason.ANSWERED,
        "events": [
            ToolEvent("get_host_summary", True, "ok"),
            ToolEvent("execute_command_plan", True, "mem=ok, disk=ok", plan=plan),
        ],
        "rounds": 3,
        "duration_seconds": 4.2,
    }
    defaults.update(kw)
    return TurnResult(**defaults)


def report(t: TurnResult | None = None, host: Any = HOST) -> str:
    return build_report(
        session_id="abcd1234",
        turn=t or turn(),
        host=host,
        secrets=[SECRET],
        generated_at=NOW,
    )


# -- required fields ------------------------------------------------------------


def test_report_contains_required_fields() -> None:
    text = report()
    for section in (
        "## Request", "## Host facts", "## Tools used",
        "## Key observations", "## Warnings and missing capabilities",
        "## Facts versus hypotheses", "## Final result",
    ):  # fmt: skip
        assert section in text
    assert "Check why my laptop is slow" in text
    assert "2026-11-21 12:00:00 UTC" in text and "abcd1234" in text
    assert "Ubuntu 26.04 LTS" in text and "apt-get" in text
    assert "get_host_summary" in text and "`cmd-mem`" in text
    assert "Mem: 16Gi total" in text
    assert "lsof" in text and "example host warning" in text
    assert "Likely** the browser" in text
    assert "Outcome: answered" in text


def test_blocked_and_failed_steps_are_recorded() -> None:
    plan = PlanResult(False, [step("a", status=Status.BLOCKED, risk="DESTRUCTIVE",
                                   reason="blocked: destructive command")])  # fmt: skip
    text = report(
        turn(events=[ToolEvent("execute_command_plan", False, "a=blocked", plan=plan)])
    )
    assert "rejected before running" in text
    assert "risk=DESTRUCTIVE" in text and "destructive command" in text
    assert "did not fully succeed" in text


def test_early_stop_and_errors_are_warned() -> None:
    text = report(turn(stop_reason=StopReason.TOOL_ROUNDS, error="Gateway error 500"))
    assert "stopped early (tool_round_limit)" in text and "Gateway error 500" in text


def test_missing_host_facts_are_stated() -> None:
    assert "Host facts were not available" in report(host=None)


def test_unknown_host_values_are_marked_unknown() -> None:
    text = report(host={"os": {}, "hardware": {}, "containers": {"detected": None}})
    assert "- OS: unknown" in text and "Container indicators: unknown" in text


def test_no_tools_and_no_answer() -> None:
    text = report(turn(events=[], text=""))
    assert "No tools or commands were used." in text
    assert "No command output was captured." in text
    assert "No final answer was produced" in text


# -- secrets and full output ------------------------------------------------------


def test_secrets_are_redacted_everywhere() -> None:
    plan = PlanResult(True, [step("a", f"password=hunter2hunter2\nkey {SECRET}\n",
                                  preview=f"echo {SECRET}")])  # fmt: skip
    t = turn(
        request=f"my key is {SECRET}",
        text=f"Your token={SECRET} and Authorization: Bearer abcdefghijklmnop",
        events=[ToolEvent("execute_command_plan", True, "ok", plan=plan)],
    )
    text = report(t)
    for leak in (SECRET, "hunter2", "abcdefghijklmnop"):
        assert leak not in text
    assert "[REDACTED]" in text


def test_full_command_output_is_not_stored() -> None:
    big = "line of output\n" * 1000
    plan = PlanResult(True, [step("a", big)])
    text = report(
        turn(events=[ToolEvent("execute_command_plan", True, "ok", plan=plan)])
    )
    assert len(text) < len(big)
    assert "more characters omitted" in text
    assert f"limited to {EXCERPT_CHARS} characters" in text


def test_control_characters_and_fence_breakouts_are_neutralised() -> None:
    evil = "ok\x1b[2J\x1b]0;pwned\x07\n```\n# Injected heading\n"
    plan = PlanResult(True, [step("a", evil)])
    text = report(
        turn(events=[ToolEvent("execute_command_plan", True, "ok", plan=plan)])
    )
    assert "\x1b" not in text and "\x07" not in text
    assert "````" in text  # fence longer than the backticks in the output


def test_fence_and_strip_helpers() -> None:
    assert fence("a ``` b").startswith("````")
    assert strip_control("a\x1b[31mb\rc\x00") == "a[31mb\nc"


# -- files ------------------------------------------------------------------------


def test_report_written_with_unique_names(tmp_path: Path) -> None:
    first = write_report(tmp_path, "one", "abcd1234", NOW)
    second = write_report(tmp_path, "two", "abcd1234", NOW)
    assert first != second
    assert first.read_text() == "one" and second.read_text() == "two"
    assert first.name == report_filename(NOW, "abcd1234", 1)


def test_existing_report_is_never_overwritten(tmp_path: Path) -> None:
    taken = tmp_path / report_filename(NOW, "abcd1234", 1)
    taken.write_text("precious")
    path = write_report(tmp_path, "new", "abcd1234", NOW)
    assert taken.read_text() == "precious"
    assert path.name == report_filename(NOW, "abcd1234", 2)


def test_concurrent_writes_never_collide(tmp_path: Path) -> None:
    paths: list[Path] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        p = write_report(tmp_path, f"content {i}", "abcd1234", NOW)
        with lock:
            paths.append(p)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(paths)) == 20
    assert sorted(p.read_text() for p in paths) == sorted(
        f"content {i}" for i in range(20)
    )


def test_no_temporary_files_left_behind(tmp_path: Path) -> None:
    write_report(tmp_path, "x", "abcd1234", NOW)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".report-")] == []


def test_report_dir_created_on_first_write(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "reports"
    assert write_report(target, "x", "abcd1234", NOW).parent == target


def test_unwritable_report_dir_raises_report_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running with privileges that ignore directory permissions")
        with pytest.raises(ReportError):
            write_report(locked, "x", "abcd1234", NOW)
    finally:
        locked.chmod(0o700)


def test_interrupted_write_leaves_no_partial_report(tmp_path: Path) -> None:
    """Kill a writer mid-way (as Ctrl+C would) and check nothing partial remains."""
    script = f"""
import os, sys, time
sys.path.insert(0, {str(Path.cwd())!r})
import agent.report_writer as rw
real_fsync = os.fsync
def slow(fd):
    print("writing", flush=True)
    time.sleep(30)
os.fsync = slow
rw.write_report(__import__("pathlib").Path({str(tmp_path)!r}), "x" * 100000, "abcd1234")
"""
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"writing"
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)
    finished = [p for p in tmp_path.iterdir() if p.name.startswith("report-")]
    assert finished == []
