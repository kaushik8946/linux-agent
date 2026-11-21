"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from dotenv import find_dotenv, load_dotenv
from rich.console import Console

from agent.approval import ApprovalService
from agent.command_plan import parse_plan
from agent.executor import ChangeExecutor
from agent.llm_client import LLMClient, LLMError, check_gateway
from agent.orchestrator import Orchestrator
from agent.task_store import JOURNAL_NAME, TaskJournal
from agent.version import get_version
from cli.session import Session
from config import Config, ConfigError, load_config
from hostplatform.capabilities import render_profile
from hostplatform.command_runner import CommandRunner
from hostplatform.discovery import discover
from tools.system import build_registry

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_GATEWAY_ERROR = 3
EXIT_PLAN_INVALID = 4
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linux-agent",
        description=(
            "Linux general-purpose AI agent. Describe an outcome in natural "
            "language; the model proposes commands that are validated and run "
            "locally."
        ),
        epilog="Configuration is read from the environment or a .env file.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {get_version()}"
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="show detected host facts and capabilities, then exit",
    )
    parser.add_argument(
        "--preview-plan",
        metavar="FILE",
        help=(
            "validate a command plan from a JSON file ('-' for stdin) and show "
            "what would run and how each step is classified, without running it"
        ),
    )
    parser.add_argument(
        "--check-gateway",
        action="store_true",
        help=(
            "send three small test requests to the configured gateway to "
            "verify text, tool calling and command-plan proposals, then exit"
        ),
    )
    return parser


def _load_environment() -> None:
    # Existing environment variables take precedence over the .env file.
    load_dotenv(find_dotenv(usecwd=True), override=False)


def preview_plan_command(source: str) -> int:
    """Show a plan's validation result and classification. Runs nothing."""
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as handle:
                text = handle.read()
        data = json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"linux-agent: cannot read plan: {type(exc).__name__}", file=sys.stderr)
        return EXIT_PLAN_INVALID
    except ValueError:
        print("linux-agent: plan is not valid JSON", file=sys.stderr)
        return EXIT_PLAN_INVALID

    plan, errors = parse_plan(data)
    if plan is None:
        print("Invalid plan:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_PLAN_INVALID

    runner = CommandRunner.for_host(discover())
    print(f"Plan ({len(plan.steps)} step(s), mode: {plan.mode}); nothing is executed.")
    print(runner.preview(plan))
    blocked = [p for p in runner.preview_steps(plan) if p.blocked_reason]
    if blocked:
        print(f"\n{len(blocked)} step(s) blocked; the plan would not run.")
        return EXIT_PLAN_INVALID
    return EXIT_OK


def check_gateway_command(config: Config) -> int:
    client = LLMClient.from_config(config)
    print(
        f"Checking gateway {config.redacted()['base_url_host']} (model: {config.model})"
    )
    failed = False
    for result in check_gateway(client):
        mark = "PASS" if result.ok else "FAIL"
        print(f"  [{mark}] {result.name}: {result.detail}")
        failed = failed or not result.ok
    return EXIT_GATEWAY_ERROR if failed else EXIT_OK


def build_session(config: Config) -> Session:
    profile = discover()
    runner = CommandRunner.for_host(profile, extra_secrets=[config.api_key])
    approvals = ApprovalService(input)
    journal = TaskJournal(config.log_dir / JOURNAL_NAME, secrets=[config.api_key])
    orchestrator = Orchestrator(
        LLMClient.from_config(config),
        build_registry(profile, runner, adapters=True),
        runner,
        secrets=[config.api_key],
        approvals=approvals,
        executor=ChangeExecutor(runner, approvals, journal),
    )
    return Session(
        orchestrator=orchestrator,
        config=config,
        profile=profile,
        console=Console(),
        secrets=[config.api_key],
    )


def run(config: Config) -> int:
    """Start the interactive session."""
    return build_session(config).run()


def main(argv: Sequence[str] | None = None) -> int:
    # --help and --version exit inside parse_args, before any config is read.
    args = build_parser().parse_args(argv)

    if args.preview_plan:
        # Previewing needs no gateway configuration and runs nothing.
        return preview_plan_command(args.preview_plan)

    if args.inspect:
        # Host inspection needs no gateway configuration and makes no model call.
        print(render_profile(discover()))
        return EXIT_OK

    try:
        _load_environment()
        config = load_config()
    except ConfigError as exc:
        print("linux-agent: configuration error:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        print("See .env.example for the expected settings.", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        if args.check_gateway:
            return check_gateway_command(config)
        return run(config)
    except LLMError as exc:
        print(f"linux-agent: gateway error: {exc}", file=sys.stderr)
        return EXIT_GATEWAY_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
