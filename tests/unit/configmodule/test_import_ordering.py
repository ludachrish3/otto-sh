"""Import-ordering regression: configmodule must be importable FIRST, standalone.

`otto/__init__.py` is import-light (PEP 562 lazy exports), so a fresh process —
e.g. an xdist worker importing the root ``tests/conftest.py`` — can make
``otto.configmodule`` (or a submodule like ``otto.configmodule.lab``) its very
first otto import, with an ``OTTO_SUT_DIRS`` repo configured but none of its
init modules imported yet. Repo init-module registration now happens lazily
inside :func:`otto.bootstrap.bootstrap`, not as an import-time side effect of
``otto.configmodule`` — so this test's job is narrower than it used to be: it
guards against a regression that would make plain package/submodule import
eager again (and therefore reintroduce the old circular-import hazard between
user init modules and ``otto.cli.main``). (Regression guard for the bug Part D's
lazy __init__ exposed.)
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO1 = Path(__file__).resolve().parents[2] / "repo1"


def test_configmodule_first_import_with_repo_no_circular():
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env["OTTO_SUT_DIRS"] = str(_REPO1)
    out = subprocess.run(
        [sys.executable, "-c", "import otto.configmodule.lab; print('IMPORT OK')"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert out.returncode == 0, (
        "configmodule first-import hit a circular import:\n" + out.stderr[-2000:]
    )
    assert "IMPORT OK" in out.stdout
