"""Who the agent runs as, and who owns the files it writes.

Started normally, the agent runs as you and nothing here changes anything.

Started with ``sudo linux-agent``, the process is root, so every command you
approve runs as root and the package and service adapters no longer need to
ask sudo for a password. The agent's own files, however, belong to the user
who ran sudo, not to root:

* "~" means that user's home, not ``/root``;
* the task journal, reports, the managed environment file and new projects
  are written *as that user* (effective UID/GID are switched for the write),
  so they keep normal ownership and a planted symlink cannot make root write
  or read somewhere it should not;
* checks that execute user-editable files (the environment-variable probe)
  run as that user, never as root.

The invoking user comes from ``SUDO_UID``/``SUDO_GID``, which are only
trusted when the process really is root (only root can have set them).
"""

from __future__ import annotations

import os
import pwd
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    euid: int
    via_sudo: bool  # root, started by sudo on behalf of another user
    user: str | None  # the person the agent works for
    uid: int | None
    gid: int | None
    home: Path

    @property
    def is_root(self) -> bool:
        return self.euid == 0

    @property
    def demote(self) -> bool:
        """True when user files must be written as the invoking user."""
        return self.is_root and self.via_sudo and self.uid not in (None, 0)


def _int(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def detect(
    environ: Mapping[str, str] | None = None,
    geteuid: Callable[[], int] = os.geteuid,
    getpwuid: Callable[[int], pwd.struct_passwd] = pwd.getpwuid,
) -> Identity:
    env = os.environ if environ is None else environ
    euid = geteuid()
    if euid == 0:
        uid, gid = _int(env.get("SUDO_UID")), _int(env.get("SUDO_GID"))
        if uid is not None and gid is not None and uid != 0:
            try:
                entry = getpwuid(uid)
            except KeyError:
                entry = None
            if entry is not None:
                return Identity(euid, True, entry.pw_name, uid, gid, Path(entry.pw_dir))
    try:
        entry = getpwuid(euid)
        name, home = entry.pw_name, Path(entry.pw_dir)
    except KeyError:
        name, home = None, Path.home()
    if euid != 0 and env.get("HOME"):
        home = Path(env["HOME"])  # an unprivileged user may point HOME elsewhere
    return Identity(euid, False, name, euid, os.getegid(), home)


_current: Identity | None = None
_switch_lock = threading.RLock()
_depth = 0


def current() -> Identity:
    global _current
    if _current is None:
        _current = detect()
    return _current


def set_current(identity: Identity | None) -> None:
    """Replace the cached identity (tests, or re-detection)."""
    global _current
    _current = identity


@contextmanager
def as_user(identity: Identity | None = None) -> Iterator[None]:
    """Run the block with the invoking user's effective UID/GID.

    A no-op unless the agent is root via sudo. If the switch back to root
    fails, the process exits instead of continuing with confused privileges.
    """
    global _depth
    ident = identity or current()
    if not ident.demote:
        yield
        return
    assert ident.uid is not None and ident.gid is not None
    with _switch_lock:
        saved_groups: list[int] = []
        if _depth == 0:
            # Drop root's supplementary groups too, or group-root files would
            # stay reachable while acting as the user.
            saved_groups = os.getgroups()
            os.setgroups(_user_groups(ident))
            try:
                os.setegid(ident.gid)
                os.seteuid(ident.uid)
            except BaseException:
                _restore(saved_groups)
                raise
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
            if _depth == 0:
                _restore(saved_groups)


def _user_groups(ident: Identity) -> list[int]:
    assert ident.gid is not None
    if ident.user:
        try:
            return os.getgrouplist(ident.user, ident.gid)
        except OSError:
            pass
    return [ident.gid]


def _restore(groups: list[int]) -> None:
    try:
        os.seteuid(0)
        os.setegid(0)
        os.setgroups(groups)
    except OSError:  # pragma: no cover - cannot fail for a real root
        os._exit(70)


def user_process_ids(
    identity: Identity | None = None,
) -> tuple[int, int, tuple[int, ...]] | None:
    """(uid, gid, groups) a child must run as to act for the user, or None."""
    ident = identity or current()
    if not ident.demote:
        return None
    assert ident.uid is not None and ident.gid is not None
    return ident.uid, ident.gid, tuple(_user_groups(ident))
