"""Tests for scripts/docs_build_stamp.py.

The stamp decides whether a docs build sits exactly on a release tag. Getting it
wrong is asymmetric: a missing banner on dev docs is a small loss, but a "this is
development documentation" banner on the *released* docs actively misinforms. So
the no-signal fallback is asserted here as explicitly as the happy paths.

Git-backed cases build a throwaway repo under tmp_path — never the dev checkout.
"""

import importlib.util

from tests._fixtures.gitrepo import TmpGitRepo
from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "docs_build_stamp.py"


def _load_stamp_module():
    spec = importlib.util.spec_from_file_location("docs_build_stamp", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_repo(tmp_path, *, tag=None, extra_commits=0, dirty=False):
    """A minimal git repo, optionally tagged and/or advanced past the tag."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("f.txt", "one\n")
    repo.commit("one")
    if tag is not None:
        repo.git("tag", tag)
    for n in range(extra_commits):
        repo.write("f.txt", f"more {n}\n")
        repo.commit(f"more {n}")
    if dirty:
        repo.write("f.txt", "uncommitted\n")
    return repo.root


# -- Tier 1: Read the Docs environment ----------------------------------------


def test_rtd_tag_build_is_a_release(tmp_path):
    mod = _load_stamp_module()
    stamp = mod.build_stamp(
        env={
            "READTHEDOCS_VERSION_TYPE": "tag",
            "READTHEDOCS_VERSION": "v0.9.0",
            "READTHEDOCS_GIT_COMMIT_HASH": "ded87ca6deadbeef",
        },
        repo=_make_repo(tmp_path),
    )
    assert stamp.is_release is True
    assert stamp.base_tag == "v0.9.0"


def test_rtd_branch_build_is_not_a_release(tmp_path):
    mod = _load_stamp_module()
    stamp = mod.build_stamp(
        env={
            "READTHEDOCS_VERSION_TYPE": "branch",
            "READTHEDOCS_VERSION": "latest",
            "READTHEDOCS_GIT_COMMIT_HASH": "ded87ca6deadbeef",
        },
        repo=_make_repo(tmp_path),
    )
    assert stamp.is_release is False
    assert stamp.commit == "ded87ca6"


def test_rtd_external_build_is_not_a_release(tmp_path):
    """Pull-request previews must never claim to be released docs."""
    mod = _load_stamp_module()
    stamp = mod.build_stamp(
        env={"READTHEDOCS_VERSION_TYPE": "external", "READTHEDOCS_VERSION": "271"},
        repo=_make_repo(tmp_path),
    )
    assert stamp.is_release is False


def test_rtd_env_wins_over_git(tmp_path):
    """A shallow RTD clone can sit on a tag by accident; the env var is authoritative."""
    mod = _load_stamp_module()
    stamp = mod.build_stamp(
        env={"READTHEDOCS_VERSION_TYPE": "branch", "READTHEDOCS_VERSION": "latest"},
        repo=_make_repo(tmp_path, tag="v0.9.0"),
    )
    assert stamp.is_release is False


# -- Tier 2: git describe ------------------------------------------------------


def test_git_exactly_on_tag_is_a_release(tmp_path):
    mod = _load_stamp_module()
    stamp = mod.build_stamp(env={}, repo=_make_repo(tmp_path, tag="v0.9.0"))
    assert stamp.is_release is True
    assert stamp.base_tag == "v0.9.0"
    assert stamp.commits_since == 0


def test_git_past_tag_reports_distance(tmp_path):
    mod = _load_stamp_module()
    stamp = mod.build_stamp(env={}, repo=_make_repo(tmp_path, tag="v0.9.0", extra_commits=3))
    assert stamp.is_release is False
    assert stamp.base_tag == "v0.9.0"
    assert stamp.commits_since == 3
    assert stamp.commit


def test_git_dirty_on_tag_is_not_a_release(tmp_path):
    """Uncommitted edits mean the build does not match the tag it sits on."""
    mod = _load_stamp_module()
    stamp = mod.build_stamp(env={}, repo=_make_repo(tmp_path, tag="v0.9.0", dirty=True))
    assert stamp.is_release is False


# -- Tier 3: no signal ---------------------------------------------------------


def test_untagged_repo_falls_back_to_silence(tmp_path):
    mod = _load_stamp_module()
    stamp = mod.build_stamp(env={}, repo=_make_repo(tmp_path))
    assert stamp.is_release is True
    assert stamp.base_tag is None


def test_missing_repo_falls_back_to_silence(tmp_path):
    """An sdist build has no .git at all — stay quiet rather than warn falsely."""
    mod = _load_stamp_module()
    stamp = mod.build_stamp(env={}, repo=tmp_path / "nope")
    assert stamp.is_release is True


# -- Presentation --------------------------------------------------------------


def test_release_title_carries_a_bare_version():
    mod = _load_stamp_module()
    stamp = mod.BuildStamp(is_release=True, base_tag="v0.9.0", commit=None, commits_since=0)
    assert mod.html_title("otto", "0.9.0", stamp) == "otto 0.9.0 documentation"


def test_dev_title_is_marked():
    mod = _load_stamp_module()
    stamp = mod.BuildStamp(is_release=False, base_tag="v0.9.0", commit="ded87ca6", commits_since=3)
    assert mod.html_title("otto", "0.9.0", stamp) == "otto 0.9.0+dev documentation"


def test_release_has_no_banner():
    mod = _load_stamp_module()
    stamp = mod.BuildStamp(is_release=True, base_tag="v0.9.0", commit=None, commits_since=0)
    assert mod.dev_banner_text(stamp) is None


def test_dev_banner_names_the_commit_and_base_tag():
    mod = _load_stamp_module()
    stamp = mod.BuildStamp(is_release=False, base_tag="v0.9.0", commit="ded87ca6", commits_since=3)
    text = mod.dev_banner_text(stamp)
    assert "ded87ca6" in text
    assert "v0.9.0" in text
    assert "3 commits" in text


def test_dev_banner_degrades_without_git_detail():
    """RTD branch builds on a tagless shallow clone still get a usable banner."""
    mod = _load_stamp_module()
    stamp = mod.BuildStamp(is_release=False, base_tag=None, commit=None, commits_since=None)
    text = mod.dev_banner_text(stamp)
    assert text
    assert "commits after" not in text


def test_dev_banner_uses_singular_for_one_commit():
    mod = _load_stamp_module()
    stamp = mod.BuildStamp(is_release=False, base_tag="v0.9.0", commit="abc1234", commits_since=1)
    assert "1 commit after" in mod.dev_banner_text(stamp)
