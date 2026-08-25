"""Fold a conformance run's observation records into ``schemas/support_matrix.json``.

**THE ONLY WRITER OF A ``measured-*`` VERDICT** (spec 2026-08-22 §5). Everything
else in this item may write ``untested``: ``tests/_fixtures/support_matrix.py``'s
``rewrite_matrix_axes`` adds and removes CELLS as the tree's surfaces and
profiles change, and it copies existing verdicts across rather than minting
them. A verdict enters the artifact here or nowhere, and
``tests/unit/test_support_matrix.py`` holds that structurally: every
``measured-*`` cell in the committed artifact must be REPRODUCIBLE from the
records this reads, so a hand-edit claiming a verdict with no backing
observation fails the guard rather than being caught by review.

Usage, from the repo root::

    uv run python -m scripts.collate_support_matrix           # report only, writes nothing
    uv run python -m scripts.collate_support_matrix --write   # fold the records in
    uv run python -m scripts.collate_support_matrix --records DIR --matrix FILE

``-m`` and not a path, so that the repo root -- and with it the ``tests``
package this reads the axes and the record format from -- is on ``sys.path``
because python put it there. Run as a path, ``sys.path[0]`` is ``scripts/`` and
the imports below need a bootstrap that has to sit ABOVE them, which is three
``# noqa: E402`` suppressions for a problem the invocation shape does not have.

THIS SCRIPT NEVER COMMITS, AND CI NEVER COMMITS. Nothing here runs ``git``, and
nothing here runs in CI's committing path -- the collate step hangs off
``make conformance-bed``, which no workflow invokes and which only this dev VM
can run. The default is a REPORT: writing takes ``--write``, so a run that was
only meant to look cannot quietly move a verdict.

WHAT COMMITS IS ``make release-matrix``, the release's re-measure stage, and it
commits only what nobody needs to see: an improvement, a new working cell, or
evidence that moved under an unchanged verdict. A new ``measured-broken``, or a
lost ``measured-ok``, is refused by ``scripts/check_matrix_downgrades.py`` and
stops the release. So a person still stands in front of every cell whose verdict
gets WORSE, and the diff is still what they stand in front of -- they are simply
no longer called for the cells where the answer could not matter.

═══════════════════════════════════════════════════════════════════════════
THE FOUR RULES, and why each is structural rather than a convention
═══════════════════════════════════════════════════════════════════════════

**1. HERMETIC OBSERVATIONS ARE DISCARDED, LOUDLY** (Chris's ruling, 2026-08-24).
The matrix's profile axis is built from the BED labs' 16 elements. The hermetic
venue's cells carry element names (``local``, ``loopback``,
``busybox-1.16.1``...) that are in no lab at all -- ``axes_for('local')`` raises
``KeyError`` -- so every hermetic record names an element no profile column
holds and its ``profile`` is ``null``. It can populate NOTHING. This step
therefore drops them and SAYS SO, with the count and the reason: a collator that
silently discarded 48 records would look exactly like a broken one, and the next
person could not tell which it was.

Consequence, stated because a reader comparing spec to artifact must not have to
guess: **§5's CI path is deliberately NOT implemented.** "The collate step also
accepts observation artifacts downloaded from the nightly ``conformance-hermetic``
job, recording ``venue: ci-hermetic``" is a spec requirement this item
consciously does not meet. There is no profile for a hermetic cell to land in,
so accepting those artifacts would mean inventing one. ``ci-hermetic`` remains a
legal ``venue`` in the schema and no code path produces it.

**2. ABSENCE NEVER DOWNGRADES A CELL.** A run that did not draw a cell says
NOTHING about it. Only an observation may change a verdict, so a cell with no
records this collation is left BYTE-IDENTICAL -- not reset, not re-dated, not
touched. This is not a nicety: the bed lane at its default budget draws a
fraction of the space, so a collator that reset undrawn cells would destroy the
matrix on every sampled run and the destruction would look like ordinary churn.
The records directory is deliberately NOT cleared between runs for the same
reason (``tests/conformance/_observation.py`` gives each record a filename
stable per cell, so re-measuring REPLACES and never accumulates duplicates); a
cell drawn last week keeps last week's record and last week's ``as_of`` until
something measures it again.

**3. A MIXED PROFILE STAYS MIXED.** Profiles are not capability-uniform, so a
cell is an aggregate over its profile's elements and it must never publish
``measured-ok`` while hiding elements it could not measure. The real case, and
the one Chris's ruling exists for: ``zephyr-3.7`` x transfer is observable on
``zephyr37_fat`` and ``zephyr37_lfs`` and NOT on ``zephyr37_nofs`` /
``zephyr37_llext``, whose ``EmbeddedFileSystem`` reports ``supports_transfer``
False. That cell comes out ``measured-ok`` with a two-element ``observed_on``
AND a two-entry ``not_observable`` naming what was probed on each.

``observed_on`` and ``not_observable`` need not exhaust the profile. The third
state -- "no run has drawn it yet" -- is DERIVED (``cell_coverage().unaccounted``)
and never stored: filing it under ``not_observable`` would claim the environment
cannot express the observable, which is a claim nobody made.

AND THE SAME RULE APPLIES ONE LEVEL FURTHER DOWN, because the bed's real
measurement unit is neither the profile nor the element but the (element, term,
transfer) CELL. ``bb1161`` moves files fine over ``shell`` and not at all over
``nc``, so ``busybox-1.16.1`` x transfer-roundtrip is honestly
``measured-broken`` and a reader who takes that as "no file transfer to a
BusyBox 1.16.1 device" has been misled by a scalar over a space that is not
uniform. ``observed_cells`` carries that split STRUCTURALLY -- one entry per
cell, with its transport and its outcome -- so the renderer states it instead
of reconstructing it from a pipe-joined ``observable`` and an English
``failure_summary``. The schema ties the entries to the status in both
directions: every entry of a ``measured-ok`` cell passed, and a
``measured-broken`` cell holds at least one that did not.

**4. A ``measured-ok`` CELL CANNOT BE WRITTEN WITHOUT ITS EVIDENCE.** Three
things must all be present, and each is refused rather than defaulted:

* an OBSERVABLE the contract declared for that cell
  (``tests/conformance/_observable.py``). Not derived from the surface id: a
  field that cannot disagree with the cell's own key satisfies the schema while
  proving nothing, and §5 asks for it precisely because a surface's observable
  differs by environment.
* a POSITIVE CONTROL, named as the PARAMETRIZED nodeid on a cell of an element
  in this cell's own ``observed_on``. The unparametrized form names a control
  that ran *somewhere*, which is not evidence about this cell.
* that control having actually RUN AND PASSED on the very cell the observation
  came from. This is the half a constructed nodeid cannot supply. A collator
  that only builds the string publishes *a test with this name is collected*,
  which is wiring; if the control ran and FAILED, the instrument could not tell
  a wrong answer from a right one and the contract's pass beside it proves
  nothing -- the defect this whole item exists to expose.

★ AND THE RULE IS PER ROUTE, NOT PER CELL, because the page's claims are.
MEASURED 2026-08-25: all ten mixed ``measured-broken`` cells published *"Only
over ``shell``. You can put a file on the device and get the same bytes back
over ``shell``"* -- a ``measured-ok``-strength claim about one route -- while
citing no control at all, because the CELL-level citation needs every
contributing route controlled and a mixed cell's failing route never is. The
passing route's control had run and PASSED, and its record was collected and
then discarded. So every entry of ``observed_cells`` whose ``outcome`` is
``passed`` now carries its own ``positive_control``, the schema requires it
there and permits it nowhere it would be invented, and a cell whose contract
PASSED on a route no control passed on is REFUSED rather than published
uncited.

Any cell that fails one of these is REFUSED: left unchanged, named in the
report, and the process exits non-zero. Never downgraded to a weaker verdict it
did not earn, and never written anyway.
"""

import argparse
import copy
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tests._fixtures.support_matrix import (
    MATRIX_PATH,
    cell_coverage,
    cell_outcome_errors,
    element_accounting_errors,
    positive_control_errors,
)
from tests.conformance._controls import positive_control_for
from tests.conformance._observation import (
    CONTROL,
    DEFAULT_OBSERVATIONS_DIR,
    DOMAIN_EXCLUSION,
    FORMAT,
    OBSERVATION,
    PASSED,
    is_evidential,
    read_records,
)

BED = "bed"
"""The one venue whose records may populate a cell. See rule 1."""

MEASURED_OK = "measured-ok"
MEASURED_BROKEN = "measured-broken"
NOT_OBSERVABLE = "not-observable"


@dataclass
class Discarded:
    """One reason records were dropped, with the records themselves."""

    reason: str
    records: "list[dict]" = field(default_factory=list)

    def line(self) -> str:
        """One report line: how many, why, and a sample so it can be chased."""
        sample = sorted({record.get("cell_label") or "?" for record in self.records})[:3]
        return f"  {len(self.records):4d}  {self.reason}\n        e.g. {', '.join(sample)}"


@dataclass
class Collation:
    """Everything one collation decided, so the report and the write share a source."""

    matrix: dict
    """The new artifact. Identical to the old one wherever nothing was measured."""

    changed: "dict[tuple[str, str], tuple[dict, dict]]" = field(default_factory=dict)
    unchanged_with_records: "list[tuple[str, str]]" = field(default_factory=list)
    refused: "list[str]" = field(default_factory=list)
    discarded: "list[Discarded]" = field(default_factory=list)
    kept: int = 0

    @property
    def ok(self) -> bool:
        """Whether every cell that had records could be written honestly."""
        return not self.refused


FAILURE_SUMMARY_LIMIT = 2000
"""How much of a cell's joined failure text reaches the artifact.

A CAP, and `scripts/render_support_matrix.py` marks any cell that reached it --
a length comparison against this constant, never a reading of the text, because
a reader who meets a reason trailing off at a comma cannot otherwise tell
incomplete evidence from careless writing.

★ IT MUST STAY SMALLER THAN `tests/conformance/_observation.py`'s
`RECORD_SUMMARY_LIMIT`, which caps ONE record's summary before this ever joins
them. That module states the argument; the short version is that a cut made
below this cap would reach the page unannounced, because the page can only see
the length of what the join produced.

MEASURED 2026-08-25, which is why 500 was wrong. Each of the ten
`measured-broken` cells joins ONE segment, `"<cell label>: <reason>"`, and the
real segment is 780 characters -- so at 500 every published reason stopped
mid-sentence, and the ~280 characters lost were the ones saying how narrow the
gap is. 2000 holds two full segments of that size; a cell that joins more than
that is cut and says so.
"""


def _bucket(records: "list[dict]", matrix: dict) -> "tuple[list[dict], list[Discarded]]":
    """Split *records* into the ones that may populate a cell, and the rest.

    EVERY DROP IS ATTRIBUTED. The reasons are checked in a fixed order so a
    record appears under exactly one of them, and the order runs from the most
    structural cause to the most local: a malformed record cannot be judged for
    its venue, and a hermetic record's ``profile: null`` is a CONSEQUENCE of its
    venue rather than a second independent fault.
    """
    surfaces = {surface["id"] for surface in matrix["surfaces"]}
    profiles = {profile["id"] for profile in matrix["profiles"]}
    buckets = {
        reason: Discarded(reason)
        for reason in (
            f"not format {FORMAT} -- an unversioned or future record, unreadable here",
            (
                f"venue is not {BED!r} -- hermetic cells have no lab entry, so they "
                f"populate NO profile column and can establish no verdict (ruling "
                f"2026-08-24). Spec 5's `ci-hermetic` path is deliberately not implemented"
            ),
            "names no matrix surface -- only a contract's own record can carry a verdict",
            "names no profile column -- its element is in no bed lab",
            (
                "names a surface or profile this artifact does not declare -- regenerate "
                "the axes, or the record predates a rename"
            ),
            (
                "not evidence about the contract -- a skip, a setup error or an "
                "unexpected pass is a statement about the RUN"
            ),
            "an unknown record kind",
        )
    }
    order = list(buckets)
    kept: "list[dict]" = []
    for record in records:
        if record.get("format") != FORMAT:
            buckets[order[0]].records.append(record)
        elif record.get("venue") != BED:
            buckets[order[1]].records.append(record)
        elif record.get("surface") is None:
            buckets[order[2]].records.append(record)
        elif record.get("profile") is None:
            buckets[order[3]].records.append(record)
        elif record["surface"] not in surfaces or record["profile"] not in profiles:
            buckets[order[4]].records.append(record)
        elif record.get("kind") in (OBSERVATION, CONTROL) and not is_evidential(
            record.get("outcome", "")
        ):
            buckets[order[5]].records.append(record)
        elif record.get("kind") not in (OBSERVATION, DOMAIN_EXCLUSION, CONTROL):
            buckets[order[6]].records.append(record)
        else:
            kept.append(record)
    return kept, [bucket for bucket in buckets.values() if bucket.records]


def _element_order(matrix: dict, profile_id: str) -> "list[str]":
    """Answer the profile's elements, in the artifact's own order.

    Sorting by that rather than alphabetically so the artifact's diffs stay
    about verdicts: an element list that reordered itself between collations
    would show up as a change to every cell of that column.
    """
    return next(p["elements"] for p in matrix["profiles"] if p["id"] == profile_id)


@dataclass(frozen=True)
class CellEvidence:
    """Every kept record for one (surface, profile), split by what it can say."""

    observations: "list[dict]"
    exclusions: "list[dict]"
    controls: "list[dict]"


def _evidence(kept: "list[dict]") -> "dict[tuple[str, str], CellEvidence]":
    """*kept*, grouped by the cell it speaks about and split by record kind."""
    grouped: "dict[tuple[str, str], dict[str, list[dict]]]" = defaultdict(
        lambda: {OBSERVATION: [], DOMAIN_EXCLUSION: [], CONTROL: []}
    )
    for record in kept:
        grouped[(record["surface"], record["profile"])][record["kind"]].append(record)
    return {
        where: CellEvidence(
            observations=sorted(split[OBSERVATION], key=lambda r: r["cell_label"]),
            exclusions=sorted(split[DOMAIN_EXCLUSION], key=lambda r: r["cell_label"]),
            controls=sorted(split[CONTROL], key=lambda r: r["cell_label"]),
        )
        for where, split in grouped.items()
    }


def _observable_of(observations: "list[dict]") -> "str | None":
    """Answer the one observable *observations* agree on, or an attributed join.

    AN ELEMENT CAN HAVE SEVERAL CELLS -- ``test1`` alone has eight, one per
    ``(term, transfer)`` pair -- and two of them can watch genuinely different
    things: ``put(mode=...)`` is read back with ``stat -c %a`` where the backend
    carries a permission model and is a pre-flight REFUSAL where it does not.
    Collapsing those to whichever came first would publish one cell's observable
    as the whole profile's.

    ATTRIBUTED BY ELEMENT, and only where the attribution ADDS something.
    MEASURED on the first real bed collation: ``gnu`` x transfer-mode watches
    four observables (one per transfer backend) and every one of them was seen
    on all four elements, so listing the 32 cell labels that produced them made
    a 900-character field that said nothing the observables themselves did not
    already say -- each names its own transport. An observable covering every
    observed element therefore carries no parenthetical; one covering a subset
    names that subset, which is the case a reader cannot otherwise reconstruct.

    ``None`` when any contributing record declares no observable at all, which
    is what stops a surface with no declaration ever reaching ``measured-ok``.
    """
    if any(record.get("observable") is None for record in observations):
        return None
    by_observable: "dict[str, set[str]]" = defaultdict(set)
    for record in observations:
        by_observable[record["observable"]].add(record["element"])
    if len(by_observable) == 1:
        return next(iter(by_observable))
    everywhere = {record["element"] for record in observations}
    return " | ".join(
        observable if elements == everywhere else f"{observable} (on {', '.join(sorted(elements))})"
        for observable, elements in sorted(by_observable.items())
    )


def _observed_cells(
    observations: "list[dict]", observed: "list[str]", controls: "dict[str, str]"
) -> "list[dict[str, str]]":
    """Answer the per-CELL breakdown, READ off the records and never reconstructed.

    THE PROFILE IS NOT THE MEASUREMENT UNIT AND NEITHER IS THE ELEMENT. The bed
    measures an (element, term, transfer) cell, and MEASURED 2026-08-24
    ``bb1161`` answers two different things: the roundtrip passes over ``shell``
    and fails over ``nc``, against otto's registered ``nc-transfer`` gap. The
    aggregate has to be ``measured-broken`` -- rule 3, one level down -- but a
    reader scanning that row concludes otto cannot move files to a BusyBox
    1.16.1 device at all, which is FALSE. Both halves were already in the cell
    before this field existed, and only as prose: a pipe-joined ``observable``
    and an English ``failure_summary``. A renderer that recovers structure by
    parsing English is a renderer that will silently stop recovering it, and
    this artifact is published documentation.

    EVERY VALUE IS LIFTED FROM THE RECORD, including the ``cell_label`` that
    could so easily be re-spelled from the three axes beside it. Task 4's own
    best finding was that a CONSTRUCTED ``positive_control`` satisfies every
    guard in this item while saying nothing about whether the control ran; a
    constructed breakdown is the same shape -- it would name the cells the bed
    *could* draw rather than the ones a run *did*, and it would have to invent
    an outcome for each. What a run measured is a fact only the run holds.

    Ordered by the profile's own element order and then by label, so a
    re-collation of the same records writes the same list and the artifact's
    diffs stay about verdicts. Built by walking *observed* -- the same list
    ``observed_on`` is built from -- so the two can never name different
    element sets.

    ★ THE SIXTH FIELD IS A ``positive_control``, PER ROUTE, and *controls* is
    where it comes from: the ``cell_label`` -> nodeid map of the controls that
    really PASSED, built by :func:`_passed_controls` off the control records.
    Never composed from ``positive_control_for(surface)`` and the label beside
    it. Those two strings are identical, which is exactly the trap -- what
    makes the citation evidence is not its SPELLING but the fact that a record
    with that nodeid reported ``passed`` on this very cell, and a collator that
    builds the string publishes "a test with this name is collected", which is
    wiring. :func:`_verdict` refuses any cell whose contract PASSED on a route
    no control passed on, so a missing entry here is never silently omitted.

    MEASURED 2026-08-25, and it is why the field was added: all ten mixed
    ``measured-broken`` cells rendered *"Only over ``shell``. You can put a file
    on the device and get the same bytes back over ``shell``"* while citing NO
    control -- the cell-level field needs every contributing cell controlled,
    and a mixed cell's failing route is not, so the passing route's control
    record (which had run and PASSED) was collected and thrown away.

    ★ AND DELIBERATELY NOT A SEVENTH: NO PER-CELL ``observable``.
    Every record carries one, so it would be free to store, and it was left open
    twice -- once by the task that added this field and once by the renderer,
    which can say WHICH transport broke but not WHAT was watched on each.
    MEASURED 2026-08-25 over the 575 records of a real bed lane and DECIDED:
    of the 51 cells a run drew observations for (the other three are the
    ``not-observable`` timeout cells, which have none), 12 have contributing
    observations whose observables differ at all -- and in every one of the 51
    the only thing that differs is the TRANSPORT TOKEN, which the ``transfer``
    field beside it already carries
    structurally. Storing the string per cell would repeat a ~110-character
    sentence 32 times on ``gnu`` x transfer-mode to say ``scp`` / ``sftp`` /
    ``ftp`` / ``nc``, which is the 900-character ``observable`` Task 4 measured
    and shrank, restored one level down.
    ``tests/unit/test_support_matrix.py``'s
    ``test_a_cells_observables_differ_only_by_the_transport_its_entry_already_names``
    is the tripwire: the day two drawn cells of one profile watch genuinely
    different things, the cell-level join starts hiding one and that guard
    reddens rather than the decision quietly becoming wrong.
    """
    return [
        {
            "cell_label": record["cell_label"],
            "element": record["element"],
            "term": record["term"],
            "transfer": record["transfer"],
            "outcome": record["outcome"],
        }
        | (
            {"positive_control": controls[record["cell_label"]]}
            if record["cell_label"] in controls
            else {}
        )
        for element in observed
        for record in sorted(
            (r for r in observations if r["element"] == element),
            key=lambda r: r["cell_label"],
        )
    ]


def _probe_summary(exclusions: "list[dict]") -> "tuple[str, str]":
    """Join the ``probed`` / ``probe_result`` of a whole-profile exclusion.

    Both are joined from what the records actually hold rather than composed
    here. A sentence written by the collator beside a machine-written verdict is
    the fabrication path the guards exist to close.
    """
    probed = sorted({record["probed"] for record in exclusions})
    results = sorted({record["probe_result"] for record in exclusions})
    return "; ".join(probed), "; ".join(results)


def _passed_controls(surface: str, controls: "list[dict]") -> "tuple[dict[str, str], str | None]":
    """``cell_label`` -> the nodeid of this surface's control that PASSED there.

    THE PAIRING IS PER CELL, NOT PER ELEMENT, and that is the strength of it. A
    control that passed on ``bed-unix[test1:ssh:scp]`` says nothing about
    ``bed-unix[test1:telnet:nc]`` -- different transport, different backend --
    so "somewhere on this element" would be the same weakening as "somewhere in
    this profile", one level down. ``test1`` alone draws eight cells.

    READ off the records, so the map holds only cells where a control really
    ran and really passed. A control that ran and FAILED is absent from it, and
    that is the whole point: the contract's pass beside a failed control proves
    nothing, because the instrument could not tell a wrong answer from a right
    one.

    THE SECOND RETURN IS A REFUSAL, not a filter. A control record filed under
    this surface whose nodeid is some OTHER surface's control is not skipped --
    it stops the cell being written at all. Skipping it would leave the cell
    looking merely uncontrolled, and the two are different diagnoses: one says
    nobody ran a control, the other says the run produced a record this step
    cannot interpret, which is a defect in the emitter rather than in the bed.

    ★ AND THERE IS NO ``record["surface"] != surface`` FILTER, deliberately.
    *controls* arrives from :func:`_evidence`, which groups every record by
    ``(surface, profile)`` before this is called, so such a test could never be
    true: it would read as a check that fires and would in fact be dead. What
    catches a foreign record is the nodeid test below, which is live and
    REFUSES rather than skipping. MEASURED 2026-08-25 -- the filter was here,
    could not fire, and was removed rather than left to look load-bearing.
    """
    expected = positive_control_for(surface)
    passed: "dict[str, str]" = {}
    for record in sorted(controls, key=lambda r: r["cell_label"]):
        if record["outcome"] != PASSED:
            continue
        if not record["nodeid"].startswith(f"{expected}["):
            return {}, (
                f"the control record cites {record['nodeid']!r}, which is not this "
                f"surface's control ({expected}) parametrized on a cell -- refusing to "
                f"write it"
            )
        passed[record["cell_label"]] = record["nodeid"]
    return passed, None


def _unbacked_passes(observations: "list[dict]", controls: "dict[str, str]") -> "list[str]":
    """Answer the cells where the CONTRACT passed and no control passed beside it.

    ★ THE RULE APPLIED PER ROUTE, which is the unit the page makes claims
    about. A ``measured-ok`` cell may not exist without naming the control that
    proved its observable can go red; a mixed ``measured-broken`` cell renders
    *"Only over ``shell``. You can put a file on the device and get the same
    bytes back over ``shell``"*, which is the same claim about one route, and
    for ten cells it named nothing at all. So the requirement is not "this cell
    has a control" but "every route this cell passed on has one".

    A route that did NOT pass is absent from this list by design. It claims
    nothing positive, and a strict xfail xfails the control alongside the
    contract, so demanding a passing control there would make an honest broken
    cell unwritable.
    """
    return sorted(
        {r["cell_label"] for r in observations if r["outcome"] == PASSED} - controls.keys()
    )


def _verdict(
    matrix: dict, surface: str, profile: str, evidence: CellEvidence
) -> "tuple[dict | None, str | None]":
    """Answer the cell *evidence* earns, or why no honest cell can be written.

    Element by element first, then aggregated, because the aggregate is only
    honest if the breakdown is: an element with two cells is ``ok`` ONLY if
    every evidential record for it passed. Otto's registered ``nc-transfer``
    gap is exactly this shape -- ``bb1161`` transfers fine over ``shell`` and
    fails over ``nc`` -- and a cell that took the passing half would publish
    ``measured-ok`` for a profile whose transfer is measurably broken.
    """
    if not evidence.observations and not evidence.exclusions:
        return None, (
            "only positive-control records name this cell -- a control's outcome is "
            "evidence about the INSTRUMENT and never about the host, so nothing here can "
            "move a verdict"
        )
    order = _element_order(matrix, profile)
    observed = [e for e in order if any(r["element"] == e for r in evidence.observations)]
    excluded = [e for e in order if any(r["element"] == e for r in evidence.exclusions)]
    both = sorted(set(observed) & set(excluded))
    if both:
        return None, (
            f"{both} carry BOTH an observation and a domain exclusion -- one run says the "
            f"contract was evaluated there and another says it was not"
        )
    not_observable = [
        {
            "element": element,
            "probed": record["probed"],
            "probe_result": record["probe_result"],
        }
        for element in excluded
        for record in [next(r for r in evidence.exclusions if r["element"] == element)]
    ]
    as_of = max(record["as_of"] for record in evidence.observations + evidence.exclusions)

    if not observed:
        if not not_observable:
            return None, "kept records that name neither an observation nor an exclusion"
        probed, probe_result = _probe_summary(evidence.exclusions)
        return {
            "status": NOT_OBSERVABLE,
            "venue": BED,
            "as_of": as_of,
            "probed": probed,
            "probe_result": probe_result,
            "observed_on": [],
            # EMPTY IS A STATEMENT. `not-observable` means no cell of this
            # profile produced a result, and the schema requires the key so
            # that saying so is not the same as leaving it out.
            "observed_cells": [],
            "not_observable": not_observable,
        }, None

    controls, why = _passed_controls(surface, evidence.controls)
    if why is not None:
        return None, why
    unbacked = _unbacked_passes(evidence.observations, controls)
    if unbacked:
        return None, (
            f"no positive control PASSED on {', '.join(unbacked)} -- the contract's "
            f"result there is unbacked: nothing showed the observable could go red"
        )
    contract = next(s["contract"] for s in matrix["surfaces"] if s["id"] == surface)
    observable = _observable_of(evidence.observations)
    broken = [record for record in evidence.observations if record["outcome"] != PASSED]
    common = {
        "status": MEASURED_BROKEN if broken else MEASURED_OK,
        "nodeid": contract,
        "venue": BED,
        "as_of": as_of,
        "observed_on": observed,
        "observed_cells": _observed_cells(evidence.observations, observed, controls),
        "not_observable": not_observable,
    }
    if observable is None:
        return None, (
            "no observable was declared for this surface on every cell measured, so the "
            "cell could not say WHAT was watched -- see tests/conformance/_observable.py"
        )
    common["observable"] = observable
    # THE CELL-LEVEL CITATION IS THE WHOLE-CELL CLAIM, and it needs every
    # contributing route controlled -- not just the passing ones. A mixed cell
    # cannot make it (its failing route's control xfailed beside the contract),
    # which is exactly why the per-route field above exists; the cell then says
    # what it can say and no more.
    contributing = {record["cell_label"] for record in evidence.observations}
    if not contributing - controls.keys():
        common["positive_control"] = controls[min(contributing)]
    if broken:
        common["failure_summary"] = "; ".join(
            sorted(
                {
                    f"{record['cell_label']}: {record.get('failure_summary') or record['outcome']}"
                    for record in broken
                }
            )
        )[:FAILURE_SUMMARY_LIMIT]
    return common, None


def collate(matrix: dict, records: "list[dict]") -> Collation:
    """Fold *records* into *matrix*; answer the new artifact and what it decided.

    PURE, and that is why the guards can reach it: nothing here reads a clock,
    a filesystem or an environment variable, so ``tests/unit/test_support_matrix.py``
    can hand it a synthetic record set and assert the exact cell it produces.
    Everything ambient lives in :func:`main`.
    """
    kept, discarded = _bucket(records, matrix)
    result = Collation(matrix=copy.deepcopy(matrix), discarded=discarded, kept=len(kept))
    for (surface, profile), evidence in sorted(_evidence(kept).items()):
        where = f"{surface} x {profile}"
        before = matrix["cells"][surface][profile]
        cell, why = _verdict(matrix, surface, profile, evidence)
        if cell is None:
            result.refused.append(f"{where}: {why}")
            continue
        if cell == before:
            result.unchanged_with_records.append((surface, profile))
            continue
        result.matrix["cells"][surface][profile] = cell
        result.changed[(surface, profile)] = (before, cell)
    return result


def report(result: Collation, *, records_dir: Path, writing: bool) -> "list[str]":
    """Tell the whole story of one collation, in the order a reader needs it."""
    lines = [f"collate: {result.kept} usable record(s) from {records_dir}"]
    if result.discarded:
        total = sum(len(bucket.records) for bucket in result.discarded)
        lines.append(f"DISCARDED {total} record(s), every one attributed:")
        lines.extend(bucket.line() for bucket in result.discarded)
    if result.changed:
        lines.append(f"{len(result.changed)} cell(s) CHANGED:")
        for (surface, profile), (before, after) in sorted(result.changed.items()):
            coverage = cell_coverage(result.matrix, surface, profile)
            # A cell can change without its STATUS changing -- a re-measured
            # `as_of`, a re-worded observable, a different failure. Printing
            # "measured-broken -> measured-broken" and nothing else told Chris
            # a cell had moved and not what moved in it, so the fields that
            # actually differ are named. This report is what a person stands in
            # front of before committing a verdict.
            moved = sorted(
                key for key in set(before) | set(after) if before.get(key) != after.get(key)
            )
            what = (
                f"{before['status']} -> {after['status']}"
                if before["status"] != after["status"]
                else f"{after['status']}, changed: {', '.join(moved)}"
            )
            lines.append(
                f"  {surface} x {profile}: {what}  "
                f"[observed {len(coverage.observed_on)}, not-observable "
                f"{len(coverage.not_observable)}, NOT YET DRAWN "
                f"{len(coverage.unaccounted)} of {len(coverage.elements)}]"
            )
    if result.unchanged_with_records:
        lines.append(
            f"{len(result.unchanged_with_records)} cell(s) re-measured to the same verdict"
        )
    untouched = sum(
        1
        for surface, row in result.matrix["cells"].items()
        for profile in row
        if (surface, profile) not in result.changed
        and (surface, profile) not in result.unchanged_with_records
    )
    lines.append(f"{untouched} cell(s) had NO record and were left untouched (never downgraded)")
    if result.refused:
        lines.append(f"REFUSED {len(result.refused)} cell(s) -- left unchanged, NOT downgraded:")
        lines.extend(f"  {why}" for why in result.refused)
    lines.append(_write_line(result, writing=writing))
    return lines


def _write_line(result: Collation, *, writing: bool) -> str:
    """Say what happened to the FILE, distinguishing the three reasons it may not have moved.

    "nothing to write" and "not asked to write" are different states, and a
    ``make support-matrix`` that had folded a full bed lane in and then printed
    *pass --write to fold these in* -- with ``--write`` already on its command
    line -- reads as a step that failed silently.
    """
    if writing:
        return "artifact written"
    if not result.changed:
        return "no cell changed, so nothing was written"
    return f"{len(result.changed)} cell(s) WOULD change -- pass --write to fold them in"


def main(argv: "list[str]") -> int:
    """Read, collate, report, and write only when asked. Non-zero on a refusal."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=Path, default=DEFAULT_OBSERVATIONS_DIR)
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument(
        "--write",
        action="store_true",
        help="fold the records in; without it this only reports. Never commits, and "
        "neither does CI. The release stage (make release-matrix) commits an "
        "auto-acceptable refresh and refuses a downgrade, which stays a person's call.",
    )
    args = parser.parse_args(argv)

    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    result = collate(matrix, read_records(args.records))
    problems = (
        element_accounting_errors(result.matrix)
        + cell_outcome_errors(result.matrix)
        + positive_control_errors(result.matrix)
    )
    if problems:
        # Never written. A collation that contradicts the artifact's own
        # per-element rules is a bug in THIS file, and writing it would put the
        # contradiction where a reader meets it before the guard does.
        print("\n".join([*report(result, records_dir=args.records, writing=False), *problems]))
        return 2
    writing = args.write and bool(result.changed)
    if writing:
        args.matrix.write_text(json.dumps(result.matrix, indent=2) + "\n", encoding="utf-8")
    print("\n".join(report(result, records_dir=args.records, writing=writing)))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
