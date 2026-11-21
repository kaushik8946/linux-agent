"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from dotenv import find_dotenv, load_dotenv

from agent.llm_client import LLMClient, LLMError, check_gateway
from agent.version import get_version
from config import Config, ConfigError, load_config

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_GATEWAY_ERROR = 3
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


def run(config: Config) -> int:
    """Application body. The interactive loop arrives in a later build."""
    print(f"linux-agent {get_version()} ready (model: {config.model}).")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    # --help and --version exit inside parse_args, before any config is read.
    args = build_parser().parse_args(argv)

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
