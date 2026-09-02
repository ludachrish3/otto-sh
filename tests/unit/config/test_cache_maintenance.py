import os
import time

import pytest

from otto.config.home import workspace_key
from tests._fixtures.cache_workspace import OLD, YOUNG, _mk_workspace


def test_matcher_accepts_a_real_workspace_key(tmp_path):
    """Couples the matcher to the LIVE key format — format drift breaks loudly."""
    from otto.config.cache_maintenance import prune

    real_key = workspace_key([tmp_path / "some-repo"])
    ws = _mk_workspace(tmp_path, real_key)
    report = prune(tmp_path)
    assert ws / "completion_cache.json" in report.files_removed


def test_prune_age_boundary(tmp_path):
    from otto.config.cache_maintenance import prune

    old = _mk_workspace(tmp_path, "aaaaaaaa-old", cache_age=OLD)
    young = _mk_workspace(tmp_path, "bbbbbbbb-young", cache_age=YOUNG)
    report = prune(tmp_path, max_age_days=60)
    assert old / "completion_cache.json" in report.files_removed
    assert (young / "completion_cache.json").is_file()
    assert young in report.retained_young
    assert not old.exists()  # emptied -> rmdir'd
    assert old in report.dirs_removed


def test_prune_never_touches_an_env_bearing_dir_itself(tmp_path):
    from otto.config.cache_maintenance import prune

    ws = _mk_workspace(tmp_path, "cccccccc-active", cache_age=OLD, env=True)
    report = prune(tmp_path)
    assert not (ws / "completion_cache.json").exists()  # cache file goes
    assert (ws / "env" / "bin" / "python").exists()  # venv survives
    assert ws.exists()
    assert ws in report.retained_nonempty


def test_prune_removes_the_sidecar_too(tmp_path):
    from otto.config.cache_maintenance import prune

    ws = _mk_workspace(tmp_path, "dddddddd-side", cache_age=OLD, sidecar=True)
    prune(tmp_path)
    assert not ws.exists()  # both files gone, dir emptied


def test_prune_ignores_non_matching_names(tmp_path):
    from otto.config.cache_maintenance import prune

    trap = tmp_path / "inventory-cache"
    trap.mkdir()
    (trap / "completion_cache.json").write_text("{}")
    (tmp_path / "settings.toml").write_text("")  # sutrepo-exempt: decoy prune must not touch
    report = prune(tmp_path, age_blind=True)
    assert (trap / "completion_cache.json").is_file()
    assert (tmp_path / "settings.toml").is_file()
    assert report.files_removed == []


def test_prune_skips_symlinked_candidates(tmp_path):
    from otto.config.cache_maintenance import prune

    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "completion_cache.json").write_text("{}")
    (tmp_path / "eeeeeeee-link").symlink_to(real)
    prune(tmp_path, age_blind=True)
    assert (real / "completion_cache.json").is_file()


def test_dry_run_removes_nothing_but_reports_everything(tmp_path):
    from otto.config.cache_maintenance import prune

    ws = _mk_workspace(tmp_path, "ffffffff-dry", cache_age=OLD)
    report = prune(tmp_path, dry_run=True)
    assert ws / "completion_cache.json" in report.files_removed
    assert report.bytes_freed > 0
    assert (ws / "completion_cache.json").is_file()
    assert ws.exists()


def test_clear_workspace_is_single_dir_and_keeps_the_dir(tmp_path):
    from otto.config.cache_maintenance import clear_workspace

    ws = _mk_workspace(tmp_path, "abababab-cur", cache_age=YOUNG, sidecar=True)
    report = clear_workspace(ws)
    assert sorted(p.name for p in report.files_removed) == [
        "completion_cache.json",
        "remote_completion_cache.json",
    ]
    assert ws.exists()


def test_iter_workspaces_sorts_oldest_first_and_sees_env(tmp_path):
    """[Fix round 2, controller ruling] The sort key MUST be `oldest_cache_mtime`,
    the same field `otto cache info` renders in its age column — a mixed-age
    workspace (very old primary, very fresh sidecar) proves the two agree:
    sorting by `newest_cache_mtime` instead would put it last (its freshest
    file is the youngest of the three), even though the number rendered next
    to it (its OLDEST file) is the oldest of the three."""
    from otto.config.cache_maintenance import iter_workspaces

    _mk_workspace(tmp_path, "11111111-young", cache_age=YOUNG)
    _mk_workspace(tmp_path, "22222222-old", cache_age=OLD, env=True)
    _mk_workspace(
        tmp_path,
        "33333333-mixed",
        cache_age=70 * 86400,  # older than "old"'s 61d -- must sort FIRST
        sidecar=True,
        sidecar_age=1 * 86400,  # but its freshest file is the youngest of the three
    )
    infos = iter_workspaces(tmp_path)
    assert [i.key for i in infos] == ["33333333-mixed", "22222222-old", "11111111-young"]
    assert infos[0].has_env is False
    assert infos[1].has_env
    assert not infos[2].has_env
    # The rendered age column and the sort order are the SAME field: ascending
    # oldest_cache_mtime across the list means "oldest first" is literally true
    # of what a caller would print, not just of a different field that usually
    # agrees with it.
    ages = [i.oldest_cache_mtime for i in infos]
    assert ages == sorted(ages)


def test_iter_workspaces_reports_oldest_cache_mtime_separately(tmp_path):
    """`oldest_cache_mtime` tracks the STALER of the two cache files, not the
    fresher one `newest_cache_mtime` already reports — a mixed-age workspace
    (old primary, young sidecar) must show a big gap between the two fields,
    since `prune` decides per file and the old primary is what it would act
    on regardless of the young sidecar sitting next to it."""
    from otto.config.cache_maintenance import iter_workspaces

    ws = _mk_workspace(tmp_path, "eeeeeeee-mixed", cache_age=OLD, sidecar=True, sidecar_age=YOUNG)
    (info,) = iter_workspaces(tmp_path)
    assert info.path == ws
    assert info.oldest_cache_mtime < info.newest_cache_mtime
    assert (time.time() - info.oldest_cache_mtime) >= OLD
    assert (time.time() - info.newest_cache_mtime) <= YOUNG + 5  # a few seconds' test slack


def test_iter_workspaces_oldest_cache_mtime_is_none_with_no_cache_files(tmp_path):
    from otto.config.cache_maintenance import iter_workspaces

    _mk_workspace(tmp_path, "eeeeeeef-envonly", cache_age=None, env=True)
    (info,) = iter_workspaces(tmp_path)
    assert info.oldest_cache_mtime is None
    assert info.newest_cache_mtime is None


def test_missing_home_is_empty_not_an_error(tmp_path):
    from otto.config.cache_maintenance import iter_workspaces, prune

    ghost = tmp_path / "nope"
    assert iter_workspaces(ghost) == []
    assert prune(ghost).files_removed == []


def test_cache_file_names_matches_the_source_constants():
    """The module-level assert this used to be is elided under `python -O`;
    this test is the real pin, and it runs every time, `-O` or not."""
    from otto.config.cache_maintenance import CACHE_FILE_NAMES
    from otto.config.completion_cache import CACHE_FILENAME
    from otto.config.remote_completion_cache import REMOTE_CACHE_FILENAME

    assert CACHE_FILE_NAMES == [CACHE_FILENAME, REMOTE_CACHE_FILENAME]


def test_prune_survives_a_directory_named_like_a_cache_file(tmp_path):
    """`lstat` succeeds on a directory sitting where a cache file's name is
    expected; `unlink` then raises `IsADirectoryError`. The walk must still
    finish and touch the other workspace, and the bogus entry must never be
    reported as removed."""
    from otto.config.cache_maintenance import prune

    trap = _mk_workspace(tmp_path, "88888888-trap", cache_age=None)
    bogus = trap / "completion_cache.json"
    bogus.mkdir()
    now = time.time()
    os.utime(bogus, (now - OLD, now - OLD))
    other = _mk_workspace(tmp_path, "99999999-other", cache_age=OLD)

    report = prune(tmp_path)

    assert bogus.is_dir()  # untouched
    assert bogus not in report.files_removed
    assert other / "completion_cache.json" in report.files_removed  # walk continued


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory modes; this guard needs an unwritable directory",
)
def test_prune_survives_a_permission_denied_workspace(tmp_path):
    """A workspace directory this process can't write into: `unlink` raises
    `PermissionError`. The walk must still finish and touch the other
    workspace, and the locked file must never be reported as removed."""
    from otto.config.cache_maintenance import prune

    locked = _mk_workspace(tmp_path, "aaaaaaab-locked", cache_age=OLD)
    other = _mk_workspace(tmp_path, "aaaaaaac-open", cache_age=OLD)
    locked.chmod(0o500)
    try:
        report = prune(tmp_path)
    finally:
        locked.chmod(0o700)  # so tmp_path cleanup can remove it

    assert locked / "completion_cache.json" not in report.files_removed
    assert (locked / "completion_cache.json").is_file()
    assert other / "completion_cache.json" in report.files_removed


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory modes; this guard needs an unreadable directory",
)
def test_iter_workspaces_survives_a_permission_denied_workspace(tmp_path):
    """One unreadable workspace directory must not crash the whole listing —
    the read-only side of a maintenance pass must still show every other
    workspace fine."""
    from otto.config.cache_maintenance import iter_workspaces

    locked = _mk_workspace(tmp_path, "aaaaaaad-locked", cache_age=OLD)
    healthy = _mk_workspace(tmp_path, "aaaaaaae-open", cache_age=YOUNG)
    locked.chmod(0o000)
    try:
        infos = iter_workspaces(tmp_path)
    finally:
        locked.chmod(0o700)  # so tmp_path cleanup can remove it

    assert [i.key for i in infos] == [healthy.name]


def test_clear_workspace_survives_a_directory_named_like_a_cache_file(tmp_path):
    """Same defect, same fix, on the single-directory path: a directory sitting
    where a cache file's name is expected must not abort clearing the other
    cache file in the same workspace."""
    from otto.config.cache_maintenance import clear_workspace

    ws = _mk_workspace(tmp_path, "bbbbbbba-cleartrap", cache_age=None, sidecar=True)
    bogus = ws / "completion_cache.json"
    bogus.mkdir()

    report = clear_workspace(ws)

    assert bogus.is_dir()  # untouched
    assert bogus not in report.files_removed
    assert ws / "remote_completion_cache.json" in report.files_removed


def test_prune_mixed_age_dir_lands_only_in_retained_young(tmp_path):
    """`retained_young` and `retained_nonempty` are DISJOINT: a dir with an
    old primary (removed) and a young sidecar (kept) is non-empty for age
    reasons alone, and must land in exactly one bucket."""
    from otto.config.cache_maintenance import prune

    ws = _mk_workspace(tmp_path, "cccccccb-mixed", cache_age=OLD, sidecar=True, sidecar_age=YOUNG)
    report = prune(tmp_path)
    assert not (ws / "completion_cache.json").exists()
    assert (ws / "remote_completion_cache.json").is_file()
    assert ws in report.retained_young
    assert ws not in report.retained_nonempty


def test_prune_mixed_age_dir_lands_only_in_retained_young_dry_run(tmp_path):
    """The `dry_run=True` twin of `test_prune_mixed_age_dir_lands_only_in_retained_young`.

    [Reviewer-supplied gap] The dry-run branch computes `retained_nonempty`
    membership with its OWN `elif not young_here:` guard (a second copy of
    the real path's disjointness check, needed because dry-run never calls
    `rmdir` to let its `OSError` tell the real path apart). Mutating that
    `elif` to `else` left all 25 pre-existing tests in this module green --
    none of them exercised a workspace that is simultaneously young (an
    unremoved file survives) AND would-not-be-emptied under dry-run. This
    old-primary/young-sidecar workspace is exactly that case: dry-run must
    report it in `retained_young` only, never also in `retained_nonempty`.
    """
    from otto.config.cache_maintenance import prune

    ws = _mk_workspace(tmp_path, "cccccccc-mixedry", cache_age=OLD, sidecar=True, sidecar_age=YOUNG)
    report = prune(tmp_path, dry_run=True)
    assert ws / "completion_cache.json" in report.files_removed  # would remove the stale primary
    assert (ws / "remote_completion_cache.json").is_file()  # sidecar untouched -- dry-run
    assert (ws / "completion_cache.json").is_file()  # nothing actually removed under dry-run
    assert ws in report.retained_young
    assert ws not in report.retained_nonempty
    assert ws not in report.dirs_removed


def test_prune_untouched_env_only_dir_lands_in_neither_bucket(tmp_path):
    """A workspace holding only an `env/` dir and no cache files ever has
    nothing for `prune` to act on: no `rmdir` attempt, and it lands in
    NEITHER retained list — not removed, not retained, just untouched."""
    from otto.config.cache_maintenance import prune

    ws = _mk_workspace(tmp_path, "ddddddda-envonly", cache_age=None, env=True)
    report = prune(tmp_path)
    assert (ws / "env" / "bin" / "python").exists()
    assert ws.exists()
    assert ws not in report.retained_young
    assert ws not in report.retained_nonempty
    assert ws not in report.dirs_removed
