"""Render ``schemas/kgcov_matrix.json`` into ``docs/cli/cov/instrumenting/kgcov-matrix.md``.

Run from the repo root::

    uv run python -m scripts.render_kgcov_matrix           # write the page
    uv run python -m scripts.render_kgcov_matrix --check   # render, check the axes, write nothing

The page states what the last measured ``make kgcov`` observed, cell by
cell, with provenance per column; the phrasing of each state lives here and
never in the artifact (a hand-written sentence beside a machine-written
verdict is the fabrication path the guards close). ``docs/conf.py`` runs
this at ``builder-inited`` for every builder; the page is git-ignored.
"""

import argparse
import datetime as _datetime
import json
import sys
from pathlib import Path

from tests._fixtures.kgcov_matrix import (
    BED,
    BUILD,
    CONTROL_SURFACE,
    MATRIX_PATH,
    MEASURED_BROKEN,
    MEASURED_OK,
    PAGE_PATH,
    UNTESTED_STATUS,
    axes_mismatch,
    validation_errors,
)

# ○ for untested rather than an em dash: the em dash is this page's own
# separator, in the legend and in a provenance row nothing was measured for.
SYMBOL = {MEASURED_OK: "✅", MEASURED_BROKEN: "❌", UNTESTED_STATUS: "○"}
STATE_MEANING = {
    MEASURED_OK: (
        "the contract passed on this compiler in the last measured run, and (bed columns) "
        "the column's control passed in the same run"
    ),
    MEASURED_BROKEN: (
        "the contract failed or errored, or it passed under a control that did not — the "
        "failure summary says which"
    ),
    UNTESTED_STATUS: "no run has drawn this cell; not a claim either way",
}

#: How much of a failure summary a table cell can hold before it crowds the grid out.
SUMMARY_LIMIT = 160


def _esc(text: str) -> str:
    """Escape what a Markdown table row cannot hold literally."""
    return text.replace("|", "\\|").replace("\n", " ")


def _grid(matrix: dict, venue: str) -> "list[str]":
    """One venue's grid: its contracts down the side, its compilers across the top."""
    rows = [s for s in matrix["surfaces"] if s["venue"] == venue]
    cols = [p for p in matrix["profiles"] if p["venue"] == venue]
    out = [
        "| contract | " + " | ".join(f"`{p['id']}`" for p in cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for s in rows:
        cells = []
        for p in cols:
            cell = matrix["cells"][s["id"]][p["id"]]
            token = SYMBOL[cell["status"]]
            if cell["status"] == MEASURED_BROKEN:
                token += f" {_esc(cell['failure_summary'][:SUMMARY_LIMIT])}"
            cells.append(token)
        label = f"{_esc(s['title'])}" + (" *(control)*" if s["control"] else "")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return out


def _control_caveats(matrix: dict) -> "list[str]":
    """Name every bed column holding ✅ cells above a control that is not ✅ itself.

    A run's verdicts bind only the cells that run drew; every other cell is
    copied forward from the previous artifact. So a partial hand run can write
    this column's control ❌ and leave its other cells ✅ from an older run.
    The provenance table shows the mismatch; this says what it means.
    """
    out = []
    for p in matrix["profiles"]:
        if p["venue"] != BED:
            continue
        cells = {
            s["id"]: matrix["cells"][s["id"]][p["id"]]
            for s in matrix["surfaces"]
            if s["venue"] == BED
        }
        control = cells[CONTROL_SURFACE.id]
        if control["status"] == MEASURED_OK:
            continue
        if not any(c["status"] == MEASURED_OK for s, c in cells.items() if s != CONTROL_SURFACE.id):
            continue
        out.append(
            f"- `{p['id']}`: this column's {SYMBOL[MEASURED_OK]} cells stand on a control that "
            f"did not pass in the run that last drew them — they were measured earlier and "
            f"copied forward. A full `make kgcov` re-measures them."
        )
    return ["", *out] if out else []


def _provenance(matrix: dict) -> "list[str]":
    """Say what each column's verdicts were measured with, read off the cells themselves."""
    out = [
        "| column | venue | compiler version | kernel release | as of | control |",
        "|---|---|---|---|---|---|",
    ]
    for p in matrix["profiles"]:
        measured = [
            c
            for s in matrix["surfaces"]
            if s["venue"] == p["venue"]
            for c in [matrix["cells"][s["id"]][p["id"]]]
            if c["status"] != UNTESTED_STATUS
        ]
        if not measured:
            out.append(f"| `{p['id']}` | {p['venue']} | — | — | — | — |")
            continue
        versions = sorted({c["compiler_version"] for c in measured})
        releases = sorted({c["kernel_release"] for c in measured})
        as_of = max(c["as_of"] for c in measured)
        if p["venue"] == BED:
            control = matrix["cells"][CONTROL_SURFACE.id][p["id"]]
            control_text = f"{SYMBOL[control['status']]} {control['status']}"
        else:
            control_text = "none — a build-only column proves what a build can prove"
        out.append(
            f"| `{p['id']}` | {p['venue']} | {', '.join(versions)} | "
            f"{', '.join(releases)} | {as_of} | {control_text} |"
        )
    return out


def render(matrix: dict, *, rendered_on: "_datetime.date | None" = None) -> str:
    """Compose the whole page, as Markdown."""
    rendered_on = rendered_on or _datetime.datetime.now(tz=_datetime.timezone.utc).date()
    lines = [
        "(kgcov-matrix)=",
        "# otto_kgcov compatibility matrix",
        "",
        (
            "<!-- GENERATED by scripts/render_kgcov_matrix.py from"
            " schemas/kgcov_matrix.json; do not edit -->"
        ),
        "",
        "What the last measured `make kgcov` run observed, per compiler and per contract, on",
        "this project's dev VM: its unix bed hosts for the bed columns and its prepared x86_64",
        "kernel source tree for the build column, with the compilers installed there. A cell",
        "is written only by a run (`scripts/collate_kgcov_matrix.py`), carries the compiler",
        "version, kernel release and date it was measured with, and a lost verdict stops",
        "`make release`. GCC and clang parity is read off identical rows: the `clang` column",
        "against each `gcc-N` column.",
        "",
        "What it does not claim: the compiler range the",
        "[kernel-modules page](kernel-modules.md#another-kernel-isa-or-compiler) states is the",
        "vendored table's own claim, and compilers this VM cannot run are not measured here",
        "and have no column.",
        "",
        "## Bed columns",
        "",
        "Each column's library and demo were built with that compiler for the bed's running",
        "kernel, loaded on the bed, and driven through `otto test --cov`. The *control* row is",
        "the column's positive control: the library refusing a demo from another compiler.",
        "A bed cell is ✅ only when that control passed in the same run.",
        "",
        *_grid(matrix, BED),
        *_control_caveats(matrix),
        "",
        "## Build column",
        "",
        "The x86_64 cross build from the kernel source tree; built, never loaded. It has no",
        "control, and its cells prove what a build can prove.",
        "",
        *_grid(matrix, BUILD),
        "",
        "## Provenance",
        "",
        *_provenance(matrix),
        "",
        "## Legend",
        "",
        *(f"- {SYMBOL[state]} `{state}` — {meaning}" for state, meaning in STATE_MEANING.items()),
        "",
        (
            f"Rendered {rendered_on.isoformat()} from `schemas/kgcov_matrix.json` "
            f"(format {matrix['format']})."
        ),
        "",
    ]
    return "\n".join(lines)


def main(argv: "list[str]") -> int:
    """Render the page, or check the artifact and write nothing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument("--output", type=Path, default=PAGE_PATH)
    parser.add_argument(
        "--check", action="store_true", help="render and check the axes; write nothing"
    )
    args = parser.parse_args(argv)
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    problems = validation_errors(matrix) + axes_mismatch(matrix)
    if problems:
        print(
            "\n".join(
                [
                    "kgcov matrix: the artifact disagrees with its schema or the tree's axes:",
                    *problems,
                ]
            ),
            file=sys.stderr,
        )
        return 1
    page = render(matrix)
    if args.check:
        print(f"kgcov matrix: {args.matrix} renders ({len(page.splitlines())} lines); axes agree")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"kgcov matrix: wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
