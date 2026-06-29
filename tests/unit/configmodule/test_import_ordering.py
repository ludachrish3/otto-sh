"""Import-ordering regression: configmodule must be importable FIRST.

`otto/__init__.py` is import-light (PEP 562 lazy exports), so a fresh process —
e.g. an xdist worker importing the root ``tests/conftest.py`` — can make
``otto.configmodule`` its very first otto import. configmodule runs
``apply_repo_settings()`` at import time, which exec's each SUT repo's init
module; those modules legitimately ``from otto.cli.run import instruction`` →
``cli.main`` → ``from ..configmodule import get_completion_names``. That accessor
therefore MUST be defined before the ``apply_repo_settings()`` call, or the
nested import hits a partially-initialized module and raises a circular
ImportError. (Regression guard for the bug Part D's lazy __init__ exposed.)
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
        env=env,
    )
    assert out.returncode == 0, (
        "configmodule first-import hit a circular import:\n" + out.stderr[-2000:]
    )
    assert "IMPORT OK" in out.stdout
