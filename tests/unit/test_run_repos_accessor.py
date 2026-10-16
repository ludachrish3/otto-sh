"""Where no context object is in hand, the run's repos come through one accessor (spec 2 §4)."""

import contextlib

import pytest

from otto import bootstrap as bs
from otto.context import OttoContext, reset_context, set_context
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, patch_bootstrap
from tests._fixtures.fake_repo import fake_repo


@pytest.fixture(autouse=True)
def _fresh():
    bs.invalidate()
    yield
    bs.invalidate()


def test_the_accessor_reads_the_installed_context_first(monkeypatch):
    from otto.declared import current_ordered_repos, current_repos

    # Composed in a dependency order unlike the discovery order, so each reader's
    # fallback must answer its own list.
    composed = [fake_repo("from_bootstrap"), fake_repo("its_dependency")]
    patch_bootstrap(monkeypatch, composed, ordered=composed[::-1])
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([fake_repo("from_ctx")]))
    binding = set_context(ctx)
    try:
        assert [r.name for r in current_repos()] == ["from_ctx"]
        assert [r.name for r in current_ordered_repos()] == ["from_ctx"]
    finally:
        reset_context(binding)
    assert [r.name for r in current_repos()] == ["from_bootstrap", "its_dependency"]
    assert [r.name for r in current_ordered_repos()] == ["its_dependency", "from_bootstrap"]


@pytest.mark.usefixtures("no_composition_root")
def test_with_no_composition_root_a_read_that_would_compose_refuses():
    """Nothing published, no run installed, no initiator: every composing read refuses."""
    from otto import declared

    for read in [declared.compose, declared.get_repos, declared.current_repos]:
        with pytest.raises(RuntimeError, match="no composition root is loaded"):
            read()


def test_the_docker_conveniences_still_refuse_without_a_context():
    from otto.docker.deployment import resolve_use_case

    with pytest.raises(RuntimeError, match="No active OttoContext"):
        resolve_use_case("web", parent=None, provide=None)


def test_run_tests_builds_its_context_before_it_selects_repos(monkeypatch, tmp_path):
    from otto.suite import run as suite_run

    order: list[str] = []
    real = suite_run._session_context

    @contextlib.contextmanager
    def spy(log_dir):
        # Recorded on ENTRY, not on the call: a caller that built the context
        # manager first and read the repos before entering it would pass a
        # call-time spy.
        order.append("context")
        with real(log_dir) as ctx:
            yield ctx

    monkeypatch.setattr("otto.suite.run._session_context", spy)
    monkeypatch.setattr(
        "otto.bootstrap.bootstrap", lambda: order.append("repos") or fake_bootstrap_result([])
    )
    # A name, since with neither a name nor markers run_tests raises ValueError first.
    with pytest.raises(suite_run.NoTestsMatchedError):
        suite_run.run_tests(["test_x"], output_dir=tmp_path)
    assert order[0] == "context"
