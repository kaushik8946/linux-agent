"""Running the agent as root via sudo (hostplatform/identity.py).

Real UID changes need root, so the privilege calls are recorded by a stub.
"""

import os
import pwd
from pathlib import Path
from typing import Any

import pytest

from hostplatform import identity
from hostplatform.identity import Identity, as_user, detect


class Entry:
    def __init__(self, name: str, uid: int, gid: int, home: str) -> None:
        self.pw_name, self.pw_uid, self.pw_gid, self.pw_dir = name, uid, gid, home


def lookup(table: dict[int, Entry]) -> Any:
    def get(uid: int) -> pwd.struct_passwd:
        if uid not in table:
            raise KeyError(uid)
        return table[uid]  # type: ignore[return-value]

    return get


ALICE = {1000: Entry("alice", 1000, 1000, "/home/alice")}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    log: list[tuple[str, Any]] = []
    monkeypatch.setattr(os, "seteuid", lambda v: log.append(("seteuid", v)))
    monkeypatch.setattr(os, "setegid", lambda v: log.append(("setegid", v)))
    monkeypatch.setattr(os, "setgroups", lambda v: log.append(("setgroups", list(v))))
    monkeypatch.setattr(os, "getgroups", lambda: [0, 4])
    monkeypatch.setattr(os, "getgrouplist", lambda _u, g: [g, 27], raising=False)
    return log


# -- detection ---------------------------------------------------------------------


def test_normal_user_is_not_demoted() -> None:
    ident = detect({"HOME": "/home/bob"}, geteuid=lambda: 1000, getpwuid=lookup(ALICE))
    assert not ident.is_root and not ident.demote
    assert ident.home == Path("/home/bob")


def test_sudo_identifies_the_invoking_user() -> None:
    env = {"SUDO_UID": "1000", "SUDO_GID": "1000", "HOME": "/root"}
    ident = detect(env, geteuid=lambda: 0, getpwuid=lookup(ALICE))
    assert ident.is_root and ident.via_sudo and ident.demote
    assert ident.user == "alice" and ident.home == Path("/home/alice")


def test_root_without_sudo_is_not_demoted() -> None:
    ident = detect(
        {}, geteuid=lambda: 0, getpwuid=lookup({0: Entry("root", 0, 0, "/root")})
    )
    assert ident.is_root and not ident.via_sudo and not ident.demote


def test_sudo_uid_zero_is_not_trusted_as_a_user() -> None:
    env = {"SUDO_UID": "0", "SUDO_GID": "0"}
    ident = detect(
        env, geteuid=lambda: 0, getpwuid=lookup({0: Entry("root", 0, 0, "/root")})
    )
    assert not ident.demote


@pytest.mark.parametrize("value", ["abc", "-1", "", "1000x"])
def test_malformed_sudo_ids_are_ignored(value: str) -> None:
    env = {"SUDO_UID": value, "SUDO_GID": "1000"}
    ident = detect(
        env, geteuid=lambda: 0, getpwuid=lookup({0: Entry("root", 0, 0, "/root")})
    )
    assert not ident.via_sudo


def test_unknown_sudo_user_is_not_trusted() -> None:
    env = {"SUDO_UID": "4242", "SUDO_GID": "4242"}
    ident = detect(
        env, geteuid=lambda: 0, getpwuid=lookup({0: Entry("root", 0, 0, "/root")})
    )
    assert not ident.via_sudo


def test_sudo_variables_ignored_when_not_root() -> None:
    env = {"SUDO_UID": "1000", "SUDO_GID": "1000"}
    ident = detect(env, geteuid=lambda: 1001, getpwuid=lookup(ALICE))
    assert not ident.via_sudo and not ident.demote


# -- switching ------------------------------------------------------------------------


def demoted() -> Identity:
    return Identity(0, True, "alice", 1000, 1000, Path("/home/alice"))


def test_as_user_is_a_no_op_when_not_demoting(calls: list[tuple[str, Any]]) -> None:
    with as_user(Identity(1000, False, "bob", 1000, 1000, Path("/home/bob"))):
        pass
    assert calls == []


def test_as_user_switches_groups_then_ids_and_restores(
    calls: list[tuple[str, Any]],
) -> None:
    with as_user(demoted()):
        assert calls == [
            ("setgroups", [1000, 27]),
            ("setegid", 1000),
            ("seteuid", 1000),
        ]
    assert calls[-3:] == [("seteuid", 0), ("setegid", 0), ("setgroups", [0, 4])]


def test_as_user_restores_even_when_the_block_fails(
    calls: list[tuple[str, Any]],
) -> None:
    with pytest.raises(RuntimeError), as_user(demoted()):
        raise RuntimeError("boom")
    assert ("seteuid", 0) in calls and ("setgroups", [0, 4]) in calls


def test_nested_as_user_switches_once(calls: list[tuple[str, Any]]) -> None:
    with as_user(demoted()), as_user(demoted()):
        pass
    assert [c for c in calls if c[0] == "seteuid"] == [
        ("seteuid", 1000),
        ("seteuid", 0),
    ]


def test_user_process_ids_only_when_demoting(calls: list[tuple[str, Any]]) -> None:
    plain = Identity(1000, False, "bob", 1000, 1000, Path("/home/bob"))
    assert identity.user_process_ids(plain) is None
    uid, gid, groups = identity.user_process_ids(demoted())  # type: ignore[misc]
    assert (uid, gid) == (1000, 1000) and 27 in groups


# -- the agent's own paths follow the invoking user ---------------------------------------


def test_default_workspace_is_the_invoking_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config import load_config

    monkeypatch.setattr(identity, "_current", demoted())
    cfg = load_config(
        {"OPENAI_API_KEY": "sk-abcdefgh1234", "OPENAI_BASE_URL": "https://g.example/v1",
         "OPENAI_MODEL": "m"},
        base_dir=Path("/tmp"),
    )  # fmt: skip
    assert cfg.workspace_root == Path("/home/alice/projects")
    monkeypatch.setattr(identity, "_current", None)


def test_expand_home_for_a_root_only_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import expand_home

    monkeypatch.setattr(
        identity, "_current", Identity(0, False, "root", 0, 0, Path("/root"))
    )
    assert expand_home(Path("~/projects")) == Path("/root/projects")
    monkeypatch.setattr(identity, "_current", None)


def test_journal_reads_and_writes_inside_as_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]
) -> None:
    from agent.task_store import TaskJournal

    monkeypatch.setattr(identity, "_current", demoted())
    journal = TaskJournal(tmp_path / "t.jsonl")
    journal.record("t-1", "task_started")
    assert ("seteuid", 1000) in calls  # the write happened as the user
    assert calls[-1] == ("setgroups", [0, 4])  # and root is back afterwards
    monkeypatch.setattr(identity, "_current", None)
