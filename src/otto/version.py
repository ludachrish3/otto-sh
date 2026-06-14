from importlib.metadata import version


def get_version():
    """Get `otto`'s package version."""

    return version('otto-sh')
