"""Ratchet for otto's import cycle: its members and the edges inside it can only go.

``tach.toml``'s end state is ``forbid_circular_dependencies = true``, and one
cycle of modules that can all reach each other keeps the flag off. The cycle
used to be recorded in prose in ``tach.toml``'s header, and it grew from
sixteen members to twenty-three without anyone deciding it should (#522,
#590). This file is the record now. It reads the same declared graph that
``scripts/render_module_graph.py`` draws on the modules page. ``tach check
--exact`` keeps that graph equal to the imports in the code: an import has to
be declared, and a declared edge has to be imported.

Four ways to fail:

* a module joins a cycle (it is not a key of ``BASELINE``);
* a new edge appears between two modules of one cycle — a member-only check
  would let coupling inside the cycle grow unseen;
* a module leaves every cycle and its entry is still here;
* an edge stops running inside a cycle and is still listed here.

The last two are the anti-vacuity half: delete the entry in the same commit as
the fix, so the fix is recorded and the edge cannot come back unnoticed.

Cutting an edge is the work #590 tracks. To see where the cycle is and which
pairs import each other, read ``docs/architecture/modules.md``. When the
cycle is gone, ``BASELINE`` is empty and the flag can be turned on.
"""

import pytest

from scripts import render_module_graph as rmg

pytestmark = pytest.mark.interpreter_agnostic

# Each module in a cycle -> the modules of the same cycle it may import.
# Shrink-only: delete entries as edges are cut, never add one to quiet a failure.
# One owner-approved re-baseline (spec 2026-10-06 repo-and-scope-inputs, S-5) added the seven
# edges to otto.bootstrap and cut otto.lifecycle -> otto.config. Shrink-only again from here.
# All seven of those edges have since retired. otto.lifecycle's went when the deadline readers
# moved to the run policy; otto.coverage's, otto.docker's, otto.host's, otto.monitor.live's,
# otto.project's and otto.suite's went when their readers took the run's repos from the context
# (ctx.repos) or, with no context object in hand, from otto.config.fleet.current_repos().
# otto.lifecycle has since left the cycle: the host layer and the lifecycle stopped importing
# the context once each event loop's host registry moved to otto.invocation.
BASELINE: dict[str, list[str]] = {
    "otto.bootstrap": ["otto.config", "otto.host"],
    "otto.check": ["otto.host"],
    "otto.cli": [
        "otto.bootstrap",
        "otto.check",
        "otto.config",
        "otto.context",
        "otto.coverage",
        "otto.docker",
        "otto.host",
        "otto.init",
        "otto.instructions",
        "otto.inventory",
        "otto.link",
        "otto.models",
        "otto.monitor",
        "otto.monitor.live",
        "otto.project",
        "otto.reservations",
        "otto.session",
        "otto.suite",
        "otto.tunnel",
    ],
    "otto.config": [
        "otto.bootstrap",
        "otto.cli",
        "otto.context",
        "otto.host",
        "otto.instructions",
        "otto.inventory",
        "otto.labs",
        "otto.link",
        "otto.models",
        "otto.reservations",
    ],
    "otto.context": [
        "otto.bootstrap",
        "otto.config",
        "otto.host",
        "otto.reservations",
        "otto.session",
    ],
    "otto.coverage": ["otto.config", "otto.context", "otto.host", "otto.models"],
    "otto.creds": ["otto.models"],
    "otto.docker": ["otto.config", "otto.host", "otto.models"],
    "otto.host": [
        "otto.config",
        "otto.docker",
        "otto.models",
    ],
    "otto.init": ["otto.config", "otto.host", "otto.inventory", "otto.labs", "otto.models"],
    "otto.instructions": ["otto.context", "otto.project", "otto.session"],
    "otto.inventory": ["otto.config", "otto.creds", "otto.host", "otto.models"],
    "otto.labs": ["otto.config", "otto.host", "otto.inventory", "otto.link", "otto.models"],
    "otto.link": ["otto.check", "otto.host", "otto.models"],
    "otto.models": ["otto.config", "otto.host", "otto.link"],
    "otto.monitor": ["otto.host", "otto.link", "otto.models"],
    "otto.monitor.live": [
        "otto.config",
        "otto.context",
        "otto.monitor",
        "otto.tunnel",
    ],
    "otto.project": [
        "otto.config",
        "otto.context",
        "otto.host",
        "otto.instructions",
        "otto.link",
        "otto.models",
        "otto.tunnel",
    ],
    "otto.reservations": ["otto.config", "otto.models"],
    "otto.session": [
        "otto.bootstrap",
        "otto.config",
        "otto.docker",
        "otto.host",
        "otto.inventory",
        "otto.labs",
        "otto.models",
    ],
    "otto.suite": [
        "otto.config",
        "otto.context",
        "otto.coverage",
        "otto.host",
        "otto.models",
        "otto.monitor",
        "otto.monitor.live",
        "otto.project",
    ],
    "otto.tunnel": ["otto.check", "otto.host", "otto.models"],
}

_HOW_TO_FIX = (
    "Cut the import that closes the loop: move the code to the module that owns "
    "its data, or have the caller hand the value in. "
    "docs/architecture/modules.md shows the cycle, and #590 tracks the work. "
    "Do not add an entry to BASELINE in tests/unit/test_import_cycle_ratchet.py "
    "to quiet this."
)


def _edges(graph: "dict[str, list[str]]") -> "set[tuple[str, str]]":
    return {(module, dep) for module, deps in graph.items() for dep in deps}


@pytest.fixture(scope="module")
def measured() -> "dict[str, list[str]]":
    return rmg.cycle_edges(rmg.load_graph())


def test_the_measurement_sees_the_cycle(measured) -> None:
    # Without this, a broken reader (no modules found, say) would empty the
    # measurement and the stale-entry test below would blame the baseline.
    assert rmg.load_graph().modules, f"no [[modules]] read from {rmg.TACH_TOML}"
    assert measured or not BASELINE, "the graph has no cycle, yet BASELINE lists one"


def test_no_module_joins_the_cycle(measured) -> None:
    joined = sorted(set(measured) - set(BASELINE))
    assert not joined, f"New member(s) of an import cycle: {joined}. {_HOW_TO_FIX}"


def test_no_new_edge_inside_the_cycle(measured) -> None:
    added = sorted(_edges(measured) - _edges(BASELINE))
    assert not added, (
        f"New edge(s) between two modules of one import cycle: {added}. "
        f"Each one couples the cycle tighter and makes it harder to take apart. {_HOW_TO_FIX}"
    )


def test_the_baseline_has_no_stale_member(measured) -> None:
    left = sorted(set(BASELINE) - set(measured))
    assert not left, (
        f"{left} no longer in an import cycle — good. Delete the entry from BASELINE "
        "in this commit, so the record says so and the module cannot slip back in."
    )


def test_the_baseline_has_no_stale_edge(measured) -> None:
    cut = sorted(_edges(BASELINE) - _edges(measured))
    assert not cut, (
        f"{cut} no longer run inside an import cycle — good. Delete them from "
        "BASELINE in this commit, so the edges cannot come back unnoticed."
    )
