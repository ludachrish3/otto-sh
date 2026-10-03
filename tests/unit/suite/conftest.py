"""Shared fixtures for the ``tests/unit/suite`` package.

The ``otto_plugins`` family builds the two plugins ``otto test`` hands its
inner pytest session; the SUT-repo fixtures are shared with ``tests/unit/cli``.
"""

import pytest

from tests._fixtures.sut_repos import (  # noqa: F401 — fixtures, shared with tests/unit/cli
    _generated_modules_evicted,
    one_repo_double,
    sut_repo,
    two_sut_repos,
)


@pytest.fixture
def otto_output_dir(tmp_path):
    """Where ``otto_plugins`` installs the run's output_dir and its ``ArtifactLayout`` root."""
    return tmp_path / "otto-out"


@pytest.fixture
def otto_plugins(otto_output_dir):
    """The two plugins ``otto test`` hands its inner session, under an installed context.

    Built as ``otto.suite.run._run_pytest_session`` builds them for a run with
    no stability, monitor or SUT-directory options. The context's
    ``output_dir`` (and the plugin's ``ArtifactLayout``) live under
    ``otto_output_dir``, and the context stays installed for the whole test,
    so a test can inspect it after the inner session returns.
    """
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from otto.suite.layout import ArtifactLayout
    from otto.suite.plugin import OttoPlugin
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    token = set_context(OttoContext(lab=Lab(name="_test_stub"), output_dir=otto_output_dir))
    try:
        yield [OttoPlugin(), OttoFixturesPlugin(layout=ArtifactLayout(root=otto_output_dir))]
    finally:
        reset_context(token)


@pytest.fixture
def otto_plugins_iterations_2(otto_plugins):
    """``otto_plugins`` with ``OttoPlugin(iterations=2)`` swapped in for the plain plugin."""
    from otto.suite.plugin import OttoPlugin

    otto_plugins[0] = OttoPlugin(iterations=2)
    return otto_plugins


@pytest.fixture
def otto_plugins_with_monitor(otto_plugins):
    """``otto_plugins`` with a scripted session monitor collector in the plugin's slot.

    Returns the plugins and a callable listing the events the collector has
    recorded. The slot is set directly: ``_otto_session_monitor`` only fills it
    under ``--monitor``, which would also start collecting from real hosts.
    """
    from tests._fixtures._fake_collector import FakeCollector

    collector = FakeCollector()
    otto_plugins[0].session_monitor_collector = collector
    return otto_plugins, collector.get_events


@pytest.fixture
def _restore_ensure_installed():
    """Undo an inner test file's ``project.ensure_installed = ...`` exactly.

    ``ensure_installed`` is a lazy export, so "exactly" means taking the name
    back OUT of the package ``__dict__``. Setting the real function back (what
    ``monkeypatch.setattr`` would do) leaves it cached there, shadowing the
    package's ``__getattr__`` for the rest of the worker; the root conftest's
    lazy-export guard fails a test that does.
    """
    from otto import project

    missing = object()
    before = vars(project).get("ensure_installed", missing)
    yield
    if before is missing:
        vars(project).pop("ensure_installed", None)
    else:
        vars(project)["ensure_installed"] = before


@pytest.fixture
def real_shell_bytecode(monkeypatch):
    """A real shell's bytecode settings for otto's sessions: no pycache prefix, writing on.

    The suite runs under a prefix (``tests/conftest.py``); a real shell has none.
    Writing is on ONLY inside :func:`otto.suite.run._outside_the_repo`, where the
    session's own prefix is set, and off everywhere else in the test. Turning it
    on for the whole test would let every module this worker imports for the
    first time outside a session — otto's own lazy imports included — write a
    ``__pycache__`` beside its source, under ``src/otto``, which fails the
    suite's session guard in an import order the seed happens to draw (#518).
    """
    import contextlib
    import sys

    from otto.suite import run

    real_outside_the_repo = run._outside_the_repo

    @contextlib.contextmanager
    def writing_inside_the_session(**kwargs):
        with real_outside_the_repo(**kwargs) as args:
            sys.dont_write_bytecode = False
            try:
                yield args
            finally:
                sys.dont_write_bytecode = True

    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(run, "_outside_the_repo", writing_inside_the_session)
