"""Environment-based configuration loading and validation.

Secrets (the API key) are never included in exceptions, ``repr`` output or
the redacted summary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PLACEHOLDER_API_KEY = "replace_me"

DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_MAX_TOOL_ROUNDS = 6
DEFAULT_REPORT_DIR = "reports"
DEFAULT_LOG_DIR = "logs"
DEFAULT_WORKSPACE_ROOT = "~/projects"

MAX_REQUEST_TIMEOUT = 600.0
MAX_TOOL_ROUNDS_LIMIT = 50


class ConfigError(Exception):
    """Raised when configuration is missing or malformed.

    The message lists every problem found and never contains secret values.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    request_timeout: float
    max_tool_rounds: int
    report_dir: Path
    log_dir: Path
    workspace_root: Path = field(
        default_factory=lambda: Path("~/projects").expanduser()
    )

    def ensure_log_dir(self) -> Path:
        """Create the log directory (private). Call only when logging."""
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.log_dir

    def ensure_report_dir(self) -> Path:
        """Create the report directory. Call only when a report is written."""
        self.report_dir.mkdir(parents=True, exist_ok=True)
        return self.report_dir

    def redacted(self) -> dict[str, object]:
        """Return a log-safe summary with the API key masked."""
        return {
            "api_key": "***",
            "base_url_host": urlparse(self.base_url).hostname,
            "model": self.model,
            "request_timeout": self.request_timeout,
            "max_tool_rounds": self.max_tool_rounds,
            "report_dir": str(self.report_dir),
            "log_dir": str(self.log_dir),
            "workspace_root": str(self.workspace_root),
        }


def _get(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _resolve_dir(
    env: Mapping[str, str],
    name: str,
    default: str,
    base_dir: Path,
    problems: list[str],
) -> Path:
    """Resolve a directory setting without creating it."""
    path = Path(_get(env, name) or default)
    if "\x00" in str(path):
        problems.append(f"{name} contains an invalid character")
        return path
    path = path.expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if path.exists() and not path.is_dir():
        problems.append(f"{name} exists but is not a directory")
    return path


def load_config(
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> Config:
    """Build a validated :class:`Config` from environment variables.

    ``env`` defaults to ``os.environ``. A relative report directory is resolved
    against ``base_dir`` (default: the current working directory). The report
    directory is not created here.
    """
    env = os.environ if env is None else env
    base_dir = Path.cwd() if base_dir is None else base_dir
    problems: list[str] = []

    api_key = _get(env, "OPENAI_API_KEY")
    if not api_key:
        problems.append("OPENAI_API_KEY is not set")
    elif api_key == PLACEHOLDER_API_KEY:
        problems.append("OPENAI_API_KEY is still the placeholder value")

    # The base URL may embed credentials, so its value is never echoed.
    base_url = _get(env, "OPENAI_BASE_URL")
    if not base_url:
        problems.append("OPENAI_BASE_URL is not set")
    else:
        try:
            parsed = urlparse(base_url)
            valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            problems.append("OPENAI_BASE_URL must be a valid http(s) URL")

    model = _get(env, "OPENAI_MODEL")
    if not model:
        problems.append("OPENAI_MODEL is not set")

    timeout = DEFAULT_REQUEST_TIMEOUT
    raw_timeout = _get(env, "AGENT_REQUEST_TIMEOUT")
    if raw_timeout:
        try:
            timeout = float(raw_timeout)
        except ValueError:
            problems.append("AGENT_REQUEST_TIMEOUT must be a number of seconds")
        else:
            if not 0 < timeout <= MAX_REQUEST_TIMEOUT:
                problems.append(
                    f"AGENT_REQUEST_TIMEOUT must be > 0 and <= {MAX_REQUEST_TIMEOUT:g}"
                )

    rounds = DEFAULT_MAX_TOOL_ROUNDS
    raw_rounds = _get(env, "AGENT_MAX_TOOL_ROUNDS")
    if raw_rounds:
        try:
            rounds = int(raw_rounds)
        except ValueError:
            problems.append("AGENT_MAX_TOOL_ROUNDS must be an integer")
        else:
            if not 1 <= rounds <= MAX_TOOL_ROUNDS_LIMIT:
                problems.append(
                    f"AGENT_MAX_TOOL_ROUNDS must be between 1 and {MAX_TOOL_ROUNDS_LIMIT}"
                )

    report_dir = _resolve_dir(
        env, "AGENT_REPORT_DIR", DEFAULT_REPORT_DIR, base_dir, problems
    )
    log_dir = _resolve_dir(env, "AGENT_LOG_DIR", DEFAULT_LOG_DIR, base_dir, problems)
    workspace_root = _resolve_dir(
        env, "AGENT_WORKSPACE_ROOT", DEFAULT_WORKSPACE_ROOT, base_dir, problems
    )

    if problems:
        raise ConfigError(problems)

    return Config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        request_timeout=timeout,
        max_tool_rounds=rounds,
        report_dir=report_dir,
        log_dir=log_dir,
        workspace_root=workspace_root,
    )
