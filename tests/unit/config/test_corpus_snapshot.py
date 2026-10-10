"""The rebuild's stat memo: one stat per path inside a scope, plain calls outside."""

import os

import pytest

from otto.config import corpus_snapshot as cs
from tests._fixtures.generated_repo import generate_repo
from tests._fixtures.paths import PROJECT_ROOT


def test_outside_a_scope_nothing_is_memoized(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x")
    first = cs.stat(f)
    f.write_text("xx")
    assert cs.stat(f).st_size == 2 != first.st_size


def test_inside_a_scope_each_path_is_stat_once(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("x")
    calls = []
    real = os.stat

    def spy(p, *a, **k):
        # `os.stat` is process-global: coverage's tracer stats the source of
        # code it has not seen yet (3.14's sys.monitoring does, the first time
        # corpus_snapshot runs in a worker), so count only this test's paths.
        if str(p).startswith(str(tmp_path)):
            calls.append(str(p))
        return real(p, *a, **k)

    monkeypatch.setattr(cs.os, "stat", spy)
    with cs.corpus_snapshot():
        cs.stat(f)
        cs.stat(f)
    assert calls == [str(f)]


def test_snapshot_records_a_vanished_path_as_missing(tmp_path):
    gone = tmp_path / "gone.py"
    with cs.corpus_snapshot():
        assert cs.stat(gone) is None
        gone.write_text("appeared")
        assert cs.stat(gone) is None  # consistent within the scope; the next run sees it


def test_scopes_nest_by_reusing_the_outer_one():
    with cs.corpus_snapshot() as outer, cs.corpus_snapshot() as inner:
        assert inner is outer


@pytest.fixture(params=["generated", "repo1", "repo2"])
def discovered(request, tmp_path, monkeypatch):
    from otto import bootstrap as bs

    if request.param == "generated":
        sut = generate_repo(tmp_path, files=12, dirs=3, realistic=True)
    else:
        sut = PROJECT_ROOT / "tests" / request.param
    monkeypatch.setenv("OTTO_SUT_DIRS", str(sut))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    bs.invalidate()
    try:
        yield bs.discover().repos
    finally:
        bs.invalidate()


def test_every_consumer_sees_the_same_files_through_the_snapshot(discovered):
    from otto.config import cache_sections as sec
    from otto.config.completion_tree import stat_triple

    names = sec.section_by_name("names")

    def observe():
        keys = sec.writer_key_paths(names, discovered)
        return {
            "names": sorted(map(str, keys)),
            "digests": sec.section_digests(discovered, sec.SECTIONS),
            "triples": [stat_triple(p) for p in sorted(set(keys))],
        }

    plain = observe()
    with cs.corpus_snapshot():
        scoped = observe()
    assert scoped == plain
