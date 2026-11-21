"""Docker safe operations (Build 12, module 3).

Changing (approval required): ``start_container``, ``stop_container``,
``restart_container``, ``pull_image``. Each is verified by re-inspecting
Docker. Removing containers, images, networks or volumes and pruning are
deliberately not offered.

Membership of the ``docker`` group is equivalent to root, so these are
classified SYSTEM_CHANGE.
"""

from __future__ import annotations

import shlex
from typing import Any

from agent.command_policy import RiskClass
from agent.operations import Operation, OperationSpec
from agent.registry import ToolRegistry
from agent.verifier import Check, CheckResult
from hostplatform.docker import (
    DockerClient,
    DockerStatus,
    validate_container,
    validate_image,
)

START_CONTAINER = "start_container"
STOP_CONTAINER = "stop_container"
RESTART_CONTAINER = "restart_container"
PULL_IMAGE = "pull_image"

_CONTAINER = {
    "type": "object",
    "properties": {"container": {"type": "string", "maxLength": 128}},
    "required": ["container"],
    "additionalProperties": False,
}
_IMAGE = {
    "type": "object",
    "properties": {"image": {"type": "string", "maxLength": 255}},
    "required": ["image"],
    "additionalProperties": False,
}


def register_docker_operations(
    registry: ToolRegistry, client: DockerClient | None
) -> None:
    if client is None or not client.available():
        return

    def describe(container: str) -> str:
        return f"{container}: {client.container_state(container) or 'not found'}"

    def op(
        tool: str,
        args: dict[str, Any],
        summary: str,
        argv: list[str] | None,
        baseline: str,
        expected: str,
        checks: list[Check],
        probe: Any,
        recovery: str,
        **flags: Any,
    ) -> Operation:
        action, target = tool.split("_")[0], next(iter(args.values()))
        return Operation(
            tool=tool,
            arguments=args,
            summary=summary,
            commands=[shlex.join(argv)] if argv else [],
            risk=RiskClass.SYSTEM_CHANGE,
            baseline=baseline,
            expected_end_state=expected,
            affected=[f"{'image' if tool == PULL_IMAGE else 'container'} {target}"],
            verification=[c.name for c in checks],
            recovery=recovery,
            perform=lambda: client.act(action, target),
            checks=checks,
            probe_state=probe,
            diagnose=lambda: [describe(target)] if tool != PULL_IMAGE else [],
            **flags,
        )

    def daemon_problem() -> str | None:
        info = client.info()
        if info.status is DockerStatus.READY:
            return None
        return f"docker is {info.status.value}: {info.detail}"[:300]

    def container_op(tool: str, want: str, verb: str) -> Any:
        def prepare(args: dict[str, Any]) -> Operation:
            name = validate_container(args["container"])
            problem = daemon_problem()
            state = None if problem else client.container_state(name)
            baseline = describe(name) if not problem else "unknown"

            def is_want() -> CheckResult:
                now = client.container_state(name)
                return CheckResult(f"{name} is {want}", now == want, f"state {now}")

            def stays() -> CheckResult:
                inspect = client.inspect(name)
                st = (
                    (inspect.get("state") or {})
                    if inspect.get("status") == "ok"
                    else {}
                )
                if want != "running":
                    return CheckResult("not restarting", not st.get("restarting"), "")
                health = st.get("health")
                ok = st.get("running") is True and health in (
                    None,
                    "healthy",
                    "starting",
                )
                return CheckResult(
                    f"{name} stays up",
                    ok,
                    f"health {health}, restarting {st.get('restarting')}",
                )

            checks = [Check("state", is_want), Check("stable", stays)]
            argv = client.action_argv(verb, name)
            refused = problem or (None if state else f"container {name} not found")
            already = not refused and verb != "restart" and state == want
            undo = {
                "start": f"stop it again with: docker stop {name}",
                "stop": f"start it again with: docker start {name}",
                "restart": "a restart has no undo; check the container's logs",
            }[verb]
            return op(
                tool,
                {"container": name},
                f"{name} is already {want}; nothing to do"
                if already
                else f"{verb} container {name}",
                None if (refused or already) else argv,
                baseline,
                f"{name} {want}",
                checks,
                lambda: describe(name),
                undo,
                refused=refused,
                no_change_needed=already,
            )

        return prepare

    def prepare_pull(args: dict[str, Any]) -> Operation:
        ref = validate_image(args["image"])
        problem = daemon_problem()
        present = (not problem) and client.has_image(ref)
        checks = [
            Check(
                "image present",
                lambda: CheckResult(
                    f"{ref} present locally", client.has_image(ref), ""
                ),
            )
        ]
        return op(
            PULL_IMAGE,
            {"image": ref},
            f"pull image {ref}",
            None if problem else client.action_argv("pull", ref),
            "unknown" if problem else f"{ref}: {'present' if present else 'absent'}",
            f"{ref}: present",
            checks,
            lambda: f"{ref}: {'present' if client.has_image(ref) else 'absent'}",
            f"remove it yourself if unwanted: docker image rm {ref}. "
            "This agent does not delete images.",
            refused=problem,
        )

    specs = [
        (START_CONTAINER, "Start an existing stopped container.", container_op(START_CONTAINER, "running", "start")),
        (STOP_CONTAINER, "Stop a running container (it is not removed).", container_op(STOP_CONTAINER, "exited", "stop")),
        (RESTART_CONTAINER, "Restart a container.", container_op(RESTART_CONTAINER, "running", "restart")),
    ]  # fmt: skip
    for name, desc, prepare in specs:
        registry.register_operation(
            OperationSpec(
                name=name,
                description=desc + " Needs local approval. Removal is not supported.",
                input_schema=_CONTAINER,
                prepare=prepare,
            )
        )
    registry.register_operation(
        OperationSpec(
            name=PULL_IMAGE,
            description=(
                "Download a container image. Needs local approval. Deleting "
                "images or volumes is not supported."
            ),
            input_schema=_IMAGE,
            prepare=prepare_pull,
        )
    )
