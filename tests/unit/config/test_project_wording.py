"""Load failures name the project root.

The run gate's wording (otto.session.errors) is pinned beside its package, in
``tests/unit/session/test_run_gate_wording.py``.
"""

from pathlib import Path

from otto.config.scope import BootstrapError, DependencyError


def test_a_load_failure_names_the_project_root():
    err = BootstrapError(Path("/w/p"), ".otto/settings.toml", ValueError("x"))
    assert str(err).startswith("project root /w/p: failed to load .otto/settings.toml")
    assert str(DependencyError(Path("/w/p"), "boom")) == "project root /w/p: boom"
