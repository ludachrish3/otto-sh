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

**THE PATHS ARE PINNED TOO, and the ``OPEN`` ones are the point.** A record's
:attr:`~otto.host.userland.Gap.paths` say where otto touches the surface and what
is true of it there, and an ``OPEN`` path is a hole otto has found and not
closed. A hole recorded only in the source is a hole the operator reading this
page never learns about — they read a section that says otto refuses and get a
silent truncation on the path they happened to take. So every open path has to
appear on the page, and (the other direction, and the worse one again) every path
the page prints has to be a declared one in the declared state, or the page can
claim a hole otto closed. The COUNTS are pinned as well, against
:func:`~otto.host.userland.gap_path_totals` and
:func:`~otto.host.userland.wired_guards`, because a hand-maintained number was
what the path data replaced.

VACUITY IS THE HAZARD HERE. Every assertion below is driven by lists parsed out
of a markdown file, and a parser that quietly finds nothing turns half of them
green: "every docs row is a registry gap" is trivially true of zero rows. So
:func:`_table_rows`, :func:`_sections`, :func:`_path_bullets` and
:func:`_state_counts` FAIL LOUDLY when they find nothing, naming what they were
looking for, rather than returning an empty list.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from otto.host.userland import (
    GAP_DOCS_PAGE,
    GAPS,
    PATH_OPEN,
    gap_path_totals,
    wired_guards,
)
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

# ``- `otto.host.session.SessionManager.run_cmd` — **WIRED** by {func}`...`: ...``
#
# Anchored on the bullet so a path claim cannot hide inside a paragraph, and the
# state is matched as bold caps so it reads the same to a human as to this regex.
# Everything after the state is free prose: the DETAIL is deliberately not pinned,
# for the same reason the reasons and measurements are not -- see the module
# header. What is compulsory is that the site and the state on the page are the
# site and the state in the record.
_PATH_BULLET_RE = re.compile(
    r"^-\s+`(?P<site>otto\.[A-Za-z0-9_.]+)`\s+—\s+\*\*(?P<state>[A-Z_]+)\*\*"
)

# ``| `WIRED` | 4 |`` -- the count table under "Where otto consults this table".
_STATE_COUNT_RE = re.compile(r"^\|\s*`(?P<state>[A-Z_]+)`\s*\|\s*(?P<count>\d+)\s*\|")

_GUARD_COUNT_RE = re.compile(r"through \*\*(?P<count>\d+)\*\* guard functions")
"""How the page states the number of distinct guards, in one checkable form.

Written as a bold digit rather than a word so it can be read by an assertion. An
English number word on this page would be a number no test can see, which is
exactly what the path data exists to abolish.
"""


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


@dataclass(frozen=True)
class _PathBullet:
    """One parsed ``- `site` — **STATE**`` bullet, and the section it sits in."""

    surface: str
    """The ``### <surface>`` section the bullet was found under."""

    site: str
    """The dotted call site, from the bullet's leading code span."""

    state: str
    """``WIRED`` / ``PROBE_REFUSED`` / ``PROTECTED`` / ``OPEN``, as the page prints it."""


def _path_bullets() -> list[_PathBullet]:
    """Every path bullet on the page, tagged with its section. Fails if there are none.

    Bullets outside any ``### <surface>`` section are ignored rather than
    guessed at: a path claim belongs to a record, and a homeless one would be
    compared against nothing.
    """
    bullets: list[_PathBullet] = []
    pending: str | None = None
    for line in _page_text().splitlines():
        heading = _SECTION_RE.match(line)
        if heading is not None:
            pending = heading.group("surface")
            continue
        match = _PATH_BULLET_RE.match(line)
        if match is not None and pending is not None:
            bullets.append(
                _PathBullet(
                    surface=pending,
                    site=match.group("site"),
                    state=match.group("state"),
                )
            )
    assert bullets, (
        f"{GAP_DOCS_PAGE} has no path bullets at all. Every assertion about paths below "
        f"is 'every docs bullet ...' or compares two sets, and an empty page satisfies "
        f"half of them without checking anything. Each `measured-broken` section must "
        f"carry a `- ``<site>`` — **<STATE>**` bullet per declared path."
    )
    return bullets


def _state_counts() -> dict[str, int]:
    """The page's own per-state path counts, or a failure naming the table.

    Read off the page rather than recomputed, obviously — the whole point is to
    compare the page's numbers with the registry's derived ones.
    """
    counts: dict[str, int] = {}
    for line in _page_text().splitlines():
        match = _STATE_COUNT_RE.match(line)
        if match is not None:
            counts[match.group("state")] = int(match.group("count"))
    assert counts, (
        f"{GAP_DOCS_PAGE} has no `| `<STATE>` | <n> |` count rows. Those rows are the "
        f"page's only statement of how many call sites are wired and how many are still "
        f"open, and they are the replacement for a number a human used to retype. Without "
        f"them this test compares nothing and the page is free to drift."
    )
    return counts


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


# ===========================================================================
# The paths, both directions -- and the OPEN ones are why this exists
# ===========================================================================


class TestEveryOpenPathIsVisibleToAReader:
    """Direction 1, and the one that protects the operator rather than the table.

    An ``OPEN`` path is a hole otto has FOUND and not closed. Recorded only in
    the source, it is a hole the person reading this page never learns about:
    they read a section saying otto refuses, take the one path that does not, and
    get the silent failure the record describes. So the page has to name every
    one of them.
    """

    def test_every_open_path_appears_on_the_page(self) -> None:
        documented = {(b.surface, b.site) for b in _path_bullets()}
        missing = sorted(
            f"{gap.surface}: {path.site}"
            for gap in GAPS
            for path in gap.open_paths
            if (gap.surface, path.site) not in documented
        )
        assert not missing, (
            f"{missing} are declared OPEN in GAPS (src/otto/host/userland.py) and appear "
            f"nowhere in {GAP_DOCS_PAGE}. An open path is a hole otto knows about; left off "
            f"this page it is a hole only the table knows about, and the reader arriving "
            f"from a refusal's `See ...` link is told the surface is handled. Add a "
            f"`- ``<site>`` — **OPEN**: <why>` bullet to that surface's section."
        )

    def test_the_page_agrees_that_they_are_open(self) -> None:
        """Naming the site is not enough; a hole listed as `WIRED` reads as closed."""
        on_page = {(b.surface, b.site): b.state for b in _path_bullets()}
        wrong = sorted(
            f"{gap.surface}: {path.site} registry=OPEN docs={on_page[(gap.surface, path.site)]}"
            for gap in GAPS
            for path in gap.open_paths
            if (gap.surface, path.site) in on_page
            and on_page[(gap.surface, path.site)] != PATH_OPEN
        )
        assert not wrong, (
            f"{GAP_DOCS_PAGE} prints a state other than OPEN for a path the registry "
            f"declares open: {wrong}. The state is what tells a reader whether they are "
            f"protected on that path."
        )

    def test_a_surface_with_an_open_path_does_not_read_as_covered(self) -> None:
        """The **Status:** line must not claim more than the paths support.

        A surface can be guarded where ``Host.run()`` reaches it and open on a
        named session, and a **Status:** line that says only "otto refuses" is
        then true of one path and false of another. The rule pinned here is
        cheap and hard to argue with: if any path is open, the section has to
        contain the word ``OPEN`` somewhere -- which the paths list supplies -- so
        the reader cannot leave with "covered" as the only impression.
        """
        bullets_by_surface: dict[str, list[str]] = {}
        for bullet in _path_bullets():
            bullets_by_surface.setdefault(bullet.surface, []).append(bullet.state)
        silent = sorted(
            gap.surface
            for gap in GAPS
            if gap.open_paths and PATH_OPEN not in bullets_by_surface.get(gap.surface, [])
        )
        assert not silent, (
            f"{silent} have OPEN paths and their sections in {GAP_DOCS_PAGE} state no OPEN "
            f"path at all, so the section reads as covered. A surface with any open path "
            f"must not."
        )


class TestThePageInventsNoPath:
    """Direction 2, the worse one again: the page must not claim what the code does not.

    A bullet with no record behind it is a call site someone renamed, deleted, or
    never had — and if it says ``OPEN``, readers keep working around a hole that
    is not there; if it says ``WIRED``, they believe they are protected on a path
    nothing guards.
    """

    def test_every_bullet_belongs_to_a_declared_gap(self) -> None:
        declared = _registry_statuses()
        extra = sorted({b.surface for b in _path_bullets() if b.surface not in declared})
        assert not extra, (
            f"{GAP_DOCS_PAGE} carries path bullets under {extra}, which are not in GAPS."
        )

    def test_every_bullet_is_a_declared_path_in_its_declared_state(self) -> None:
        declared = {(gap.surface, path.site): path.state for gap in GAPS for path in gap.paths}
        wrong: list[str] = []
        for bullet in _path_bullets():
            key = (bullet.surface, bullet.site)
            if key not in declared:
                wrong.append(f"{bullet.surface}: {bullet.site} is on the page but not in GAPS")
            elif declared[key] != bullet.state:
                wrong.append(
                    f"{bullet.surface}: {bullet.site} registry={declared[key]} docs={bullet.state}"
                )
        assert not wrong, (
            f"{GAP_DOCS_PAGE} and GAPS (src/otto/host/userland.py) disagree about paths: "
            f"{sorted(wrong)}. Either the record moved and the page did not, or the page "
            f"names a call site otto no longer has -- and a stale `WIRED` bullet tells a "
            f"reader they are protected where nothing guards them."
        )

    def test_every_declared_path_appears_exactly_once(self) -> None:
        """Not only the open ones: a wired path left off the page understates otto too.

        And a path listed twice lets an editor fix one copy, which is how a page
        ends up disagreeing with itself.
        """
        on_page = [(b.surface, b.site) for b in _path_bullets()]
        declared = [(gap.surface, path.site) for gap in GAPS for path in gap.paths]
        assert sorted(on_page) == sorted(declared), (
            f"the set of paths in {GAP_DOCS_PAGE} is not the set in GAPS.\n"
            f"  only on the page: {sorted(set(on_page) - set(declared))}\n"
            f"  only in GAPS:     {sorted(set(declared) - set(on_page))}\n"
            f"  duplicated on the page: "
            f"{sorted({p for p in on_page if on_page.count(p) > 1})}"
        )


class TestThePagesNumbersAreTheRegistrysNumbers:
    """No number on this page may be one a human maintains.

    The comment block these records replaced said "EXACTLY THREE, and the count
    is the point", after saying "none yet", then "exactly one", then "exactly
    two" — and it was also counting guard functions while reading as though it
    counted call sites. Both counts are derived now, they differ, and both are
    pinned here.
    """

    def test_the_state_counts_match_the_registry(self) -> None:
        assert _state_counts() == gap_path_totals(), (
            f"{GAP_DOCS_PAGE}'s per-state counts are not "
            f"`otto.host.userland.gap_path_totals()`. Update the count table -- or, if a "
            f"state is genuinely gone from the table, its row and its entry in the legend "
            f"above it."
        )

    def test_the_guard_count_matches_the_registry(self) -> None:
        match = _GUARD_COUNT_RE.search(_page_text())
        assert match is not None, (
            f"{GAP_DOCS_PAGE} no longer states how many guard functions the wired paths "
            f"reach `refuse_if_gapped` through, in the form `through **<n>** guard "
            f"functions`. That sentence is the one that keeps 'four wired paths' and "
            f"'three guards' from being read as the same number; without it this "
            f"assertion checks nothing, so it fails rather than passing quietly."
        )
        assert int(match.group("count")) == len(wired_guards()), (
            f"{GAP_DOCS_PAGE} says the wired paths go through {match.group('count')} guard "
            f"functions; `otto.host.userland.wired_guards()` finds {len(wired_guards())} "
            f"({wired_guards()})."
        )

    def test_the_two_counts_are_not_the_same_number(self) -> None:
        """The non-vacuity guard for the pair above, and a real property of the data.

        Were paths and guards ever one-to-one, the two assertions above would be
        indistinguishable and the page's careful "on purpose" sentence would be
        noise. They are not one-to-one today because ``read_file`` and
        ``write_file`` share a guard — and if that ever stops being true, this
        reds and the sentence should go rather than quietly become false.
        """
        assert gap_path_totals()["WIRED"] != len(wired_guards()), (
            "every wired path now has its own guard, so the page's sentence about the two "
            "numbers differing 'on purpose' is no longer about anything. Reword it and "
            "delete this test, deliberately."
        )
