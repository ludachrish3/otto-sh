"""Spec §4.2: what the shim trusts, what it re-checks, and what makes it hand over."""

import json
import os
import time
from pathlib import Path

import pytest

from otto import _shim_complete as sc
from otto import bootstrap
from otto.config import completion_cache as cc
from otto.config.completion_tree import build_shim_payload
from tests._fixtures.generated_repo import generate_repo


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    repo = generate_repo(tmp_path, files=3, dirs=1, top_level=1)
    # Backdate every path under repo by 2s BEFORE write_cache stat()s them: the
    # kernel's coarse mtime clock (measured 1ms granularity on this VM) can
    # otherwise land generate_repo's writes and a later test write in the same
    # tick, so a directory's mtime would not visibly move (see obligation 2,
    # task-9 brief) and stat-pass pins that expect staleness would flake green.
    backdated = time.time_ns() - 2_000_000_000
    for dirpath, dirnames, filenames in os.walk(repo):
        for name in dirnames + filenames:
            os.utime(Path(dirpath) / name, ns=(backdated, backdated))
    os.utime(repo, ns=(backdated, backdated))
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setenv("OTTO_HOME", str(home))
    bootstrap.invalidate()
    repos = bootstrap.discover().repos
    cc.write_cache(repos, [], [], ["local"], shim=build_shim_payload(repos))
    cache = Path(sc.locate_cache(dict(os.environ)))
    assert cache == cc._cache_path()
    yield repo, cache
    bootstrap.invalidate()


def _data(cache: Path) -> dict:
    return json.loads(cache.read_text())


def test_locate_hands_over_without_sut_dirs(monkeypatch):
    monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)
    with pytest.raises(sc.Handover, match="SUT"):
        sc.locate_cache({})


def test_a_cold_cache_file_hands_over_with_a_plain_reason(tmp_path, monkeypatch):
    # The common first-run case: OTTO_HOME has no completion_cache.json yet.
    monkeypatch.setenv("OTTO_SUT_DIRS", str(tmp_path / "repo"))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto ")
    monkeypatch.setenv("COMP_CWORD", "1")
    out = sc.answer_or_reason(dict(os.environ))
    assert out.items is None
    assert out.reason == "no cache file"


def test_inspect_shim_on_an_absent_cache_names_the_plain_reason(tmp_path):
    """``inspect_shim`` must not let ``open()``'s ``FileNotFoundError`` leak past the
    validator's own ``no cache file`` reason (Handover's ``except`` branch) into the
    catch-all ``except Exception`` branch, which would report ``error:
    FileNotFoundError: ...`` instead — the same carry-forward defect ``otto cache
    info`` surfaced (Task 13)."""
    assert sc.inspect_shim(tmp_path / "absent.json") == "handing over — no cache file"


def test_a_fresh_entry_validates_by_stat_then_by_marker(workspace):
    _, cache = workspace
    now = time.time()
    assert sc.validate_keys(cache, _data(cache), "names", now) == "stat"
    marker = cache.parent / sc.MARKER_FILENAMES["names"]
    assert marker.is_file()
    assert sc.validate_keys(cache, _data(cache), "names", now + 30) == "marker"
    assert sc.validate_keys(cache, _data(cache), "names", now + 61) == "stat"


def test_tests_site_checks_both_key_sets_and_both_markers(workspace):
    _, cache = workspace
    now = time.time()
    assert sc.validate_keys(cache, _data(cache), "tests", now) == "stat"
    assert (cache.parent / sc.MARKER_FILENAMES["tests"]).is_file()


def test_an_edited_key_file_fails_the_stat_pass_and_the_digest(workspace):
    repo, cache = workspace
    (repo / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: the edit under test
        'name = "genrepo"\nversion = "0.1.0"\n\n'
    )
    with pytest.raises(sc.Handover, match="stale"):
        sc.validate_keys(cache, _data(cache), "names", time.time())
    assert cc.cache_rebuild_is_worthwhile(bootstrap.discover().repos) is True


def test_a_file_created_in_a_watched_directory_fails_the_stat_pass(workspace):
    repo, cache = workspace
    (repo / "tests" / "test_added.py").write_text("def test_z():\n    pass\n")
    with pytest.raises(sc.Handover, match="stale"):
        sc.validate_keys(cache, _data(cache), "names", time.time())


def test_a_stored_missing_path_that_appears_fails(workspace):
    _repo, cache = workspace
    data = _data(cache)
    missing = next(t for t in data["sections"]["shim"]["payload"]["keys"]["names"] if t[1] is None)
    Path(missing[0]).parent.mkdir(parents=True, exist_ok=True)
    Path(missing[0]).write_text("")
    with pytest.raises(sc.Handover, match="appeared"):
        sc.validate_keys(cache, data, "names", time.time())


def test_a_marker_older_than_the_cache_file_is_not_trusted(workspace):
    _, cache = workspace
    now = time.time()
    sc.validate_keys(cache, _data(cache), "names", now)
    marker = cache.parent / sc.MARKER_FILENAMES["names"]
    os.utime(marker, ns=(cache.stat().st_mtime_ns - 1, cache.stat().st_mtime_ns - 1))
    assert sc.validate_keys(cache, _data(cache), "names", now + 1) == "stat"


def test_a_deleted_marker_sends_the_next_tab_back_through_the_stat_pass(workspace):
    """`_marker_fresh` returns False when the marker is GONE, not just when it is old.

    The window is opened by a file, so deleting it (a cleaned cache dir, a `tmp`
    reaper) must close it: an OSError read as "fresh" would serve an unchecked key
    set for as long as the entry lives.
    """
    _, cache = workspace
    now = time.time()
    marker = cache.parent / sc.MARKER_FILENAMES["names"]
    assert sc.validate_keys(cache, _data(cache), "names", now) == "stat"
    assert sc.validate_keys(cache, _data(cache), "names", now + 1) == "marker"
    marker.unlink()
    assert sc.validate_keys(cache, _data(cache), "names", now + 2) == "stat"
    assert marker.is_file()  # and the pass re-creates it


def test_a_marker_that_cannot_be_created_is_ignored(workspace, monkeypatch):
    """The other half of the failed-touch rule: `utime` raises FileNotFoundError and
    the CREATE that follows fails too (a read-only cache dir). Every TAB then pays
    the stat pass — slower, never wrong — instead of raising into the shell."""
    _, cache = workspace
    data = _data(cache)  # read before `open` is intercepted
    marker = cache.parent / sc.MARKER_FILENAMES["names"]
    assert not marker.exists()
    real_open = open

    def only_the_marker_fails(file, *args, **kwargs):
        if str(file) == str(marker):
            raise PermissionError("read-only cache dir")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", only_the_marker_fails)
    assert sc.validate_keys(cache, data, "names", time.time()) == "stat"
    assert not marker.exists()


def test_a_failed_touch_is_ignored(workspace, monkeypatch):
    _, cache = workspace

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(sc.os, "utime", boom)
    assert sc.validate_keys(cache, _data(cache), "names", time.time()) == "stat"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: d.__setitem__("schema", 17), "schema"),
        (lambda d: d["sections"]["shim"].__setitem__("tainted", True), "tainted"),
        (lambda d: d["sections"]["shim"].__setitem__("generated_at", 0), "expired"),
        (lambda d: d["sections"].pop("names"), "names"),
        (
            lambda d: d["sections"]["shim"]["payload"].__setitem__("inventory", {"kind": "opaque"}),
            "opaque",
        ),
    ],
)
def test_servable_shim_hands_over(workspace, mutate, reason):
    _, cache = workspace
    data = _data(cache)
    mutate(data)
    with pytest.raises(sc.Handover, match=reason):  # noqa: PT012 — two calls: opaque only fails the 2nd
        sc.servable_shim(data, time.time())
        sc.validate_keys(cache, data, "names", time.time())


def test_answer_or_reason_end_to_end(workspace, monkeypatch):
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto ho")
    monkeypatch.setenv("COMP_CWORD", "1")
    out = sc.answer_or_reason(dict(os.environ))
    assert out.items is not None and "host" in out.items and out.reason == ""  # noqa: PT018
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_zsh")
    out = sc.answer_or_reason(dict(os.environ))
    assert out.items is None and out.reason == "not bash"  # noqa: PT018
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto tunnel remove ")
    monkeypatch.setenv("COMP_CWORD", "3")
    out = sc.answer_or_reason(dict(os.environ))
    assert out.items is None and "live" in out.reason  # noqa: PT018


def test_a_node_missing_a_required_key_hands_over_rather_than_guessing(workspace, monkeypatch):
    """A Node without ``group`` is a payload the shim cannot read — not a leaf.

    ``SCHEMA`` deliberately stays 18 across this plan, so an entry written by an
    earlier commit on this branch VALIDATES and yet has no ``group`` key. Read with
    ``.get`` that would silently parse every group with leaf semantics (an
    option-looking word after a group's positional parsed as an option, which is
    exactly the mismatch the differential caught); indexed, it raises and
    ``answer_or_reason`` hands over. Same rule as ``params`` and ``commands``.
    """
    _, cache = workspace
    data = _data(cache)
    data["sections"]["shim"]["payload"]["tree"].pop("group")
    cache.write_text(json.dumps(data))
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto host ")
    monkeypatch.setenv("COMP_CWORD", "2")
    out = sc.answer_or_reason(dict(os.environ))
    assert out.items is None
    assert out.reason.startswith("error: KeyError")


def test_answer_never_raises(monkeypatch):
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto ")
    monkeypatch.setenv("COMP_CWORD", "not-an-int")
    assert sc.answer(dict(os.environ)) is None


def test_schema_constants_track_the_product():
    assert sc.SCHEMA == cc.SCHEMA_VERSION
    assert sc.COLLECTED_SCHEMA == cc.COLLECTED_SCHEMA_VERSION
    assert sc.COLLECTED_TTL_SECONDS == cc.CACHE_TTL_SECONDS
    assert sc.CACHE_FILENAME == cc.CACHE_FILENAME
    from otto.config import cache_maintenance as cmn

    assert sc.MARKER_FILENAMES == cmn.MARKER_FILENAMES
    assert sc.WINDOW_SECONDS == cmn.SHIM_WINDOW_SECONDS
