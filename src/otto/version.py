"""Package version accessor for otto-sh."""

from importlib.metadata import version


def get_version() -> str:
    """Get `otto`'s package version."""
    return version("otto-sh")
