"""Single source for the installed application version."""

from importlib.metadata import PackageNotFoundError, version

FALLBACK_VERSION = "0.1.0"


def get_version() -> str:
    try:
        return version("linux-agent")
    except PackageNotFoundError:
        return FALLBACK_VERSION
