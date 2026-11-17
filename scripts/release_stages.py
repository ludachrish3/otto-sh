"""Which release leg each of main's CI jobs proves — the one evidence map.

``make release`` used to replay stages that main's pull-request CI already
ran on the same tree. ``release_record.py pending`` reads main's verdict
(the trusted run); the legs below are the ones the release no longer runs
because a required CI job, or the coverage stamp, proved them. The skip
lines ``pending`` prints and the table in docs/architecture/quality-gates.md
are both rendered from :data:`EVIDENCE`; nothing restates it.

Stdlib only, and imports nothing from the other scripts.
"""

STAMP = "coverage stamp"
"""The evidence for a leg the coverage stamp proves rather than a CI job."""

EVIDENCE: "dict[str, list[str]]" = {
    "Makefile:check-python": ["lint-python", "typecheck-python"],
    "Makefile:lint-arch": ["tests_hostless-3.10"],
    # docs-lint, html -W, sphinx doctest and the source doctests of the first build
    "Makefile:docs@first": ["docs"],
    "noxfile.py:tests_hostless_unit@3.11": ["tests_hostless-3.11"],
    "noxfile.py:tests_hostless_unit@3.12": ["tests_hostless-3.12"],
    "noxfile.py:tests_hostless_unit@3.13": ["tests_hostless-3.13"],
    "noxfile.py:tests_hostless_unit@3.14": ["tests_hostless-3.14"],
    "noxfile.py:tests_all@3.15:hostless": ["tests_hostless-3.15"],
    # chromium is not CI-only evidence: coverage-python's `dashboard` runs it locally
    "Makefile:dashboard-all": ["dashboard-e2e (firefox)", "dashboard-e2e (webkit)"],
    "Makefile:check-ts": ["check-ts"],
    "Makefile:coverage-ts": [STAMP],
}
"""Release leg → the required CI jobs (or :data:`STAMP`) that prove it."""


def skip_lines(run_url: str) -> "list[str]":
    """One ``skipped <leg>: proved by …`` line per leg, in map order."""
    lines = []
    for leg, names in EVIDENCE.items():
        if names == [STAMP]:
            lines.append(f"skipped {leg}: proved by the coverage stamp")
        else:
            lines.append(f"skipped {leg}: proved by {', '.join(names)} in {run_url}")
    return lines


def render_table() -> str:
    """Render the map as the Markdown table pasted verbatim into the quality-gates page."""
    rows = "".join(f"| {leg} | {', '.join(names)} |\n" for leg, names in EVIDENCE.items())
    return "| Release leg | Evidence |\n|---|---|\n" + rows
