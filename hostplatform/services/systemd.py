"""systemd adapter.

Status uses ``systemctl show`` with explicit properties (machine-readable,
no root). Start runs ``sudo -- /usr/bin/systemctl start -- <unit>``. After
starting, the unit is polled briefly and must report ``ActiveState=active``;
a unit that starts and immediately fails is reported as failed.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from hostplatform.privileged import Run, as_root, is_permission_error, run_fixed
from hostplatform.services.base import (
    ServiceAdapter,
    ServiceState,
    ServiceStatus,
    StartResult,
    StartStatus,
    validate_service_name,
)

QUERY_TIMEOUT = 15.0
START_TIMEOUT = 120.0
SETTLE_SECONDS = 3.0
ENV = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "SYSTEMD_PAGER": ""}
_PROPS = "LoadState,ActiveState,SubState,UnitFileState,MainPID"


class SystemdAdapter(ServiceAdapter):
    name = "systemd"

    def __init__(
        self,
        which: Callable[[str], str | None],
        is_root: bool,
        running: bool,
        run: Run = run_fixed,
        sleep: Callable[[float], None] = time.sleep,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self.systemctl = which("systemctl")
        self.sudo = which("sudo")
        self._is_root = is_root
        self._running = running
        self._run = run
        self._sleep = sleep
        self._settle = settle_seconds

    def available(self) -> bool:
        return bool(self.systemctl) and self._running

    def status(self, service: str) -> ServiceState:
        unit = validate_service_name(service)
        if not self.available():
            return ServiceState(
                unit, ServiceStatus.UNKNOWN, detail="systemd is not running"
            )
        assert self.systemctl
        result = self._run(
            [self.systemctl, "show", f"--property={_PROPS}", "--no-pager", "--", unit],
            QUERY_TIMEOUT,
            ENV,
        )
        if not result.ok:
            return ServiceState(
                unit, ServiceStatus.UNKNOWN, detail="status could not be read"
            )
        props = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if props.get("LoadState") == "not-found":
            return ServiceState(unit, ServiceStatus.NOT_FOUND, detail="no such service")
        active = props.get("ActiveState", "")
        status = {
            "active": ServiceStatus.ACTIVE,
            "reloading": ServiceStatus.ACTIVE,
            "inactive": ServiceStatus.INACTIVE,
            "deactivating": ServiceStatus.INACTIVE,
            "failed": ServiceStatus.FAILED,
            "activating": ServiceStatus.ACTIVATING,
        }.get(active, ServiceStatus.UNKNOWN)
        pid_text = props.get("MainPID", "0")
        pid = int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else None
        return ServiceState(
            unit,
            status,
            props.get("SubState") or None,
            props.get("UnitFileState") or None,
            pid,
        )

    def start_argv(self, service: str) -> list[str] | None:
        unit = validate_service_name(service)
        if not self.available():
            return None
        assert self.systemctl
        return as_root([self.systemctl, "start", "--", unit], self.sudo, self._is_root)

    def start(self, service: str) -> StartResult:
        unit = validate_service_name(service)
        before = self.status(unit)
        if before.status is ServiceStatus.NOT_FOUND:
            return StartResult(
                unit, StartStatus.NOT_FOUND, f"{unit} does not exist", before
            )
        if before.status is ServiceStatus.ACTIVE:
            return StartResult(
                unit,
                StartStatus.ALREADY_ACTIVE,
                f"{unit} is already active; nothing changed",
                before,
                before,
            )
        argv = self.start_argv(unit)
        if argv is None:
            return StartResult(
                unit,
                StartStatus.NOT_ATTEMPTED,
                "systemd or sudo is not available",
                before,
            )
        result = self._run(argv, START_TIMEOUT, ENV)
        after = self._settled_status(unit)
        stderr = result.stderr.strip()[-600:]
        if result.ok and after.status is ServiceStatus.ACTIVE:
            return StartResult(
                unit,
                StartStatus.STARTED,
                f"{unit} is active ({after.sub_state})",
                before,
                after,
                stderr,
            )
        if is_permission_error(result):
            return StartResult(
                unit,
                StartStatus.PERMISSION_DENIED,
                "permission denied (sudo was refused or not authorised)",
                before,
                after,
                stderr,
            )
        if "not found" in stderr.lower() and "unit" in stderr.lower():
            return StartResult(
                unit,
                StartStatus.NOT_FOUND,
                f"{unit} does not exist",
                before,
                after,
                stderr,
            )
        if result.ok:
            message = f"start command succeeded but {unit} is {after.status.value}"
        else:
            message = f"start failed (exit {result.exit_code})"
        return StartResult(unit, StartStatus.FAILED, message, before, after, stderr)

    def _settled_status(self, unit: str) -> ServiceState:
        """Poll until the unit leaves 'activating' or the settle time passes."""
        waited = 0.0
        state = self.status(unit)
        while state.status is ServiceStatus.ACTIVATING and waited < self._settle:
            self._sleep(0.5)
            waited += 0.5
            state = self.status(unit)
        if state.status is ServiceStatus.ACTIVE and self._settle > 0:
            # A unit can flip to active then crash; check once more.
            self._sleep(min(1.0, self._settle))
            state = self.status(unit)
        return state
