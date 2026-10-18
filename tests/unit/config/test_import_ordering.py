"""Import-ordering regression: config must be importable FIRST, standalone.

`otto/__init__.py` is import-light (PEP 562 lazy exports), so a fresh process —
e.g. an xdist worker importing the root ``tests/conftest.py`` — can make
``otto.config`` (or a submodule like ``otto.config.project``) its very
first otto import, with an ``OTTOPATH`` repo configured but none of its
init modules imported yet. Repo init-module registration now happens lazily
inside :func:`otto.bootstrap.bootstrap`, not as an import-time side effect of
``otto.config`` — so this test's job is narrower than it used to be: it
guards against a regression that would make plain package/submodule import
eager again (and therefore reintroduce the old circular-import hazard between
user init modules and ``otto.cli.main``). (Regression guard for the bug Part D's
lazy __init__ exposed.)
"""

import os
import subprocess
import sys

from otto.config.env import is_otto_env_var
from tests._fixtures.paths import TESTS_ROOT

_REPO1 = TESTS_ROOT / "repo1"


def test_config_first_import_with_repo_no_circular():
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env["OTTOPATH"] = str(_REPO1)
    out = subprocess.run(
        [sys.executable, "-c", "import otto.config.project; print('IMPORT OK')"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert out.returncode == 0, "config first-import hit a circular import:\n" + out.stderr[-2000:]
    assert "IMPORT OK" in out.stdout


def test_labs_first_import_no_circular():
    """``otto.labs`` (a public library entry point) must import standalone.

    The package holds the lab model and its loader (``otto.labs.loader``) beside
    the backends that build labs. ``otto.labs.json_repository`` imports ``Lab``
    only at ``load_lab()`` call time (a lazy, function-local import — see the
    module), so a bare ``import otto.labs`` as the very first otto import loads
    neither, and no import order between them can trip a cycle.
    """
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    out = subprocess.run(
        [sys.executable, "-c", "import otto.labs; print('IMPORT OK')"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert out.returncode == 0, "labs first-import hit a circular import:\n" + out.stderr[-2000:]
    assert "IMPORT OK" in out.stdout
