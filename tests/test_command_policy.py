import pytest

from agent.command_policy import RiskClass, classify, env_name_problem


def risk(name: str, *args: str) -> RiskClass:
    return classify(name, list(args)).risk


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("uname", ["-a"]), ("free", ["-h"]), ("df", ["-h"]), ("uptime", []),
        ("ps", ["aux"]), ("ps", ["-ef"]), ("lscpu", []), ("ip", ["addr"]),
        ("ip", ["-br", "link", "show"]), ("ip", ["route"]), ("ss", ["-tulpn"]),
        ("systemctl", ["status", "ssh"]), ("systemctl", ["is-active", "ssh"]),
        ("systemctl", ["--no-pager", "list-units", "--failed"]),
        ("systemctl", ["--failed", "--no-pager"]),
        ("journalctl", ["-n", "50", "--no-pager"]), ("hostname", []),
        ("docker", ["ps", "-a"]), ("docker", ["system", "df"]),
        ("docker", ["stats", "--no-stream"]), ("ls", ["-la", "/var/log"]),
        ("date", []), ("date", ["+%s"]), ("find", ["/var/log", "-name", "x"]),
        ("dmesg", []), ("tail", ["-n", "20", "/var/log/syslog"]),
    ],
)  # fmt: skip
def test_read_only_commands(name: str, args: list[str]) -> None:
    assert risk(name, *args) is RiskClass.READ_ONLY


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("rm", ["-rf", "x"], RiskClass.DESTRUCTIVE),
        ("dd", ["if=/dev/zero", "of=/dev/sda"], RiskClass.DESTRUCTIVE),
        ("mkfs.ext4", ["/dev/sda1"], RiskClass.DESTRUCTIVE),
        ("docker", ["system", "prune"], RiskClass.DESTRUCTIVE),
        ("docker", ["rm", "x"], RiskClass.DESTRUCTIVE),
        ("sudo", ["ls"], RiskClass.PRIVILEGED),
        ("su", [], RiskClass.PRIVILEGED),
        ("pkexec", ["ls"], RiskClass.PRIVILEGED),
        ("apt-get", ["install", "x"], RiskClass.SYSTEM_CHANGE),
        ("chmod", ["777", "x"], RiskClass.SYSTEM_CHANGE),
        ("systemctl", ["restart", "ssh"], RiskClass.SYSTEM_CHANGE),
        ("hostname", ["newname"], RiskClass.SYSTEM_CHANGE),
        ("date", ["010112002030"], RiskClass.SYSTEM_CHANGE),
        ("journalctl", ["--vacuum-size=1M"], RiskClass.SYSTEM_CHANGE),
        ("kill", ["-9", "1"], RiskClass.SYSTEM_CHANGE),
        ("mkdir", ["x"], RiskClass.USER_LEVEL_CHANGE),
        ("touch", ["x"], RiskClass.USER_LEVEL_CHANGE),
    ],
)  # fmt: skip
def test_modifying_commands_not_read_only(
    name: str, args: list[str], expected: RiskClass
) -> None:
    assert risk(name, *args) is expected


@pytest.mark.parametrize(
    "name",
    ["bash", "sh", "zsh", "dash", "env", "xargs", "nohup", "timeout", "nice",
     "strace", "eval", "exec", "ssh", "busybox", "setsid", "watch", "command"],
)  # fmt: skip
def test_shells_and_wrappers_unsupported(name: str) -> None:
    assert risk(name, "-c", "ls") is RiskClass.UNSUPPORTED


@pytest.mark.parametrize(
    "name",
    ["ls;id", "ls|id", "$(id)", "a b", "", "-rf", "../ls", "ls\n", "`id`", "ls&"],
)
def test_malformed_executable_names_unsupported(name: str) -> None:
    assert risk(name) is RiskClass.UNSUPPORTED


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("find", ["/", "-exec", "rm", "{}", ";"]), ("find", [".", "-delete"]),
        ("find", [".", "-fprint", "/tmp/x"]), ("find", [".", "-ok", "rm", "{}", ";"]),
        ("tail", ["-f", "x"]), ("tail", ["-F", "x"]), ("tail", ["--follow", "x"]),
        ("tail", ["--fol", "x"]), ("dmesg", ["-c"]), ("dmesg", ["--clear"]),
        ("dmesg", ["-w"]), ("ss", ["-K"]), ("ip", ["link", "set", "eth0", "down"]),
        ("ip", ["addr", "add", "1.2.3.4/24", "dev", "eth0"]), ("ip", ["netns", "exec", "x", "sh"]),
        ("systemctl", ["-H", "evil", "status"]), ("systemctl", []),
        ("systemctl", ["frobnicate"]), ("docker", ["run", "x"]) , ("docker", ["-H", "tcp://x", "ps"]),
        ("docker", ["logs", "-f", "x"]), ("docker", ["stats"]), ("somerandomtool", []),
        ("python3", ["-c", "print(1)"]), ("perl", ["-e", "1"]), ("curl", ["http://x"]),
        ("ps", ["e"]), ("ps", ["axe"]),
    ],
)  # fmt: skip
def test_dangerous_variants_are_not_read_only(name: str, args: list[str]) -> None:
    assert risk(name, *args) is not RiskClass.READ_ONLY


@pytest.mark.parametrize(
    "arg",
    ["~/.ssh/id_rsa", "/home/u/.ssh/config", "/etc/shadow", "/root/.aws/credentials",
     "/home/u/.env", "/home/u/proj/.env.production", "/proc/self/environ",
     "/etc/ssl/private/server.key", "x/../../etc/shadow", "/home/u/.gnupg/x", "/x/.netrc",
     "/home/u/.kube/config", "/home/u/.docker/config.json"],
)  # fmt: skip
def test_sensitive_paths_blocked_even_for_read_only_tools(arg: str) -> None:
    assert risk("cat", arg) is RiskClass.SENSITIVE
    assert risk("grep", "x", arg) is RiskClass.SENSITIVE
    assert risk("ls", arg) is RiskClass.SENSITIVE


def test_ordinary_paths_not_sensitive() -> None:
    assert risk("ls", "/var/log") is RiskClass.READ_ONLY
    assert risk("cat", "/etc/os-release") is RiskClass.READ_ONLY
    assert risk("grep", "-i", "error", "/var/log/syslog") is RiskClass.READ_ONLY


def test_model_opinion_is_not_an_input() -> None:
    # classify() has no parameter for the model's classification_reason.
    assert risk("rm", "-rf", "/") is RiskClass.DESTRUCTIVE


def test_unknown_command_is_conservative() -> None:
    c = classify("brandnewtool", ["--do-thing"])
    assert c.risk is RiskClass.UNKNOWN


@pytest.mark.parametrize(
    "name",
    ["PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "BASH_ENV", "IFS",
     "API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "1BAD", "A B", "A=B", ""],
)  # fmt: skip
def test_dangerous_env_names_rejected(name: str) -> None:
    assert env_name_problem(name)


@pytest.mark.parametrize("name", ["LANG", "TZ", "TERM", "HOME", "USER"])
def test_benign_env_names_allowed(name: str) -> None:
    assert env_name_problem(name) is None
