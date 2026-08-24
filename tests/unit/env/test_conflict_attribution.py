"""otto adds at most ONE line to a resolver's message, and only when it is true.

The rule this pins is the one the module states: a guess is worse than silence,
because it sends the operator to edit the wrong pyproject. Both halves are
tested -- that a real two-repo collision IS attributed, and that the shapes
which merely MENTION repo names are not.
"""

from pathlib import Path

from otto.env import _attribute_conflict


class _Repo:
    """Enough of a Repo for attribution: a name and a directory."""

    def __init__(self, name, sut_dir):
        self.name = name
        self.sut_dir = Path(sut_dir)


def test_a_real_two_repo_collision_is_attributed():
    repos = [_Repo("alpha", "/w/alpha"), _Repo("bravo", "/w/bravo")]
    text = "Because alpha depends on click>=8 and bravo depends on click<8, ..."
    assert (
        _attribute_conflict(repos, text) == "the conflicting requirements came from: alpha, bravo"
    )


def test_one_named_repo_is_not_enough_to_attribute():
    """With a single name there is nothing to attribute BETWEEN."""
    repos = [_Repo("alpha", "/w/alpha"), _Repo("bravo", "/w/bravo")]
    assert _attribute_conflict(repos, "Failed to build alpha: no setuptools") is None


def test_names_appearing_only_in_the_env_path_are_not_a_collision():
    """THE REGRESSION. The workspace key is ``<hash>-<slug>``, and the slug is
    built from the repo basenames -- so every installer message that echoes the
    environment path contains every repo's name. Matching the raw text
    attributed a plain "setuptools missing" build failure to "repo1, repo4",
    which is exactly the wrong-pyproject send-off the rule forbids.
    """
    repos = [_Repo("repo1", "/w/repo1"), _Repo("repo4", "/w/repo4")]
    text = (
        "Using Python 3.10.20 environment at: "
        "/tmp/home/134b91c0-repo1-repo4/env\n"
        "  Failed to build `otto-sample-repo4`\n"
        "  Because setuptools was not found in the provided package locations"
    )
    assert _attribute_conflict(repos, text) is None


def test_a_real_collision_still_attributes_when_the_path_is_also_present():
    """The path scrub must not swallow a genuine collision mentioned elsewhere."""
    repos = [_Repo("repo1", "/w/repo1"), _Repo("repo4", "/w/repo4")]
    text = (
        "Using Python 3.10.20 environment at: /tmp/home/134b91c0-repo1-repo4/env\n"
        "Because repo1 depends on click>=8 and repo4 depends on click<8, ..."
    )
    assert (
        _attribute_conflict(repos, text) == "the conflicting requirements came from: repo1, repo4"
    )
