"""The workspace key and the paths under otto's user-level home.

Pure-function tests: nothing here creates a directory or reads a cache. The
key is the whole contract — two invocations that mean the same workspace must
land in the same directory, and two that do not must not collide.
"""

from pathlib import Path

from otto.config.home import otto_home, workspace_home, workspace_key


class TestOttoHome:
    def test_defaults_under_the_user_home(self, monkeypatch):
        monkeypatch.delenv("OTTO_HOME", raising=False)
        assert otto_home() == Path.home() / ".otto"

    def test_otto_home_relocates_it_wholesale(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "elsewhere"))
        assert otto_home() == tmp_path / "elsewhere"

    def test_it_never_creates_the_directory(self, monkeypatch, tmp_path):
        target = tmp_path / "not-yet"
        monkeypatch.setenv("OTTO_HOME", str(target))
        otto_home()
        assert not target.exists(), "otto_home() must be a pure path computation"


class TestWorkspaceKey:
    def test_order_does_not_matter(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert workspace_key([a, b]) == workspace_key([b, a])

    def test_a_symlink_keys_as_its_target(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert workspace_key([link]) == workspace_key([real])

    def test_a_relative_path_keys_as_its_absolute_form(self, tmp_path, monkeypatch):
        real = tmp_path / "here"
        real.mkdir()
        monkeypatch.chdir(tmp_path)
        assert workspace_key([Path("here")]) == workspace_key([real])

    def test_different_sets_do_not_collide(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert workspace_key([a]) != workspace_key([a, b])

    def test_the_empty_set_is_legal_and_stable(self):
        key = workspace_key([])
        assert key == workspace_key([])
        assert key.endswith("-no-repos"), key

    def test_the_empty_set_does_not_collide_with_a_real_one(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        assert workspace_key([]) != workspace_key([a])

    def test_the_shape_is_hash8_dash_slug(self, tmp_path):
        a = tmp_path / "myrepo"
        a.mkdir()
        key = workspace_key([a])
        head, _, slug = key.partition("-")
        assert len(head) == 8, key
        assert all(c in "0123456789abcdef" for c in head), key
        assert slug == "myrepo", key

    def test_the_slug_is_pep503_normalized(self, tmp_path):
        a = tmp_path / "My_Repo.Name"
        a.mkdir()
        assert workspace_key([a]).endswith("-my-repo-name")

    def test_the_slug_joins_multiple_basenames(self, tmp_path):
        a, b = tmp_path / "alpha", tmp_path / "beta"
        a.mkdir()
        b.mkdir()
        assert workspace_key([a, b]).endswith("-alpha-beta")

    def test_two_sets_sharing_a_slug_still_differ_by_hash(self, tmp_path):
        """The slug is for humans; the hash is what makes the key correct."""
        one = tmp_path / "x" / "repo"
        two = tmp_path / "y" / "repo"
        one.mkdir(parents=True)
        two.mkdir(parents=True)
        assert workspace_key([one]).endswith("-repo")
        assert workspace_key([two]).endswith("-repo")
        assert workspace_key([one]) != workspace_key([two])


class TestWorkspaceHome:
    def test_it_is_the_key_under_the_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        assert workspace_home([repo]) == tmp_path / "home" / workspace_key([repo])

    def test_it_reads_the_env_when_given_nothing(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
        assert workspace_home() == workspace_home([repo])

    def test_no_sut_dirs_env_is_the_no_repos_workspace(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)
        assert workspace_home().name.endswith("-no-repos")

    def test_it_never_creates_the_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)
        home = workspace_home()
        assert not home.exists(), "workspace_home() must be a pure path computation"


def test_adjacent_paths_cannot_collide():
    """The newline separator in the hash input is load-bearing.

    Without it, ('/a/a', '/b') and ('/a', '/a/b') concatenate to the identical
    byte string '/a/a/b' and hash alike -- and because both sets have the
    basenames {a, b}, the slug half cannot tell them apart either. So the FULL
    key collides and two distinct workspaces share one home.

    Both conditions are needed for the guard to bite, which is why the obvious
    pair ('/a/bc','/d') vs ('/a','/bc/d') does NOT work: its hashes do collide,
    but its basenames differ ('bc-d' vs 'a-d') and the slug rescues the key.

    LITERAL absolute paths, deliberately not ``tmp_path``: a tmp_path-based
    pair shares an absolute prefix, which makes the boundary shift impossible
    to construct at all. ``resolve()`` is non-strict, so these need not exist.
    """
    assert workspace_key([Path("/a/a"), Path("/b")]) != workspace_key([Path("/a"), Path("/a/b")])


class TestCacheRelocation:
    """Both caches live in the workspace home, and one function decides it."""

    def test_the_main_cache_lands_in_the_workspace_home(self, monkeypatch, tmp_path):
        from otto.config.completion_cache import _cache_path

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
        assert _cache_path() == workspace_home([repo]) / "completion_cache.json"

    def test_the_remote_cache_follows_without_its_own_derivation(self, monkeypatch, tmp_path):
        """F2: the remote cache derives from _cache_path BY DESIGN. If this
        passes while the main cache moved, the single-source rule held."""
        from otto.config.remote_completion_cache import _path

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
        assert _path() == workspace_home([repo]) / "remote_completion_cache.json"

    def test_caching_now_works_without_an_xdir(self, monkeypatch, tmp_path):
        """F1: the behaviour change, pinned.

        Before this task _cache_path() returned None with no xdir set, so a
        user who never set OTTO_XDIR had completion caching silently OFF. The
        workspace home is a stable per-user location, so there is no such case
        any more. This test is the only place that fact is written down.
        """
        from otto.config.completion_cache import _cache_path

        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("OTTO_XDIR", raising=False)
        path = _cache_path()
        assert path is not None, "no-xdir must no longer disable caching"
        assert tmp_path / "home" in path.parents

    def test_the_xdir_no_longer_appears_in_the_cache_path(self, monkeypatch, tmp_path):
        from otto.config.completion_cache import _cache_path

        xdir = tmp_path / "xdir"
        xdir.mkdir()
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("OTTO_XDIR", str(xdir))
        monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)
        assert xdir not in _cache_path().parents
