"""Load the Getting Started example project the way a user's project loads.

``gs_example`` is imported inside :func:`import_gs_example`, never at module
scope: its init module registers process-global extensions (a command frame,
a reservation backend, parsers, session-setup hooks), and the root conftest's
``_isolate_registries`` snapshots every registry per test and evicts the
modules a test imported. An import at collection time would land inside that
baseline and leak into every later test on the worker, so every caller pays
the one-line lazy import here.
"""

import sys

from otto.config.lab import load_lab
from tests._fixtures.paths import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "docs" / "examples" / "getting-started"
LIBS = EXAMPLE / "libs"


def import_gs_example() -> None:
    """Import the example's init module (idempotent; registers its frames, hooks, parsers)."""
    if str(LIBS) not in sys.path:
        sys.path.insert(0, str(LIBS))
    __import__("gs_example")


def load_example_lab(labs: str):
    """Build *labs* from the example project's lab data, with its init module loaded."""
    import_gs_example()
    return load_lab(labs, search_paths=[EXAMPLE / "lab_data"])
