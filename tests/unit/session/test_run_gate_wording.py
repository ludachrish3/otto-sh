"""The run gate names the failing project."""

from pathlib import Path

from otto.config.scope import BootstrapError
from otto.session.errors import ProjectLoadError


def test_the_run_gate_says_project():
    err = ProjectLoadError([BootstrapError(Path("/w/p"), "x", ValueError("y"))], [])
    assert str(err).startswith("cannot run while a project fails to load:")
