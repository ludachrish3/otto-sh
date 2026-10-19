"""otto adds at most ONE line to a resolver's message, and only when it is true.

The rule this pins is the one the module states: a guess is worse than silence,
because it sends the operator to edit the wrong pyproject. Both halves are
tested -- that a real two-project collision IS attributed, and that the shapes
which merely MENTION project names are not.
"""

from pathlib import Path

from otto.env.manage import _attribute_conflict


class _Project:
    """Enough of a Project for attribution: a name and a directory."""

    def __init__(self, name, root):
        self.name = name
        self.root = Path(root)


def test_a_real_two_project_collision_is_attributed():
    projects = [_Project("alpha", "/w/alpha"), _Project("bravo", "/w/bravo")]
    text = "Because alpha depends on click>=8 and bravo depends on click<8, ..."
    assert (
        _attribute_conflict(projects, text)
        == "the conflicting requirements came from: alpha, bravo"
    )


def test_one_named_project_is_not_enough_to_attribute():
    """With a single name there is nothing to attribute BETWEEN."""
    projects = [_Project("alpha", "/w/alpha"), _Project("bravo", "/w/bravo")]
    assert _attribute_conflict(projects, "Failed to build alpha: no setuptools") is None


def test_names_appearing_only_in_the_env_path_are_not_a_collision():
    """THE REGRESSION. The workspace key is ``<hash>-<slug>``, and the slug is
    built from the project basenames -- so every installer message that echoes the
    environment path contains every project's name. Matching the raw text
    attributed a plain "setuptools missing" build failure to "project1, project4",
    which is exactly the wrong-pyproject send-off the rule forbids.
    """
    projects = [_Project("project1", "/w/project1"), _Project("project4", "/w/project4")]
    text = (
        "Using Python 3.10.20 environment at: "
        "/tmp/home/134b91c0-project1-project4/env\n"
        "  Failed to build `otto-sample-project4`\n"
        "  Because setuptools was not found in the provided package locations"
    )
    assert _attribute_conflict(projects, text) is None


def test_a_real_collision_still_attributes_when_the_path_is_also_present():
    """The path scrub must not swallow a genuine collision mentioned elsewhere."""
    projects = [_Project("project1", "/w/project1"), _Project("project4", "/w/project4")]
    text = (
        "Using Python 3.10.20 environment at: /tmp/home/134b91c0-project1-project4/env\n"
        "Because project1 depends on click>=8 and project4 depends on click<8, ..."
    )
    assert (
        _attribute_conflict(projects, text)
        == "the conflicting requirements came from: project1, project4"
    )
