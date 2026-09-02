"""Loader for the import-budget measurement harness (``scripts/import_budget.py``).

The harness is a script, not an importable package module, so test code reaches
it by path. ONE loader, shared: ``tests/unit/import_budget/`` measures the
surface table and ``tests/unit/config/test_lazy_exports.py`` measures the help
surface against the same table, and a second hand-copied loader would drift the
moment the script moves or its spec-loading needs change.
"""

import importlib.util
from types import ModuleType

from tests._fixtures.paths import PROJECT_ROOT

HARNESS_PATH = PROJECT_ROOT / "scripts" / "import_budget.py"
"""Path to the harness script. Anchored on ``PROJECT_ROOT`` (G9: no
``Path(__file__).parents[N]`` arithmetic outside ``paths.py``)."""


def load_harness() -> ModuleType:
    """Import ``scripts/import_budget.py`` by path and return the module object.

    Deliberately NOT registered in ``sys.modules``: the script is measurement
    tooling, and a test that measures import graphs must not add itself to one.
    """
    spec = importlib.util.spec_from_file_location("import_budget", HARNESS_PATH)
    assert spec is not None, HARNESS_PATH
    assert spec.loader is not None, HARNESS_PATH
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
