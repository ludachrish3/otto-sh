"""Shared fixtures for the ``tests/unit/suite`` package.

Every OttoSuite subclass named ``Test*`` auto-registers into the process-wide
:data:`otto.suite.register.SUITES` registry at class-definition time (via
``OttoSuite.__init_subclass__``). Many tests in this package define such
subclasses — directly, or by importing/running suite files through an
*in-process* inner pytest session (``pytest.main([...])`` in
``test_otto_suite.py``; ``pytester.runpytest_inprocess`` in
``test_options_plugin.py``). Those inner sessions share the interpreter, so
their registrations land in the *same* global ``SUITES`` as the outer run.

``register_suite_class`` only treats a re-registration as a silent same-file
overwrite when the source *path* matches exactly. Each inner run writes its
suite to a fresh ``tmp_path``, so when the same outer test runs a second time
in one process — e.g. the nightly ``tests_hostless`` matrix under
``pytest --count=N --repeat-scope=session`` — the second registration comes
from a different path and raises "already registered by a different file",
interrupting inner collection. A single CI pass runs each test once and never
trips this, which is why it only surfaced in the nightly repeat job.

This directory-wide autouse fixture snapshots the registry before each test
and restores it afterward, so every test leaves ``SUITES`` exactly as it found
it and repeat runs stay independent. It supersedes the per-module copies that
previously lived in ``test_auto_registration.py`` and ``test_options_plugin.py``.
(``test_import_and_register.py`` keeps its own ``clean_registry`` fixture: it is
requested explicitly by the ``repo1`` fixture and carries extra import-cache
coupling beyond registry isolation.)
"""

import pytest

from otto.suite.register import SUITES


@pytest.fixture(autouse=True)
def _isolate_suites():
    """Park registered suites before each test and restore them after.

    Snapshots every entry in the global ``SUITES`` registry, clears it so the
    test starts from a predictable empty baseline, then on teardown removes any
    entries the test added and re-registers the originals. This keeps the
    registry byte-for-byte stable across tests and across repeat iterations of
    the same test in one process.
    """
    parked = {name: (SUITES.get(name), SUITES.origin(name)) for name in SUITES.names()}
    for name in list(SUITES.names()):
        SUITES.unregister(name)

    yield

    # Drop anything the test registered, then restore the original entries.
    for name in list(SUITES.names()):
        SUITES.unregister(name)
    for name, (entry, origin) in parked.items():
        SUITES.register(name, entry, overwrite=True, origin=origin)
