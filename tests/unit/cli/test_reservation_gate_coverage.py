"""Every built-in CLI leaf that contacts a lab host runs the reservation gate.

``docs/cli/reservation/index.md`` promises the gate "runs at the top of every
live-lab subcommand". This walks the REAL registered built-in command tree and
asks, for each leaf, the one function the preamble itself asks
(:func:`otto.cli.invoke.leaf_is_gated`). The leaves that are NOT gated are the
explicit table below, each with its reason: a new leaf fails
``test_the_ungated_leaves_are_exactly_the_table`` until someone classifies it.

The walk sees every leaf of a group whose leaves are static, so the table names
EXACT leaf paths and a new leaf in any such group is unclassified until it is
added. Two things the walk cannot see, stated rather than hidden: the groups
whose leaves are built at run time (``host``, ``run``, ``test``) appear as ONE
entry each and are gated at the group level, so a leaf added under them is not
enumerated; and a new OFFLINE leaf added under a gated group passes silently as
gated, which is the safe direction (an unneeded reservation check, never a
skipped one) and is fixed by marking it ``__cli_gate__ = False`` when someone
notices.
"""

import functools
from typing import Any
from unittest.mock import MagicMock

import pytest

from otto.cli import invoke
from otto.cli.builtin_commands import builtin_command_names
from otto.cli.registry import CLI_COMMANDS, CommandSpec, resolve_spec_command
from otto.reservations import MissingReservationError
from tests._fixtures.preamble_ctx import FakeLeafCtx

# EXACT leaf paths, each with its reason. No entry names a group: every group
# here has static leaves, so a new leaf under it must be classified by hand. (The
# run-time-built groups `host`, `run` and `test` are gated, so they need no entry.)
UNGATED: dict[tuple[str, ...], str] = {
    ("init",): "scaffolds a project; no lab, no host",
    ("env", "create"): "builds the workspace environment; no lab, no host",
    ("env", "show"): "reports the workspace environment; no lab, no host",
    ("env", "sync"): "installs into the workspace environment; no lab, no host",
    ("cache", "clear"): "acts on the filesystem under OTTO_HOME only",
    ("cache", "disable"): "acts on the filesystem under OTTO_HOME only",
    ("cache", "enable"): "acts on the filesystem under OTTO_HOME only",
    ("cache", "info"): "acts on the filesystem under OTTO_HOME only",
    ("cache", "prune"): "acts on the filesystem under OTTO_HOME only",
    ("reservation", "check"): "the gate's own report; it queries the backend, not a lab host",
    ("reservation", "whoami"): "reports the resolved identity; queries no lab host",
    ("inventory", "diff"): "reads configuration and its source; never a lab host",
    ("inventory", "export"): "reads configuration and its source; never a lab host",
    ("inventory", "list"): "reads configuration and its source; never a lab host",
    ("inventory", "lookup"): "reads configuration and its source; never a lab host",
    ("inventory", "refresh"): "refreshes the inventory from its source; never a lab host",
    ("schema", "export"): "exports JSON Schema; no lab, no host",
    ("lab", "render"): "renders a lab source file in memory; no lab, no host",
    ("lab", "explain"): "reads the -l lab's configuration and its layers; contacts no host",
    ("monitor",): (
        "review reads a local file; the live branch runs the shared live-lab opening itself"
    ),
    ("docker", "use-cases"): "lists declared use-cases from configuration; contacts no host",
    ("cov", "report"): "builds an HTML report from local artifacts; contacts no host",
    ("cov", "check-stamp"): "offline (lab-free): reads a coverage stamp",
    ("cov", "kmodcov", "export"): "offline (lab-free): exports local kernel-module data",
    ("cov", "kmodcov", "check"): "offline (lab-free): checks local kernel-module data",
    ("cov", "embedded-gcov", "export"): "offline (lab-free): exports local data",
    ("cov", "embedded-gcov", "check"): "offline (lab-free): checks local data",
    ("cov", "embedded-gcov", "finish"): "offline (lab-free): finishes local data",
}


@functools.cache
def _leaves() -> dict[tuple[str, ...], tuple[CommandSpec, Any]]:
    found: dict[tuple[str, ...], tuple[CommandSpec, Any]] = {}

    def walk(path: tuple[str, ...], spec: CommandSpec, cmd: Any) -> None:
        children = getattr(cmd, "commands", None)
        if children:
            for name, child in children.items():
                walk((*path, name), spec, child)
        else:
            found[path] = (spec, cmd)

    for name in builtin_command_names():
        spec = CLI_COMMANDS.get(name)
        walk((name,), spec, resolve_spec_command(spec))
    return found


def test_the_walk_finds_the_live_verbs():
    live = {("docker", "compose", "up"), ("cov", "get"), ("cov", "clean")}
    assert live <= set(_leaves())


def test_the_ungated_leaves_are_exactly_the_table():
    """Every ungated leaf has a table entry, and every table entry is an ungated leaf.

    Groups whose leaves are built at run time (``host``, ``run``, ``test``)
    appear in the walk as ONE entry each and are gated at the group level, so
    their individual leaves are not enumerated here.
    """
    ungated = {
        path
        for path, (spec, cmd) in _leaves().items()
        if not invoke.leaf_is_gated(spec, cmd.callback)
    }
    unclassified = ungated - set(UNGATED)
    wrongly_gated = {p for p in UNGATED if p in _leaves() and p not in ungated}
    assert not unclassified, f"leaves skip the reservation gate with no table entry: {unclassified}"
    assert not wrongly_gated, f"table says exempt, but the leaf is gated: {wrongly_gated}"
    stale = set(UNGATED) - set(_leaves())
    assert not stale, f"table entries matching no leaf: {stale}"


def _refusing() -> MagicMock:
    res = MagicMock()
    res.evaluate.side_effect = MissingReservationError("resource held by bob")
    return res


@pytest.mark.parametrize(
    "path",
    [
        ("docker", "build"),
        ("docker", "compose", "up"),
        ("docker", "compose", "down"),
        ("docker", "image", "rm"),
        ("cov", "get"),
        ("cov", "clean"),
    ],
)
def test_a_live_verb_is_refused_before_its_body(preamble_gate, monkeypatch, path):
    spec, cmd = _leaves()[path]
    reached_body = MagicMock()
    monkeypatch.setattr(invoke, "stop_at_dry_run_seam", reached_body)
    ctx = FakeLeafCtx(spec, _refusing(), cmd.callback)
    with pytest.raises(MissingReservationError, match="held by bob"):
        invoke.command_preamble(ctx)  # ty: ignore[invalid-argument-type]
    reached_body.assert_not_called()


@pytest.mark.parametrize("path", [("cov", "report"), ("docker", "use-cases")])
def test_an_offline_verb_is_not_asked(preamble_gate, path):
    spec, cmd = _leaves()[path]
    res = _refusing()
    invoke.command_preamble(FakeLeafCtx(spec, res, cmd.callback))  # ty: ignore[invalid-argument-type]
    res.evaluate.assert_not_called()
