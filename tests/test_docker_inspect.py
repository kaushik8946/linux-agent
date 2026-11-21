"""Docker read-only inspection (Build 12, module 2). A scripted fake replaces
every docker call, so no daemon is needed."""

import json
from collections.abc import Mapping, Sequence

import pytest

from agent.registry import ToolRegistry
from hostplatform.docker import (
    DockerClient,
    DockerNameError,
    DockerStatus,
    validate_container,
)
from hostplatform.privileged import ProcResult
from tools.docker import (
    CONTAINER_LOGS,
    DOCKER_STATUS,
    INSPECT_CONTAINER,
    LIST_CONTAINERS,
    LIST_IMAGES,
    register_docker_tools,
)

DOCKER = "/usr/bin/docker"
INSPECT = [
    {
        "Id": "a" * 64,
        "Name": "/web",
        "RestartCount": 2,
        "Config": {
            "Image": "nginx:1",
            "Env": ["DB_PASSWORD=hunter2hunter2", "TOKEN=abcdef123456"],
            "Cmd": ["nginx", "--secret-flag"],
        },
        "State": {"Status": "running", "Running": True, "ExitCode": 0,
                  "Health": {"Status": "healthy"}},
        "HostConfig": {"RestartPolicy": {"Name": "always"}},
        "NetworkSettings": {"Ports": {"80/tcp": None}},
        "Mounts": [{"Destination": "/data", "Source": "/home/me/private"}],
    }
]  # fmt: skip


class FakeDocker:
    def __init__(self) -> None:
        self.state = "ready"  # ready | down | denied
        self.calls: list[tuple[str, ...]] = []
        self.logs = "line one\nTOKEN=abcdef123456 leaked\n"

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None
    ) -> ProcResult:
        argv = tuple(argv)
        self.calls.append(argv)
        assert argv[0] == DOCKER
        cmd = argv[1]
        if self.state == "down":
            return ProcResult(argv, 1, json.dumps({"Client": {"Version": "27"}}),
                "Cannot connect to the Docker daemon. Is the docker daemon running?")  # fmt: skip
        if self.state == "denied":
            return ProcResult(argv, 1, "", "permission denied while trying to connect")
        if cmd == "version":
            return ProcResult(argv, 0, json.dumps(
                {"Client": {"Version": "27"}, "Server": {"Version": "27.1"}}), "")  # fmt: skip
        if cmd == "ps":
            row = {"ID": "abc", "Names": "web", "Image": "nginx", "State": "running",
                   "Status": "Up", "Ports": "80/tcp", "CreatedAt": "x", "Labels": "secret"}  # fmt: skip
            return ProcResult(argv, 0, json.dumps(row) + "\nnot json\n", "")
        if cmd == "images":
            row = {"ID": "i1", "Repository": "nginx", "Tag": "1", "Size": "1MB",
                   "CreatedSince": "now"}  # fmt: skip
            return ProcResult(argv, 0, json.dumps(row), "")
        if cmd == "inspect":
            if argv[-1] != "web":
                return ProcResult(argv, 1, "", "Error: No such container: " + argv[-1])
            return ProcResult(argv, 0, json.dumps(INSPECT), "")
        if cmd == "logs":
            if argv[-1] != "web":
                return ProcResult(argv, 1, "", "Error: No such container")
            return ProcResult(argv, 0, self.logs, "")
        raise AssertionError(f"unexpected docker command {argv}")


def make(state: str = "ready") -> tuple[ToolRegistry, FakeDocker]:
    fake = FakeDocker()
    fake.state = state
    reg = ToolRegistry()
    register_docker_tools(reg, DockerClient(lambda _n: DOCKER, run=fake))
    return reg, fake


def test_tools_absent_without_docker() -> None:
    reg = ToolRegistry()
    register_docker_tools(reg, DockerClient(lambda _n: None))
    assert reg.names() == []


def test_all_tools_registered_and_read_only() -> None:
    reg, _ = make()
    assert set(reg.names()) == {DOCKER_STATUS, LIST_CONTAINERS, LIST_IMAGES,
                                INSPECT_CONTAINER, CONTAINER_LOGS}  # fmt: skip
    assert not any(reg.is_operation(n) for n in reg.names())


def test_status_ready_down_and_denied() -> None:
    assert make()[0].call(DOCKER_STATUS, {})["status"] == "ready"
    down = make("down")[0].call(DOCKER_STATUS, {})
    assert down["status"] == DockerStatus.DAEMON_UNREACHABLE.value
    assert make("denied")[0].call(DOCKER_STATUS, {})["status"] == "permission_denied"


def test_list_containers_keeps_only_safe_fields() -> None:
    data = make()[0].call(LIST_CONTAINERS, {})
    assert data["count"] == 1 and data["containers"][0]["Names"] == "web"
    assert "Labels" not in data["containers"][0]


def test_list_when_daemon_down_reports_status_not_error() -> None:
    assert make("down")[0].call(LIST_CONTAINERS, {})["status"] == "daemon_unreachable"
    assert make("down")[0].call(LIST_IMAGES, {})["status"] == "daemon_unreachable"


def test_list_images() -> None:
    assert make()[0].call(LIST_IMAGES, {})["images"][0]["Repository"] == "nginx"


def test_inspect_never_returns_environment_or_secrets() -> None:
    data = make()[0].call(INSPECT_CONTAINER, {"container": "web"})
    text = json.dumps(data)
    assert data["status"] == "ok" and data["env_omitted"] is True
    assert data["state"]["health"] == "healthy" and data["restart_policy"] == "always"
    for leaked in ("hunter2", "abcdef123456", "secret-flag", "/home/me/private"):
        assert leaked not in text
    assert data["mount_destinations"] == ["/data"]


def test_inspect_unknown_container() -> None:
    assert (
        make()[0].call(INSPECT_CONTAINER, {"container": "nope"})["status"]
        == "not_found"
    )


def test_logs_are_redacted_and_bounded() -> None:
    reg, fake = make()
    fake.logs = "ok\n" + "x" * 20000 + "\nTOKEN=abcdef123456"
    data = reg.call(CONTAINER_LOGS, {"container": "web", "lines": 5})
    assert "abcdef123456" not in json.dumps(data)
    assert len(data["log"]) <= 8000 and "untrusted" in data["note"]
    assert ("logs", "--tail", "5", "--", "web") == fake.calls[-1][1:]


def test_logs_line_count_is_schema_bounded() -> None:
    reg, _ = make()
    assert "error" in reg.call(CONTAINER_LOGS, {"container": "web", "lines": 5000})
    assert "error" in reg.call(CONTAINER_LOGS, {"container": "web", "lines": 0})


@pytest.mark.parametrize(
    "name", ["-v", "--all", "a b", "a;b", "$(id)", "", "../x", "x" * 129]
)
def test_hostile_container_names_never_reach_docker(name: str) -> None:
    reg, fake = make()
    assert "error" in reg.call(INSPECT_CONTAINER, {"container": name})
    assert "error" in reg.call(CONTAINER_LOGS, {"container": name})
    assert not any(c[1] in ("inspect", "logs") for c in fake.calls)


def test_validate_container_accepts_normal_names() -> None:
    for ok in ("web", "my-app_1", "a.b", "0123abc"):
        assert validate_container(ok) == ok
    with pytest.raises(DockerNameError):
        validate_container(5)


def test_only_read_subcommands_are_ever_run() -> None:
    reg, fake = make()
    for tool, args in [(DOCKER_STATUS, {}), (LIST_CONTAINERS, {}), (LIST_IMAGES, {}),
                       (INSPECT_CONTAINER, {"container": "web"}),
                       (CONTAINER_LOGS, {"container": "web"})]:  # fmt: skip
        reg.call(tool, args)
    assert {c[1] for c in fake.calls} <= {"version", "ps", "images", "inspect", "logs"}


def test_unreadable_output_is_reported() -> None:
    fake = FakeDocker()
    reg = ToolRegistry()
    client = DockerClient(lambda _n: DOCKER, run=fake)
    register_docker_tools(reg, client)
    orig = fake.__call__

    def garbled(
        argv: Sequence[str], t: float, e: Mapping[str, str] | None
    ) -> ProcResult:
        if argv[1] == "inspect":
            return ProcResult(tuple(argv), 0, "not json", "")
        return orig(argv, t, e)

    client._run = garbled
    assert reg.call(INSPECT_CONTAINER, {"container": "web"})["status"] == "error"


def test_newer_docker_connection_message_is_daemon_unreachable() -> None:
    fake = FakeDocker()
    client = DockerClient(lambda _n: DOCKER, run=fake)

    def refused(
        argv: Sequence[str], t: float, e: Mapping[str, str] | None
    ) -> ProcResult:
        return ProcResult(
            tuple(argv), 1, "",
            "failed to connect to the docker API at unix:///x.sock; check if the"
            " path is correct: dial unix /x.sock: connect: no such file or directory",
        )  # fmt: skip

    client._run = refused
    assert client.info().status is DockerStatus.DAEMON_UNREACHABLE
