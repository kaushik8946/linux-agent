"""Agent-managed environment variables (Build 12, module 1).

Variables live in one file the agent owns, ``~/.config/linux-agent/env.sh``,
as ``export NAME='value'`` lines. A single marked block in ``~/.profile``
sources that file. Nothing else in the user's files is touched. Names and
values are validated, values are shell-quoted, and variables that change how
programs load code or how shells parse input are refused.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent.redaction import redact
from hostplatform.identity import as_user

NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
MAX_VALUE = 1000
BEGIN = "# >>> linux-agent environment (managed) >>>"
END = "# <<< linux-agent environment (managed) <<<"

# Changing these alters code loading, shell parsing or identity.
FORBIDDEN_EXACT = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "IFS", "ENV", "BASH_ENV",
        "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "CDPATH", "PS4", "PYTHONPATH",
        "PYTHONSTARTUP", "NODE_OPTIONS", "PERL5OPT", "RUBYOPT", "SUDO_ASKPASS",
    }
)  # fmt: skip
FORBIDDEN_PREFIXES = ("LD_", "BASH_FUNC_", "DYLD_")
SECRET_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY",
                "PRIVATE_KEY", "CREDENTIAL")  # fmt: skip


class EnvError(ValueError):
    pass


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise EnvError("name must be UPPER_CASE letters, digits and _ (max 64)")
    if name in FORBIDDEN_EXACT or name.startswith(FORBIDDEN_PREFIXES):
        raise EnvError(f"{name} changes how programs or shells load code; refused")
    if any(w in name for w in SECRET_WORDS):
        raise EnvError("secret-like variables are not stored in plain files; refused")
    return name


def validate_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise EnvError("value must be a non-empty string")
    if len(value) > MAX_VALUE:
        raise EnvError(f"value is longer than {MAX_VALUE} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EnvError("value must not contain control characters or newlines")
    if redact(value) != value:
        raise EnvError("value looks like a secret; it is not stored in a plain file")
    return value


@dataclass(frozen=True)
class EnvState:
    name: str
    managed: bool
    value: str | None


class EnvStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.file = home / ".config" / "linux-agent" / "env.sh"
        self.profile = home / ".profile"

    # -- reading -------------------------------------------------------------

    def read_all(self) -> dict[str, str]:
        try:
            with as_user():
                text = self.file.read_text(encoding="utf-8")
        except OSError:  # missing, or a path component is not a directory
            return {}
        values: dict[str, str] = {}
        for line in text.splitlines():
            if not line.startswith("export "):
                continue
            name, sep, rest = line[7:].partition("=")
            if not sep or not NAME_RE.fullmatch(name):
                continue
            try:
                parts = shlex.split(rest)
            except ValueError:
                continue
            if len(parts) == 1:
                values[name] = parts[0]
        return values

    def get(self, name: str) -> EnvState:
        values = self.read_all()
        return EnvState(name, name in values, values.get(name))

    def profile_hooked(self) -> bool:
        try:
            with as_user():
                return BEGIN in self.profile.read_text(encoding="utf-8")
        except OSError:
            return False

    # -- writing -------------------------------------------------------------

    def set(self, name: str, value: str) -> None:
        with as_user():
            values = self.read_all()
            values[name] = value
            self._write_vars(values)
            self._ensure_profile_hook()

    def unset(self, name: str) -> None:
        with as_user():
            values = self.read_all()
            values.pop(name, None)
            self._write_vars(values)

    def _write_vars(self, values: dict[str, str]) -> None:
        lines = ["# Managed by linux-agent. Edit with the agent or remove lines."]
        lines += [f"export {n}={shlex.quote(v)}" for n, v in sorted(values.items())]
        self._atomic(self.file, "\n".join(lines) + "\n", 0o600)

    def _ensure_profile_hook(self) -> None:
        if self.profile_hooked():
            return
        try:
            existing = self.profile.read_text(encoding="utf-8")
            mode = self.profile.stat().st_mode & 0o777
        except FileNotFoundError:
            existing, mode = "", 0o644
        block = (
            f"{BEGIN}\n"
            f'[ -r "{self.file}" ] && . "{self.file}"\n'
            f"{END}\n"
        )  # fmt: skip
        sep = "" if not existing or existing.endswith("\n") else "\n"
        self._atomic(self.profile, existing + sep + block, mode)

    @staticmethod
    def _atomic(path: Path, text: str, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".agent-tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
