"""``real_shell_bytecode`` lifts the suite's pycache prefix without leaking bytecode (#518).

The fixture gives otto's pytest sessions a real shell's bytecode settings: no
prefix, writing on. It runs in an xdist worker, so it must not do that for the
whole test: every module the worker imports for the first time while the prefix
is gone and writing is on writes a ``__pycache__`` beside its source — under
``src/otto`` for otto's own lazy imports, which fails the suite's session guard
(``tests/conftest.py``). Writing is on only inside
:func:`otto.suite.run._outside_the_repo`, where the session's own prefix is set.

Both tests import a module from a throwaway package on ``sys.path`` for the
first time, which is exactly the hostile condition: a first import under the
fixture's settings.
"""

import importlib
import sys
from pathlib import Path

import pytest

from otto.suite import run


@pytest.fixture
def fresh_module(tmp_path, monkeypatch):
    """Return an importer of ``leakprobe.<name>`` modules, each imported for the first time."""
    pkg = tmp_path / "pkgroot" / "leakprobe"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(pkg.parent))
    imported: list[str] = []

    def load(name: str) -> Path:
        (pkg / f"{name}.py").write_text("VALUE = 1\n")
        importlib.invalidate_caches()
        imported.append(f"leakprobe.{name}")
        importlib.import_module(f"leakprobe.{name}")
        return pkg

    yield load
    for mod in ["leakprobe", *imported]:
        sys.modules.pop(mod, None)


def test_an_import_outside_a_session_writes_no_bytecode_beside_it(
    real_shell_bytecode, fresh_module
):
    assert sys.pycache_prefix is None, "a real shell's prefix: none"

    pkg = fresh_module("outside")

    assert not (pkg / "__pycache__").exists(), "a first import outside a session leaked bytecode"


def test_an_import_inside_a_session_writes_under_the_sessions_prefix(
    real_shell_bytecode, fresh_module, tmp_path
):
    home = tmp_path / "home"

    with run._outside_the_repo(collect_only=True, home=home):
        assert sys.dont_write_bytecode is False, "a session writes, as in a real shell"
        pkg = fresh_module("inside")
        prefix = Path(sys.pycache_prefix or "")

    assert not (pkg / "__pycache__").exists()
    assert list(prefix.rglob("inside.*.pyc")), "the session's bytecode went under its prefix"
    assert sys.pycache_prefix is None
    assert sys.dont_write_bytecode is True, "writing is off again once the session ends"
