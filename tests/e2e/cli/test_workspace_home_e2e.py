"""The relocation, observed through the real binary.

The discriminator is the same invocation writing to a different place: before
this change it wrote under ``$OTTO_XDIR/.otto/``, after it writes under the
workspace home and leaves the xdir clean.
"""

import pytest

from tests.e2e._otto_subprocess import REPO1, run_otto

pytestmark = pytest.mark.hostless


def _run(argv, *, home, xdir, sut_dirs=REPO1):
    return run_otto(
        argv,
        xdir=xdir,
        sut_dirs=None,
        extra_argv_prefix=["-R"],
        extra_env={"OTTO_SUT_DIRS": str(sut_dirs), "OTTO_HOME": str(home)},
    )


class TestCacheLandsInTheWorkspaceHome:
    def test_a_completion_warm_writes_under_the_home_and_leaves_the_xdir_clean(self, tmp_path):
        home, xdir = tmp_path / "home", tmp_path / "xdir"
        xdir.mkdir()
        result = _run(["run", "--list-instructions"], home=home, xdir=xdir)
        assert result.returncode == 0, result.stderr

        caches = list(home.rglob("completion_cache.json"))
        assert caches, f"no cache written under {home}; tree: {list(home.rglob('*'))}"
        assert caches[0].parent.parent == home, caches[0]

        assert not (xdir / ".otto").exists(), (
            f"the xdir must stay clean of caches; found {list(xdir.rglob('*'))}"
        )

    def test_one_workspace_keeps_one_cache_however_many_xdirs(self, tmp_path):
        """The deduplication the relocation buys, and could not be tested before.

        The cache's content was always a pure function of the workspace --
        compute_fingerprint hashes each repo's settings and init modules and
        nothing else -- but its LOCATION was the xdir, so invoking otto from N
        directories against the same repos maintained N byte-identical caches.
        Keyed by the workspace instead, two xdirs share one cache.

        Not a restatement of the unit tests: those pin the key as a pure
        function, this pins that the running binary actually stores by it.
        """
        home = tmp_path / "home"
        first, second = tmp_path / "x1", tmp_path / "x2"
        first.mkdir()
        second.mkdir()

        for xdir in (first, second):
            result = _run(["run", "--list-instructions"], home=home, xdir=xdir)
            assert result.returncode == 0, result.stderr

        caches = sorted(home.rglob("completion_cache.json"))
        assert len(caches) == 1, f"expected one cache for one workspace, got {caches}"
        for xdir in (first, second):
            assert not (xdir / ".otto").exists(), f"{xdir} still holds a cache"
