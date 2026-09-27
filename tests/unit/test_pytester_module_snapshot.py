"""An otto module first imported inside a pytester run must survive the restore.

pytester puts back its ``sys.modules`` snapshot after every in-process run. By
default that drops every module the run imported, including an ``otto.*``
submodule imported there for the first time, while the parent package keeps
the dropped module bound as an attribute. A later ``mock.patch`` on a dotted
path through that package then patches the orphan, and the code under test
re-imports a fresh copy that the patch never touched. That was the
intermittent ``tests/unit/suite/test_plugin.py`` failure: a monitor test built
a real collector for ``10.0.0.1`` because ``otto.monitor.factory`` had been
orphaned by a sibling pytester test. ``tests/_fixtures/_pytester_snapshot.py``
has the mechanism.

pytester restores in two places, after each inline run and in its teardown
(``Pytester._finalize``, restoring the snapshot taken when the fixture was
built). Both snapshots are built from the swapped class, so both keep otto's
modules.

The probe module is synthetic and uniquely named, so it cannot have been
imported before this test runs, whatever ran earlier on the worker. The inner
run is guaranteed to be its first importer. Removing the
``install_product_preserving_snapshot()`` call from ``tests/conftest.py`` turns
this test red.
"""

import importlib
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import otto

pytest_plugins = ["pytester"]


def test_an_otto_submodule_first_imported_in_an_inline_run_keeps_one_identity(
    pytester: pytest.Pytester,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = f"_pytester_snapshot_probe_{uuid.uuid4().hex}"
    full_name = f"otto.{probe}"
    probe_dir: Path = tmp_path_factory.mktemp("otto_ns")
    (probe_dir / f"{probe}.py").write_text('VALUE = "real"\n')
    # otto's __path__ gains a directory that holds only the probe, so
    # `import otto.<probe>` resolves as a genuine otto submodule.
    monkeypatch.setattr(otto, "__path__", [*otto.__path__, str(probe_dir)])

    pytester.makepyfile(
        test_inner=f"""
        def test_imports_the_probe():
            import {full_name}
            assert {full_name}.VALUE == "real"
        """
    )
    try:
        result = pytester.runpytest_inprocess(
            "-p",
            "no:cacheprovider",
            "-p",
            "no:playwright",
            # pytest-asyncio refuses to configure with this option unset.
            "-o",
            "asyncio_default_fixture_loop_scope=function",
        )
        result.assert_outcomes(passed=1)

        bound = otto.__dict__.get(probe)
        assert bound is not None, "the inner run never imported the probe"
        assert sys.modules.get(full_name) is bound, (
            f"{full_name} is bound on the otto package but pytester's restore dropped it "
            "from sys.modules. The next import makes a second copy that a dotted-path "
            "patch cannot reach."
        )

        # The consequence, as test_plugin.py hit it: patch by dotted path, then
        # import the name the way code under test does, at call time.
        with patch(f"{full_name}.VALUE", "patched"):
            assert importlib.import_module(full_name).VALUE == "patched"
    finally:
        sys.modules.pop(full_name, None)
        otto.__dict__.pop(probe, None)
