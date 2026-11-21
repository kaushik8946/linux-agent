import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent.command_plan import CommandPlan, parse_plan
from agent.command_policy import Classification, RiskClass
from hostplatform.command_runner import CommandRunner, Limits, Status

ALLOW_ALL = frozenset(RiskClass)


def plan(*steps: dict[str, Any]) -> CommandPlan:
    full = []
    for i, s in enumerate(steps):
        full.append({"id": f"s{i}", "purpose": "test", "args": [], **s})
    parsed, errors = parse_plan({"steps": full})
    assert parsed is not None, errors
    return parsed


def runner(tmp_path: Path, **kw: Any) -> CommandRunner:
    kw.setdefault("allowed_roots", [tmp_path])
    kw.setdefault("default_cwd", tmp_path)
    return CommandRunner(**kw)


def one(r: CommandRunner, **step: Any) -> Any:
    result = r.run_plan(plan(step))
    return result.results[0]


def read_only_anything(name: str, args: Any) -> Classification:
    return Classification(RiskClass.READ_ONLY, "test policy")


# -- normal execution ----------------------------------------------------------


def test_harmless_command(tmp_path: Path) -> None:
    res = one(runner(tmp_path), executable="echo", args=["hello", "world"])
    assert res.status is Status.OK
    assert res.exit_code == 0
    assert res.stdout == "hello world\n"
    assert res.duration_seconds >= 0
    assert res.preview == "echo hello world"


def test_multi_step_plan_runs_in_order(tmp_path: Path) -> None:
    result = runner(tmp_path).run_plan(
        plan(
            {"executable": "uname", "args": ["-s"]},
            {"executable": "echo", "args": ["2"]},
        )
    )
    assert result.ok and result.executed
    assert [r.stdout for r in result.results] == ["Linux\n", "2\n"]


def test_non_zero_exit_stops_plan(tmp_path: Path) -> None:
    r = runner(tmp_path, policy=read_only_anything)
    result = r.run_plan(
        plan({"executable": "false"}, {"executable": "echo", "args": ["x"]})
    )
    assert result.results[0].status is Status.FAILED
    assert result.results[0].exit_code == 1
    assert result.results[1].status is Status.SKIPPED
    assert not result.ok


def test_stderr_captured(tmp_path: Path) -> None:
    res = one(runner(tmp_path), executable="ls", args=[str(tmp_path / "missing")])
    assert res.status is Status.FAILED
    assert "No such file" in res.stderr


# -- shell metacharacters stay literal ------------------------------------------


@pytest.mark.parametrize(
    "arg",
    ["$(id)", "`id`", "a;id", "a|id", "a && id", "> /tmp/pwned", "*", "~", "$HOME",
     "${PATH}", "a\nid", "'quoted'", "!", "{a,b}"],
)  # fmt: skip
def test_metacharacters_are_literal(tmp_path: Path, arg: str) -> None:
    res = one(runner(tmp_path), executable="echo", args=[arg])
    assert res.status is Status.OK
    assert res.stdout == arg + "\n"


def test_redirection_creates_no_file(tmp_path: Path) -> None:
    target = tmp_path / "pwned"
    one(runner(tmp_path), executable="echo", args=["x", ">", str(target)])
    assert not target.exists()


def test_glob_not_expanded(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("")
    res = one(runner(tmp_path), executable="ls", args=["*.txt"])
    assert res.status is Status.FAILED  # literal "*.txt" does not exist


def test_no_shell_process_is_spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    seen: list[Any] = []
    real = subprocess.Popen

    def spy(argv: Any, **kw: Any) -> Any:
        seen.append((argv, kw))
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    one(runner(tmp_path), executable="echo", args=["$(id)"])
    ((argv, kw),) = seen
    assert kw["shell"] is False
    assert isinstance(argv, list)
    assert os.path.basename(argv[0]) == "echo"
    assert argv[1] == "$(id)"


@pytest.mark.parametrize("shell", ["bash", "sh", "dash", "env", "xargs"])
def test_shells_cannot_run_even_with_permissive_risk_set(
    tmp_path: Path, shell: str
) -> None:
    r = runner(tmp_path, allowed_risks=frozenset({RiskClass.READ_ONLY}))
    res = one(r, executable=shell, args=["-c", "id"])
    assert res.status is Status.BLOCKED
    assert res.risk == "UNSUPPORTED"


# -- policy enforcement ---------------------------------------------------------


@pytest.mark.parametrize(
    ("executable", "args", "risk"),
    [
        ("rm", ["-rf", "/tmp/nothing"], "DESTRUCTIVE"),
        ("sudo", ["ls"], "PRIVILEGED"),
        ("mkdir", ["x"], "USER_LEVEL_CHANGE"),
        ("chmod", ["777", "x"], "SYSTEM_CHANGE"),
        ("python3", ["-c", "print(1)"], "UNKNOWN"),
        ("cat", ["/etc/shadow"], "SENSITIVE"),
    ],
)
def test_non_read_only_commands_are_blocked(
    tmp_path: Path, executable: str, args: list[str], risk: str
) -> None:
    if not shutil.which(executable):
        pytest.skip(f"{executable} not installed")
    res = one(runner(tmp_path), executable=executable, args=args)
    assert res.status is Status.BLOCKED
    assert res.risk == risk
    assert res.exit_code is None


def test_blocked_step_prevents_whole_plan(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    r = runner(tmp_path)
    result = r.run_plan(
        plan(
            {"executable": "touch", "args": [str(marker)]},
            {"executable": "rm", "args": ["-rf", str(tmp_path)]},
        )
    )
    assert not result.executed
    assert not marker.exists()
    assert tmp_path.exists()
    assert {x.status for x in result.results} == {Status.BLOCKED}


def test_one_bad_step_blocks_good_steps_too(tmp_path: Path) -> None:
    result = runner(tmp_path).run_plan(
        plan(
            {"executable": "echo", "args": ["ok"]}, {"executable": "rm", "args": ["x"]}
        )
    )
    assert not result.executed
    assert result.results[0].status is Status.SKIPPED
    assert result.results[0].stdout == ""


# -- executable resolution ------------------------------------------------------


def test_missing_executable(tmp_path: Path) -> None:
    res = one(runner(tmp_path), executable="definitely-not-a-command")
    assert res.status is Status.BLOCKED
    assert "not found" in (res.reason or "")


def _make_bin(directory: Path, name: str, body: str = "#!/bin/sh\necho fake\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text(body)
    exe.chmod(0o755)
    return exe


def test_ambiguous_executable_rejected(tmp_path: Path) -> None:
    a = _make_bin(tmp_path / "a", "mytool")
    b = _make_bin(tmp_path / "b", "mytool")
    for d in (a.parent, b.parent):
        d.chmod(0o755)
    r = runner(tmp_path, path_dirs=[a.parent, b.parent], require_root_owned=False,
               policy=read_only_anything)  # fmt: skip
    res = one(r, executable="mytool")
    assert res.status is Status.BLOCKED
    assert "ambiguous" in (res.reason or "")


def test_untrusted_writable_dir_is_ignored(tmp_path: Path) -> None:
    exe = _make_bin(tmp_path / "evil", "uname")
    exe.parent.chmod(0o777)
    r = runner(tmp_path, path_dirs=[exe.parent, "/usr/bin"], require_root_owned=False)
    assert exe.parent.resolve() not in r.trusted_dirs
    res = one(r, executable="uname", args=["-s"])
    assert res.stdout == "Linux\n"


def test_user_owned_path_dir_not_trusted_by_default(tmp_path: Path) -> None:
    exe = _make_bin(tmp_path / "home_bin", "uname")
    exe.parent.chmod(0o755)
    r = runner(tmp_path, path_dirs=[exe.parent, "/usr/bin"])
    assert exe.parent.resolve() not in r.trusted_dirs


def test_relative_path_dirs_ignored(tmp_path: Path) -> None:
    r = runner(tmp_path, path_dirs=[".", "bin", "/usr/bin"])
    assert r.trusted_dirs == [Path("/usr/bin").resolve()]


@pytest.mark.parametrize(
    "exe",
    ["./uname", "../bin/uname", "/usr/bin/../bin/uname", "/tmp/uname", "bin/uname"],
)
def test_untrusted_executable_paths_rejected(tmp_path: Path, exe: str) -> None:
    res = one(runner(tmp_path), executable=exe)
    assert res.status is Status.BLOCKED


def test_absolute_path_in_trusted_dir_accepted(tmp_path: Path) -> None:
    uname = shutil.which("uname", path="/usr/bin:/bin")
    assert uname
    res = one(runner(tmp_path), executable=uname, args=["-s"])
    assert res.status is Status.OK


# -- working directory ----------------------------------------------------------


def test_working_dir_inside_allowed_root(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    res = one(runner(tmp_path), executable="pwd", working_dir=str(sub))
    assert res.stdout.strip() == str(sub.resolve())


@pytest.mark.parametrize("wd", ["/", "/etc", "/root", "/nonexistent-dir"])
def test_working_dir_outside_roots_rejected(tmp_path: Path, wd: str) -> None:
    res = one(runner(tmp_path), executable="pwd", working_dir=wd)
    assert res.status is Status.BLOCKED


def test_working_dir_symlink_escape_rejected(tmp_path: Path) -> None:
    link = tmp_path / "escape"
    link.symlink_to("/etc")
    res = one(runner(tmp_path), executable="pwd", working_dir=str(link))
    assert res.status is Status.BLOCKED


def test_working_dir_that_is_a_file_rejected(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("")
    assert (
        one(runner(tmp_path), executable="pwd", working_dir=str(f)).status
        is Status.BLOCKED
    )


def test_working_dir_sensitive_rejected(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    assert (
        one(runner(tmp_path), executable="ls", working_dir=str(ssh)).status
        is Status.BLOCKED
    )


# -- environment ----------------------------------------------------------------


def test_environment_is_minimal(tmp_path: Path) -> None:
    r = runner(tmp_path, environ={"PATH": "/usr/bin:/bin", "HOME": "/home/x",
                                  "OPENAI_API_KEY": "sk-should-not-leak-123456",
                                  "RANDOM_VAR": "nope"}, policy=read_only_anything)  # fmt: skip
    res = one(r, executable="printenv")
    names = {line.split("=", 1)[0] for line in res.stdout.splitlines()}
    assert names == {"PATH", "LC_ALL", "HOME"}
    assert "sk-should-not-leak" not in res.stdout


def test_env_allowlist_passes_named_benign_vars(tmp_path: Path) -> None:
    r = runner(
        tmp_path,
        environ={"PATH": "/usr/bin:/bin", "TZ": "UTC"},
        policy=read_only_anything,
    )
    res = one(r, executable="printenv", args=["TZ"], env_allowlist=["TZ"])
    assert res.stdout == "UTC\n"


@pytest.mark.parametrize(
    "name", ["LD_PRELOAD", "PATH", "OPENAI_API_KEY", "BASH_ENV", "PYTHONPATH"]
)
def test_env_allowlist_rejects_dangerous_names(tmp_path: Path, name: str) -> None:
    res = one(runner(tmp_path), executable="echo", env_allowlist=[name])
    assert res.status is Status.BLOCKED


def test_path_given_to_child_is_trusted_dirs_only(tmp_path: Path) -> None:
    r = runner(
        tmp_path,
        environ={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        policy=read_only_anything,
    )
    res = one(r, executable="printenv", args=["PATH"])
    assert str(tmp_path) not in res.stdout


# -- timeout, cancellation, output limits -----------------------------------------


def test_timeout_kills_process(tmp_path: Path) -> None:
    r = runner(tmp_path, policy=read_only_anything)
    start = time.monotonic()
    res = one(r, executable="sleep", args=["30"], timeout_seconds=1)
    assert time.monotonic() - start < 6
    assert res.status is Status.TIMEOUT
    assert res.timed_out


def test_timeout_capped_by_application_limit(tmp_path: Path) -> None:
    r = runner(
        tmp_path,
        policy=read_only_anything,
        limits=Limits(max_timeout=1, kill_grace=0.5),
    )
    start = time.monotonic()
    res = one(r, executable="sleep", args=["30"], timeout_seconds=300)
    assert res.status is Status.TIMEOUT
    assert time.monotonic() - start < 5


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    script = _make_bin(tmp_path / "bin", "spawner",
                       f"#!/bin/sh\nsleep 30 &\necho $! > {tmp_path}/child\nwait\n")  # fmt: skip
    script.parent.chmod(0o755)
    r = runner(tmp_path, path_dirs=[script.parent], require_root_owned=False,
               policy=read_only_anything)  # fmt: skip
    one(r, executable="spawner", timeout_seconds=1)
    time.sleep(0.3)
    child = int((tmp_path / "child").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_cancellation(tmp_path: Path) -> None:
    r = runner(tmp_path, policy=read_only_anything)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    result = r.run_plan(
        plan(
            {"executable": "sleep", "args": ["30"]},
            {"executable": "echo", "args": ["after"]},
        ),
        cancel,
    )
    assert time.monotonic() - start < 5
    assert result.results[0].status is Status.CANCELLED
    assert result.results[1].status is Status.SKIPPED


def test_output_truncated_to_step_limit(tmp_path: Path) -> None:
    res = one(runner(tmp_path, policy=read_only_anything),
              executable="head", args=["-c", "100000", "/dev/zero"], max_output_bytes=1000)  # fmt: skip
    assert len(res.stdout.encode()) <= 1000
    assert res.stdout_truncated


def test_step_limit_cannot_exceed_application_cap(tmp_path: Path) -> None:
    r = runner(tmp_path, policy=read_only_anything, limits=Limits(max_output_bytes=500))
    res = one(
        r,
        executable="head",
        args=["-c", "5000", "/dev/urandom"],
        max_output_bytes=100000,
    )
    assert len(res.stdout.encode()) <= 500
    assert res.stdout_truncated


def test_endless_output_is_stopped(tmp_path: Path) -> None:
    r = runner(tmp_path, policy=read_only_anything,
               limits=Limits(hard_read_bytes=2_000_000, kill_grace=0.5))  # fmt: skip
    start = time.monotonic()
    res = one(r, executable="yes", timeout_seconds=60)
    assert time.monotonic() - start < 10
    assert res.stdout_truncated
    assert "hard read limit" in (res.reason or "")


def test_small_output_not_marked_truncated(tmp_path: Path) -> None:
    res = one(runner(tmp_path), executable="echo", args=["x"])
    assert not res.stdout_truncated and not res.stderr_truncated


def test_invalid_utf8_output_does_not_crash(tmp_path: Path) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\xff\xfe\x00abc\x80")
    res = one(runner(tmp_path), executable="cat", args=[str(f)])
    assert res.status is Status.OK
    assert "abc" in res.stdout and "\x00" not in res.stdout


# -- stdin chaining -------------------------------------------------------------


def test_stdin_from_previous_step(tmp_path: Path) -> None:
    result = runner(tmp_path).run_plan(
        plan(
            {"executable": "echo", "args": ["one two three"]},
            {"executable": "wc", "args": ["-w"], "stdin_from_step": "s0"},
        )
    )
    assert result.results[1].stdout.strip() == "3"


def test_stdin_uses_only_bounded_output(tmp_path: Path) -> None:
    result = runner(tmp_path, policy=read_only_anything).run_plan(
        plan(
            {
                "executable": "head",
                "args": ["-c", "50000", "/dev/zero"],
                "max_output_bytes": 100,
            },
            {"executable": "wc", "args": ["-c"], "stdin_from_step": "s0"},
        )
    )
    assert int(result.results[1].stdout.strip()) <= 100


def test_steps_without_stdin_get_devnull(tmp_path: Path) -> None:
    res = one(
        runner(tmp_path, policy=read_only_anything), executable="cat", timeout_seconds=5
    )
    assert res.status is Status.OK and res.stdout == ""


# -- redaction and preview -------------------------------------------------------


def test_secrets_redacted_from_output(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("password=hunter2hunter2\nok line\n")
    res = one(runner(tmp_path), executable="cat", args=[str(f)])
    assert "hunter2" not in res.stdout
    assert "ok line" in res.stdout


def test_known_env_secret_values_redacted(tmp_path: Path) -> None:
    secret = "verysecretvalue987"
    r = runner(tmp_path, environ={"PATH": "/usr/bin:/bin", "MY_TOKEN": secret})
    res = one(r, executable="echo", args=[f"value {secret}"])
    assert secret not in res.stdout
    assert secret not in res.preview


def test_preview_is_shell_quoted_but_never_executed(tmp_path: Path) -> None:
    r = runner(tmp_path)
    p = plan({"executable": "echo", "args": ["a b", "$(id)"]})
    text = r.preview(p)
    assert "echo 'a b' '$(id)'" in text
    assert "READ_ONLY" in text


def test_preview_shows_blocked_steps(tmp_path: Path) -> None:
    text = runner(tmp_path).preview(plan({"executable": "rm", "args": ["-rf", "/"]}))
    assert "BLOCKED" in text and "DESTRUCTIVE" in text


def test_preview_starts_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("preview must not start a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    runner(tmp_path).preview(plan({"executable": "echo", "args": ["x"]}))


def test_model_dict_marks_output_untrusted(tmp_path: Path) -> None:
    data = (
        runner(tmp_path)
        .run_plan(plan({"executable": "echo", "args": ["x"]}))
        .to_model_dict()
    )
    assert "untrusted" in data["note"]
    assert data["results"][0]["stdout"] == "x\n"


def test_blocked_result_dict_has_no_output_fields(tmp_path: Path) -> None:
    data = (
        runner(tmp_path)
        .run_plan(plan({"executable": "rm", "args": ["x"]}))
        .to_model_dict()
    )
    assert "stdout" not in data["results"][0]


def test_unreadable_trusted_dir_is_skipped(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        r = runner(tmp_path, path_dirs=[locked, "/usr/bin"], require_root_owned=False)
        assert one(r, executable="echo", args=["x"]).status is Status.OK
    finally:
        locked.chmod(stat.S_IRWXU)


# -- stepwise execution: the model sees each result before the next step --------


def test_stepwise_runs_only_first_step(tmp_path: Path) -> None:
    marker = tmp_path / "second"
    p = parse_plan(
        {
            "mode": "stepwise",
            "steps": [
                {"id": "a", "purpose": "p", "executable": "echo", "args": ["one"]},
                {
                    "id": "b",
                    "purpose": "p",
                    "executable": "touch",
                    "args": [str(marker)],
                },
            ],
        }
    )[0]
    assert p is not None
    r = CommandRunner(
        allowed_roots=[tmp_path],
        default_cwd=tmp_path,
        allowed_risks=frozenset({RiskClass.READ_ONLY, RiskClass.USER_LEVEL_CHANGE}),
    )
    result = r.run_plan(p)
    assert [x.step_id for x in result.results] == ["a"]
    assert result.pending == ["b"]
    assert not marker.exists()
    data = result.to_model_dict()
    assert data["pending_steps"] == ["b"]
    assert "have not run" in data["note_pending"]


def test_stepwise_still_validates_every_step_first(tmp_path: Path) -> None:
    p = parse_plan(
        {
            "mode": "stepwise",
            "steps": [
                {"id": "a", "purpose": "p", "executable": "echo", "args": ["one"]},
                {"id": "b", "purpose": "p", "executable": "rm", "args": ["x"]},
            ],
        }
    )[0]
    assert p is not None
    result = runner(tmp_path).run_plan(p)
    assert not result.executed
    assert result.results[0].status is Status.SKIPPED


def test_batch_is_the_default_mode() -> None:
    p = parse_plan(
        {"steps": [{"id": "a", "purpose": "p", "executable": "echo", "args": []}]}
    )[0]
    assert p is not None and p.mode == "batch"


def test_invalid_mode_rejected() -> None:
    plan_, errors = parse_plan(
        {
            "mode": "yolo",
            "steps": [{"id": "a", "purpose": "p", "executable": "echo", "args": []}],
        }
    )
    assert plan_ is None and errors


def test_batch_has_no_pending(tmp_path: Path) -> None:
    result = runner(tmp_path).run_plan(plan({"executable": "echo", "args": ["x"]}))
    assert result.pending == []
    assert "pending_steps" not in result.to_model_dict()


def test_schema_exposes_mode_to_the_model() -> None:
    from agent.llm_client import EXECUTE_COMMAND_PLAN_TOOL

    mode = EXECUTE_COMMAND_PLAN_TOOL["function"]["parameters"]["properties"]["mode"]
    assert mode["enum"] == ["batch", "stepwise"]


# -- executable resolution uses discovered host capabilities ---------------------


def _profile(tmp_path: Path, *present: str, systemd: bool = True) -> Any:
    from hostplatform.discovery import discover
    from tests.test_discovery import UBUNTU, fake_which, make_root

    root = make_root(tmp_path / "hostroot", UBUNTU, systemd_running=systemd)
    return discover(
        root=root,
        which=fake_which(*present),
        euid=lambda: 1000,
        uname=lambda: ("Linux", "6", "x86_64"),
    )


def test_discovered_missing_utility_is_rejected_with_reason(tmp_path: Path) -> None:
    host = _profile(tmp_path, "free")  # lsof not discovered
    r = CommandRunner.for_host(host, allowed_roots=[tmp_path], default_cwd=tmp_path)
    res = one(r, executable="lsof", args=["-v"])
    assert res.status is Status.BLOCKED
    assert "discovery" in (res.reason or "")


def test_systemctl_rejected_when_systemd_not_running(tmp_path: Path) -> None:
    host = _profile(tmp_path, "systemctl", systemd=False)
    r = CommandRunner.for_host(host, allowed_roots=[tmp_path], default_cwd=tmp_path)
    res = one(r, executable="systemctl", args=["status"])
    assert res.status is Status.BLOCKED
    assert "systemd is not running" in (res.reason or "")


def test_discovered_utility_still_runs(tmp_path: Path) -> None:
    host = _profile(tmp_path, "uname", "free", "df")
    host = host.__class__(**{**host.__dict__, "available_utilities": ("uname",)})
    r = CommandRunner(host_profile=None, allowed_roots=[tmp_path], default_cwd=tmp_path)
    assert one(r, executable="uname", args=["-s"]).status is Status.OK


def test_commands_discovery_never_probed_are_not_ruled_out(tmp_path: Path) -> None:
    host = _profile(tmp_path)  # nothing discovered at all
    r = CommandRunner.for_host(host, allowed_roots=[tmp_path], default_cwd=tmp_path)
    assert one(r, executable="uname", args=["-s"]).status is Status.OK


def test_runner_without_profile_does_not_apply_capability_checks(
    tmp_path: Path,
) -> None:
    assert one(runner(tmp_path), executable="echo", args=["x"]).status is Status.OK


def test_preview_steps_structured(tmp_path: Path) -> None:
    items = runner(tmp_path).preview_steps(
        plan(
            {"executable": "echo", "args": ["a b"]}, {"executable": "rm", "args": ["x"]}
        )
    )
    assert items[0].risk == "READ_ONLY" and items[0].command == "echo 'a b'"
    assert items[1].blocked_reason and items[1].risk == "DESTRUCTIVE"


def test_short_env_value_does_not_mangle_version_strings(tmp_path: Path) -> None:
    r = runner(tmp_path, environ={"PATH": "/usr/bin:/bin", "MY_PASSWORD": "ubuntu"})
    res = one(r, executable="echo", args=["g++ (Ubuntu 15.2.0-16ubuntu1) 15.2.0"])
    assert "16ubuntu1" in res.stdout and "[REDACTED]" not in res.stdout
    hidden = one(r, executable="echo", args=["the password is ubuntu"])
    assert "ubuntu" not in hidden.stdout
