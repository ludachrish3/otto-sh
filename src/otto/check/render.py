"""The stdout table every otto check prints through (spec 2026-09-24 §3.5).

Both ``otto link check`` and ``otto tunnel check`` build their results into
``CheckSection`` objects and hand them to :func:`~otto.check.render_sections`: one
heading and table per host/interface pair, with evidence (the commands
otto ran, what a rejected tool said, a doc hint) folded into extra rows
under the feature that needs it, so the shape a user pastes into an issue
is exactly what they saw on their terminal.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from .verdict import FeatureResult, Verdict, count_verdicts

if TYPE_CHECKING:
    from .fingerprint import HostFingerprint

_SHARED_HINT_MIN_CELLS = 2
"""A shared hint across one non-passing cell is nothing to collapse — it already reads as itself."""

RAN_LINE_CAP = 300
"""The longest a ``ran:`` line prints on screen, in characters, before it is cut.

A probe script can run to 700 characters, a dozen wrapped lines of shell in
the middle of the table. Cut, the line keeps its start (the tool and the
address it aimed at) and says how much is missing and where the whole
command is: ``-v`` prints it uncut, and ``--report`` always has it. 300 keeps
``otto link check``'s longest socat probe (its rate transfer, about 255
characters with its ``live ran:`` prefix) whole."""


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


def _with_detail(body: str | None, detail: str | None) -> str:
    """Join *body* and *detail* as ``body — detail``; either alone, or ``""`` for neither.

    A *detail* that only repeats *body* is not said twice.
    """
    if body and detail and detail != body:
        return f"{body} — {detail}"
    return body or detail or ""


def _cell_text(cell: FeatureResult) -> str:
    """One cell's own detail-column text (rendering rules, spec 2026-09-24 §3.5).

    A cell says what it measured first (``measured … , want …`` when it is not
    a pass), then `` — `` and its detail, so a caveat a pass carries (a
    handshake that proves no payload) or the cause a fail names (the tunnel
    that holds a port) is never hidden behind the measurement. An
    ``unmeasured`` cell's text always LEADS with its reason code, whatever
    else it carries (``noisy-baseline: measured loss 20%, …``): the code is
    what a reader looks up on the verdicts page, so it must never be the part
    a measurement crowds out.
    """
    if cell.verdict is Verdict.PASS:
        return _with_detail(cell.measured, cell.detail)
    body = _with_detail(_measured_wanted(cell), cell.detail)
    if cell.reason is not None:
        return f"{cell.reason.value}: {body}" if body else cell.reason.value
    return body


@dataclass(frozen=True)
class _ColumnText:
    """One column's own detail text within a row."""

    column: str
    text: str


@dataclass(frozen=True)
class RowDetail:
    """A row's ``detail`` column text, and the per-column lines printed under it."""

    text: str
    continuation: list[str] = field(default_factory=list)
    """``<col> <text>`` for each column after the first, when the columns disagree."""


def _row_texts(columns: list[str], cells: list[FeatureResult | None]) -> list[_ColumnText]:
    """Every cell of *cells* with non-empty text, in column order."""
    texts = []
    for column, cell in zip(columns, cells, strict=True):
        if cell is None:
            continue
        text = _cell_text(cell)
        if text:
            texts.append(_ColumnText(column, text))
    return texts


def _row_detail(columns: list[str], cells: list[FeatureResult | None]) -> RowDetail:
    """Build the row's ``detail`` column text, and any per-column continuation lines.

    Every non-``None`` cell's own text is computed (:func:`_cell_text`); when
    they all agree (or there is only one column), the row prints that text
    once, unprefixed. Otherwise each cell's ``<col> <text>`` prints on its
    own line: the first on the row itself, the rest as continuation lines
    (emitted like evidence rows, before them), so two columns that both pass
    with different values (e.g. tcp/udp byte counts) are never silently
    collapsed to one.
    """
    texts = _row_texts(columns, cells)
    if not texts:
        return RowDetail("")
    if len({t.text for t in texts}) == 1:
        return RowDetail(texts[0].text)
    first, *rest = texts
    return RowDetail(f"{first.column} {first.text}", [f"{t.column} {t.text}" for t in rest])


def proven_range_line(fp: "HostFingerprint", labels: dict[str, str], order: list[str]) -> str:
    """One host's ``proven range: …`` line: each of *order*'s components and its label.

    *labels* maps a component to its :class:`~otto.check.RangeLabel` value. A
    version-only component (``kernel``, ``socat``, ``iproute2``, …) leads with
    its own name; ``isa`` and ``userland`` have a value worth reading on its
    own, so they lead with it instead (``aarch64 within``, not ``isa within``).
    """
    lead = {"isa": fp.isa or "isa", "userland": fp.userland}
    parts = [f"{lead.get(name, name)} {labels[name]}" for name in order]
    return f"proven range: {' · '.join(parts)}"


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


def _ran_line(prefix: str, command: str, *, verbose: bool) -> str:
    """One ``ran:`` line, cut at :data:`RAN_LINE_CAP` unless *verbose*."""
    line = f"{prefix}ran: {command}"
    if verbose or len(line) <= RAN_LINE_CAP:
        return line
    cut = len(line) - RAN_LINE_CAP
    return f"{line[:RAN_LINE_CAP]} … (+{cut} chars; full command with -v or in --report)"


def _ran_said_lines(cell: FeatureResult, prefix: str, *, verbose: bool) -> list[str]:
    """``ran:`` / ``said:`` lines for one non-passing cell (its own ``hint:`` is separate).

    *prefix* is the column name (``"live "``) on a multi-column section, or
    ``""`` on a single-column one, so a reader can tell which column an
    evidence line belongs to once ``--live`` adds a second one.
    """
    lines = [_ran_line(prefix, cmd, verbose=verbose) for cmd in cell.commands]
    if cell.verdict is Verdict.UNSUPPORTED and cell.output:
        stripped = cell.output.strip()
        if stripped:
            lines.append(f"{prefix}said: {stripped.splitlines()[-1]}")
    return lines


def _row_shared_hint(row: CheckRow) -> str | None:
    """Return the hint every non-passing cell of *row* shares, or ``None``.

    A cascade of skipped rows downstream of one failed segment gives every
    column's cell the identical hint; printed once, unprefixed, it reads as
    the one cause it is instead of a near-duplicate line per column (an
    18-line wall for a 2-column, ~10-row cascade otherwise). One non-passing
    cell alone is not a cascade — its own ``<col> hint: …`` line already says
    which column it is.
    """
    non_pass = [cell for cell in row.cells if cell is not None and cell.verdict is not Verdict.PASS]
    if len(non_pass) < _SHARED_HINT_MIN_CELLS:
        return None
    hints = {cell.hint for cell in non_pass}
    if len(hints) != 1:
        return None
    [hint] = hints
    return hint


def _output_block_lines(cell: FeatureResult, prefix: str) -> list[str]:
    """``output:`` plus each indented output line, for one cell that has output."""
    output = cell.output or ""
    return [f"{prefix}output:", *(f"  {line}" for line in output.splitlines())]


@dataclass(frozen=True)
class _Evidence:
    """The lines printed under one row, in order, and which of them are ``hint:`` lines."""

    lines: list[str]
    hints: list[str]


def _row_evidence(
    section: CheckSection, row: CheckRow, shown_hint: str | None, *, verbose: bool
) -> _Evidence:
    """Each non-passing cell's ``ran:``/``said:``/``hint:`` lines, then a row-shared hint.

    A hint the section already printed under its heading (*shown_hint*) is
    not repeated on the row.
    """
    multi_column = len(section.columns) > 1
    row_hint = _row_shared_hint(row)
    lines: list[str] = []
    hints: list[str] = []
    for column, cell in zip(section.columns, row.cells, strict=True):
        if cell is None or cell.verdict is Verdict.PASS:
            continue
        prefix = f"{column} " if multi_column else ""
        lines += _ran_said_lines(cell, prefix, verbose=verbose)
        if row_hint is None and cell.hint and cell.hint != shown_hint:
            hints.append(f"{prefix}hint: {cell.hint}")
            lines.append(hints[-1])
    if row_hint is not None and row_hint != shown_hint:
        hints.append(f"hint: {row_hint}")
        lines.append(hints[-1])
    return _Evidence(lines, hints)


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
    blank = [""] * len(section.columns)
    previous_hints: list[str] = []
    for row in section.rows:
        cell_words = [cell.verdict.value if cell is not None else "n/a" for cell in row.cells]
        detail = _row_detail(section.columns, row.cells)
        table.add_row(row.label, *cell_words, detail.text)
        for line in detail.continuation:
            table.add_row("", *blank, line)
        evidence = _row_evidence(section, row, shown_hint, verbose=verbose)
        # The hint(s) the row before just printed, whatever their number, are
        # not printed again: a cascade of skipped rows says its one cause (or,
        # with two columns failing for different causes, its one pair of
        # causes) once, under the first.
        repeat = bool(evidence.hints) and evidence.hints == previous_hints
        previous_hints = evidence.hints
        already_shown = set(evidence.hints) if repeat else set()
        for line in evidence.lines:
            if line not in already_shown:
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
