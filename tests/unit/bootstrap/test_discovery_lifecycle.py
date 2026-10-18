"""Discovery is cached until ``invalidate()``, through the root's public names alone.

Phase 1 of composition, discovery, parses the environment and each repo's settings and
runs no user code; its result is cached, and ``otto.bootstrap.invalidate()`` drops it
with the composed state, so the next read discovers again (spec 2026-10-11
placement-and-phases §3.9, "Invalidation"). Every read here goes through
``otto.bootstrap``'s public names, wherever discovery is defined.
"""

from otto import bootstrap as bs
from tests._fixtures.sutrepo import make_sut_repo


def test_invalidate_drops_the_discovery_and_the_next_read_discovers_again(tmp_path, monkeypatch):
    first = make_sut_repo(tmp_path / "first", name="first")
    second = make_sut_repo(tmp_path / "second", name="second")
    monkeypatch.setenv("OTTOPATH", str(first))
    bs.invalidate()
    assert [path.name for path in bs.get_env().ottopath] == ["first"]
    monkeypatch.setenv("OTTOPATH", str(second))
    assert [path.name for path in bs.get_env().ottopath] == ["first"], "cached until invalidated"
    bs.invalidate()
    assert [path.name for path in bs.get_env().ottopath] == ["second"]
    assert not bs.is_bootstrapped(), "discovery composes nothing"


def test_invalidate_drops_the_discovery_a_composition_read(tmp_path, monkeypatch):
    first = make_sut_repo(tmp_path / "first", name="first")
    second = make_sut_repo(tmp_path / "second", name="second")
    monkeypatch.setenv("OTTOPATH", str(first))
    bs.invalidate()
    assert [project.name for project in bs.bootstrap().projects] == ["first"]
    monkeypatch.setenv("OTTOPATH", str(second))
    bs.invalidate()
    assert not bs.is_bootstrapped()
    assert [project.name for project in bs.get_projects()] == ["second"]
