"""Dependabot's groups sort a bump by what landing it costs.

Membership is decided by ``routine``'s exclusions: Dependabot gives a package
to the group whose pattern names it most specifically and checks update-types
only when it builds that group's pull request, so a one-package group with
update-types would claim a package and reject its other bumps, which then
skip routine and open alone (the comment at the top of
``.github/dependabot.yml`` cites the dependabot-core source).
"""

import pytest
import yaml

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

CONFIG = yaml.safe_load((CHECKOUT_ROOT / ".github" / "dependabot.yml").read_text())

ALWAYS_ALONE = {
    "uv": ["ty", "ruff", "typer", "pytest", "pytest-asyncio"],
    "npm": ["@biomejs/biome", "knip"],
}


def _groups(ecosystem: str) -> dict:
    (update,) = [u for u in CONFIG["updates"] if u["package-ecosystem"] == ecosystem]
    return update["groups"]


@pytest.mark.parametrize("ecosystem", sorted(ALWAYS_ALONE))
def test_routine_takes_patch_and_minor_of_all_but_the_packages_that_land_alone(ecosystem):
    routine = _groups(ecosystem)["routine"]
    assert routine == {
        "patterns": ["*"],
        "exclude-patterns": ALWAYS_ALONE[ecosystem],
        "update-types": ["minor", "patch"],
    }


@pytest.mark.parametrize("ecosystem", sorted(ALWAYS_ALONE))
def test_only_routine_restricts_update_types(ecosystem):
    """A narrower group with update-types would keep its package's other bumps out of
    routine; routine's own restriction only keeps majors out of it."""
    for name, group in _groups(ecosystem).items():
        if name != "routine":
            assert "update-types" not in group, name
