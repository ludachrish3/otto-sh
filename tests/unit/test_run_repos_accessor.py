"""Where no context object is in hand, the run's repos come through one accessor (spec 2 §4)."""

import contextlib
from types import SimpleNamespace

import pytest

from otto import bootstrap as bs
from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, patch_bootstrap
from tests._fixtures.fake_repo import fake_repo


@pytest.fixture(autouse=True)
def _fresh():
    bs.invalidate()
    yield
    bs.invalidate()


def _docker_repo(name, use_case):
    """A repo declaring one docker use-case named *use_case* (as test_docker_host.py builds it)."""
    use_cases = [SimpleNamespace(name=use_case)]
    return fake_repo(name, docker_settings=SimpleNamespace(use_cases=use_cases))


def test_the_accessor_reads_the_installed_context_first(monkeypatch):
    from otto.config.fleet import current_ordered_repos, current_repos

    patch_bootstrap(monkeypatch, [fake_repo("from_bootstrap")])
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([fake_repo("from_ctx")]))
    binding = set_context(ctx)
    try:
        assert [r.name for r in current_repos()] == ["from_ctx"]
        assert [r.name for r in current_ordered_repos()] == ["from_ctx"]
    finally:
        reset_context(binding)
    assert [r.name for r in current_repos()] == ["from_bootstrap"]


def test_the_declared_use_case_query_works_inside_a_context_and_without_one(monkeypatch):
    from otto.docker.resolve import is_declared_use_case

    patch_bootstrap(monkeypatch, [_docker_repo("r", "web")])
    assert is_declared_use_case("web")
    assert not is_declared_use_case("db")
    binding = set_context(OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([])))
    try:
        assert not is_declared_use_case("web")  # the context's repos, not bootstrap's
    finally:
        reset_context(binding)


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
