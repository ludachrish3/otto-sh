"""The tree-pure static targets and the gate entry points' Makefile wiring."""

import re

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.test_lane_invariants import make_reachable, makefile_live_recipe
from tests.unit.test_make_release_lanes import _recipe, _rule

pytestmark = pytest.mark.interpreter_agnostic

_MAKEFILE = (CHECKOUT_ROOT / "Makefile").read_text()


def _targets(text: str) -> list[str]:
    return re.findall(r"^([A-Za-z0-9_-]+)\s*:(?!=)", text, re.MULTILINE)


def test_lock_check_is_uv_lock_check():
    assert [line.strip() for line in _recipe("lock-check").splitlines()] == [
        '@$(SAY) "uv lock --check"',
        "@uv lock --check",
    ]


def test_check_python_checks_the_lock_first_then_the_static_lanes_one_goal_per_line():
    prereqs, recipe = _rule(_MAKEFILE, "check-python")
    assert prereqs == []
    assert [line.strip() for line in recipe] == [
        "@$(MAKE) lock-check",
        "@$(MAKE) lint-python",
        "@$(MAKE) typecheck-python",
    ]
    # One goal per `$(MAKE)` line is what lets the census follow the recipe.
    assert {"lock-check", "lint-python", "typecheck-python"} <= set(
        make_reachable("check-python", _MAKEFILE)
    )


def test_lint_arch_has_no_history_prerequisite():
    prereqs, _ = _rule(_MAKEFILE, "lint-arch")
    assert "check-breaking" not in prereqs


def test_only_check_breaking_reads_the_range():
    readers = sorted(
        {
            t
            for t in set(_targets(_MAKEFILE))
            if any("$(RANGE)" in line for line in makefile_live_recipe(_MAKEFILE, t))
        }
    )
    assert readers == ["check-breaking"]


@pytest.mark.parametrize("entry", ["gate-tip", "gate-status"])
def test_the_gate_entry_points_run_python_locked(entry):
    assert _recipe(entry).strip().startswith("@uv run --locked python scripts/gate_tip.py")


def test_the_hook_runs_python_locked():
    hook = (CHECKOUT_ROOT / ".githooks/pre-push").read_text()
    assert 'exec uv run --locked python scripts/gate_tip.py --pre-push --remote "$1"' in hook


def test_nox_typecheck_runs_the_make_target():
    noxfile = (CHECKOUT_ROOT / "noxfile.py").read_text()
    assert 'session.run("make", "typecheck-python", external=True)' in noxfile


def test_land_is_the_documented_steps_in_order():
    prereqs, recipe = _rule(_MAKEFILE, "land")
    assert prereqs == []
    assert [line.strip() for line in recipe] == [
        (
            '@b="$$(git branch --show-current)"; [ -n "$$b" ] && [ "$$b" != main ] || '
            '{ echo "land: run on a feature branch (main takes no direct push)" >&2; exit 1; }'
        ),
        "@$(MAKE) gate-tip",
        '@git push -u origin "$$(git branch --show-current)"',
        "@gh pr view --json url -q .url >/dev/null 2>&1 || gh pr create --draft --fill",
        "@$(MAKE) gate-status",
    ]
