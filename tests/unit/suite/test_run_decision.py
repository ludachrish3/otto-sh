"""Which files hold the names, decided across every repo before any pytest session starts.

``otto.suite.run._decide`` reads only the tables and what ``classify`` found:
a name a trusted record holds is placed; a repo that is cold or has files the
table cannot vouch for is uncertain. A name no trusted record holds sends the
uncertain repos to a collection first (one uncertain repo folds that
collection into its own run session, which then must match the name), and a
name still unplaced is unknown before any session runs. Design:
``docs/superpowers/specs/2026-09-27-test-name-cache-design.md`` §11.2, §12.2.

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
    new: tuple[str, ...] = (),
    candidate_dirs: tuple[str, ...] = (),
    cold: bool = False,
    read: tuple[str, ...] = (),
    searched: bool = False,
) -> _Known:
    """A repo under *root* whose records hold *files* (name -> tests), classified as given."""
    table = RepoTable(
        sut_dir=root,
        env={},
        dirs={},
        files={str(root / k): _record(*tests) for k, tests in files.items()},
    )
    stale = {*changed, *new} if not cold else set(files)
    classification = Classification(
        fresh=[root / k for k in files if k not in stale],
        changed=[root / k for k in (changed if not cold else files)],
        new=[root / k for k in new],
        deleted=[],
        whole_tree=cold,
        env={},
        stats={},
        dirs={},
        candidate_dirs=[root / d for d in candidate_dirs],
    )
    return _Known(
        table=None if cold and not files else table,
        classification=classification,
        read={str(root / k) for k in read},
        searched=searched,
    )


def _sessions(decision, roots: list[Path]) -> list[tuple[int, list[str] | None, list[str]]]:
    """Each planned session as (repo index, candidate names relative to the repo, must_match)."""
    return [
        (
            i,
            None if s.candidates is None else [str(p.relative_to(roots[i])) for p in s.candidates],
            s.must_match,
        )
        for i, s in decision.sessions.items()
    ]


def test_names_trusted_records_hold_run_where_they_are_held(tmp_path):
    a = tmp_path / "a"
    known = [_known(a, {"test_a.py": ["test_x"], "test_b.py": ["test_y"]})]

    decision = _decide(["test_x"], known)

    assert decision.refresh == []
    assert decision.unknown == []
    assert _sessions(decision, [a]) == [(0, ["test_a.py"], ["test_x"])]


def test_the_files_the_table_cannot_vouch_for_ride_along_with_the_holders(tmp_path):
    a = tmp_path / "a"
    known = [
        _known(
            a,
            {"test_a.py": ["test_x"], "test_b.py": ["test_y"]},
            changed=("test_b.py",),
            candidate_dirs=("sub",),
        )
    ]

    decision = _decide(["test_x"], known)

    assert decision.sessions[0].candidate_dirs == [a / "sub"]
    assert _sessions(decision, [a]) == [(0, ["test_a.py", "test_b.py"], ["test_x"])]


def test_an_unplaced_name_with_nothing_uncertain_is_unknown_before_any_session(tmp_path):
    a = tmp_path / "a"
    known = [_known(a, {"test_a.py": ["test_x"]})]

    decision = _decide(["test_x", "test_nowhere"], known)

    assert decision.unknown == ["test_nowhere"]
    assert decision.sessions == {}
    assert decision.refresh == []


def test_one_uncertain_repo_folds_its_refresh_into_its_session_and_runs_first(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_x"]}),
        _known(b, {"test_b.py": ["test_old"]}, changed=("test_b.py",)),
    ]

    decision = _decide(["test_x", "test_new"], known)

    assert decision.refresh == []
    assert decision.unknown == []
    assert _sessions(decision, [a, b]) == [
        (1, ["test_b.py"], ["test_new"]),
        (0, ["test_a.py"], ["test_x"]),
    ]


def test_one_cold_repo_with_an_unplaced_name_folds_its_seed_into_its_session(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {}, cold=True)]

    decision = _decide(["test_x", "test_new"], known)

    assert _sessions(decision, [a, b]) == [(1, None, ["test_new"]), (0, ["test_a.py"], ["test_x"])]


def test_a_cold_repo_beside_a_warm_one_is_searched_whole_even_when_every_name_is_placed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_x"]}, cold=True)]

    decision = _decide(["test_x"], known)

    assert decision.refresh == []
    assert _sessions(decision, [a, b]) == [(0, ["test_a.py"], ["test_x"]), (1, None, [])]


def test_names_split_across_repos_must_match_where_they_are_held(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_y"]})]

    decision = _decide(["test_x", "test_y"], known)

    assert _sessions(decision, [a, b]) == [
        (0, ["test_a.py"], ["test_x"]),
        (1, ["test_b.py"], ["test_y"]),
    ]


def test_a_name_two_repos_hold_is_required_of_neither(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_x"]})]

    decision = _decide(["test_x"], known)

    assert _sessions(decision, [a, b]) == [(0, ["test_a.py"], []), (1, ["test_b.py"], [])]


def test_two_uncertain_repos_and_an_unplaced_name_are_refreshed_first(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    known = [
        _known(a, {"test_a.py": ["test_x"]}, changed=("test_a.py",)),
        _known(b, {"test_b.py": ["test_y"]}),
        _known(c, {}, cold=True),
    ]

    decision = _decide(["test_new"], known)

    assert decision.refresh == [0, 2]
    assert decision.sessions == {}
    assert decision.unknown == []


def test_two_uncertain_repos_with_every_name_placed_are_not_refreshed(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    known = [
        _known(a, {"test_a.py": ["test_x"]}, changed=("test_a.py",)),
        _known(b, {"test_b.py": ["test_y"]}),
        _known(c, {}, cold=True),
    ]

    decision = _decide(["test_y"], known)

    assert decision.refresh == []
    assert _sessions(decision, [a, b, c]) == [
        (0, ["test_a.py"], []),
        (1, ["test_b.py"], ["test_y"]),
        (2, None, []),
    ]


def test_after_a_refresh_what_it_read_places_a_name_even_when_still_changed(tmp_path):
    """A record the refresh just wrote is trusted, even one a new dependency keeps changed."""
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_new"]}, changed=("test_a.py",), read=("test_a.py",)),
        _known(b, {"test_b.py": ["test_y"]}, searched=True),
    ]
    known[0].searched = True

    decision = _decide(["test_new"], known)

    assert decision.unknown == []
    assert _sessions(decision, [a, b]) == [(0, ["test_a.py"], ["test_new"])]


def test_a_name_still_unplaced_after_the_refresh_is_unknown(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [
        _known(a, {"test_a.py": ["test_x"]}, read=("test_a.py",), searched=True),
        _known(b, {"test_b.py": ["test_y"]}, searched=True),
    ]

    decision = _decide(["test_nowhere"], known)

    assert decision.unknown == ["test_nowhere"]
    assert decision.sessions == {}


def test_a_repo_holding_nothing_asked_for_and_vouched_for_whole_gets_no_session(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    known = [_known(a, {"test_a.py": ["test_x"]}), _known(b, {"test_b.py": ["test_y"]})]

    decision = _decide(["test_x"], known)

    assert list(decision.sessions) == [0]


def test_a_deleted_holder_places_nothing(tmp_path):
    """A record whose file is gone is not fresh: its names are not placed by it."""
    a = tmp_path / "a"
    known = _known(a, {"test_a.py": ["test_x"], "test_b.py": ["test_y"]})
    known.classification.fresh.remove(a / "test_b.py")
    known.classification.deleted.append(a / "test_b.py")

    decision = _decide(["test_y"], [known])

    assert decision.unknown == ["test_y"]
