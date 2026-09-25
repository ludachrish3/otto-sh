"""The stdout table every otto check prints through (spec 2026-09-24 §3.5).

Both ``otto link check`` and ``otto tunnel check`` build their results into
``CheckSection`` objects and hand them to :func:`~otto.check.render_sections`: one
heading and table per host/interface pair, with evidence (the commands
otto ran, what a rejected tool said, a doc hint) folded into extra rows
under the feature that needs it, so the shape a user pastes into an issue
is exactly what they saw on their terminal.
"""

from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from .verdict import FeatureResult, Verdict, count_verdicts


@dataclass(frozen=True)
class CheckRow:
    """One feature's result across every column of a section (sandbox, live, …)."""

    label: str
    cells: list[FeatureResult | None]


@dataclass(frozen=True)
class CheckSection:
    """One host/interface pair's table: heading, subheadings, columns and rows."""

    heading: str
    subheadings: list[str]
    columns: list[str]
    rows: list[CheckRow]
    summary_name: str
    legend: list[str] = field(default_factory=list)


def section_counts(section: CheckSection) -> dict[Verdict, int]:
    """Count every non-``None`` cell in *section*, by verdict."""
    cells = [cell for row in section.rows for cell in row.cells if cell is not None]
    return count_verdicts(cells)


def _measured_wanted(cell: FeatureResult) -> str | None:
    if cell.measured is not None and cell.wanted is not None:
        return f"measured {cell.measured}, want {cell.wanted}"
    if cell.measured is not None:
        return f"measured {cell.measured}"
    if cell.wanted is not None:
        return f"want {cell.wanted}"
    return None


def _row_detail(target: FeatureResult | None, first_cell: FeatureResult | None) -> str:
    """Build the row's ``detail`` column text (rendering rules, spec 2026-09-24 §3.5).

    An ``unmeasured`` cell's detail always LEADS with its reason code, whatever
    else it carries (``noisy-baseline: measured loss 20%, …``): the code
    is what a reader looks up on the verdicts page, so it must never be the
    part a measurement crowds out.
    """
    if target is None:
        # A passing row still says what it measured, or a caveat it carries.
        if first_cell is None:
            return ""
        return first_cell.measured or first_cell.detail or ""
    body = _measured_wanted(target) or target.detail
    if target.reason is not None:
        return f"{target.reason.value}: {body}" if body else target.reason.value
    return body or ""


def shared_hint(section: CheckSection) -> str | None:
    """Return the hint EVERY cell of *section* carries, or ``None``.

    A cause that stops a whole host (no root, no namespaces) gives every row
    the same hint; printed once under the heading, it reads as the one cause
    it is rather than as the same line after every row. The rows keep it, so
    ``--report`` still has it on each one.
    """
    cells = [cell for row in section.rows for cell in row.cells if cell is not None]
    hints = {cell.hint for cell in cells}
    if len(cells) <= 1 or len(hints) != 1:
        return None
    [hint] = hints
    return hint


def _evidence_lines(cell: FeatureResult, prefix: str, *, shown_hint: str | None) -> list[str]:
    """``ran:`` / ``said:`` / ``hint:`` lines for one non-passing cell.

    *prefix* is the column name (``"live "``) on a multi-column section, or
    ``""`` on a single-column one, so a reader can tell which column an
    evidence line belongs to once ``--live`` adds a second one. A hint equal
    to *shown_hint* was already printed under the heading and is not repeated.
    """
    lines = [f"{prefix}ran: {cmd}" for cmd in cell.commands]
    if cell.verdict is Verdict.UNSUPPORTED and cell.output:
        stripped = cell.output.strip()
        if stripped:
            lines.append(f"{prefix}said: {stripped.splitlines()[-1]}")
    if cell.hint and cell.hint != shown_hint:
        lines.append(f"{prefix}hint: {cell.hint}")
    return lines


def _output_block_lines(cell: FeatureResult, prefix: str) -> list[str]:
    """``output:`` plus each indented output line, for one cell that has output."""
    output = cell.output or ""
    return [f"{prefix}output:", *(f"  {line}" for line in output.splitlines())]


def _add_section(console: Console, section: CheckSection, *, verbose: bool) -> None:
    console.print(section.heading, markup=False, highlight=False)
    for subheading in section.subheadings:
        console.print(subheading, markup=False, highlight=False)
    shown_hint = shared_hint(section)
    if shown_hint is not None:
        console.print(f"hint: {shown_hint}", markup=False, highlight=False)
    console.print()

    table = Table(
        "feature",
        *section.columns,
        "detail",
        box=None,
        pad_edge=False,
        padding=(0, 2, 0, 0),
    )
    multi_column = len(section.columns) > 1
    for row in section.rows:
        target = next(
            (c for c in row.cells if c is not None and c.verdict is not Verdict.PASS), None
        )
        first_cell = next((c for c in row.cells if c is not None), None)
        cell_words = [cell.verdict.value if cell is not None else "n/a" for cell in row.cells]
        table.add_row(row.label, *cell_words, _row_detail(target, first_cell))
        blank = [""] * len(section.columns)
        for column, cell in zip(section.columns, row.cells, strict=True):
            if cell is None or cell.verdict is Verdict.PASS:
                continue
            prefix = f"{column} " if multi_column else ""
            for line in _evidence_lines(cell, prefix, shown_hint=shown_hint):
                table.add_row("", *blank, line)
        if verbose:
            for column, cell in zip(section.columns, row.cells, strict=True):
                if cell is None or not cell.output:
                    continue
                prefix = f"{column} " if multi_column else ""
                for line in _output_block_lines(cell, prefix):
                    table.add_row("", *blank, line)
    console.print(table, markup=False, highlight=False)

    for line in section.legend:
        console.print(line, markup=False, highlight=False)

    counts = section_counts(section)
    parts = [f"{count} {verdict.value}" for verdict, count in counts.items() if count]
    console.print(f"{section.summary_name}: {' · '.join(parts)}", markup=False, highlight=False)


def render_sections(
    console: Console, sections: list[CheckSection], *, verbose: bool = False
) -> None:
    """Print every section: heading, subheadings, table, legend, summary."""
    for index, section in enumerate(sections):
        if index:
            console.print()
        _add_section(console, section, verbose=verbose)
