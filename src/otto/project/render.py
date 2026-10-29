"""How ``otto run status`` prints: the per-project table, the --full sections, the exit code."""

from typing import TYPE_CHECKING

from ..result import Result
from ..utils import Status
from .state import Cleanliness, CleanlinessKind, InstallState

if TYPE_CHECKING:
    from rich.table import Table

    from ..params import OptionsSource
    from .plan import ProjectPlan, TailStep
    from .state import CleanlinessReport, ProjectScopeSummary, ProjectStatus

STATE_ANSWERS: "dict[InstallState, Result]" = {
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
    CleanlinessKind.PROJECT: "products, dev tools & staging",
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


async def render_status(report: "ProjectStatus", source: "OptionsSource") -> Result:
    """Print *report* and return the Result whose exit code IS the answer.

    THE EXIT CODE IS THE ANSWER, so a script branches on it without parsing
    the table: 0 fully installed, 1 fully uninstalled, 2 partial. ``--full``
    adds the lab's other axis -- what cleanup would still find -- and never
    touches the exit code, which keeps meaning install state alone.
    """
    from rich import print as rprint
    from rich.table import Table
    from rich.text import Text

    from . import orchestrator  # function-scope: the orchestrator imports this module's caller
    from .options import StatusOptions

    full = source.build(StatusOptions).full
    # A dependency-skipped project appears only in --full's scoping table.
    unusable = [
        (name, row) for name, row in report.scoping.items() if not row.usable and not row.skipped
    ]
    if report.projects or unusable:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for project_name, state in report.projects.items():
            table.add_row(project_name, f"[{_STATE_STYLES[state]}]{state.value}[/]")
        for project_name, row in unusable:
            # A ``Text``, not a markup string like the states above, because the
            # lab names and the patterns in this cell come from a settings file:
            # a '[' in one -- and a host_patterns entry like `[a-z]+` is a
            # character class -- would be read as markup and swallow the rest of
            # the row.
            table.add_row(project_name, Text(_skipped_cell(row), style="dim"))
        rprint(table)
    if full:
        _print_scoping(report.scoping)
        _print_cleanliness(await orchestrator.cleanliness())
    return STATE_ANSWERS[report.overall]


def _skipped_cell(row: "ProjectScopeSummary") -> str:
    """Say why the walks left this project out, in the terms of the axis that failed.

    The display twin of the orchestrator's skip WARNING and of D3's abort
    message, and it splits on the same flag both of those do. A project the walks
    skipped is either EXCLUDED -- it declared labs and none of them was loaded,
    so the loaded labs are what it needs to see -- or applicable to a loaded
    lab and matching no host in it, where naming the labs would blame the one
    thing that is already right and hide the ``host_patterns`` that are not.

    Args:
        row: A skipped project's row (``usable`` False).

    Returns:
        The cell text, patterns and all.
    """
    if not row.applicable:
        return f"not applicable (labs: {_joined(row.loaded_labs)})"
    return f"no matching hosts (host_patterns: {_joined(row.host_patterns)})"


def _joined(names: "tuple[str, ...]") -> str:
    """Render a name list for a cell, spelling emptiness out rather than leaving a blank.

    An empty cell reads as a renderer that lost the value; ``(none)`` is the
    same phrasing the scoping errors use for the same fact.
    """
    return ", ".join(names) or "(none)"


def _print_scoping(scoping: "dict[str, ProjectScopeSummary]") -> None:
    """Print each project's fleet of interest -- the labs it applies to, the hosts it targets.

    ``--full`` only, and for the reason the cleanliness rows are: the answer a
    bare ``otto run status`` is asked is "are the products on?", and the
    resolved fleet is a second axis, not part of it.

    ONE ROW PER RESOLVED PROJECT, declared or not. An undeclared project is not
    skipped here: its verdict is the whole-lab fallback, so its row says every
    loaded lab and every host, which is the true answer to "which hosts does
    this project even mean" and the only way an operator can tell a project that
    narrowed nothing from a project that narrowed to everything by writing
    ``[".*"]``. A project the dependency pass skipped gets its row too, marked
    in a fourth cell: its declaration narrows the fleet even though no walk
    acts on it. The early return below is NOT a fallback rule -- it covers a
    genuinely empty mapping, which means nothing was RESOLVED (a library
    context, a run with no project roots), and an announced heading over an empty
    table reads as a renderer that lost its data.

    The hosts are the resolve-time universe. It is display data by
    construction (a walk re-derives membership live), which is exactly what
    makes it safe to print: this is the operator's answer to "which hosts does
    this project even mean", not a walk's answer to "which host next".
    """
    if not scoping:
        return
    from rich import print as rprint
    from rich.table import Table
    from rich.text import Text

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    for project_name, row in scoping.items():
        table.add_row(
            project_name,
            Text(f"labs: {_joined(row.applicable_labs)}"),
            Text(f"hosts: {_joined(row.universe)}"),
            Text("skipped (unmet dependencies)" if row.skipped else ""),
        )
    rprint("[bold]fleet of interest[/]")
    rprint(table)


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


_PHASES: dict[str, list[tuple[str, str, str]]] = {
    "install": [("stage", "stage", "products"), ("install", "install", "products")],
    "uninstall": [("uninstall", "uninstall", "products")],
    "install-tools": [("stage", "stage", "products"), ("install", "install", "products")],
    "uninstall-tools": [("remove tool", "uninstall", "dev_tools")],
    "cleanup": [("uninstall", "uninstall", "products"), ("remove tool", "uninstall", "dev_tools")],
}
"""The phases each previewed instruction prints, in the order its steps are listed.

Each phase is ``(label, plan attribute, HostPlan list)``: the label printed in
the step column, the :class:`~otto.host.product.ProductPlan` list its lines
come from, and the :class:`~otto.project.plan.HostPlan` list whose entries it
walks.
"""


def render_plan(name: str, plans: "list[ProjectPlan]", tail: "list[TailStep] | None" = None) -> str:
    """Render :func:`~otto.project.plan.plan_instruction`'s answer as indented plain text.

    A plan is a step list, not a table. ``install`` prints every product's
    ``stage`` lines before any ``install`` line, the order
    :meth:`~otto.host.host.BaseHost.install` really uses, while
    ``install-tools`` keeps each tool's two phases together, as
    :meth:`~otto.host.host.BaseHost.install_dev_tools` does. Gaps close each
    project under ``not checked:``, project-level ones first. A gap that is true of
    the whole lab arrives under every project, so each fully rendered gap line
    prints once, at its first occurrence, and a project left with none prints no
    block. ``cleanup`` lists each host's products and then its dev tools, and
    *tail*, :func:`~otto.project.plan.plan_lab_tail`'s steps, closes the plan
    under ``after every project``: per host, then the lab-wide steps.
    """
    phases = _PHASES[name]
    width = max(len(label) for label, _, _ in phases)
    seen: set[str] = set()
    out: list[str] = []
    for project in plans:
        out.append(project.project)
        for host in project.hosts:
            out.append(f"  {host.host_id}")
            names = max((len(entry.name) for entry in [*host.products, *host.dev_tools]), default=0)
            steps = (
                [(phase, entry) for entry in host.products for phase in phases]
                if name == "install-tools"
                else [(phase, entry) for phase in phases for entry in getattr(host, phase[2])]
            )
            for (label, attr, _), entry in steps:
                first = True
                for line in getattr(entry.plan, attr):
                    # A multi-line command keeps its extra lines in the step column.
                    for physical in line.splitlines() or [""]:
                        if first:
                            column = f"{label:<{width}}  {entry.name:<{names}}"
                            first = False
                        else:
                            column = " " * (width + 2 + names)
                        out.append(f"    {column}  {physical}")
        gaps = list(project.gaps) + [
            f"{host.host_id}: {gap}" for host in project.hosts for gap in host.gaps
        ]
        fresh = [gap for gap in dict.fromkeys(gaps) if gap not in seen]
        seen.update(fresh)
        if fresh:
            out.append("  not checked:")
            out.extend(f"    {gap}" for gap in fresh)
    if tail:
        out += _render_tail(tail)
    return "\n".join(out)


def _render_tail(tail: "list[TailStep]") -> list[str]:
    """Render the lab-wide steps under one heading, grouped by host, the lab's own last."""
    width = max(len(step.label) for step in tail)
    out = ["after every project"]
    scope: object = object()  # matches no host id and not None, so the first step heads
    for step in tail:
        if step.host_id != scope:
            scope = step.host_id
            out.append(f"  {'lab' if scope is None else scope}")
        out.append(f"    {step.label:<{width}}  {step.line}")
    return out
