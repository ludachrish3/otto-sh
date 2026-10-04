"""Which repos run a session for the names, and which names each must find.

``otto.suite.run._decide`` reads only the tables and what ``classify`` found,
and never narrows a collection: every session collects its repo's whole
tree, because a table is a hint (a test can appear without any file it
watches changing, #592). The tables decide the order of the sessions and how
early a name no repo holds is refused: a name no record places is searched
for by one repo not yet searched in this run (one whose table is stale
first), whose session runs first and must find it, and a name still unplaced
once every repo is searched is unknown before any session runs.

Every table here is synthetic: no file is read, no pytest runs.
"""

from pathlib import Path

from otto.config.collected_tests import Classification, FileRecord, RecordedTest, RepoTable
from otto.suite.run import _decide, _Known


def _record(*names: str) -> FileRecord:
    return FileRecord(stat=[1, 1], tests=[RecordedTest(classes=[], name=n) for n in names])


def _known(
    root: Path,
    files: dict[str, list[str]],
    *,
    changed: tuple[str, ...] = (),
    cold: bool = False,
    deleted: tuple[str, ...] = (),
    found: tuple[str, ...] | None = None,
) -> _Known:
    """A repo under *root* whose records hold *files* (name -> tests), classified as given.

    *found*: the repo was searched in this run, and its session matched these names.
    """
    table = RepoTable(
        sut_dir=root,
        env={},
        dirs={},
        files={str(root / k): _record(*tests) for k, tests in files.items()},
    )
    stale = {*changed, *deleted} if not cold else set(files)
    classification = Classification(
        fresh=[root / k for k in files if k not in stale],
        changed=[root / k for k in (changed if not cold else files)],
        new=[],
        deleted=[root / k for k in deleted],
        whole_tree=cold,
        env={},
        stats={},
        dirs={},
        candidate_dirs=[],
    )
    return _Known(
        table=None if cold and not files else table,
        classification=classification,
        searched=found is not None,
        found=set(found or ()),
    )


def _sessions(decision) -> list[tuple[int, list[str]]]:
    """Each planned session as (repo index, must_match); every one collects the whole tree."""
    assert all(s.candidates is None for s in decision.sessions.values())
    return [(i, s.must_match) for i, s in decision.sessions.items()]


def test_a_session_collects_the_whole_tree_even_where_a_record_places_the_name(tmp_path):
    a = tmp_path / "a"
    known = [_known(a, {"test_a.py": ["test_x"], "test_b.py": ["test_y"]})]

    decision = _decide(["test_x"], known)

    assert decision.unknown == []
    assert decision.searching is None
    assert _sessions(decision) == [(0, ["test_x"])]


def test_one_repo_must_find_every_name_it_may_hold(tmp_path):
    """Placed or not, a name in a one-repo run can only be there: its session searches."""
    a = tmp_path / "a"
    known = [_known(a, {"test_a.py": ["test_x"]})]

    decision = _decide(["test_x", "test_generated"], known)

    assert decision.unknown == []
    assert decision.searching == 0
    assert _sessions(decision) == [(0, ["test_x", "test_generated"])]


def test_an_unplaced_name_is_searched_first_where_a_test_was_just_written(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_x"]}),
        _known(b, {"test_b.py": ["test_old"]}, changed=("test_b.py",)),
    ]

    decision = _decide(["test_x", "test_new"], known)

    assert decision.searching == 1
    assert _sessions(decision) == [(1, ["test_new"]), (0, [])]


def test_an_unplaced_name_with_nothing_stale_is_searched_by_the_first_repo(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_y"]})]

    decision = _decide(["test_generated"], known)

    assert decision.searching == 0
    assert _sessions(decision) == [(0, ["test_generated"]), (1, [])]


def test_a_cold_repo_searches_for_an_unplaced_name(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {}, cold=True)]

    decision = _decide(["test_x", "test_new"], known)

    assert _sessions(decision) == [(1, ["test_new"]), (0, [])]


def test_a_name_another_repo_may_hold_is_required_of_neither(tmp_path):
    """Records place test_x in a and test_y in b, but either may now be in the other."""
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_y"]})]

    decision = _decide(["test_x", "test_y"], known)

    assert decision.searching is None
    assert _sessions(decision) == [(0, []), (1, [])]


def test_every_repo_runs_even_one_whose_records_hold_none_of_the_names(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_y"]})]

    decision = _decide(["test_x"], known)

    assert list(decision.sessions) == [0, 1]


def test_a_repo_searched_whole_that_holds_none_of_the_names_gets_no_session(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_x"]}, found=()),
        _known(b, {"test_b.py": ["test_y"]}),
    ]

    decision = _decide(["test_y"], known)

    assert _sessions(decision) == [(1, ["test_y"])]


def test_after_a_search_misses_the_next_unsearched_repo_searches(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    known = [
        _known(a, {"test_a.py": ["test_x"]}, found=("test_x",)),
        _known(b, {"test_b.py": ["test_y"]}),
        _known(c, {"test_c.py": ["test_z"]}),
    ]

    decision = _decide(["test_x", "test_new"], known)

    assert decision.searching == 1
    assert _sessions(decision) == [(1, ["test_new"]), (0, []), (2, [])]


def test_what_a_search_read_places_a_name_for_certain(tmp_path):
    """A searched repo holding the name is the only place it can be once the rest are searched."""
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_new"]}, found=("test_new",)),
        _known(b, {"test_b.py": ["test_y"]}, found=()),
    ]

    decision = _decide(["test_new"], known)

    assert decision.unknown == []
    assert _sessions(decision) == [(0, ["test_new"])]


def test_a_name_unplaced_once_every_repo_is_searched_is_unknown(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_x"]}, found=()),
        _known(b, {"test_b.py": ["test_y"]}, found=()),
    ]

    decision = _decide(["test_nowhere"], known)

    assert decision.unknown == ["test_nowhere"]
    assert decision.sessions == {}


def test_a_searched_repo_trusts_only_what_its_session_matched(tmp_path):
    """A record the session did not bear out (pytest no longer collects it) places nothing."""
    a = tmp_path / "a"
    known = [_known(a, {"test_a.py": ["test_x"], "test_gone.py": ["test_y"]}, found=())]

    decision = _decide(["test_y"], known)

    assert decision.unknown == ["test_y"]


def test_a_name_a_searched_session_matched_is_placed_though_no_record_holds_it(tmp_path):
    """A custom collector's item matches without a module record: the match is what counts."""
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": []}, found=("test_custom",)),
        _known(b, {"test_b.py": ["test_y"]}, found=("test_y",)),
    ]

    decision = _decide(["test_custom", "test_y"], known)

    assert decision.unknown == []
    assert _sessions(decision) == [(0, ["test_custom"]), (1, ["test_y"])]


def test_a_deleted_holder_places_nothing(tmp_path):
    """A record whose file is gone is no hint: its name is searched for."""
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_x"], "test_b.py": ["test_y"]}, deleted=("test_b.py",)),
        _known(b, {"test_c.py": ["test_z"]}, changed=("test_c.py",)),
    ]

    decision = _decide(["test_y"], known)

    assert decision.searching == 1
