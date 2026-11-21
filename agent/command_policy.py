"""Command classification: what a proposed command would do.

The policy is local and conservative. Only commands that are positively
recognised as read-only are ``READ_ONLY``; everything unrecognised is
``UNKNOWN``. The model's own opinion of a step (its ``classification_reason``)
is never consulted here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RiskClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    USER_LEVEL_CHANGE = "USER_LEVEL_CHANGE"
    SYSTEM_CHANGE = "SYSTEM_CHANGE"
    DESTRUCTIVE = "DESTRUCTIVE"
    PRIVILEGED = "PRIVILEGED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"  # would let a command string run another command
    SENSITIVE = "SENSITIVE"  # touches credentials or secret material


@dataclass(frozen=True)
class Classification:
    risk: RiskClass
    reason: str


EXECUTABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

SHELLS = frozenset(
    {"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "ash", "busybox"}
)
# Wrappers that run another command, which would bypass per-command policy.
WRAPPERS = frozenset(
    {
        "env",
        "xargs",
        "nohup",
        "nice",
        "ionice",
        "timeout",
        "time",
        "watch",
        "strace",
        "ltrace",
        "chroot",
        "setsid",
        "script",
        "eval",
        "exec",
        "command",
        "builtin",
        "stdbuf",
        "flock",
        "parallel",
        "at",
        "batch",
        "nsenter",
        "unshare",
        "runuser",
        "ssh",
        "scp",
        "sftp",
        "gdb",
    }
)
PRIVILEGED = frozenset({"sudo", "sudoedit", "su", "doas", "pkexec", "run0"})
DESTRUCTIVE = frozenset(
    {
        "rm",
        "rmdir",
        "shred",
        "dd",
        "wipefs",
        "fdisk",
        "sfdisk",
        "parted",
        "truncate",
        "srm",
        "lvremove",
        "vgremove",
        "pvremove",
        "mkswap",
    }
)
SYSTEM_CHANGE = frozenset(
    {
        "apt",
        "apt-get",
        "aptitude",
        "dpkg",
        "dnf",
        "yum",
        "rpm",
        "pacman",
        "zypper",
        "apk",
        "snap",
        "flatpak",
        "service",
        "chmod",
        "chown",
        "chgrp",
        "kill",
        "pkill",
        "killall",
        "reboot",
        "shutdown",
        "poweroff",
        "halt",
        "mount",
        "umount",
        "iptables",
        "ip6tables",
        "nft",
        "ufw",
        "firewall-cmd",
        "sysctl",
        "modprobe",
        "rmmod",
        "insmod",
        "crontab",
        "useradd",
        "usermod",
        "userdel",
        "groupadd",
        "passwd",
        "chpasswd",
        "visudo",
        "update-grub",
        "setenforce",
        "swapon",
        "swapoff",
        "ldconfig",
        "update-alternatives",
        "systemd-run",
    }
)
USER_LEVEL_CHANGE = frozenset(
    {"mkdir", "touch", "cp", "mv", "ln", "tee", "install", "unzip"}
)

SIMPLE_READ_ONLY = frozenset(
    {
        "uname",
        "free",
        "df",
        "uptime",
        "lscpu",
        "lsblk",
        "whoami",
        "id",
        "pwd",
        "nproc",
        "arch",
        "lsmod",
        "lspci",
        "lsusb",
        "vmstat",
        "iostat",
        "mpstat",
        "w",
        "who",
        "last",
        "echo",
        "ps",
        "lsof",
        "dig",
        "stat",
        "ls",
        "wc",
        "du",
        "readlink",
        "which",
        "findmnt",
        "blkid",
        "lsns",
        "lsipc",
        "sensors",
        "cat",
        "head",
        "grep",
        "date",
        "dmesg",
        "tail",
        "ss",
        "file",
        "find",
    }
)

# Flags that turn an otherwise read-only command into a modifying or
# long-running one. Long flags also match getopt-style unique abbreviations.
_DENIED_FLAGS: dict[str, dict[str, Sequence[str] | str]] = {
    "date": {"short": "s", "long": ("--set",)},
    "dmesg": {
        "short": "cCwDEn",
        "long": (
            "--clear",
            "--read-clear",
            "--follow",
            "--follow-new",
            "--console-on",
            "--console-off",
            "--console-level",
        ),
    },
    "tail": {"short": "fF", "long": ("--follow",)},
    "ss": {"short": "K", "long": ("--kill",)},
    "file": {"short": "C", "long": ("--compile",)},
    "find": {
        "single": (
            "-exec",
            "-execdir",
            "-ok",
            "-okdir",
            "-delete",
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-fls",
        )
    },
}

_SENSITIVE_PATH = re.compile(
    r"(?:^|/)\.(?:ssh|gnupg|aws|kube|netrc|git-credentials|npmrc|pypirc)(?:/|$)"
    r"|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|(?:^|/)\.env(?:\.[\w.-]*)?$"
    r"|(?:^|/)etc/(?:shadow|gshadow|sudoers)"
    r"|(?:^|/)proc/[^/]+/(?:environ|mem)$"
    r"|(?:^|/)credentials(?:\.json)?$"
    r"|\.docker/config\.json$"
    r"|\.(?:pem|key|p12|pfx|keystore)$",
    re.IGNORECASE,
)

SECRET_ENV_NAME = re.compile(
    r"key|token|secret|passw|credential|auth|cookie|session|private", re.IGNORECASE
)
RESERVED_ENV_NAMES = frozenset(
    {"PATH", "IFS", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "CDPATH"}
)
RESERVED_ENV_PREFIXES = ("LD_", "PYTHON", "PERL", "RUBY", "NODE_", "JAVA_TOOL")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def env_name_problem(name: str) -> str | None:
    """Why an environment variable may not be passed to a command, if so."""
    if not ENV_NAME.fullmatch(name):
        return "is not a valid variable name"
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES):
        return "is reserved and cannot be set"
    if SECRET_ENV_NAME.search(name):
        return "looks like it holds a secret and cannot be passed"
    return None


def _denied(args: Sequence[str], rules: Mapping[str, Sequence[str] | str]) -> bool:
    short = str(rules.get("short", ""))
    long_flags = tuple(rules.get("long", ()))
    single = tuple(rules.get("single", ()))
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if any(
                d == name or (len(name) >= 3 and d.startswith(name)) for d in long_flags
            ):
                return True
        elif arg.startswith("-") and len(arg) > 1:
            if arg in single:
                return True
            if short and any(c in short for c in arg[1:]):
                return True
    return False


def is_sensitive_arg(arg: str) -> bool:
    """True if an argument names credential or secret material."""
    if not arg or arg.startswith("-") and "/" not in arg:
        return False
    candidates = [arg, os.path.normpath(arg)]
    if len(arg) < 4096 and ("/" in arg or arg.startswith("~")):
        try:
            candidates.append(str(Path(arg).expanduser().resolve()))
        except (OSError, RuntimeError, ValueError):
            pass
    return any(_SENSITIVE_PATH.search(c) for c in candidates)


def _ro(reason: str = "recognised read-only command") -> Classification:
    return Classification(RiskClass.READ_ONLY, reason)


def _unknown(reason: str) -> Classification:
    return Classification(RiskClass.UNKNOWN, reason)


def _positionals(args: Sequence[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _hostname(args: Sequence[str]) -> Classification:
    if _positionals(args) or _denied(
        args, {"short": "Fb", "long": ("--file", "--boot")}
    ):
        return Classification(RiskClass.SYSTEM_CHANGE, "hostname with a value sets it")
    return _ro()


_IP_FLAGS = frozenset(
    {
        "-4",
        "-6",
        "-s",
        "-stats",
        "-statistics",
        "-br",
        "-brief",
        "-c",
        "-color",
        "-d",
        "-details",
        "-h",
        "-human",
        "-human-readable",
        "-j",
        "-json",
        "-o",
        "-oneline",
        "-p",
        "-pretty",
        "-a",
        "-all",
    }
)
_IP_OBJECTS = frozenset(
    {
        "addr",
        "address",
        "a",
        "link",
        "l",
        "route",
        "r",
        "neigh",
        "neighbor",
        "n",
        "rule",
        "maddr",
        "maddress",
    }
)
_IP_VERBS = frozenset({"show", "list", "ls", "lst", "get", "help"})


def _ip(args: Sequence[str]) -> Classification:
    positional: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            if positional:
                continue  # filters after the verb, e.g. ``show -s``
            if arg not in _IP_FLAGS:
                return _unknown(f"ip option {arg!r} is not recognised as read-only")
        else:
            positional.append(arg)
    if not positional or positional[0] not in _IP_OBJECTS:
        return _unknown("ip object is not recognised as read-only")
    if len(positional) > 1 and positional[1] not in _IP_VERBS:
        return _unknown("ip verb is not a read-only verb")
    return _ro()


_SYSTEMCTL_READ = frozenset(
    {
        "status",
        "is-active",
        "is-enabled",
        "is-failed",
        "show",
        "list-units",
        "list-unit-files",
        "list-timers",
        "list-sockets",
        "list-dependencies",
        "list-jobs",
        "cat",
        "get-default",
        "is-system-running",
    }
)
_SYSTEMCTL_VALUE_FLAGS = frozenset(
    {"-t", "--type", "-p", "--property", "-n", "--lines", "-o", "--output", "--state"}
)
_SYSTEMCTL_DENIED: dict[str, Sequence[str] | str] = {
    "short": "HMfs",
    "long": (
        "--host",
        "--machine",
        "--root",
        "--force",
        "--now",
        "--runtime",
        "--kill-whom",
        "--kill-who",
        "--signal",
    ),
}
_SYSTEMCTL_MUTATING = frozenset(
    {
        "start",
        "stop",
        "restart",
        "reload",
        "try-restart",
        "reload-or-restart",
        "enable",
        "disable",
        "mask",
        "unmask",
        "daemon-reload",
        "kill",
        "reset-failed",
        "set-default",
        "isolate",
        "edit",
        "link",
        "revert",
        "reboot",
        "poweroff",
        "halt",
        "suspend",
        "hibernate",
        "set-property",
    }
)


def _systemctl(args: Sequence[str]) -> Classification:
    if _denied(args, _SYSTEMCTL_DENIED):
        return _unknown("systemctl option is not allowed for read-only use")
    expect_value = False
    for arg in args:
        if expect_value:
            expect_value = False
            continue
        if arg in _SYSTEMCTL_VALUE_FLAGS:
            expect_value = True
            continue
        if arg.startswith("-"):
            continue
        if arg in _SYSTEMCTL_READ:
            return _ro("systemctl read-only subcommand")
        if arg in _SYSTEMCTL_MUTATING:
            return Classification(
                RiskClass.SYSTEM_CHANGE, f"systemctl {arg} changes services"
            )
        return _unknown("systemctl subcommand is not recognised as read-only")
    # With no subcommand, systemctl runs "list-units", which is read-only.
    # Only options were given (e.g. --failed); dangerous ones were refused above.
    if any(a.startswith("-") for a in args):
        return _ro("systemctl defaults to list-units")
    return _unknown("systemctl needs a recognised read-only subcommand")


_JOURNALCTL_DENIED: dict[str, Sequence[str] | str] = {
    "short": "f",
    "long": (
        "--follow",
        "--vacuum-size",
        "--vacuum-time",
        "--vacuum-files",
        "--rotate",
        "--flush",
        "--sync",
        "--relinquish-var",
        "--smart-relinquish-var",
        "--setup-keys",
        "--update-catalog",
        "--cursor-file",
        "--verify",
    ),
}


def _journalctl(args: Sequence[str]) -> Classification:
    if _denied(args, _JOURNALCTL_DENIED):
        return Classification(
            RiskClass.SYSTEM_CHANGE, "journalctl option modifies or follows logs"
        )
    return _ro()


_DOCKER_READ = frozenset(
    {"ps", "images", "info", "version", "inspect", "top", "port", "diff", "history"}
)
_DOCKER_GROUPS = frozenset({"container", "image", "volume", "network"})
_DOCKER_GROUP_READ = frozenset(
    {"ls", "list", "inspect", "history", "logs", "top", "port", "diff"}
)
_DOCKER_DESTRUCTIVE = frozenset({"rm", "rmi", "prune"})


def _docker(args: Sequence[str]) -> Classification:
    if not args or args[0].startswith("-"):
        return _unknown("docker global options are not recognised as read-only")
    verb, rest = args[0], args[1:]
    if verb in _DOCKER_READ:
        return _ro("docker read-only subcommand")
    if verb == "logs":
        return (
            _unknown("docker logs -f follows indefinitely")
            if _denied(rest, {"short": "f", "long": ("--follow",)})
            else _ro("docker logs")
        )
    if verb == "stats":
        return (
            _ro("docker stats --no-stream")
            if "--no-stream" in rest
            else _unknown("docker stats needs --no-stream")
        )
    if verb == "system" and rest[:1] in (["df"], ["info"]):
        return _ro("docker system df/info")
    if verb in _DOCKER_GROUPS and rest[:1] and rest[0] in _DOCKER_GROUP_READ:
        return _ro("docker read-only subcommand")
    if verb in _DOCKER_DESTRUCTIVE or any(a in _DOCKER_DESTRUCTIVE for a in rest[:1]):
        return Classification(RiskClass.DESTRUCTIVE, "docker removes resources")
    if verb in {
        "run",
        "exec",
        "start",
        "stop",
        "restart",
        "kill",
        "pull",
        "push",
        "build",
        "create",
        "compose",
    }:
        return Classification(
            RiskClass.SYSTEM_CHANGE, f"docker {verb} changes system state"
        )
    return _unknown("docker subcommand is not recognised as read-only")


def _ps(args: Sequence[str]) -> Classification:
    # BSD-style option groups come first; ``e`` there prints each process's
    # environment, which can contain secrets.
    for arg in args:
        if arg.startswith("-"):
            break
        if re.fullmatch(r"[A-Za-z]+", arg) and "e" in arg:
            return _unknown("ps 'e' option prints process environments")
    return _ro()


_SPECIAL_RULES: dict[str, Callable[[Sequence[str]], Classification]] = {
    "ps": _ps,
    "hostname": _hostname,
    "ip": _ip,
    "systemctl": _systemctl,
    "journalctl": _journalctl,
    "docker": _docker,
}


def classify(name: str, args: Sequence[str]) -> Classification:
    """Classify one proposed command. Conservative: unknown means blocked."""
    if not EXECUTABLE_NAME.fullmatch(name):
        return Classification(
            RiskClass.UNSUPPORTED, "executable must be a plain command name"
        )
    if name in SHELLS:
        return Classification(
            RiskClass.UNSUPPORTED, "shell interpreters are not supported"
        )
    if name in WRAPPERS:
        return Classification(
            RiskClass.UNSUPPORTED, "commands that run other commands are not supported"
        )
    if name in PRIVILEGED:
        return Classification(
            RiskClass.PRIVILEGED, "privilege escalation is never automatic"
        )
    if any(is_sensitive_arg(a) for a in args):
        return Classification(
            RiskClass.SENSITIVE, "argument refers to credentials or secret material"
        )
    if name in DESTRUCTIVE or name.startswith("mkfs"):
        return Classification(RiskClass.DESTRUCTIVE, "destructive command")
    if name in SYSTEM_CHANGE:
        return Classification(RiskClass.SYSTEM_CHANGE, "modifies system state")
    if name in USER_LEVEL_CHANGE:
        return Classification(RiskClass.USER_LEVEL_CHANGE, "modifies files")
    if name in _SPECIAL_RULES:
        return _SPECIAL_RULES[name](args)
    if name == "date" and any(not a.startswith(("-", "+")) for a in args):
        return Classification(
            RiskClass.SYSTEM_CHANGE, "date with a bare value sets the clock"
        )
    if name in SIMPLE_READ_ONLY:
        rules = _DENIED_FLAGS.get(name)
        if rules and _denied(args, rules):
            return _unknown(f"{name} option is not allowed for read-only use")
        return _ro()
    return _unknown("command is not in the read-only allowlist")
