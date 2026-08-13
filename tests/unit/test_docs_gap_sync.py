"""The BusyBox gap docs page must say exactly what the gap registry says.

``src/otto/host/userland.py``'s :data:`~otto.host.userland.GAPS` is the single
source of truth for three audiences: the runtime error
(:class:`~otto.host.errors.UnsupportedOnUserlandError`), the parity queue under
``todo/``, and the user-facing page named by
:data:`~otto.host.userland.GAP_DOCS_PAGE`. This module pins the third one.

**BOTH DIRECTIONS, and they catch different mistakes.** A registry entry with
no docs row means otto refuses something the docs never mention — the operator
reads a page that says everything is fine and gets a refusal anyway. A docs row
with no registry entry is the reverse and is the worse of the two: the gap was
CLOSED in code and the page still tells people it is open, so they keep working
around something that works. A one-directional sync test catches one of those
and reports green for the other, which is the shape of half-guard this repo
keeps paying for.

The status is pinned as well as the surface, because "open" versus "closed" is
what the two can disagree about most cheaply: flipping a record from
``measured-broken`` to ``untested`` changes whether otto blocks the call, and a
page still printing the old value is wrong in exactly the way this test exists
to prevent.

WHAT IS DELIBERATELY NOT PINNED: the prose. No assertion can tell whether a
``measured_on`` string is true, and pinning whole paragraphs verbatim would buy
a copying ritual rather than a check — it would redden on a typo fix and stay
green on a lie. The STRUCTURE is compulsory (a row, a section, an anchor, a
status, in the registry's order) and the wording is review's job. The page says
so too, under "Keeping this page true".

VACUITY IS THE HAZARD HERE. Every assertion below is driven by lists parsed out
of a markdown file, and a parser that quietly finds nothing turns half of them
green: "every docs row is a registry gap" is trivially true of zero rows. So
:func:`_table_rows` and :func:`_sections` FAIL LOUDLY when they find nothing,
naming what they were looking for, rather than returning an empty list.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from otto.host.userland import GAP_DOCS_PAGE, GAPS
from tests._fixtures.paths import PROJECT_ROOT

DOCS_PAGE = PROJECT_ROOT / GAP_DOCS_PAGE
"""Resolved through the constant, never spelled out here.

``GAP_DOCS_PAGE`` is what every rendered error message points at, so a page
move has to be one edit. A second hard-coded copy in this file would let the
page move while the error kept pointing at the old path — and this test would
still be green, having checked the wrong file.
"""

DOCS_ROOT = PROJECT_ROOT / "docs"

_TABLE_HEADER = "| Surface | Status | What it means for you |"
"""The exact header row of the table under test.

Matched literally so that renaming the table is a LOUD failure here rather than
a silent one: a parser that hunts for "the first table on the page" would
happily latch onto a different table someone added above it.
"""

# ``| [`shell-transfer-base64`](#shell-transfer-base64) | `measured-broken` | ... |``
_ROW_RE = re.compile(
    r"^\|\s*\[`(?P<surface>[^`]+)`\]\(#(?P<anchor>[^)]+)\)\s*\|\s*`(?P<status>[^`]+)`\s*\|"
)

# ``### shell-transfer-base64`` -- bare, because myst_heading_anchors slugifies
# the heading text and the slug has to come out equal to the surface.
_SECTION_RE = re.compile(r"^###\s+(?P<surface>\S+)\s*$")

# ``**Status:** `measured-broken` -- otto refuses before it sends anything.``
_SECTION_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s+`(?P<status>[^`]+)`")


@dataclass(frozen=True)
class _Row:
    """One parsed row of the docs table."""

    surface: str
    """The surface id, from the row's link text."""

    anchor: str
    """The fragment the row's link points at. Its own surface, if the row is right."""

    status: str
    """``measured-broken`` or ``untested``, as the page prints it."""


def _page_text() -> str:
    """The docs page's contents, or a failure naming the constant that located it."""
    assert DOCS_PAGE.is_file(), (
        f"{GAP_DOCS_PAGE} does not exist. That path is `GAP_DOCS_PAGE` in "
        f"src/otto/host/userland.py, and every `UnsupportedOnUserlandError` raised from "
        f"the registry prints it as the place to read more, so a missing page is a "
        f"dangling link out of a runtime error. Create the page, or move the constant."
    )
    return DOCS_PAGE.read_text()


def _table_rows() -> list[_Row]:
    """Parse the gap table, or fail loudly rather than report zero rows."""
    lines = _page_text().splitlines()
    header_at = next((i for i, line in enumerate(lines) if line.strip() == _TABLE_HEADER), None)
    assert header_at is not None, (
        f"{GAP_DOCS_PAGE} has no row reading exactly `{_TABLE_HEADER}`. That header is "
        f"how this test finds the gap table; if the table was renamed or restructured, "
        f"update `_TABLE_HEADER` (and `_ROW_RE`) deliberately -- do not leave the test "
        f"looking for a table that is no longer there, because it would then check "
        f"nothing at all."
    )

    rows: list[_Row] = []
    for line in lines[header_at + 2 :]:  # +2 skips the header and its `| --- |` rule
        if not line.startswith("|"):
            break
        match = _ROW_RE.match(line)
        assert match is not None, (
            f"{GAP_DOCS_PAGE} has a row in the gap table this test cannot read:\n"
            f"  {line}\n"
            f"Every row must start "
            f"``| [`<surface>`](#<surface>) | `<status>` |``. An unparseable row would "
            f"otherwise vanish from the comparison and be reported as sync."
        )
        rows.append(
            _Row(
                surface=match.group("surface"),
                anchor=match.group("anchor"),
                status=match.group("status"),
            )
        )

    assert rows, (
        f"{GAP_DOCS_PAGE}'s gap table has a header and no rows. Half the assertions in "
        f"this module are 'every docs row ...', which an empty table satisfies without "
        f"checking anything."
    )
    return rows


def _sections() -> dict[str, str]:
    """Map each ``### <surface>`` section to the status it states, failing if there are none."""
    sections: dict[str, str] = {}
    pending: str | None = None
    for line in _page_text().splitlines():
        heading = _SECTION_RE.match(line)
        if heading is not None:
            pending = heading.group("surface")
            sections.setdefault(pending, "")
            continue
        status = _SECTION_STATUS_RE.match(line)
        if status is not None and pending is not None and not sections[pending]:
            sections[pending] = status.group("status")
    assert sections, (
        f"{GAP_DOCS_PAGE} has no `### <surface>` sections. Those headings ARE the "
        f"anchors `Gap.docs_anchor` points at, so a page without them makes every "
        f"registry error message link into nothing."
    )
    return sections


def _registry_statuses() -> dict[str, str]:
    return {gap.surface: gap.status for gap in GAPS}


# ===========================================================================
# The page has to be where the registry says, and reachable once it is there
# ===========================================================================


class TestThePageIsWhereTheRegistrySaysItIs:
    """Before any table can be compared, the file has to exist and be reachable."""

    def test_the_page_named_by_gap_docs_page_exists(self) -> None:
        assert _page_text().strip(), f"{GAP_DOCS_PAGE} exists but is empty"

    def test_the_page_is_named_by_a_docs_index(self) -> None:
        """An orphan page is a silent failure: it renders nowhere and links to nothing.

        Sphinx's own ``-W`` build is the authoritative orphan check, but it runs
        in the docs lane only. This is the cheap unit-lane version: some
        ``index`` under ``docs/`` has to name the page, or nobody navigating the
        rendered site can reach the thing the error messages point at.
        """
        referrers = _index_files_naming_the_page()
        assert referrers, (
            f"No index under docs/ names {GAP_DOCS_PAGE}, so the page is an orphan: it "
            f"is built, warned about under `-W`, and reachable only by URL. Add it to "
            f"the toctree of the index for its area."
        )


def _index_files_naming_the_page() -> list[Path]:
    """Every ``docs/**/index.{rst,md}`` whose body names the page as a toctree entry."""
    page = DOCS_PAGE.resolve()
    referrers: list[Path] = []
    for index in sorted(DOCS_ROOT.rglob("index.*")):
        if index.suffix not in (".rst", ".md"):
            continue
        for line in index.read_text().splitlines():
            entry = line.strip()
            if entry.endswith(">") and "<" in entry:  # ``Title <path/to/page>``
                entry = entry[entry.index("<") + 1 : -1]
            if not entry or " " in entry:
                continue
            for suffix in (".md", ".rst"):
                if (index.parent / f"{entry}{suffix}").resolve() == page:
                    referrers.append(index)
                    break
    return referrers


# ===========================================================================
# Both directions
# ===========================================================================


class TestTheTableAndTheRegistryAgree:
    """The point of the module. Neither side may carry a surface the other lacks."""

    def test_every_registry_gap_has_a_docs_row(self) -> None:
        """Direction 1: code declares it, the page must document it."""
        documented = {row.surface for row in _table_rows()}
        missing = [gap.surface for gap in GAPS if gap.surface not in documented]
        assert not missing, (
            f"{missing} are declared in GAPS (src/otto/host/userland.py) but have no row "
            f"in {GAP_DOCS_PAGE}. otto would refuse a surface the docs never mention, "
            f"and the refusal's `See ...` link would land on a page with nothing to say "
            f"about it. Add a table row and a `### <surface>` section for each."
        )

    def test_every_docs_row_has_a_registry_gap(self) -> None:
        """Direction 2, the worse one: the page must not claim a gap the code closed."""
        declared = _registry_statuses()
        extra = [row.surface for row in _table_rows() if row.surface not in declared]
        assert not extra, (
            f"{extra} have a row in {GAP_DOCS_PAGE} but no record in GAPS "
            f"(src/otto/host/userland.py). Either the gap was closed in code and the "
            f"page still tells people to work around it, or the row is a typo whose "
            f"anchor no error message will ever point at. Delete the row, or add the "
            f"record."
        )

    def test_every_row_prints_the_status_the_registry_declares(self) -> None:
        """`measured-broken` refuses and `untested` does not: the row must not lie about which."""
        declared = _registry_statuses()
        wrong = sorted(
            f"{row.surface}: registry={declared[row.surface]} docs={row.status}"
            for row in _table_rows()
            if row.surface in declared and row.status != declared[row.surface]
        )
        assert not wrong, (
            f"{GAP_DOCS_PAGE} prints a status the registry does not declare: {wrong}. "
            f"The status decides whether otto blocks the call, so a stale one tells a "
            f"reader the opposite of what otto will do."
        )

    def test_the_rows_are_in_the_registry_order(self) -> None:
        """GAPS' own docstring says it is ordered as the docs table renders it."""
        assert [row.surface for row in _table_rows()] == [gap.surface for gap in GAPS], (
            f"{GAP_DOCS_PAGE}'s table is not in GAPS' order. GAPS documents itself as "
            f"'in the order the docs table renders them' and is grouped deliberately "
            f"(the file-moving surfaces, then the command surfaces, then the unknowns); "
            f"reorder whichever of the two moved."
        )


# ===========================================================================
# The anchors, which are what a runtime error hands to a user
# ===========================================================================


class TestTheAnchorsResolve:
    """`Gap.docs_anchor` is printed in an exception. It has to land somewhere."""

    def test_every_gap_has_a_section_to_anchor_at(self) -> None:
        sections = _sections()
        missing = [gap.surface for gap in GAPS if gap.surface not in sections]
        assert not missing, (
            f"{GAP_DOCS_PAGE} has no `### <surface>` section for {missing}, so "
            f"`Gap.docs_anchor` renders a link to a fragment the page does not have. "
            f"With `myst_heading_anchors = 3` the heading text IS the anchor, so the "
            f"heading has to be the bare surface id."
        )

    def test_every_section_is_a_declared_gap(self) -> None:
        declared = _registry_statuses()
        extra = sorted(surface for surface in _sections() if surface not in declared)
        assert not extra, (
            f"{GAP_DOCS_PAGE} has `### <surface>` sections for {extra}, which are not in "
            f"GAPS. A section with no record is a gap the page invented."
        )

    def test_every_row_links_to_its_own_section(self) -> None:
        crossed = sorted(
            f"{row.surface} -> #{row.anchor}" for row in _table_rows() if row.anchor != row.surface
        )
        assert not crossed, (
            f"Rows in {GAP_DOCS_PAGE} link to a section other than their own: {crossed}. "
            f"A copy-pasted row that kept the previous row's anchor reads correctly and "
            f"navigates to the wrong evidence."
        )

    def test_every_section_restates_the_registry_status(self) -> None:
        """The page states each status twice; both statements are pinned, so neither drifts."""
        declared = _registry_statuses()
        wrong = sorted(
            f"{surface}: registry={declared[surface]} section={status or '(no **Status:** line)'}"
            for surface, status in _sections().items()
            if surface in declared and status != declared[surface]
        )
        assert not wrong, (
            f"A `### <surface>` section in {GAP_DOCS_PAGE} states a status the registry "
            f"does not: {wrong}. Each section must open with a "
            f"``**Status:** `<status>` `` line -- it is what a reader arriving from an "
            f"error message's anchor sees first, without the table in view."
        )
