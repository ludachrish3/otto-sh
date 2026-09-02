"""``otto cache`` — the Typer surface over ``otto.config.cache_maintenance``.

Driven directly with typer's ``CliRunner`` against ``cache_app`` (the shape
``tests/unit/cli/test_schema_cli.py`` and ``test_inventory_cli.py`` use for
the other settings-only, lab-free groups) rather than the root ``app``: every
verb here is a plain ``def``, so there is no lifecycle-bridge/dispatch
machinery to exercise, and going through the sub-app directly keeps
``OTTO_HOME`` the only thing under test.

``_mk_workspace``/``OLD``/``YOUNG`` are Task 1's fixture helper, promoted to
``tests/_fixtures/cache_workspace.py`` (fix round 1) since it is now shared
across two DIFFERENT test directories (``tests/unit/config`` and
``tests/unit/cli``) — the repo convention for that is ``tests/_fixtures/``,
not one test module importing a private helper out of another.

Every presentation assertion below is anchored on the phrase/character the
guard exists for (never a bare word a locals dump or the fixture's own key
could satisfy) precisely so it can be mutation-proven — see the fix-round-1
report for the six-mutation sweep these assertions were built to catch.
"""

from typer.testing import CliRunner

from tests._fixtures.cache_workspace import OLD, YOUNG, _mk_workspace

runner = CliRunner()


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    return home


def test_clear_bare_clears_only_current_workspace(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app
    from otto.config.home import workspace_key

    home = _home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    current = _mk_workspace(home, workspace_key([repo]), cache_age=YOUNG, sidecar=True)
    other = _mk_workspace(home, "99999999-other", cache_age=YOUNG)

    result = runner.invoke(cache_app, ["clear"])

    assert result.exit_code == 0
    assert not (current / "completion_cache.json").exists()
    assert not (current / "remote_completion_cache.json").exists()
    assert (other / "completion_cache.json").is_file()
    assert current.exists()  # bare clear never removes the dir


def test_clear_bare_reports_nothing_to_remove_when_empty(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    from otto.cli.cache import cache_app

    result = runner.invoke(cache_app, ["clear"])

    assert result.exit_code == 0
    assert "nothing to remove" in result.output


def test_clear_all_clears_every_workspace_and_rmdirs_empty(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    home = _home(monkeypatch, tmp_path)
    bare = _mk_workspace(home, "aaaaaaaa-bare", cache_age=YOUNG)
    withenv = _mk_workspace(home, "bbbbbbbb-env", cache_age=YOUNG, env=True)

    result = runner.invoke(cache_app, ["clear", "--all"])

    assert result.exit_code == 0
    assert not bare.exists()  # emptied -> rmdir'd
    assert withenv.exists()  # env/ keeps the dir
    assert not (withenv / "completion_cache.json").exists()
    # `--all` runs the full prune engine (age_blind) underneath; a removed
    # directory must be SAID, not just done — this is the exact case
    # (two dirs, one rmdir'd) fix round 1 caught with zero signal in the output.
    assert "1 dir(s) removed" in result.output
    # [Fix wave 3] withenv survived a failed rmdir (env/, not age -- `--all` is
    # age-blind) -- the why-split must say "non-empty", never "young", for it.
    assert "1 workspace(s) kept: 1 non-empty" in result.output
    assert "young" not in result.output


def test_info_home_line_annotates_from_otto_home_when_env_pins_it(tmp_path, monkeypatch):
    """[Fix wave 3] `info`'s home line must say whether $OTTO_HOME determined
    the resolved path -- pinned case. Anchored on the exact annotated line
    (not just the substring "from $OTTO_HOME" anywhere in output), so a
    mutation that prints the right words on the wrong line still fails this."""
    from otto.cli.cache import cache_app

    home = _home(monkeypatch, tmp_path)  # sets OTTO_HOME explicitly

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0
    assert f"home: {home} (from $OTTO_HOME)" in result.output
    assert "(default)" not in result.output


def test_info_home_line_annotates_default_when_env_is_unset(monkeypatch):
    """[Fix wave 3] Unpinned case: no $OTTO_HOME at all -- must read "default",
    never "from $OTTO_HOME"."""
    from otto.cli.cache import cache_app

    monkeypatch.delenv("OTTO_HOME", raising=False)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0
    assert "(default)" in result.output
    assert "(from $OTTO_HOME)" not in result.output


def test_info_home_line_treats_empty_otto_home_as_unset(monkeypatch):
    """`OttoEnvSettings` (`env_ignore_empty=True`) treats `OTTO_HOME=""` as
    unset, same as `otto_home()` itself does -- this is the nuance a naive
    "is the var present in os.environ" check would get wrong. Pinning it to
    the empty string, rather than deleting it, is what makes this test
    different from the unset case above."""
    from otto.cli.cache import cache_app

    monkeypatch.setenv("OTTO_HOME", "")

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0
    assert "(default)" in result.output
    assert "(from $OTTO_HOME)" not in result.output


def test_info_lists_workspaces_oldest_first(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    home = _home(monkeypatch, tmp_path)
    _mk_workspace(home, "11111111-young", cache_age=YOUNG)
    _mk_workspace(home, "22222222-old", cache_age=OLD, env=True)
    # [Fix round 2, controller ruling B1] A mixed-age workspace: its OLDEST
    # file (70d, older than "old"'s 61d) must sort it FIRST, even though its
    # FRESHEST file (1d, the sidecar) is the youngest of the three — proving
    # the sort key (oldest_cache_mtime) and the rendered age column are the
    # SAME number, not two fields that happen to usually agree.
    _mk_workspace(home, "33333333-mixed", cache_age=70 * 86400, sidecar=True, sidecar_age=1 * 86400)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0
    assert str(home) in result.output
    assert (
        result.output.index("33333333-mixed")
        < result.output.index("22222222-old")
        < result.output.index("11111111-young")
    )

    # The rounded table actually rendered (a real rich.table.Table with
    # box=box.ROUNDED — not just some rows of plain text): assert on a
    # box-drawing character unique to that box style, never on Table
    # internals.
    assert "╭" in result.output

    # env marker appears on the env-bearing row only — assert on the
    # rendered CELL (its own line), not just "yes" appearing anywhere in
    # the screen.
    lines = result.output.splitlines()
    mixed_line = next(line for line in lines if "33333333-mixed" in line)
    old_line = next(line for line in lines if "22222222-old" in line)
    young_line = next(line for line in lines if "11111111-young" in line)
    assert "yes" in old_line
    assert "yes" not in mixed_line
    assert "yes" not in young_line

    # The rendered age is each row's OLDEST file, and the three rows read
    # monotonically top-to-bottom (70d, 61d, 59d) — the literal "renders
    # oldest first" claim, not just a row-order claim divorced from what the
    # age column actually says.
    assert "70d" in mixed_line
    assert "61d" in old_line
    assert "59d" in young_line

    # Totals + over-threshold caption: mixed (70d) and old (61d) are past the
    # 60d default cutoff, young (59d) is not.
    assert "3 workspace(s)" in result.output
    assert "2 older than 60" in result.output

    empty = runner.invoke(cache_app, ["info"], env={"OTTO_HOME": str(tmp_path / "ghost")})
    assert empty.exit_code == 0  # empty/missing home is not an error


def test_info_mixed_age_workspace_counts_over_threshold_by_oldest_file(tmp_path, monkeypatch):
    """[Controller ruling, fix round 1] `info` must read the OLDEST cache file's
    age, not the newest — `prune` decides per file, so a workspace with a
    61-day-old primary and a 1-day-old sidecar is still something a prune
    would touch, even though its freshest file looks young."""
    from otto.cli.cache import cache_app

    home = _home(monkeypatch, tmp_path)
    _mk_workspace(home, "ffffffff-mixed", cache_age=OLD, sidecar=True, sidecar_age=1 * 86400)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0
    assert "1 older than 60" in result.output
    line = next(line for line in result.output.splitlines() if "ffffffff-mixed" in line)
    assert "61d" in line  # the OLDEST file's age — not "1d", the sidecar's


def test_prune_default_60d_and_flags(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    home = _home(monkeypatch, tmp_path)
    old = _mk_workspace(home, "cccccccc-old", cache_age=OLD)
    young = _mk_workspace(home, "dddddddd-young", cache_age=2 * 86400)

    result = runner.invoke(cache_app, ["prune"])
    assert result.exit_code == 0
    assert not old.exists()
    assert (young / "completion_cache.json").is_file()
    # young's cache file is well under the 60d cutoff -> retained_young ->
    # the kept count must say so, and say WHY (young, not non-empty).
    assert "1 workspace(s) kept: 1 young" in result.output

    dry = runner.invoke(cache_app, ["prune", "--age", "1", "--dry-run"])
    assert dry.exit_code == 0
    assert (young / "completion_cache.json").is_file()  # dry-run removed nothing
    assert "dddddddd-young" in dry.output  # but reported it
    assert "would remove" in dry.output  # tensed as hypothetical, not done
    # [Fix round 2] EVERY clause in the summary line must be tensed under
    # --dry-run, not just the per-file verb: young's directory would also
    # empty out (its only cache file goes), so both the bytes clause and the
    # dir clause must read as hypothetical too.
    assert "would free" in dry.output
    assert "would be removed" in dry.output

    result = runner.invoke(cache_app, ["prune", "--age", "1"])
    assert result.exit_code == 0
    assert not young.exists()
    # Real run: the same two clauses, un-tensed, and no "would" anywhere —
    # the mirror image of the dry-run assertions above.
    assert "freed" in result.output
    assert "would" not in result.output


def test_prune_kept_summary_shows_why_split_in_dry_run_and_real_mode(tmp_path, monkeypatch):
    """[Fix wave 3] The merged "N workspace(s) kept" count must split its why:
    young (age-protected) vs non-empty (env/ survived a failed rmdir).
    Covers both `--dry-run` and the real prune -- the summary line is shared
    code (`_print_prune_report`), and the split must read identically in
    both: "kept" is stative, not an action verb, so only the removal clauses
    (already covered above) tense under `--dry-run`, never this one.

    Deliberately ASYMMETRIC counts (2 young, 1 non-empty): a mutation that
    swaps the two bucket sources (`retained_young`/`retained_nonempty`) is
    invisible at 1-and-1 -- the rendered text would read the same either way
    -- so this test carries its own mutation-catching power independent of
    the other single-cause assertions in this file."""
    from otto.cli.cache import cache_app

    home = _home(monkeypatch, tmp_path)
    _mk_workspace(home, "11111111-young1", cache_age=YOUNG)
    _mk_workspace(home, "44444444-young2", cache_age=YOUNG)
    _mk_workspace(home, "22222222-envold", cache_age=OLD, env=True)
    _mk_workspace(home, "33333333-old", cache_age=OLD)

    dry = runner.invoke(cache_app, ["prune", "--dry-run"])
    assert dry.exit_code == 0
    assert "3 workspace(s) kept: 2 young, 1 non-empty" in dry.output

    result = runner.invoke(cache_app, ["prune"])
    assert result.exit_code == 0
    assert "3 workspace(s) kept: 2 young, 1 non-empty" in result.output
