"""The checked-in sample repos must carry no install artifacts.

``pip install -e <dir>`` / ``uv pip install -e <dir>`` writes an ``.egg-info``
into the directory it installs. Point one at ``tests/repo4`` and the suite has
mutated its own corpus -- and because otto puts each repo's declared ``libs``
on ``sys.path``, that directory then becomes visible to
``importlib.metadata``, which reads it as an installed distribution.

The consequence is not cosmetic. A dependency preflight that reads installed
metadata FIRST gets ``Requires-Dist`` (normalized: ``pkg>=0.1``) on a machine
that has run the install suite, and the pyproject (spaced: ``pkg >= 0.1``) on a
clean clone. An assertion on that string passes for one and fails for the
other, and ``.egg-info`` is gitignored, so nothing in ``git status`` says why.

This gate can only ever fire LOCALLY -- CI clones fresh, so the artifact cannot
be there -- which is aimed correctly: a local checkout is the only place the
pollution accumulates. A test that needs an installable repo must install a
COPY: see :func:`tests._fixtures.sutrepo.copy_sample_repo`.
"""

from tests._fixtures.paths import TESTS_ROOT

_ARTIFACTS = ("*.egg-info", "*.dist-info")
"""What an editable install leaves behind in the directory it installs."""


def _sample_repo_trees():
    """Every sample repo DIRECTLY under ``tests/``, found by its settings file.

    Derived rather than listed: four other gates already mirror the sample-tree
    denylist by hand, and a fifth copy would be one more place to forget. A
    directory holding ``.otto/settings.toml`` IS a sample repo, which is the
    same fact those lists encode.

    Blind spot, stated: the glob is one level deep, matching the four
    hand-written denylists, which are flat for the same reason -- every sample
    repo today is a direct child. One added at ``tests/e2e/repo_x/`` would go
    unscanned, and the premise test below would not notice because the family
    it looks for is still there. ``rglob`` would close it at the cost of
    walking ``tests/firmware``, a large tree that cannot carry an
    editable-install artifact (no ``pyproject.toml`` anywhere in it). Widen
    this the day a nested sample repo exists, not before.
    """
    return sorted({p.parent.parent for p in TESTS_ROOT.glob("*/.otto/settings.toml")})


def test_the_sample_repos_are_findable_at_all():
    """The scan's premise, asserted rather than assumed.

    Without this, a glob that matched nothing would make the gate below pass by
    looking at no trees at all.
    """
    trees = _sample_repo_trees()
    assert len(trees) >= 4, f"expected the repo1..repo4 family, found {trees}"
    assert any(tree.name == "repo4" for tree in trees), trees
    # The other way the scan can look at nothing: emptying the pattern list
    # leaves every tree scanned for no artifact at all.
    assert _ARTIFACTS, "an empty pattern list makes the gate below unfailable"


def test_no_sample_repo_carries_an_editable_install_artifact():
    found = [
        str(hit.relative_to(TESTS_ROOT))
        for tree in _sample_repo_trees()
        for pattern in _ARTIFACTS
        for hit in tree.rglob(pattern)
    ]
    assert found == [], (
        f"a test installed a sample repo IN PLACE, leaving {found} in the "
        "checked-in tree. Install a COPY instead — "
        "`tests._fixtures.sutrepo.copy_sample_repo(src, tmp_path / name)` — and "
        "delete the artifact(s) above; they are gitignored, so they will not "
        "show in `git status`."
    )
