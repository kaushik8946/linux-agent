from collections.abc import Callable
from pathlib import Path

import pytest

from hostplatform.capabilities import (
    DIAGNOSTIC_UTILITIES,
    HostProfile,
    render_profile,
)
from hostplatform.discovery import (
    detect_containers,
    detect_service_manager,
    discover,
    parse_os_release,
)

DEBIAN = """PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
ID=debian
"""
UBUNTU = """PRETTY_NAME="Ubuntu 24.04 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
ID=ubuntu
ID_LIKE=debian
"""
FEDORA = """NAME="Fedora Linux"
VERSION_ID=40
ID=fedora
PRETTY_NAME="Fedora Linux 40 (Workstation Edition)"
"""
ROCKY = """NAME="Rocky Linux"
VERSION_ID="9.3"
ID="rocky"
ID_LIKE="rhel centos fedora"
PRETTY_NAME="Rocky Linux 9.3 (Blue Onyx)"
"""
ARCH = """NAME="Arch Linux"
PRETTY_NAME="Arch Linux"
ID=arch
BUILD_ID=rolling
"""
ALPINE = """NAME="Alpine Linux"
ID=alpine
VERSION_ID=3.19.1
PRETTY_NAME="Alpine Linux v3.19"
"""


def make_root(
    tmp_path: Path,
    os_release: str | None = None,
    *,
    systemd_running: bool = False,
    cgroup: str | None = "0::/init.scope\n",
    files: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    if os_release is not None:
        (root / "etc/os-release").write_text(os_release)
    if systemd_running:
        (root / "run/systemd/system").mkdir(parents=True)
    if cgroup is not None:
        (root / "proc/1").mkdir(parents=True)
        (root / "proc/1/cgroup").write_text(cgroup)
    for rel, text in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def fake_which(*present: str) -> Callable[[str], str | None]:
    return lambda name: f"/usr/bin/{name}" if name in present else None


def run(root: Path, *present: str, euid: int | None = 1000) -> HostProfile:
    return discover(
        root=root,
        which=fake_which(*present),
        euid=lambda: euid,
        uname=lambda: ("Linux", "6.1.0-test", "x86_64"),
    )


# -- os-release parsing -------------------------------------------------------


def test_parse_os_release_handles_quotes_comments_and_junk() -> None:
    data = parse_os_release(
        '# comment\nID=debian\nNAME="Debian \\"Stable\\""\nVERSION=\'12 (x)\'\n'
        "garbage line\nlowercase=ignored\n\nEMPTY=\n"
    )
    assert data["ID"] == "debian"
    assert data["NAME"] == 'Debian "Stable"'
    assert data["VERSION"] == "12 (x)"
    assert data["EMPTY"] == ""
    assert "lowercase" not in data


# -- distribution fixtures ----------------------------------------------------


def test_debian_like(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, UBUNTU), "apt-get", "dpkg")
    assert p.os.distro_id == "ubuntu"
    assert p.os.id_like == ("debian",)
    assert p.os.version_id == "24.04"
    assert p.os.pretty_name == "Ubuntu 24.04 LTS"
    assert p.package_managers == ("apt-get",)


def test_plain_debian_has_no_id_like(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, DEBIAN), "apt-get")
    assert p.os.distro_id == "debian"
    assert p.os.id_like == ()


def test_fedora_like(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, ROCKY), "dnf")
    assert p.os.distro_id == "rocky"
    assert p.os.id_like == ("rhel", "centos", "fedora")
    assert p.package_managers == ("dnf",)
    p = run(make_root(tmp_path / "fedora", FEDORA), "dnf", "yum")
    assert p.os.distro_id == "fedora"
    assert p.package_managers == ("dnf", "yum")


def test_arch_like(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, ARCH), "pacman")
    assert p.os.distro_id == "arch"
    assert p.os.version_id is None  # rolling release: no VERSION_ID, not an error
    assert p.package_managers == ("pacman",)


def test_alpine_like(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, ALPINE), "apk")
    assert p.os.distro_id == "alpine"
    assert p.package_managers == ("apk",)
    assert p.service_manager.name is None


# -- degraded hosts ------------------------------------------------------------


def test_missing_os_release_is_unknown_not_a_failure(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, None))
    assert p.os.distro_id is None
    assert p.os.pretty_name is None
    assert p.os.source is None
    assert any("os-release" in w for w in p.warnings)
    assert "unknown" in render_profile(p)


def test_falls_back_to_usr_lib_os_release(tmp_path: Path) -> None:
    root = make_root(tmp_path, None, files={"usr/lib/os-release": DEBIAN})
    p = run(root)
    assert p.os.distro_id == "debian"
    assert p.os.source == "/usr/lib/os-release"


def test_unknown_distribution_still_discovered(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, 'NAME="Mystery OS"\nID=mystery\n'))
    assert p.os.distro_id == "mystery"
    assert p.os.id_like == ()
    assert p.package_managers == ()
    assert "none found" in render_profile(p)


def test_os_release_without_id_warns(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, 'NAME="No Id"\n'))
    assert p.os.distro_id is None
    assert any("does not define ID" in w for w in p.warnings)


def test_binary_garbage_os_release_does_not_crash(tmp_path: Path) -> None:
    root = make_root(tmp_path, None)
    (root / "etc/os-release").write_bytes(b"\xff\xfe\x00 not utf8")
    p = run(root)
    assert p.os.distro_id is None


def test_missing_optional_utilities_are_listed(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, DEBIAN), "free", "df")
    assert p.available_utilities == ("free", "df")
    assert "lsof" in p.missing_utilities
    assert set(p.available_utilities) | set(p.missing_utilities) == set(
        DIAGNOSTIC_UTILITIES
    )


def test_non_linux_host_warns(tmp_path: Path) -> None:
    p = discover(
        root=make_root(tmp_path, DEBIAN),
        which=fake_which(),
        euid=lambda: 1000,
        uname=lambda: ("Darwin", "23.0", "arm64"),
    )
    assert any("not Linux" in w for w in p.warnings)


def test_empty_uname_fields_become_none(tmp_path: Path) -> None:
    p = discover(
        root=make_root(tmp_path, DEBIAN),
        which=fake_which(),
        euid=lambda: 1000,
        uname=lambda: ("Linux", "", ""),
    )
    assert p.kernel is None
    assert p.architecture is None


# -- service manager -----------------------------------------------------------


def test_systemd_running(tmp_path: Path) -> None:
    root = make_root(tmp_path, UBUNTU, systemd_running=True)
    assert detect_service_manager(root, fake_which("systemctl")).state == "running"


def test_systemctl_present_but_systemd_not_running(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, UBUNTU, systemd_running=False), "systemctl")
    assert p.service_manager.name == "systemd"
    assert p.service_manager.state == "installed_not_running"
    assert any("not running" in w for w in p.warnings)


def test_systemd_dir_without_binary_still_counts_as_running(tmp_path: Path) -> None:
    root = make_root(tmp_path, UBUNTU, systemd_running=True)
    assert detect_service_manager(root, fake_which()).state == "running"


def test_openrc_detected(tmp_path: Path) -> None:
    root = make_root(tmp_path, ALPINE, files={"run/openrc/started/x": ""})
    sm = detect_service_manager(root, fake_which("rc-service"))
    assert (sm.name, sm.state) == ("openrc", "running")


def test_no_service_manager(tmp_path: Path) -> None:
    sm = detect_service_manager(make_root(tmp_path, ALPINE), fake_which())
    assert (sm.name, sm.state) == (None, "absent")


# -- privileges ----------------------------------------------------------------


def test_privileges_reported_without_running_sudo(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, DEBIAN), "sudo")
    assert (p.privileges.is_root, p.privileges.sudo_available) == (False, True)
    assert p.privileges.doas_available is False


def test_root_user(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, DEBIAN), euid=0)
    assert p.privileges.is_root is True
    assert not any("sudo" in w for w in p.warnings)


def test_unprivileged_without_sudo_warns(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, DEBIAN))
    assert any("no sudo or doas" in w for w in p.warnings)


def test_unknown_euid(tmp_path: Path) -> None:
    p = run(make_root(tmp_path, DEBIAN), euid=None)
    assert p.privileges.is_root is None
    assert "root: unknown" in render_profile(p)


# -- containers / restricted environments --------------------------------------


def test_bare_metal_has_no_container_indicators(tmp_path: Path) -> None:
    assert detect_containers(make_root(tmp_path, DEBIAN)) == ()


def test_dockerenv_marker(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, files={".dockerenv": ""})
    assert any(".dockerenv" in i for i in detect_containers(root) or ())


def test_podman_marker(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, files={"run/.containerenv": ""})
    assert any(".containerenv" in i for i in detect_containers(root) or ())


def test_cgroup_marker(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, cgroup="0::/docker/abc123\n")
    assert any("docker" in i for i in detect_containers(root) or ())


def test_systemd_container_file(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, files={"run/systemd/container": "lxc\n"})
    assert any("container=lxc" in i for i in detect_containers(root) or ())


def test_restricted_environment_nonewprivs(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, files={"proc/self/status": "NoNewPrivs:\t1\n"})
    assert any("NoNewPrivs" in i for i in detect_containers(root) or ())
    root2 = make_root(
        tmp_path / "b", DEBIAN, files={"proc/self/status": "NoNewPrivs:\t0\n"}
    )
    assert detect_containers(root2) == ()


def test_container_state_unknown_when_nothing_readable(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, cgroup=None)
    assert detect_containers(root) is None
    p = run(root)
    assert any("container detection incomplete" in w for w in p.warnings)
    assert "unknown" in render_profile(p)


def test_marker_found_even_when_cgroup_unreadable(tmp_path: Path) -> None:
    root = make_root(tmp_path, DEBIAN, cgroup=None, files={".dockerenv": ""})
    assert detect_containers(root) not in (None, ())


# -- rendering and safety ------------------------------------------------------


def test_render_marks_unknowns_and_never_invents_facts(tmp_path: Path) -> None:
    text = render_profile(run(make_root(tmp_path, None, cgroup=None), euid=None))
    for label in ("OS:", "Kernel:", "root: unknown"):
        assert label in text
    assert "OS:            unknown" in text


def test_render_lists_detected_facts(tmp_path: Path) -> None:
    text = render_profile(
        run(
            make_root(tmp_path, UBUNTU, systemd_running=True), "apt-get", "sudo", "free"
        )
    )
    assert "Ubuntu 24.04 LTS" in text
    assert "apt-get" in text
    assert "systemd: running" in text
    assert "sudo: yes" in text


def test_discovery_does_not_spawn_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess

    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("discovery must not start a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    run(make_root(tmp_path, UBUNTU, systemd_running=True), "sudo", "systemctl")


def test_real_host_discovery_does_not_crash() -> None:
    p = discover()
    assert p.python_version.count(".") == 2
    assert isinstance(render_profile(p), str)


# -- CLI -----------------------------------------------------------------------


def test_inspect_flag_needs_no_gateway_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli import app

    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert app.main(["--inspect"]) == app.EXIT_OK
    out = capsys.readouterr().out
    assert "Host" in out and "Service manager" in out
