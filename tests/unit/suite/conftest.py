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
def _restore_ensure_installed(monkeypatch):
    """Put ``otto.project.ensure_installed`` back after an inner test file replaces it."""
    from otto import project

    monkeypatch.setattr(project, "ensure_installed", project.ensure_installed)
