"""``otto.bootstrap``'s declared surface, and the repo accessors folded into it.

Spec ``docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md``
§2 and §2.1, and appendix F of the 2026-10-04 manifest spec: twelve names
are declared; ``discover``, ``DiscoveryResult`` and ``set_completion_names``
stay phase-1 and CLI plumbing; the accessors keep their semantics.
"""

from types import SimpleNamespace

import pytest

from otto import bootstrap as bs
from tests._fixtures.sutrepo import make_sut_repo

APPENDIX_F = [
    "BootstrapError",
    "BootstrapResult",
    "BootstrapWarning",
    "DependencyError",
    "ProjectScopeError",
    "bootstrap",
    "get_completion_names",
    "get_env",
    "get_ordered_repos",
    "get_repos",
    "invalidate",
    "is_bootstrapped",
]


@pytest.fixture(autouse=True)
def _fresh_bootstrap():
    bs.invalidate()
    yield
    bs.invalidate()


def test_otto_bootstrap_declares_exactly_appendix_f():
    assert sorted(bs.__all__) == sorted(APPENDIX_F)
    assert {"discover", "DiscoveryResult", "set_completion_names"}.isdisjoint(bs.__all__)
    namespace: dict[str, object] = {}
    exec("from otto.bootstrap import *", namespace)  # noqa: S102 — a star import needs a statement
    assert sorted(set(namespace) - {"__builtins__"}) == sorted(APPENDIX_F)


def test_the_accessors_read_the_composition_root(tmp_path, monkeypatch):
    repo = make_sut_repo(tmp_path / "r", name="r")
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "7.5")

    assert bs.get_env().teardown_deadline == 7.5
    assert not bs.is_bootstrapped()  # get_env runs discovery, never bootstrap
    assert [r.name for r in bs.get_repos()] == ["r"]
    assert bs.is_bootstrapped()
    assert bs.get_repos() is bs.bootstrap().repos
    assert bs.get_ordered_repos() is bs.bootstrap().ordered_repos


def test_the_accessors_follow_a_patched_bootstrap(monkeypatch):
    """They look ``bootstrap`` up when called, so a test's patch of it reaches them."""
    fake = SimpleNamespace(repos=["x"], ordered_repos=["y"])
    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: fake)
    assert bs.get_repos() == ["x"]
    assert bs.get_ordered_repos() == ["y"]


def test_get_completion_names_documents_the_snapshot_keys():
    """The fold kept the docstring that documents the snapshot's keys (spec §2.1)."""
    doc = bs.get_completion_names.__doc__ or ""
    for key in (
        "instructions",
        "suites",
        "hosts",
        "term_backends",
        "transfer_backends",
    ):
        assert f"``{key}``" in doc, key
