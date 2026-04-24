from importlib.metadata import version


def getVersion():
    """Get `otto`'s package version."""

    return version('otto-sh')
