"""Pins for scripts/release_bump.py — git-cliff's census decides the bump.

Otto is pre-1.0 (Cargo/npm convention): a breaking (`!`) commit bumps MINOR
while major is 0, everything else bumps PATCH. `make release` used to default
to `BUMP=patch` unconditionally, so a patch release could ship an unmarked
breaking change. git-cliff already computes the census
(`--bumped-version`, driven by `cliff.toml`'s `[bump]` section); this module
is the glue that lets a human's `BUMP=`/`NEW_VERSION=` RAISE that census but
never silently lower it.

Most of these tests inject the census/computed values directly into the pure
`decide_bump` function — they never shell out to git-cliff or
bump-my-version. Two tests (`test_real_git_cliff_bump_config_...`) drive the
real `git-cliff` binary against a synthetic repo, one per `[bump]` knob:
`breaking_always_bump_major = false` (a `!` commit resolves to minor, not
major, on a 0.x tag) and `features_always_bump_minor = false` (a `feat:`
commit resolves to patch, not minor) — that config is the one thing a pure
Python test cannot pin, and a `fix:` commit would resolve to patch under
git-cliff's unconfigured defaults too, so it would never have gone red for a
missing `features_always_bump_minor` line.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.release_bump import (
    BumpDecision,
    bump_floor,
    census_guard,
    decide_bump,
    git_last_tag,
    is_breaking_commit,
    main,
    offending_commits,
)
from tests._fixtures.gitrepo import TmpGitRepo
from tests._fixtures.paths import PROJECT_ROOT

CLIFF = PROJECT_ROOT / "cliff.toml"


# ---------------------------------------------------------------------------
# is_breaking_commit / offending_commits — the classifier the refusal message
# names commits with.
# ---------------------------------------------------------------------------


def test_bang_after_type_is_breaking() -> None:
    assert is_breaking_commit("feat!: drop the old flag", "")


def test_bang_after_scope_is_breaking() -> None:
    assert is_breaking_commit("fix(cli)!: rename a flag", "")


def test_breaking_change_footer_is_breaking() -> None:
    assert is_breaking_commit("feat(cov): add a thing", "BREAKING CHANGE: store.json v5 -> v6.")


def test_breaking_change_footer_hyphen_spelling_is_breaking() -> None:
    """Conventional Commits declares BREAKING-CHANGE: a synonym for BREAKING CHANGE:."""
    assert is_breaking_commit("feat(cov): add a thing", "BREAKING-CHANGE: store.json v5 -> v6.")


def test_ordinary_commit_is_not_breaking() -> None:
    assert not is_breaking_commit("fix(cli): mend a thing", "")


def test_offending_commits_selects_only_the_breaking_ones() -> None:
    commits = [
        ("aaa1111", "feat!: drop the old flag", ""),
        ("bbb2222", "fix(cli): mend a thing", ""),
        ("ccc3333", "feat(cov): add a thing", "BREAKING CHANGE: store.json v5 -> v6."),
    ]
    assert offending_commits(commits) == [
        "aaa1111 feat!: drop the old flag",
        "ccc3333 feat(cov): add a thing",
    ]


# ---------------------------------------------------------------------------
# bump_floor — which BUMP= value the census corresponds to, for the refusal
# message's "BUMP=minor is the floor" line.
# ---------------------------------------------------------------------------


def test_bump_floor_names_minor_for_a_minor_census() -> None:
    assert bump_floor("0.10.0", "0.11.0") == "minor"


def test_bump_floor_names_patch_for_a_patch_census() -> None:
    assert bump_floor("0.10.0", "0.10.1") == "patch"


def test_bump_floor_names_major_for_a_major_census() -> None:
    assert bump_floor("0.10.0", "1.0.0") == "major"


# ---------------------------------------------------------------------------
# census_guard — refuses a census that git-cliff has no actual news for:
# nothing releasable (empty string) or stuck at (not past) the last tag.
# Without this, the no-override path would silently re-tag an existing
# version and the Makefile would proceed to `git-cliff --tag`/`bump-my-version`
# with it.
# ---------------------------------------------------------------------------


def test_census_guard_is_none_when_the_census_advances() -> None:
    assert census_guard("0.10.0", "0.11.0") is None


def test_census_guard_refuses_a_census_equal_to_the_last_tag() -> None:
    message = census_guard("0.10.0", "0.10.0")
    assert message is not None
    assert "0.10.0" in message
    assert "REFUSED" in message


def test_census_guard_refuses_an_empty_census() -> None:
    message = census_guard("0.10.0", "")
    assert message is not None
    assert "REFUSED" in message
    assert "nothing releasable" in message


def test_census_guard_refuses_an_unavailable_census_naming_the_error() -> None:
    """census=None (git-cliff itself failed) gets its own message, distinct

    from the empty-census ("printed nothing") and stuck-census ("did not
    advance") cases -- collapsing all three via `census or ""` previously
    made an outright subprocess failure print "printed nothing", which is
    wrong: git-cliff printed an error, not nothing.
    """
    message = census_guard("0.10.0", None, census_error="Semver error: unexpected character 'v'")
    assert message is not None
    assert "REFUSED" in message
    assert "could not compute a census" in message
    assert "Semver error: unexpected character 'v'" in message
    assert "printed nothing" not in message
    assert "stuck" not in message


def test_census_guard_unavailable_census_without_a_detail_still_names_it() -> None:
    message = census_guard("0.10.0", None)
    assert message is not None
    assert "REFUSED" in message
    assert "could not compute a census" in message


# ---------------------------------------------------------------------------
# decide_bump — the pure decision function. Census strings are injected;
# nothing here shells out.
# ---------------------------------------------------------------------------


def test_decide_bump_refuses_when_the_census_does_not_advance_past_last_tag() -> None:
    decision = decide_bump(last_tag="0.10.0", census="0.10.0")
    assert decision.decision == "refused"
    assert "0.10.0" in decision.message


def test_decide_bump_refuses_an_empty_census() -> None:
    decision = decide_bump(last_tag="0.10.0", census="")
    assert decision.decision == "refused"
    assert "nothing releasable" in decision.message


def test_decide_bump_refuses_an_unavailable_census_even_with_bump_set() -> None:
    """census=None (git-cliff itself failed) must refuse cleanly, not crash.

    Unlike NEW_VERSION=, BUMP= (and no override) have nothing for a human's
    override to compare against, so a census failure is a refusal here, not
    a warning -- and the message must name git-cliff's own failure reason
    rather than the "printed nothing"/"stuck" wording those describe.
    """
    decision = decide_bump(
        last_tag="0.10.0",
        census=None,
        census_error="Semver error: unexpected character 'v'",
        bump="patch",
        bump_computed="0.10.1",
    )
    assert decision.decision == "refused"
    assert "could not compute a census" in decision.message
    assert "Semver error: unexpected character 'v'" in decision.message
    assert decision.message.count("REFUSED") == 1


def test_no_bump_no_new_version_uses_the_census() -> None:
    decision = decide_bump(last_tag="0.10.0", census="0.11.0")
    assert decision == BumpDecision("census", "0.11.0", decision.message)
    assert "0.11.0" in decision.message


def test_bump_lower_than_census_is_refused_and_names_the_breaking_commit() -> None:
    decision = decide_bump(
        last_tag="0.10.0",
        census="0.11.0",
        bump="patch",
        bump_computed="0.10.1",
        offending=["abc1234 feat!: drop the old flag"],
    )
    assert decision.decision == "refused"
    assert decision.version == "0.11.0"
    assert "abc1234 feat!: drop the old flag" in decision.message
    assert "BUMP=minor" in decision.message


def test_bump_higher_than_census_is_honoured() -> None:
    decision = decide_bump(
        last_tag="0.10.0",
        census="0.11.0",
        bump="major",
        bump_computed="1.0.0",
    )
    assert decision == BumpDecision("honoured", "1.0.0", decision.message)


def test_bump_equal_to_census_is_honoured_not_refused() -> None:
    decision = decide_bump(
        last_tag="0.10.0",
        census="0.11.0",
        bump="minor",
        bump_computed="0.11.0",
    )
    assert decision.decision == "honoured"
    assert decision.version == "0.11.0"


def test_new_version_lower_than_census_is_a_warning_not_a_refusal() -> None:
    decision = decide_bump(last_tag="0.10.0", census="0.11.0", new_version="0.10.1")
    assert decision.decision == "prerelease-warning"
    assert decision.version == "0.10.1"
    assert "WARNING" in decision.message
    assert "0.10.1" in decision.message
    assert "0.11.0" in decision.message


def test_new_version_at_or_above_census_is_honoured() -> None:
    decision = decide_bump(last_tag="0.10.0", census="0.11.0", new_version="0.12.0")
    assert decision.decision == "honoured"
    assert decision.version == "0.12.0"


def test_new_version_honoured_with_warning_when_census_is_unavailable() -> None:
    """git-cliff failing outright (e.g. a `vX.Y.ZrcN` tag it cannot parse as semver)

    must not turn the documented NEW_VERSION= escape hatch into a refusal --
    ``main()`` passes census=None + census_error in this case (see the
    try/except around ``git_cliff_bumped_version``). The message must name
    the real reason (unavailable), not "printed nothing" (empty) or "stuck"
    (a real-but-equal census) -- those are different conditions with
    different causes.
    """
    decision = decide_bump(
        last_tag="0.10.0",
        census=None,
        census_error="Semver error: unexpected character 'v'",
        new_version="0.12.0",
    )
    assert decision.decision == "honoured"
    assert decision.version == "0.12.0"
    assert "WARNING" in decision.message
    assert "could not compute a census" in decision.message
    assert "Semver error: unexpected character 'v'" in decision.message
    assert "printed nothing" not in decision.message
    assert "stuck" not in decision.message
    assert "REFUSED" not in decision.message


def test_new_version_honoured_with_warning_when_census_is_stuck_at_last_tag() -> None:
    """rc1 -> final promotion: at the rc tag with no further commits, git-cliff

    echoes the rc tag back as the census, equal to last_tag -- census_guard
    would refuse that for the census/BUMP= branches, but NEW_VERSION= must
    still be honoured. The message must say "stuck", not "unavailable" --
    this census is real, just not advanced.
    """
    decision = decide_bump(last_tag="0.12.0rc1", census="0.12.0rc1", new_version="0.12.0")
    assert decision.decision == "honoured"
    assert decision.version == "0.12.0"
    assert "WARNING" in decision.message
    assert "stuck" in decision.message
    assert "could not compute a census" not in decision.message
    assert "REFUSED" not in decision.message


def test_new_version_honoured_with_warning_when_census_is_empty() -> None:
    """git-cliff ran fine and found nothing releasable (census="") -- distinct

    from both "unavailable" (subprocess failed) and "stuck" (a real,
    non-advancing census).
    """
    decision = decide_bump(last_tag="0.10.0", census="", new_version="0.10.1")
    assert decision.decision == "honoured"
    assert "WARNING" in decision.message
    assert "printed nothing" in decision.message
    assert "could not compute a census" not in decision.message
    assert "REFUSED" not in decision.message


def test_new_version_takes_priority_over_bump() -> None:
    """NEW_VERSION is the prerelease escape hatch; a stray BUMP= must not fight it."""
    decision = decide_bump(
        last_tag="0.10.0",
        census="0.11.0",
        bump="patch",
        bump_computed="0.10.1",
        new_version="0.12.0rc1",
    )
    assert decision.decision == "honoured"
    assert decision.version == "0.12.0rc1"


# ---------------------------------------------------------------------------
# main() — the CLI entry. Subprocess calls are monkeypatched out except in
# the one real-git-cliff test below.
# ---------------------------------------------------------------------------


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    census: str,
    last_tag: str = "0.10.0",
    bump_computed: "str | None" = None,
    log_commits=(),
) -> None:
    monkeypatch.setattr("scripts.release_bump.git_last_tag", lambda cwd=None: f"v{last_tag}")
    monkeypatch.setattr(
        "scripts.release_bump.git_cliff_bumped_version", lambda cwd=None: f"v{census}"
    )
    monkeypatch.setattr(
        "scripts.release_bump.git_log_commits", lambda last_tag, cwd=None: list(log_commits)
    )
    if bump_computed is not None:
        monkeypatch.setattr(
            "scripts.release_bump.bump_my_version_new_version", lambda bump, cwd=None: bump_computed
        )


def test_main_no_bump_prints_the_census_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_subprocess(monkeypatch, census="0.11.0")
    monkeypatch.delenv("BUMP", raising=False)
    monkeypatch.delenv("NEW_VERSION", raising=False)
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "0.11.0"


def test_main_refuses_a_lower_bump_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_subprocess(
        monkeypatch,
        census="0.11.0",
        bump_computed="0.10.1",
        log_commits=[("abc1234", "feat!: drop the old flag", "")],
    )
    monkeypatch.setenv("BUMP", "patch")
    monkeypatch.delenv("NEW_VERSION", raising=False)
    rc = main([])
    out = capsys.readouterr()
    assert rc == 1
    assert out.out.strip() == ""
    assert "abc1234 feat!: drop the old flag" in out.err
    assert "BUMP=minor" in out.err


def test_main_honours_a_higher_bump_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_subprocess(monkeypatch, census="0.11.0", bump_computed="1.0.0")
    monkeypatch.setenv("BUMP", "major")
    monkeypatch.delenv("NEW_VERSION", raising=False)
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "1.0.0"


def test_main_refuses_when_the_census_does_not_advance_past_last_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_subprocess(monkeypatch, census="0.10.0", last_tag="0.10.0")
    monkeypatch.delenv("BUMP", raising=False)
    monkeypatch.delenv("NEW_VERSION", raising=False)
    rc = main([])
    out = capsys.readouterr()
    assert rc == 1
    assert out.out.strip() == ""
    assert "0.10.0" in out.err


def test_main_refuses_cleanly_when_git_cliff_itself_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """git-cliff exiting non-zero (e.g. it cannot parse a `vX.Y.ZrcN` tag as

    semver) must be caught and turned into a clean refusal -- rc 1, nothing
    on stdout, the real failure named on stderr -- not an uncaught
    CalledProcessError traceback. No override is set, so this is the
    census/BUMP= branches' behaviour (NEW_VERSION= is covered by the real
    git-cliff test below).
    """
    monkeypatch.setattr("scripts.release_bump.git_last_tag", lambda cwd=None: "v0.10.0")

    def _boom(cwd=None):
        raise subprocess.CalledProcessError(
            1, ["git-cliff", "--bumped-version"], output="", stderr="Semver error: bad tag"
        )

    monkeypatch.setattr("scripts.release_bump.git_cliff_bumped_version", _boom)
    monkeypatch.delenv("BUMP", raising=False)
    monkeypatch.delenv("NEW_VERSION", raising=False)
    rc = main([])
    out = capsys.readouterr()
    assert rc == 1
    assert out.out.strip() == ""
    assert "could not compute a census" in out.err
    assert "Semver error: bad tag" in out.err


def test_main_warns_on_a_lower_new_version_but_still_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_subprocess(monkeypatch, census="0.11.0")
    monkeypatch.delenv("BUMP", raising=False)
    monkeypatch.setenv("NEW_VERSION", "0.10.1")
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "0.10.1"
    assert "WARNING" in out.err


# ---------------------------------------------------------------------------
# The two tests that drive the real git-cliff binary, one per [bump] knob,
# to prove cliff.toml's [bump] section actually changes the resolved version
# on a 0.x tag rather than just matching git-cliff's unconfigured defaults.
# ---------------------------------------------------------------------------


def test_real_git_cliff_bump_config_yields_minor_for_a_breaking_commit(tmp_path_factory) -> None:
    assert CLIFF.is_file(), f"cliff.toml not found at {CLIFF}"
    cliff = shutil.which("git-cliff")
    assert cliff, (
        "git-cliff is a declared dev dependency (pyproject [dependency-groups] dev) "
        "and this test drives the real renderer — run `uv sync` rather than skipping"
    )
    repo = TmpGitRepo(tmp_path_factory.mktemp("bumprepo"), dates="2026-01-01T00:00:00Z")
    repo.write("seed", "x")
    repo.commit("feat: the released past")
    repo.git("tag", "v0.10.0")
    repo.write("f1", "x")
    repo.commit("feat(cli)!: drop the old flag")

    proc = subprocess.run(
        [cliff, "--config", str(CLIFF), "--offline", "--bumped-version"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"git-cliff exited {proc.returncode}\n--- stderr ---\n{proc.stderr}"
    )
    assert proc.stdout.strip() == "v0.11.0"


def test_real_git_cliff_bump_config_yields_patch_for_a_feature_commit(tmp_path_factory) -> None:
    """Pins ``features_always_bump_minor = false``.

    A `fix:` commit resolves to patch under git-cliff's UNCONFIGURED
    defaults too, so it would never go red if that config line were deleted
    -- a `feat:` commit is the one that actually depends on it: with the
    knob it stays patch, without it git-cliff would compute a minor bump.
    """
    cliff = shutil.which("git-cliff")
    assert cliff, "git-cliff is a declared dev dependency (pyproject [dependency-groups] dev)"
    repo = TmpGitRepo(tmp_path_factory.mktemp("bumprepo2"), dates="2026-01-01T00:00:00Z")
    repo.write("seed", "x")
    repo.commit("feat: the released past")
    repo.git("tag", "v0.10.0")
    repo.write("f1", "x")
    repo.commit("feat(cli): add a thing")

    proc = subprocess.run(
        [cliff, "--config", str(CLIFF), "--offline", "--bumped-version"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"git-cliff exited {proc.returncode}\n--- stderr ---\n{proc.stderr}"
    )
    assert proc.stdout.strip() == "v0.10.1"


# ---------------------------------------------------------------------------
# The documented prerelease path, end to end against real git-cliff and real
# git: NEW_VERSION= must survive git-cliff's census failing outright at a
# `vX.Y.ZrcN` tag it cannot parse as semver.
# ---------------------------------------------------------------------------


def test_real_git_cliff_new_version_survives_a_census_that_cannot_parse_the_rc_tag(
    tmp_path_factory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    cliff = shutil.which("git-cliff")
    assert cliff, "git-cliff is a declared dev dependency (pyproject [dependency-groups] dev)"
    repo = TmpGitRepo(tmp_path_factory.mktemp("rcrepo"), dates="2026-01-01T00:00:00Z")
    repo.write("seed", "x")
    repo.commit("feat: the released past")
    repo.git("tag", "v0.10.0")
    repo.write("f1", "x")
    repo.commit("feat(cli)!: drop the old flag")
    repo.git("tag", "v0.12.0rc1")
    repo.write("f2", "x")
    repo.commit("fix: a follow-up after the rc tag")

    # Confirm the premise for real, unmediated by this script: git-cliff
    # cannot compute a census here because git describe resolves to the rc
    # tag and git-cliff's semver parser rejects the leading "v".
    probe = subprocess.run(
        [cliff, "--bumped-version"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0, "premise broken: git-cliff unexpectedly computed a census"

    monkeypatch.setenv("PATH", str(Path(cliff).parent) + ":/usr/bin:/bin")
    monkeypatch.setenv("NEW_VERSION", "0.12.0")
    monkeypatch.delenv("BUMP", raising=False)
    rc = main(["--cwd", str(repo.root)])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "0.12.0"
    assert "WARNING" in out.err


def test_git_last_tag_ignores_a_non_version_tag_via_match(tmp_path_factory) -> None:
    """A non-version tag (e.g. an archive marker) newer than the last release

    must not be picked -- ``--match 'v[0-9]*'`` mirrors cliff.toml's own
    ``tag_pattern``.
    """
    repo = TmpGitRepo(tmp_path_factory.mktemp("matchrepo"), dates="2026-01-01T00:00:00Z")
    repo.write("seed", "x")
    repo.commit("feat: the released past")
    repo.git("tag", "v0.10.0")
    repo.write("f1", "x")
    repo.commit("chore: archive some plans")
    repo.git("tag", "plans-archive-2026-08-03")

    assert git_last_tag(cwd=repo.root) == "v0.10.0"
