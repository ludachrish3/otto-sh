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

Cutting the edges was the work #590 tracked, and the cycle is gone:
``BASELINE`` is empty, so a module that joins any cycle fails the first
check. ``docs/architecture/modules.md`` draws the graph. The flag is turned on
once every file is in a module (spec 2026-10-11 placement-and-phases §5.2).
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
# (ctx.repos) or, with no context object in hand, from the fleet's current_repos(), now
# otto.declared.current_repos().
# otto.lifecycle has since left the cycle: the host layer and the lifecycle stopped importing
# the context once each event loop's host registry moved to otto.invocation.
# otto.cli, otto.coverage, otto.init, otto.monitor, otto.monitor.live and otto.suite left it
# when the completion cache stopped importing the CLI (its walkers of the command tree moved into
# otto.cli.registry) and otto.monitor.live folded into otto.monitor (spec 2026-10-11
# placement-and-phases §3.1, §3.11).
# otto.instructions, otto.project and otto.tunnel left it when the instruction tables moved into
# otto.instruction_catalog, below the layers that read them, and otto.config stopped importing
# the execution face (spec 2026-10-11 placement-and-phases §3.2).
# otto.models -> otto.link went when the impairer registry and its contract became foundation
# modules of their own, and otto.host -> otto.models when the host specs moved into otto.host
# and what both packages share into the leaf otto.models.base (spec 2026-10-11
# placement-and-phases §3.3).
# otto.host -> otto.docker went when a container host stopped starting its own stack: a
# stopped declared container now fails fast and names the explicit ways to start it (spec
# 2026-10-11 placement-and-phases §3.4).
# otto.host and otto.check left it when the composed repos moved into a catalog in otto.declared
# that the composition root fills, scope_for_repo moved beside it, and otto.config.scope,
# otto.declared and otto.models.dependencies became foundation modules of their own: the host
# layer imports nothing of otto.config but the scope predicate (spec 2026-10-11
# placement-and-phases §3.5).
# otto.models, otto.creds and otto.link left it when the runtime settings types and the
# settings file's location moved from otto.config.repo into the foundation module
# otto.config.scope, and the OTTO_SUT_DIRS splitter from otto.config.env into otto.utils:
# otto.models reads nothing of otto.config but the scope module (spec 2026-10-11
# placement-and-phases §3.6).
# otto.inventory left it when otto.config.home and otto.config.user_settings became modules of
# their own in the specs layer: the inventory reads the user's home and settings file without
# importing otto.config, bar the foundation scope module that names the settings file
# (spec 2026-10-11 placement-and-phases §3.6).
# otto.labs left it when the lab model and its loader moved out of otto.config into
# otto.labs.loader, beside the lab sources it drives: the lab sources import nothing of
# otto.config, and otto.session reaches the loader there (spec 2026-10-11
# placement-and-phases §3.7).
# otto.reservations left it when the reservation gate read its lab and hosts in play off the
# active run, a slot otto.invocation holds, instead of otto.config's fleet: the package
# imports nothing of otto.config (spec 2026-10-11 placement-and-phases §3.8).
# otto.bootstrap, otto.config, otto.context, otto.docker and otto.session, the last five, left
# it when the composed state moved into otto.declared, otto.docker read its lab off the active
# run, and the fleet left otto.config for a module of its own, otto.fleet: otto.config imports
# neither the root nor the context (spec 2026-10-11 placement-and-phases §3.9). The cycle is
# gone. BASELINE stays, empty, so that any cycle a later change closes fails here.
BASELINE: dict[str, list[str]] = {}

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
