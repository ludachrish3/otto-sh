"""First-party default instructions: thin wrappers over :mod:`otto.project`.

``otto run install`` and its five siblings are ORDINARY INSTRUCTIONS -- the
same decorator a repo uses, the same lifecycle bridge, the same generated
``--help`` -- whose entire body is a forward to
:mod:`otto.project.orchestrator`. Bootstrap imports this module before any
repo's init runs, so every lab has them.

NEVER AN OVERRIDE POINT. A repo customizes lab behavior by registering a
:class:`~otto.project.actions.ProjectActions` subclass; these wrappers, the
``ensure_*`` fixtures, and anything else that calls the orchestrator all pick
that change up for free. A repo that tries to claim one of these names is
refused at registration instead (:func:`otto.cli.run.instruction`), because two
code paths to "install the lab" is exactly the split-brain the project layer
exists to prevent.

So every body below is dispatch and nothing else -- flag names in, orchestrator
keywords out. A wrapper thick enough to have a bug of its own belongs in the
orchestrator, where the fixtures reach it too.
"""

from typing import TYPE_CHECKING, Annotated

import typer

from ..cli.run import instruction
from ..result import Result
from ..utils import Status
from . import orchestrator
from .state import Cleanliness, CleanlinessKind, InstallState

if TYPE_CHECKING:
    from rich.table import Table

    from .state import CleanlinessReport

_STATE_ANSWERS: "dict[InstallState, Result]" = {
    InstallState.INSTALLED: Result(Status.Success, value="lab is installed"),
    InstallState.UNINSTALLED: Result(Status.Failed, msg="lab is uninstalled"),
    InstallState.PARTIAL: Result(
        Status.Error,
        msg="lab is partially installed; otto run install --ensure recovers it",
    ),
}
"""``otto run status``' answer, as a value the leaf renderer can exit on.

RETURNED, never raised: ``typer.Exit`` outside ``otto.cli`` would couple the
project layer to the CLI framework (the suite runner's ``CommandResult`` is the
same call), and a returned value is also what lets a caller who imports this
instruction READ the answer instead of catching it. The three codes come from
the Result family's own mapping (:attr:`otto.result.Result.exit_code` is
``status.value`` when the result is not ok), which is why the states are
spelled as statuses here rather than as bare integers: Success exits 0, Failed
1, Error 2.

Frozen dataclasses, so sharing one instance per state is safe.
"""

_STATE_STYLES: "dict[InstallState, str]" = {
    InstallState.INSTALLED: "green",
    InstallState.UNINSTALLED: "dim",
    InstallState.PARTIAL: "yellow",
}

_CLEANLINESS_STYLES: "dict[Cleanliness, str]" = {
    Cleanliness.CLEAN: "green",
    Cleanliness.DIRTY: "yellow",
    Cleanliness.UNKNOWN: "red",
}
"""UNKNOWN is the loud one, not DIRTY.

A dirty lab is an ordinary lab with a command that fixes it; a row nobody could
read is the one an operator has to go and do something about before any of this
means anything.
"""

_CLEANLINESS_LABELS: "dict[CleanlinessKind, str]" = {
    CleanlinessKind.REPO: "products & dev tools",
    CleanlinessKind.TOOLCHAIN: "toolchain tools",
    CleanlinessKind.IMPAIRMENT: "impairments",
    CleanlinessKind.TUNNEL: "tunnels",
}
"""Section headings, in ``cleanup``'s own step order (the report's row order)."""

_CLEANLINESS_SUMMARY: "dict[Cleanliness, str]" = {
    Cleanliness.CLEAN: "lab is clean",
    Cleanliness.DIRTY: "lab is dirty — otto run cleanup takes it off",
    Cleanliness.UNKNOWN: "lab cleanliness is unknown — see the unknown rows above",
}
"""The aggregate, which has nowhere else to go.

The install aggregate rides out on the returned :class:`~otto.result.Result`
and the leaf renderer prints it; the cleanliness axis has no such carrier,
because it deliberately does not touch the exit code.
"""


@instruction()
async def install(
    ensure: Annotated[
        bool, typer.Option(help="Converge: check state first, recover a partial install.")
    ] = False,
    recover_partial: Annotated[
        bool, typer.Option(help="With --ensure: uninstall a PARTIAL lab before installing fresh.")
    ] = True,
) -> Result:
    """Install every repo's products on the lab, dependencies first.

    Plain, this is fail-fast: the first repo that will not install stops the
    walk, because a dependent stacked on a dependency known to be missing
    produces a lab nobody can reason about.

    --ensure converges instead: the lab's current state is read and only the
    missing work is done, which is what the ensure_installed fixture does
    before a test session. --no-recover-partial then keeps a PARTIAL lab's
    remnants in place rather than tearing them down first.
    """
    return await orchestrator.install(ensure=ensure, recover_partial=recover_partial)


@instruction()
async def uninstall(
    product_logs: Annotated[
        bool, typer.Option(help="Haul each repo's product logs off before that repo comes down.")
    ] = True,
    debug_logs: Annotated[
        bool, typer.Option(help="Sweep every host's debug logs once, after every repo is down.")
    ] = True,
) -> Result:
    """Uninstall every repo's products, dependents first.

    Best-effort, unlike the install: every repo is attempted and the first
    failure is what is reported, because a repo that will not come down must
    not strand the ones behind it.
    """
    return await orchestrator.uninstall(get_product_logs=product_logs, get_debug_logs=debug_logs)


@instruction()
async def cleanup(
    product_logs: Annotated[
        bool, typer.Option(help="Haul each repo's product logs off before that repo comes down.")
    ] = True,
    debug_logs: Annotated[
        bool, typer.Option(help="Sweep every host's debug logs once, after every repo is down.")
    ] = True,
    reset_impairments: Annotated[
        bool, typer.Option(help="Repair every lab link, clearing otto's netem impairments.")
    ] = True,
    remove_tunnels: Annotated[
        bool, typer.Option(help="Reap every otto tunnel in the lab -- the very last step.")
    ] = True,
) -> Result:
    """Uninstall every repo, remove its dev tools, and clear what the lab is left wearing.

    Strictly more than uninstall: each repo also gives up its own dev tools,
    the host-global toolchain tools come off, and the lab's own leftovers --
    netem impairments and otto tunnels -- come down after them. Those last two
    belong to no repo, and the tunnel reap is last of all because a tunnel can
    be the access path the rest of the cleanup is running over.
    """
    return await orchestrator.cleanup(
        get_product_logs=product_logs,
        get_debug_logs=debug_logs,
        reset_impairments=reset_impairments,
        remove_tunnels=remove_tunnels,
    )


@instruction("get-logs")
async def get_logs(
    product_logs: Annotated[bool, typer.Option(help="Gather every repo's product logs.")] = True,
    debug_logs: Annotated[bool, typer.Option(help="Sweep every host's debug logs once.")] = True,
    require_product_logs: Annotated[
        bool, typer.Option(help="Fail when a product that declares logs surrendered none.")
    ] = False,
) -> Result:
    """Gather logs from the lab without changing it.

    Product logs are owner-scoped and hauled per repo; the debug sweep is
    host-level and happens once. --require-product-logs turns an empty haul
    into a failure, for a run whose whole purpose was the logs.
    """
    return await orchestrator.get_logs(
        product=product_logs, debug=debug_logs, require_product_logs=require_product_logs
    )


@instruction()
async def install_tools(
    dev: Annotated[bool, typer.Option(help="Install each repo's own dev tools.")] = True,
    toolchain: Annotated[
        bool, typer.Option(help="Also place each host's shared toolchain tools.")
    ] = False,
) -> Result:
    """Install the lab's tooling: each repo's dev tools, optionally the toolchains.

    The toolchain half is off by default and host-global when asked for: one
    toolchain is shared by every owner on a host, so it is placed once rather
    than per repo.
    """
    return await orchestrator.install_tools(dev=dev, toolchain=toolchain)


@instruction()
async def status(
    full: Annotated[
        bool,
        typer.Option(help="Also report cleanliness: dev tools, toolchains, impairments, tunnels."),
    ] = False,
) -> Result:
    """Report each repo's install state, and the lab's.

    THE EXIT CODE IS THE ANSWER, so a script branches on it without parsing
    the table: 0 fully installed, 1 fully uninstalled, 2 partial. Three codes
    rather than a boolean for the same reason InstallState has three members
    -- a half-installed lab and a clean one need different handling, and
    reporting them alike is how remnants get installed over.

    A repo with nothing to say about its install state (no products anywhere,
    no registered actions) is absent from the table rather than listed with a
    made-up state.

    --full adds the lab's OTHER axis: what cleanup would still find on it --
    dev tools, toolchain tools, netem impairments, otto tunnels -- row by row,
    marking anything that could not be read rather than guessing at it. It
    costs a link read per link and a process scan per host, which is why it is
    a flag; it does NOT touch the exit code, which keeps meaning install state
    and nothing else, so a fully installed but filthy lab still exits 0.
    """
    from rich import print as rprint
    from rich.table import Table

    report = await orchestrator.status()
    if report.repos:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for repo_name, state in report.repos.items():
            table.add_row(repo_name, f"[{_STATE_STYLES[state]}]{state.value}[/]")
        rprint(table)
    if full:
        _print_cleanliness(await orchestrator.cleanliness())
    return _STATE_ANSWERS[report.overall]


def _print_cleanliness(report: "CleanlinessReport") -> None:
    """Print the cleanliness rows and their aggregate, in the status table's style."""
    from rich import print as rprint

    rprint(_cleanliness_table(report))
    rprint(f"[{_CLEANLINESS_STYLES[report.overall]}]{_CLEANLINESS_SUMMARY[report.overall]}[/]")


def _cleanliness_table(report: "CleanlinessReport") -> "Table":
    """Render the report as section / name / state rows.

    The section heading is printed on the row where the kind CHANGES, which
    needs no sorting: the report hands its rows back in cleanup's own step
    order and grouped by kind, and that grouping is part of its contract.

    THE STATE CELL IS A ``Text``, NOT A MARKUP STRING like the install table's
    above, and the difference is where the words come from. That table renders
    an enum otto owns; this one appends details that came off a device -- a
    ``tc`` error, an exception's repr -- and any ``[`` in one of those would be
    read as markup by the console and swallow the rest of the cell.
    """
    from rich.table import Table
    from rich.text import Text

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    heading = ""
    for item in report.items:
        label = _CLEANLINESS_LABELS[item.kind]
        cell = Text(item.state.value, style=_CLEANLINESS_STYLES[item.state])
        if item.detail:
            cell.append(f" — {item.detail}", style="dim")
        table.add_row("" if label == heading else label, item.name, cell)
        heading = label
    return table
