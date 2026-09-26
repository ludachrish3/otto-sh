"""The rebuild's corpus memo: one walk and one stat per path inside a scope, plain calls outside."""

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


def test_inside_a_scope_each_tree_is_walked_once(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "t.py").write_text("")
    calls = []
    real = os.scandir

    def spy(p="."):
        # Process-global, like `os.stat` above: count only this test's tree.
        if str(p).startswith(str(tmp_path)):
            calls.append(str(p))
        return real(p)

    monkeypatch.setattr(os, "scandir", spy)

    def prune(name):
        return name.startswith(".")

    with cs.corpus_snapshot():
        first = cs.walk(tmp_path, prune)
        second = cs.walk(tmp_path, prune)
    assert first == second
    assert len(calls) == 2  # tmp_path and sub, once each


def test_scopes_nest_by_reusing_the_outer_one():
    with cs.corpus_snapshot() as outer, cs.corpus_snapshot() as inner:
        assert inner is outer


def test_walk_result_is_a_copy_a_caller_can_mutate_without_corrupting_the_memo(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "t.py").write_text("")

    def prune(name):
        return False

    with cs.corpus_snapshot():
        first = cs.walk(tmp_path, prune)
        # The `dirs[:] = [...]` idiom every consumer uses to prune in place.
        for _root, dirs, _files in first:
            dirs[:] = []
        second = cs.walk(tmp_path, prune)
    assert second == [(str(tmp_path), ["sub"], []), (str(tmp_path / "sub"), [], ["t.py"])]


def test_glob_result_is_a_copy_a_caller_can_mutate_without_corrupting_the_memo(tmp_path):
    (tmp_path / "test_a.py").write_text("")
    with cs.corpus_snapshot():
        first = cs.glob(tmp_path, "test_*.py")
        first.clear()
        second = cs.glob(tmp_path, "test_*.py")
    assert second == [tmp_path / "test_a.py"]


def test_walk_matches_os_walk_topdown_pruned_semantics(tmp_path):
    """``cs.walk`` must reproduce the exact shape of the idiom it replaces.

    ``os.walk(top)`` with ``dirs[:] = [d for d in dirs if not prune(d)]``
    applied at each step is what every corpus reader used before the
    ``CorpusSnapshot`` walker, and what its docstring claims to still match:
    directories named by *prune* neither listed nor descended into, and a
    symlinked directory (however it is reached — pointing outside the tree,
    looping back into it, or broken) never followed. This tree exercises all
    of that plus a real file symlink and a directory this process cannot
    read into.

    ``scandir``'s sibling order is not a documented guarantee, and the two
    walkers each open their own, separate directory stream — so rather than
    trust that two back-to-back reads of the same directory return entries
    in the same order, each step's ``dirs``/``files`` are compared SORTED,
    and the steps themselves are compared as a set keyed by root (a walker
    that skipped or duplicated a directory still shows up; only sibling
    order within a step is deliberately not pinned).
    """
    from otto.config import completion_cache as cc

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "sub").mkdir()
    (root / "pkg" / "sub" / "leaf.py").write_text("")
    (root / "pkg" / "top.py").write_text("")
    (root / ".venv").mkdir()  # pruned by name (leading dot)
    (root / ".venv" / "should_not_appear.py").write_text("")
    (root / "build").mkdir()  # pruned by name (pytest's norecursedirs)
    (root / "build" / "should_not_appear.py").write_text("")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.py").write_text("")

    (root / "to_outside").symlink_to(outside, target_is_directory=True)
    (root / "loop").symlink_to(root, target_is_directory=True)
    (root / "broken").symlink_to(root / "does_not_exist")
    (root / "file_link.py").symlink_to(root / "pkg" / "top.py")

    locked = root / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("")
    locked.chmod(0o000)
    try:
        prune = cc._is_norecurse_dir

        got = cs.walk(root, prune)

        want: list[cs.WalkStep] = []
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not prune(d)]
            want.append((r, list(dirs), list(files)))

        def normalize(steps: list[cs.WalkStep]) -> list[tuple[str, list[str], list[str]]]:
            return sorted((r, sorted(d), sorted(f)) for r, d, f in steps)

        assert normalize(got) == normalize(want)
    finally:
        locked.chmod(0o755)


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


def test_every_consumer_sees_the_same_corpus_through_the_snapshot(discovered):
    from otto.config import cache_sections as sec
    from otto.config import completion_cache as cc
    from otto.config.completion_tree import stat_triple

    def observe():
        return {
            "names": sorted(map(str, sec._names_key_paths(discovered))),
            "tests": sorted(map(str, sec._tests_key_paths(discovered))),
            "digests": sec.section_digests(discovered, sec.SECTIONS),
            "scan": cc.scan_test_corpus(discovered),
            "fingerprint": cc.compute_fingerprint(discovered),
            "triples": [stat_triple(p) for p in sorted(set(sec._tests_key_paths(discovered)))],
        }

    plain = observe()
    with cs.corpus_snapshot():
        scoped = observe()
    assert scoped == plain
