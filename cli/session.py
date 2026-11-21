"""Interactive CLI session (Build 7).

Reads requests in a loop, sends them through the orchestrator, shows progress
and results, and handles slash commands. Model text and command output are
stripped of control characters before display, so output cannot drive the
terminal.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.markup import escape

from agent.orchestrator import Orchestrator, StopReason, TurnResult
from agent.redaction import redact
from agent.report_writer import ReportError, build_report, write_report
from agent.textutil import strip_control
from config import Config
from hostplatform.capabilities import HostProfile, render_profile

HELP_TEXT = """\
Describe what you want to know in plain language, for example:
  Check why my laptop is running slowly.
  Is anything listening on network ports?

The model chooses read-only commands; the local policy decides what may run.

Commands:
  /help      Show this help
  /inspect   Show detected host facts and capabilities
  /history   List this session's requests
  /report    Save a Markdown report of the last request
  /exit      Leave (Ctrl+D also works; Ctrl+C cancels a running request)
"""

Reader = Callable[[str], str]


def new_session_id() -> str:
    return secrets.token_hex(4)


@dataclass
class Session:
    orchestrator: Orchestrator
    config: Config
    profile: HostProfile
    console: Console
    read: Reader = input
    session_id: str = field(default_factory=new_session_id)
    secrets: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)

    def _safe(self, text: str) -> str:
        """Redact secrets and strip control characters before display."""
        return strip_control(redact(text, self.secrets))

    # ------------------------------------------------------------------

    def run(self) -> int:
        self.console.print(
            f"[bold]linux-agent[/bold] session [cyan]{self.session_id}[/cyan] "
            f"(model: {escape(self.config.model)})"
        )
        self.console.print(self._host_line())
        self.console.print(
            "Type a request, or /help. Read-only mode: nothing is changed.\n"
        )
        while True:
            try:
                line = self.read("linux-agent> ")
            except EOFError:
                self.console.print()
                break
            except KeyboardInterrupt:
                self.console.print("\n[dim](Ctrl+C) Type /exit to leave.[/dim]")
                continue
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if self.command(line) == "exit":
                    break
                continue
            self.handle_request(line)
        self.console.print("Goodbye.")
        return 0

    # ------------------------------------------------------------------

    def command(self, line: str) -> str | None:
        name = line.split()[0].lower()
        if name in ("/exit", "/quit"):
            return "exit"
        if name == "/help":
            self.console.print(HELP_TEXT, markup=False, highlight=False)
        elif name == "/inspect":
            self.console.print(
                render_profile(self.profile), markup=False, highlight=False
            )
        elif name == "/history":
            self.show_history()
        elif name == "/report":
            self.save_report()
        else:
            self.console.print(f"Unknown command {escape(name[:40])}. Type /help.")
        return None

    def show_history(self) -> None:
        turns = self.orchestrator.turns
        if not turns:
            self.console.print("No requests yet in this session.")
            return
        for n, turn in enumerate(turns, 1):
            request = self._safe(turn.request)
            short = request if len(request) <= 70 else request[:67] + "..."
            commands = sum(len(e.plan.results) for e in turn.events if e.plan)
            self.console.print(
                f"{n}. {escape(short)}  [dim]({turn.stop_reason.value}, "
                f"{commands} command step(s), {turn.duration_seconds:.0f}s)[/dim]"
            )

    def save_report(self) -> None:
        if not self.orchestrator.turns:
            self.console.print("Nothing to report yet. Ask something first.")
            return
        turn = self.orchestrator.turns[-1]
        content = build_report(
            session_id=self.session_id,
            turn=turn,
            host=self.orchestrator.host_summary,
            secrets=self.secrets,
        )
        try:
            path = write_report(
                self.config.ensure_report_dir(), content, self.session_id
            )
        except (ReportError, OSError) as exc:
            self.console.print(
                f"[red]Report not saved:[/red] {escape(self._safe(str(exc)))}"
            )
            return
        self.reports.append(str(path))
        self.console.print(f"Report saved: {escape(str(path))}")

    # ------------------------------------------------------------------

    def handle_request(self, request: str) -> TurnResult | None:
        cancel = threading.Event()
        status = self.console.status("Thinking...", spinner="dots")
        self.orchestrator.set_listener(self._listener(status))
        try:
            with status:
                result = self.orchestrator.ask(request, cancel)
        except KeyboardInterrupt:  # pragma: no cover - orchestrator handles it
            cancel.set()
            self.console.print("\n[yellow]Request cancelled.[/yellow]")
            return None
        finally:
            self.orchestrator.set_listener(None)
        self.show_result(result)
        return result

    def _listener(self, status: Any) -> Callable[[str, dict[str, Any]], None]:
        def listen(event: str, data: dict[str, Any]) -> None:
            if event == "thinking":
                status.update(f"Thinking (round {data.get('round')})...")
            elif event == "tool":
                status.update(f"Running {data.get('name')}...")
                self.console.print(
                    f"[dim]  tool: {escape(str(data.get('name')))}[/dim]"
                )
            elif event == "plan":
                self.console.print(
                    f"[bold]Command plan[/bold] [dim](mode: {data.get('mode')})[/dim]"
                )
                preview = self._safe(str(data.get("preview", "")))
                for line in preview.splitlines():
                    self.console.print(f"  {escape(line)}", highlight=False)
                status.update("Running commands...")
            elif event == "step":
                colour = {"ok": "green", "blocked": "red", "skipped": "dim"}.get(
                    str(data.get("status")), "yellow"
                )
                self.console.print(
                    f"   [{colour}]{escape(str(data.get('status')))}[/{colour}] "
                    f"{escape(self._safe(str(data.get('command'))))}",
                    highlight=False,
                )

        return listen

    def show_result(self, result: TurnResult) -> None:
        if result.stop_reason is StopReason.GATEWAY_ERROR:
            self.console.print(
                f"[red]Gateway error:[/red] {escape(result.error or '')}"
            )
            self.console.print("Your request was not saved; you can try again.")
            return
        steps = sum(len(e.plan.results) for e in result.events if e.plan)
        tools = sum(1 for e in result.events if e.plan is None)
        self.console.print(
            f"[dim]{tools} tool call(s), {steps} command step(s), "
            f"{result.rounds} model round(s), {result.duration_seconds:.0f}s[/dim]\n"
        )
        text = self._safe(result.text).strip()
        self.console.print(escape(text) if text else "[dim](no answer)[/dim]")
        if result.stop_reason is not StopReason.ANSWERED:
            self.console.print(
                f"[yellow]Stopped early: {result.stop_reason.value}[/yellow]"
            )
        self.console.print("[dim]Type /report to save a report.[/dim]\n")

    def _host_line(self) -> str:
        p = self.profile
        os_name = p.os.pretty_name or p.os.distro_id or "unknown OS"
        sm = p.service_manager
        return escape(
            f"Host: {os_name}, kernel {p.kernel or 'unknown'}, {p.architecture or 'unknown'}; "
            f"service manager: {sm.name or 'none'} ({sm.state}); "
            f"package managers: {', '.join(p.package_managers) or 'none'}"
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
