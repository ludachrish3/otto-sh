"""``otto.config``'s PEP 562 lazy export (``ResolvedDependency``): identity + laziness.

Mirrors the subprocess-isolation approach in ``test_import_ordering.py``:
within this test *process*, other tests (or ``otto.config`` itself, via
``otto.bootstrap``) may already have imported ``otto.config.dependencies``,
so an in-process check of "not yet in ``sys.modules``" can't be trusted to
mean anything. A fresh subprocess gives a clean ``sys.modules`` to assert
against.
"""

import subprocess
import sys

_SCRIPT = """
import sys

import otto.config

assert "otto.config.dependencies" not in sys.modules, (
    "bare 'import otto.config' must not eagerly pull in .dependencies"
)

resolved_dependency = otto.config.ResolvedDependency

assert "otto.config.dependencies" in sys.modules, (
    "attribute access must trigger the lazy import"
)

from otto.config.dependencies import ResolvedDependency

assert resolved_dependency is ResolvedDependency
print("LAZY EXPORT OK")
"""


def test_resolved_dependency_lazy_export_identity_and_laziness():
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "LAZY EXPORT OK" in out.stdout
