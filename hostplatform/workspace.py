"""Project initialisation inside an approved workspace (Build 12, module 4).

A workspace is one directory the user has named (``AGENT_WORKSPACE_ROOT``,
default ``~/projects``). Projects are created only as direct children of it,
with a validated name and a fixed template. Existing paths are never
overwritten, no package manager or network access is used, and nothing
outside the new project directory is written.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
KINDS = ("python", "text")

PYPROJECT = """[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest", "ruff"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 88
"""

GITIGNORE = """.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
.env
"""

README = "# {name}\n\nCreated by linux-agent.\n"


class WorkspaceError(ValueError):
    pass


def validate_project_name(name: object) -> str:
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
        raise WorkspaceError(
            "project name: 1-64 lowercase letters, digits, - or _, starting with a letter or digit"
        )
    return name


@dataclass(frozen=True)
class ProjectPlan:
    root: Path
    name: str
    kind: str
    files: dict[str, str]  # relative path -> content

    @property
    def path(self) -> Path:
        return self.root / self.name


def plan_project(root: Path, name: str, kind: str) -> ProjectPlan:
    validate_project_name(name)
    if kind not in KINDS:
        raise WorkspaceError(f"kind must be one of: {', '.join(KINDS)}")
    files = {"README.md": README.format(name=name), ".gitignore": GITIGNORE}
    if kind == "python":
        files["pyproject.toml"] = PYPROJECT.format(name=name)
        files[f"{name.replace('-', '_')}/__init__.py"] = ""
        files["tests/test_placeholder.py"] = "def test_placeholder() -> None:\n    assert True\n"  # fmt: skip
    return ProjectPlan(root, name, kind, files)


def workspace_problem(root: Path) -> str | None:
    """Why ``root`` cannot be used as a workspace, or None if it can."""
    if not root.is_absolute():
        return "workspace root must be an absolute path"
    if not root.is_dir():
        return "workspace root does not exist"
    if not os.access(root, os.W_OK):
        return "workspace root is not writable"
    if root.is_symlink():
        return "workspace root must not be a symbolic link"
    if str(root) in ("/", "/etc", "/usr", "/bin", "/sbin", "/boot", "/var", "/root"):
        return "workspace root is a system directory"
    return None


def preview(plan: ProjectPlan) -> str:
    lines = [f"create {plan.path}/ with:"]
    lines += [f"  {rel}" for rel in sorted(plan.files)]
    return "\n".join(lines)


def conflicts(plan: ProjectPlan) -> list[str]:
    return [rel for rel in plan.files if (plan.path / rel).exists()] + (
        [plan.name] if plan.path.exists() and not plan.path.is_dir() else []
    )


def create(plan: ProjectPlan) -> list[str]:
    """Write the project. Refuses to touch anything that already exists."""
    if plan.path.exists():
        raise WorkspaceError("a file or directory with that name already exists")
    written: list[str] = []
    plan.path.mkdir(parents=False, exist_ok=False)
    try:
        for rel, text in plan.files.items():
            target = plan.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            written.append(rel)
    except BaseException:
        # Only remove what this call created; never follow links out of it.
        for rel in reversed(written):
            (plan.path / rel).unlink(missing_ok=True)
        raise
    return written
