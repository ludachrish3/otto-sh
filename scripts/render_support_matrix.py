"""Render ``schemas/support_matrix.json`` into ``docs/architecture/support-matrix.md``.

**A LOOKUP REFERENCE, NOT TEST BOOKKEEPING** (Chris, 2026-08-24). A reader arrives
asking *can otto do X against a device like mine?*, not *what did the suite measure on
Tuesday*. So every cell answers that question first, in plain language, and carries its
provenance second -- the shape
``docs/architecture/subsystems/busybox-support.md`` already uses for the gap registry,
whose third column is "What it means for you".

Run from the repo root::

    uv run python -m scripts.render_support_matrix           # write the page
    uv run python -m scripts.render_support_matrix --check   # report only, write nothing

``-m`` and not a path, for the reason ``scripts/collate_support_matrix.py`` gives at
length: run as a path, ``sys.path[0]`` is ``scripts/`` and the ``tests`` imports below
would need a bootstrap above them, which is three ``# noqa: E402`` suppressions for a
problem the invocation shape does not have.

═══════════════════════════════════════════════════════════════════════════
WHAT THIS FILE IS ALLOWED TO SAY, AND WHAT IT IS NOT
═══════════════════════════════════════════════════════════════════════════

**The artifact stores no prose about a verdict, deliberately** -- a hand-written
sentence beside a machine-written verdict is the fabrication path spec §5's guards
exist to close, and ``tests/_fixtures/support_matrix.py``'s ``CellCoverage`` docstring
says so: *the phrasing lives in the renderer*. :data:`VOICE` is that phrasing. It
describes a **surface** -- what the promise is, and what a contract narrowing its own
domain means for someone holding the device -- and it never describes a **verdict**.
Which state a cell is in, which devices it rests on and which transports moved are read
from the artifact's fields every time.

**NO FIELD IS RECOVERED BY PARSING ENGLISH.** The transport split is read from
``observed_cells``' own ``element`` / ``term`` / ``transfer`` keys (Task 4b made it
structural precisely so a renderer would not have to split a ``cell_label`` on brackets,
or a pipe-joined ``observable`` on ``" | "``). ``observable`` and ``failure_summary`` are
reproduced VERBATIM and never taken apart.

**THREE STATES, NOT TWO.** ``unaccounted`` -- profile elements minus ``observed_on``
minus ``not_observable`` -- is DERIVED, never stored, and it is rendered separately from
``not_observable``. "Measured on 2 of 4, one device cannot express the observable, one
has never been drawn" and "2 of 4, two cannot express it" are materially different
claims about someone's hardware.
"""

import argparse
import ast
import datetime as _datetime
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.collate_support_matrix import FAILURE_SUMMARY_LIMIT
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.support_matrix import (
    MATRIX_PATH,
    SURFACES,
    CellCoverage,
    cell_coverage,
    discover_contracts,
    discover_profiles,
)

PAGE_PATH = PROJECT_ROOT / "docs" / "architecture" / "support-matrix.md"
"""The rendered page.

GIT-IGNORED and regenerated on every Sphinx build (``docs/conf.py``'s
``builder-inited`` hook, beside ``_generate_docs_media``), the same standing
``docs/_static/generated/`` has. Committing it would put a second copy of every verdict
in the tree, and the copy that goes stale is the one nothing runs.
"""


@dataclass(frozen=True)
class Branch:
    """ONE of the observables a surface can offer, and what it lets this page say.

    ★ A SURFACE DOES NOT ALWAYS HAVE ONE PROMISE, and that is spec §5's whole
    argument rather than an edge case: *a surface's observable differs by
    ENVIRONMENT*. ``tests/conformance/_observable.py`` implements it -- a contract
    may narrow its declared observable from inside its own body, because only the
    running test knows which arm the device offered it. ``transfer-mode`` takes such
    an arm: it watches the mode ``stat -c %a`` reads back where the backend carries a
    permission model, and it watches the pre-flight REFUSAL where it does not.

    **Those two are not two spellings of one promise, and a cell whose contract
    watched a refusal must not render as a capability.** MEASURED 2026-08-25: all
    three Zephyr ``transfer-mode`` rows said *"**Yes.** You can put a file and have
    the permission mode you asked for land on it"* about devices on which otto refuses
    a mode outright -- the answer column promising the exact opposite of what the cell
    stored one column over. The observable machinery had captured the branch
    correctly; a CONSTANT :attr:`Voice.capability` then re-aggregated it away, and
    nothing tied the sentence to the field it contradicted.

    ``marker``
        A literal fragment of the observable the contract writes ON THIS ARM, and the
        one question this file ever asks of an ``observable``. **It is not a parse**:
        no field is recovered from it, nothing is split, the text is still reproduced
        verbatim in the evidence, and the answer is a yes/no about which arm ran.
        PINNED -- :func:`promise_mismatch` requires it to appear verbatim in that
        contract's own module, so rewording the arm fails the docs build instead of
        quietly returning the page to the wrong promise.
    ``capability``
        The verb phrase completing "you can ..." on this arm. **Empty means this arm
        watched a refusal**, and no promise may be composed from such a cell.
    ``headline`` / ``instead``
        What the row says in place of a promise, on a refusal arm: the bolded lead,
        and the clause behind it. ``instead``'s code spans are pinned to the contract
        module the same way :attr:`Voice.narrowed`'s are to the domain rule.
    ``grid_word``
        The at-a-glance word for a refusal arm. The grid is read first and by people
        in a hurry, so it may not publish ``works`` for a cell whose row says otto
        refuses -- that is the same false promise one level up.
    """

    marker: str
    capability: str = ""
    headline: str = ""
    instead: str = ""
    grid_word: str = ""


@dataclass(frozen=True)
class Voice:
    """How to talk about ONE SURFACE to someone holding a device.

    Three strings, none of which may mention a state:

    ``short``
        the column head in the at-a-glance grid.
    ``capability``
        a verb phrase completing "you can ..." -- the promise itself. **Only the
        promise a surface makes UNCONDITIONALLY belongs here**; a surface whose
        contract watches a different observable depending on the device declares
        :attr:`branches` instead, and :func:`promise_of` reads the cell's own
        ``observable`` to pick between them.
    ``narrowed``
        ONE CLAUSE saying what it means that a contract declared some drawn cell
        **outside its own domain**. This is the phrase that keeps ``not-observable``
        from reading as ``broken``, and it is surface-specific because the two live
        narrowings are not the same kind of claim: the transfer contract reads OTTO'S
        answer (``remote_scratch is None`` for exactly the hosts whose filesystem
        reports ``supports_transfer`` False), while the timeout contract reads THIS
        SUITE's (no Zephyr vocabulary declares a ``long_running_command``). The page
        prints the probe beside the claim so a reader can check it rather than trust
        it.

        ★ PINNED TO THE RULE, NOT TO THE DOCSTRING IT READS LIKE. These are
        hand-written sentences, and the drift that matters is a contract's DOMAIN RULE
        changing while the page keeps publishing the old reason. So
        ``tests/unit/test_support_matrix.py`` requires every ``code token`` in this
        clause to appear in that contract's own ``applicable_cell`` source, and
        requires the clause (with ``narrowed_detail``) to name at least one identifier
        the predicate's BODY reads -- ``remote_scratch`` for transfer,
        ``long_running_command`` for timeout. Rename or replace what the rule consults
        and the guard reddens.

        WHAT IS DELIBERATELY NOT PINNED IS THE PROSE ITSELF, and
        ``tests/unit/test_docs_gap_sync.py`` is the precedent rather than the
        counter-example: it says in its own docstring that pinning paragraphs verbatim
        "would buy a copying ritual rather than a check -- it would redden on a typo
        fix and stay green on a lie". The compulsory part is the mechanism named; the
        wording is review's job.

        A SURFACE WHOSE CONTRACT MODULE DECLARES NO ``applicable_cell`` NARROWS
        NOTHING, so its clause can never be rendered -- the three ``exec`` surfaces
        today. Their clause names no mechanism, and the same guard requires that (a
        surface-specific reason from a contract with no domain rule would be a claim
        nothing produced) and that no cell of theirs carries a ``not_observable`` entry.
    ``narrowed_detail``
        the rest of that story, rendered once in the evidence rather than in every
        table cell it applies to. Short by construction is not the same as complete:
        a table sentence that carried the whole explanation crowded out the verdict it
        was explaining.
    """

    short: str
    capability: str
    narrowed: str
    narrowed_detail: str = ""
    branches: "tuple[Branch, ...]" = ()
    """The observables this surface can offer, when it offers more than one.

    Empty for a surface with a single observable, which is five of the six today.
    Non-empty means :attr:`capability` is never used for a MEASURED cell: the cell's
    own stored ``observable`` decides which arm ran, and an observable matching no arm
    -- or more than one -- publishes NO promise and fails the docs build.
    """


#: surface id -> how to say it in plain language. Keyed by the ids
#: :data:`tests._fixtures.support_matrix.SURFACES` declares, and
#: ``tests/unit/test_support_matrix.py`` requires the two key sets to be EQUAL -- a
#: surface added to the tree with no voice here fails the docs build rather than
#: rendering as a blank column, which is this item's own defect shape.
VOICE: "dict[str, Voice]" = {
    "exec-exit-code": Voice(
        short="exit code",
        capability="run a command and trust the exit code otto hands back",
        narrowed="the contract declares such a device outside its own domain",
    ),
    "exec-framing": Voice(
        short="output framing",
        capability=(
            "read a command's output without otto's own prompts, echo and markers mixed into it"
        ),
        narrowed="the contract declares such a device outside its own domain",
    ),
    "exec-failure-in-sequence": Voice(
        short="failure in a sequence",
        capability=(
            "trust that a command that failed part-way through a sequence is reported "
            "as a failure rather than as success"
        ),
        narrowed="the contract declares such a device outside its own domain",
    ),
    "transfer-roundtrip": Voice(
        short="file roundtrip",
        capability="put a file on the device and get the same bytes back",
        narrowed=(
            "otto reports nowhere to put a file on such a device -- its filesystem "
            "backend answers `supports_transfer` False, so `remote_scratch` is `None` "
            "and there is no roundtrip to watch"
        ),
        narrowed_detail=(
            "That is a property of the device rather than a defect, and it is otto's own "
            "answer rather than this suite's judgement. What such a target owes a caller "
            "of `put` is a clear error rather than a half-written file, and "
            "`tests/integration/host/test_host_contract.py::"
            "test_no_filesystem_backend_surfaces_clear_error` asserts exactly that "
            "against these same devices."
        ),
    ),
    "transfer-mode": Voice(
        short="file mode",
        capability="put a file and have the permission mode you asked for land on it",
        # ★ THE ONE SURFACE WHOSE CONTRACT WATCHES TWO DIFFERENT THINGS. Which arm a
        # cell took is the cell's own `observable`, written by the run; see `Branch`.
        branches=(
            Branch(
                marker="reads back on the host after put(mode=",
                capability=("put a file and have the permission mode you asked for land on it"),
            ),
            Branch(
                marker="because it declares no permission model",
                headline="No -- and otto says so rather than pretending.",
                instead=(
                    "such a device carries no permission model at all: its transfer "
                    "backend answers `supports_mode` False, so what a run watched here "
                    "is otto's own refusal -- `put` fails before anything transfers, "
                    "per file as well as overall, rather than landing the file with "
                    "bits you did not ask for"
                ),
                grid_word="refused",
            ),
        ),
        narrowed=(
            "otto reports nowhere to put a file on such a device -- its filesystem "
            "backend answers `supports_transfer` False -- so there is no landed file "
            "whose mode could be read back"
        ),
        narrowed_detail=(
            "That is a property of the device rather than a defect, and it is otto's own "
            "answer rather than this suite's judgement: `remote_scratch` is `None` for "
            "exactly the hosts whose filesystem reports `supports_transfer` False."
        ),
    ),
    "timeout": Voice(
        short="timeout",
        capability=(
            "give a command a time budget and have a breach reported the documented way, "
            "with the session still usable afterwards"
        ),
        narrowed=(
            "no command there can be made to outlive a budget -- this suite's "
            "vocabulary for such a target declares no `long_running_command`, because "
            "the Zephyr "
            "shell is synchronous on the shell thread, so a stimulus that blocked for "
            "the budget's duration would block the very shell whose survival the other "
            "half of this contract asserts"
        ),
        narrowed_detail=(
            "Driving a single-client console to a timeout and then asserting the session "
            "recovered is also the sequence that has wedged guests before (issue #260). "
            "**None of this says otto's timeouts are broken on such a device** -- it "
            "says the surface has no observable there, which is why the cell is "
            "`not-observable` rather than `measured-broken`. Unlike the transfer "
            "contract's narrowing, this one is the SUITE's answer, not otto's: otto does "
            'not describe a userland as "can be made to block".'
        ),
    ),
}

#: How an ``observed_cells`` outcome reads to someone who does not run the suite.
#: ``xfailed`` is a STRICT xfail: a failure the suite predicted because otto already has
#: the defect registered, which is a different statement from a surprise.
OUTCOME_VOICE = {
    "passed": "passed",
    "xfailed": "failed as the suite expected -- a defect otto already has registered",
    "failed": "**FAILED, and nothing predicted it**",
}

#: Which axis of ``(element, term, transfer)`` varies, and the English that reads right
#: for it. "over `nc`" for a transfer backend, "on `bb1161`" for a device -- swapping
#: the two produces a sentence that is grammatical and wrong.
AXIS_PHRASE = {
    "transfer": "over {}",
    "term": "over the {} console",
    "element": "on {}",
}

_AXES = ("element", "term", "transfer")

#: How many distinct values the grid will name before it gives up and says
#: "partly broken". Two fits a table cell; a list of five does not.
_GRID_VALUE_BUDGET = 2


def _capitalise(text: str) -> str:
    """Return *text* with only its first letter raised -- ``capitalize`` lowers the rest."""
    return text[:1].upper() + text[1:]


def _join(parts: "list[str]") -> str:
    """Join ``[a, b, c]`` into ``a, b and c``."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _code(values: "list[str]") -> "list[str]":
    """Wrap each value in backticks, so a transport reads as an identifier."""
    return [f"`{value}`" for value in values]


def _esc(text: str) -> str:
    """Escape what a Markdown table row cannot hold literally."""
    return text.replace("|", r"\|")


@dataclass(frozen=True)
class Split:
    """One cell's drawn cells, grouped by what they answered.

    ★ THE FIELD THAT STOPS A SCALAR VERDICT LYING. ``busybox-1.16.1`` x
    ``transfer-roundtrip`` is ``measured-broken`` -- and the roundtrip **works over
    `shell`**; only `nc` fails. Rendered as a bare red cell that row tells a reader
    transfer is broken on BusyBox 1.16.1, which is false. Every field here comes from
    ``observed_cells``' own keys.
    """

    entries: "list[dict]"
    varying: "list[str]"
    by_outcome: "dict[str, list[dict]]"

    @property
    def uniform(self) -> bool:
        """Whether every drawn cell answered the same thing."""
        return len(self.by_outcome) <= 1

    def phrase(self, outcome: str) -> str:
        """Name where *outcome* was seen, by the axis that actually varies."""
        return _where(self.by_outcome.get(outcome, []), self.varying)


def _where(entries: "list[dict]", varying: "list[str]") -> str:
    """Name where *entries* were seen, by the axis that varies: "over `nc`", "on `test1`"."""
    if not entries:
        return ""
    if len(varying) == 1 and varying[0] in AXIS_PHRASE:
        values = sorted({entry[varying[0]] for entry in entries})
        return AXIS_PHRASE[varying[0]].format(_join(_code(values)))
    labels = sorted({entry["cell_label"] for entry in entries})
    noun = "drawn cell" if len(labels) == 1 else "drawn cells"
    return f"on the {noun} " + _join(_code(labels))


def split_of(cell: dict) -> Split:
    """Group *cell*'s ``observed_cells`` by outcome, and note which axes vary."""
    entries = list(cell.get("observed_cells", []))
    varying = [axis for axis in _AXES if len({entry[axis] for entry in entries}) > 1]
    by_outcome: "dict[str, list[dict]]" = {}
    for entry in entries:
        by_outcome.setdefault(entry["outcome"], []).append(entry)
    return Split(entries=entries, varying=varying, by_outcome=by_outcome)


def _devices(count: int) -> str:
    """Count devices as ``1 device`` / ``4 devices``, so no sentence says "device(s)"."""
    return f"{count} device" if count == 1 else f"{count} devices"


def _all_drawn(count: int) -> str:
    """Say a whole cell's drawn cells, never as "the 1 drawn cells".

    A profile whose device offers exactly one ``(console, transfer)`` route has ONE
    drawn cell, and every `zephyr-2.7` and `zephyr-4.4` row is such a row -- so the
    plural is not a rare shape here, it is a third of the page. Task 6b fixed this
    class one clause over, in the control citation, and left these.
    """
    return "its only drawn cell" if count == 1 else f"every one of the {count} drawn cells"


def _all_of(count: int) -> str:
    """Say "the only one" / "all 4", so no evidence bullet reads "all 1"."""
    return "the only one" if count == 1 else f"all {count}"


def _coverage_note(coverage: CellCoverage) -> str:
    """Say ``all 4 devices`` / ``2 of 4 devices`` / ``no device``, for the verdict column."""
    total, rests_on = len(coverage.elements), len(coverage.observed_on)
    if rests_on == 0:
        return "no device"
    if rests_on == total:
        return "its only device" if total == 1 else f"all {_devices(total)}"
    return f"{rests_on} of {_devices(total)}"


def grid_token(surface_id: str, cell: dict, coverage: CellCoverage) -> str:
    """Say one at-a-glance cell: short, and never uniform when the evidence is not.

    ``` `shell` only ``` rather than ``broken`` is the whole reason this function
    exists: the reader scanning the grid for a BusyBox device must not have to open a
    section to learn that the failure is one transport wide.

    ★ AND IT DELIBERATELY DOES NOT CONSULT THE ROUTE'S ``positive_control``, which is
    a judgement worth stating rather than leaving to be noticed. A grid token reports
    WHERE THE CONTRACT PASSED -- a fact read straight off ``outcome`` and true whether
    or not a control backed it. The word that needs a control is the PROMISE, *"you
    can ..."*, and that lives in :func:`meaning`, which does gate on the citation. A
    table cell cannot carry a nodeid, so holding the grid to the citation rule would
    mean suppressing a measured fact rather than qualifying a claim.

    ★ IT DOES CONSULT THE CELL'S OWN OBSERVABLE, and that is a different judgement. A
    citation is evidence FOR a claim; an observable says WHICH CLAIM was measured at
    all. The grid is read first and by people in a hurry, so publishing ``works`` under
    a column headed *file mode* for a device on which otto refuses a mode is the same
    false promise :class:`Branch` records one level down -- and it is worse here,
    because a reader who trusts the grid never opens the section that would correct it.
    """
    status = cell["status"]
    if status == "untested":
        return "untested"
    if status == "not-observable":
        return "not observable"
    partial = (
        ""
        if not (coverage.not_observable or coverage.unaccounted)
        else (f" ({len(coverage.observed_on)} of {len(coverage.elements)})")
    )
    promise = promise_of(surface_id, cell)
    if not promise.capability:
        return f"{promise.grid_word}{partial}"
    if status == "measured-ok":
        return f"works{partial}"
    split = split_of(cell)
    passed = split.by_outcome.get("passed", [])
    if not passed:
        return f"broken{partial}"
    if len(split.varying) == 1 and split.varying[0] in AXIS_PHRASE:
        values = sorted({entry[split.varying[0]] for entry in passed})
        if len(values) <= _GRID_VALUE_BUDGET:
            return f"{_join(_code(values))} only{partial}"
    return f"partly broken{partial}"


def _remainder_sentences(surface_id: str, coverage: CellCoverage) -> "list[str]":
    """Say what the verdict does NOT rest on: cannot-be-measured, and never-drawn.

    Two sentences and never one. Folding "no run has drawn this device yet" into
    "this device cannot express the observable" is the error §5 names in its other
    direction, and it is the difference between a blank and a measurement.
    """
    said: "list[str]" = []
    if coverage.not_observable:
        said.append(
            f"{_join(_code(coverage.not_observable))} could not be measured: "
            f"{VOICE[surface_id].narrowed}"
        )
    if coverage.unaccounted:
        said.append(
            f"{_join(_code(coverage.unaccounted))} "
            f"{'has' if len(coverage.unaccounted) == 1 else 'have'} never been drawn by "
            f"any run, so nothing at all is claimed about "
            f"{'it' if len(coverage.unaccounted) == 1 else 'them'} -- that is a blank, "
            f"not a verdict"
        )
    return said


def _tail(sentences: "list[str]") -> str:
    """Join the remainder clauses on, each as a sentence of its own."""
    if not sentences:
        return ""
    return " " + ". ".join(_capitalise(sentence) for sentence in sentences) + "."


@dataclass(frozen=True)
class Promise:
    """What ONE CELL's own observable lets this page say about it.

    Either :attr:`capability` is set and the row may make the promise, or it is empty
    and the row says :attr:`headline` + :attr:`instead` instead. Never both.
    """

    capability: str = ""
    headline: str = ""
    instead: str = ""
    grid_word: str = ""


_UNINTERPRETED = Promise(
    headline="Measured, and deliberately not interpreted.",
    instead=(
        "a run passed here, but what it watched matches none of the observables this "
        "page carries a promise for, so this row claims nothing beyond the record "
        "beneath it"
    ),
    grid_word="not interpreted",
)
"""What a cell whose observable fits no declared arm is allowed to say.

FAIL CLOSED, and never back to the surface's own promise -- an observable this file
cannot place is exactly the state in which the constant published a sentence nobody
had checked. :func:`promise_mismatch` reports the same cell, so ``make docs`` fails
before publishing one. This is what the page says if one reaches it anyway, and it
can: the renderer runs from ``docs/conf.py`` over whatever ``schemas/support_matrix.json``
holds and validates nothing, so a hand-edit reaches it at build time.
"""

_UNWATCHED_STATES = frozenset({"untested", "not-observable"})
"""The two states whose sentences are ABOUT the promise not having been watched.

A WHITELIST, and that direction is the whole point. :func:`promise_of` keeps the
surface's promise for these and fails closed for everything else, so a fifth status
added to the schema tomorrow reaches the fail-closed arm rather than the fall-back
one. Written as "not in this set" and never as "is a measured one" for that reason.
"""


def promise_of(surface_id: str, cell: dict) -> Promise:
    """Answer what *cell*'s OWN observable licenses, not what its surface promises at large.

    ★ THE LINE THAT WAS MISSING. :func:`meaning` composed "**Yes.** You can {capability}"
    from a per-surface CONSTANT, so a cell could store an observable saying otto refuses
    the thing and still publish the promise. Nothing related the sentence to the field,
    and no guard could see it.

    A surface with one observable is unchanged: its promise is unconditional and
    :attr:`Voice.capability` is it. A surface with declared :attr:`Voice.branches` is
    answered from the CELL, and a cell that matches no arm -- or two -- gets no promise.

    ``untested`` and ``not-observable`` cells carry no observable, and they are the two
    states whose sentences are ABOUT the promise not having been watched. Those keep the
    surface's promise: "no run has yet asked whether you can ..." is true of the surface
    whichever arm a future run would take.

    ★ AND EVERY OTHER STATE WITHOUT AN OBSERVABLE FAILS CLOSED. The schema requires
    ``observable`` on both ``measured-*`` states, so a MEASURED cell without one cannot
    be collated -- it can only be hand-edited in. MEASURED 2026-08-25 before this
    branch existed: deleting the key from `transfer-mode` x `zephyr-2.7` returned the
    surface's constant, and the page published *"**Yes.** You can put a file and have
    the permission mode you asked for land on it"* about a device that refuses one --
    the ORIGINAL blocking defect, resurrected, with :func:`promise_mismatch` answering
    ``[]`` and the build green. The absent field is exactly the state in which this
    file knows least about a cell, so it is the last state that may license a promise.
    """
    voice = VOICE[surface_id]
    if not voice.branches:
        return Promise(capability=voice.capability)
    observable = cell.get("observable")
    if not observable:
        if cell.get("status") in _UNWATCHED_STATES:
            return Promise(capability=voice.capability)
        return _UNINTERPRETED
    matched = [branch for branch in voice.branches if branch.marker in observable]
    if len(matched) != 1:
        return _UNINTERPRETED
    return Promise(
        capability=matched[0].capability,
        headline=matched[0].headline,
        instead=matched[0].instead,
        grid_word=matched[0].grid_word,
    )


def _scope(coverage: CellCoverage) -> str:
    """Name WHERE a verdict was watched: the devices, its only one, or all of them."""
    if len(coverage.observed_on) != len(coverage.elements):
        return f"on {_join(_code(coverage.observed_on))}"
    if len(coverage.elements) == 1:
        return "on its only device"
    return f"on all {_devices(len(coverage.elements))} in this profile"


def meaning(surface_id: str, cell: dict, coverage: CellCoverage) -> str:
    """Compose the "What it means for you" column from the cell's own fields.

    ★ THE PROMISE FOLLOWS THE CELL'S OWN OBSERVABLE. Which sentence a measured cell
    gets is :func:`promise_of`'s answer, read from what the run said it watched, and
    not a constant belonging to the column heading. See :class:`Branch` for the defect
    that made this necessary.
    """
    voice = VOICE[surface_id]
    promise = promise_of(surface_id, cell)
    status = cell["status"]
    if status == "untested":
        return (
            f"**Not measured.** No run has yet asked whether you can {voice.capability} "
            f"here. `untested` means untested, **not unsupported**: otto does not refuse "
            f"it, and the first run against such a device is the measurement."
        )
    remainder = _remainder_sentences(surface_id, coverage)
    if status == "not-observable":
        # Only the never-drawn half of the remainder: the could-not-be-measured half IS
        # this cell, and repeating it would read as though a SECOND set of devices had
        # also been excluded. A not-observable cell CAN still have undrawn devices --
        # `not_observable` need not exhaust the profile -- and that has to be said.
        never_drawn = remainder[-1:] if coverage.unaccounted else []
        return (
            f"**Cannot be measured here, and that is not the same as broken.** The "
            f"promise -- that you can {voice.capability} -- has nothing on such a device "
            f"to be watched against: {voice.narrowed}. otto refuses nothing here; no run "
            f"has been able to look." + _tail(never_drawn)
        )
    tail = _tail(remainder)
    split = split_of(cell)
    passed = split.by_outcome.get("passed", [])
    broken = [
        entry
        for outcome, group in split.by_outcome.items()
        if outcome != "passed"
        for entry in group
    ]
    unexpected = any(entry["outcome"] == "failed" for entry in broken)
    predicted = (
        # NAMED AS SOMEBODY ELSE'S NEWS, at the point of use. A strict xfail means otto
        # already has the defect registered and documented; a row that did not say so
        # would read as though this page had discovered it.
        "a failure the suite predicted, because otto already carries this as a "
        "registered gap ({doc}`subsystems/busybox-support`)"
        if not unexpected
        else "**a failure nothing predicted**, so it is not a registered gap"
    )
    if not promise.capability:
        # ★ THIS CELL'S CONTRACT WATCHED SOMETHING THAT IS NOT THE PROMISE, so no
        # sentence here contains "you can". Every other branch below composes one,
        # which is why this returns before any of them rather than qualifying them:
        # a hardened return protects a branch, and only an early return protects an
        # action. The outcome is still reported, because the run really did happen.
        if status == "measured-ok":
            saw = f"{_all_drawn(len(split.entries))} passed"
        elif passed:
            saw = (
                f"it held {split.phrase('passed')} and failed "
                f"{_where(broken, split.varying)} -- {predicted}"
            )
        else:
            saw = f"every drawn cell failed -- {predicted}"
        return (
            f"**{promise.headline}** {_capitalise(promise.instead)}. Watched "
            f"{_scope(coverage)}, and {saw}.{tail}"
        )
    uncited = [entry for entry in passed if not entry.get("positive_control")]
    if status == "measured-ok":
        if uncited:
            # ★ THE SAME RULE AS THE MIXED ROW BELOW, ON THE STATUS THAT MAKES THE
            # STRONGER CLAIM. MEASURED 2026-08-25: stripping the citations from a
            # `measured-ok` cell left this branch saying "**Yes.** You can put a file
            # on the device and get the same bytes back" DIRECTLY ABOVE its own
            # evidence line "*Positive control:* **none** ... so the pass there is an
            # observation and not a guarantee" -- the page contradicting itself in one
            # section. Task 6b found that shape on a mixed cell and closed it there;
            # `meaning` and `_control_lines` now read the SAME predicate, so the two
            # halves cannot disagree whatever the artifact holds.
            return (
                f"**It passed, and nothing proved it could fail there.** No positive "
                f"control passed {_where(uncited, split.varying)}, so that result is an "
                f"observation rather than a guarantee -- watched {_scope(coverage)}, and "
                f"{_all_drawn(len(split.entries))} passed.{tail}"
            )
        return (
            f"**Yes.** You can {promise.capability} -- watched {_scope(coverage)}, and "
            f"{_all_drawn(len(split.entries))} passed.{tail}"
        )
    if passed:
        if uncited:
            # ★ A POSITIVE CLAIM NEEDS A CONTROL, AND THIS ROUTE HAS NONE.
            # "You can do this over `shell`" is a `measured-ok`-strength claim
            # about one route, and MEASURED 2026-08-25 all ten mixed cells made
            # it while citing nothing -- the cell-level field cannot be written
            # for a mixed cell, so the passing route's control was collected
            # and discarded. The evidence is now per route, and where it is
            # missing the page reports what was seen instead of promising it.
            return (
                f"**It passed {split.phrase('passed')}, and nothing proved it could "
                f"fail there.** No positive control passed "
                f"{_where(uncited, split.varying)}, so that result is an observation "
                f"rather than a guarantee; {_where(broken, split.varying)} it fails -- "
                f"{predicted}.{tail}"
            )
        return (
            f"**Only {split.phrase('passed')}.** You can {promise.capability} "
            f"{split.phrase('passed')}; {_where(broken, split.varying)} it fails -- "
            f"{predicted}. Nothing here says the device cannot do it at all.{tail}"
        )
    return (
        f"**No.** Every drawn cell failed when asked to {promise.capability} -- {predicted}.{tail}"
    )


def _evidence(surface_id: str, cell: dict, coverage: CellCoverage) -> "list[str]":
    """List the provenance bullets under a profile's table: what each verdict rests on."""
    status = cell["status"]
    title = next(surface.title for surface in SURFACES if surface.id == surface_id)
    head = f"- **{title}** -- `{status}`"
    if status == "untested":
        return [f"{head}. No record; nothing measured."]
    head += (
        f", {_coverage_note(coverage)}, measured {cell['as_of']} (UTC) "
        f"in the `{cell['venue']}` venue."
    )
    lines = [head]
    if "observable" in cell:
        lines.append(f"  - *What was watched:* {cell['observable']}")
    split = split_of(cell)
    if split.entries:
        if split.uniform:
            outcome = next(iter(split.by_outcome))
            lines.append(
                f"  - *Drawn cells:* {_all_of(len(split.entries))} {OUTCOME_VOICE[outcome]}."
            )
        else:
            parts = [
                f"{split.phrase(outcome)} {OUTCOME_VOICE[outcome]}"
                for outcome in sorted(split.by_outcome)
            ]
            lines.append(f"  - *Drawn cells:* {_join(parts)}.")
    lines.extend(
        f"  - *Not observable on* `{entry['element']}`: probed "
        f"`{entry['probed']}` -> {entry['probe_result']}"
        for entry in cell.get("not_observable", [])
    )
    if cell.get("not_observable") and VOICE[surface_id].narrowed_detail:
        lines.append(f"  - *Why that is not a failure:* {VOICE[surface_id].narrowed_detail}")
    if coverage.unaccounted:
        lines.append(
            f"  - *Never drawn:* {_join(_code(coverage.unaccounted))} -- no run has "
            f"reached {'it' if len(coverage.unaccounted) == 1 else 'them'} yet."
        )
    if "probed" in cell and not cell.get("not_observable"):
        # Only when the per-element list did NOT already say it. The cell-level
        # `probed` is the semicolon-joined union of those probes, so printing both
        # is the same evidence twice and buries the part a reader needs.
        lines.append(f"  - *Probed:* `{cell['probed']}` -> {cell['probe_result']}")
    if "failure_summary" in cell:
        # Reproduced VERBATIM, never re-punctuated -- it is evidence. The one thing
        # added is a note when the collate step CUT it, which is a length comparison
        # against that step's own constant rather than a reading of the text.
        cut = (
            " *(cut short by the collate step at "
            f"{FAILURE_SUMMARY_LIMIT} characters; the whole text is in the observation "
            "record the run left behind)*"
            if len(cell["failure_summary"]) >= FAILURE_SUMMARY_LIMIT
            else ""
        )
        lines.append(f"  - *What failed:* {cell['failure_summary']}{cut}")
    if "nodeid" in cell:
        lines.append(f"  - *Contract:* `{cell['nodeid']}`")
    lines += _control_lines(cell, split)
    return lines


def _control_lines(cell: dict, split: Split) -> "list[str]":
    """Cite the control behind every route this page claims positively.

    ★ THE CITATION FOLLOWS THE CLAIM. A ``measured-ok`` cell makes one claim
    about the whole cell and cites the cell-level ``positive_control``; the
    collate step cannot write that field unless EVERY contributing route was
    controlled, so one nodeid stands for all of them and the line says how
    many. A mixed cell makes a claim about ONE ROUTE -- *"only over
    ``shell``"* -- and MEASURED 2026-08-25 it cited nothing at all, because the
    cell-level field is unwritable there. Those cite per route.

    A passing route with NO citation is named as such rather than passed over
    in silence: :func:`meaning` has already downgraded the sentence above from
    a promise to an observation, and a reader looking for the reason must find
    it in the evidence rather than have to notice the wording changed.
    """
    uncited = [
        entry for entry in split.by_outcome.get("passed", []) if not entry.get("positive_control")
    ]
    # EVERY CITED ROUTE, WHATEVER IT ANSWERED. A control that passed on a route
    # whose CONTRACT failed is not noise: it says the instrument could tell a
    # wrong answer from a right one there, so the failure is the product's and
    # not the check's blindness. Dropping those would be a silent omission of
    # evidence the artifact holds, which is this page's own subject.
    cited = [entry for entry in split.entries if entry.get("positive_control")]
    if not cited and not uncited:
        return (
            [f"  - *Positive control:* `{cell['positive_control']}`"]
            if "positive_control" in cell
            else []
        )
    lines: "list[str]" = []
    if uncited:
        lines.append(
            f"  - *Positive control:* **none** {_where(uncited, split.varying)} -- so the "
            f"pass there is an observation and not a guarantee."
        )
    if cited and not uncited and split.uniform and "positive_control" in cell:
        # ONE NODEID STANDS FOR ALL OF THEM HERE, and the clause says how many
        # rather than leaving a reader to assume a single route was controlled:
        # the collate step cannot write the cell-level field unless EVERY
        # contributing route had a control of its own pass on it.
        others = len(cited) - 1
        head = f"  - *Positive control:* `{cell['positive_control']}`"
        if others == 1:
            head += " -- and the other drawn cell carries a control of its own."
        elif others > 1:
            head += (
                f" -- and each of the other {others} drawn cells carries a control of its "
                f"own, on its own route."
            )
        lines.append(head)
    else:
        lines += [
            f"  - *Positive control {_where([entry], split.varying)}:* "
            f"`{entry['positive_control']}`"
            for entry in cited
        ]
    return lines


def _slug(profile_id: str) -> str:
    """Build a Sphinx label for a profile section. Dots out, so the ref is unambiguous."""
    return "matrix-profile-" + profile_id.replace(".", "-")


def axes_mismatch(matrix: dict) -> "list[str]":
    """List every way the artifact's axes disagree with the ones THIS TREE declares.

    ★ SPEC §5'S FAIL-ON-UNDECLARED, and it is a build FAILURE rather than a warning:
    ``docs/conf.py`` raises on a non-zero exit, so a renamed profile or a deleted
    contract stops ``make docs`` instead of publishing a verdict about something that
    no longer exists. The tree is asked three questions, not one -- the contract
    modules (by AST walk), :data:`~tests._fixtures.support_matrix.SURFACES`, and
    :func:`~tests._fixtures.support_matrix.discover_profiles` -- because a surface can
    go missing from any of them independently.
    """
    problems: "list[str]" = []
    declared = {surface.contract for surface in SURFACES}
    walked = set(discover_contracts())
    for contract in sorted(declared - walked):
        problems.append(
            f"SURFACES declares the contract {contract!r}, which the tree no longer has"
        )
    for contract in sorted(walked - declared):
        problems.append(f"the tree has a contract {contract!r} that SURFACES does not declare")

    tree_surfaces = {surface.id: surface for surface in SURFACES}
    artifact_surfaces = {entry["id"]: entry for entry in matrix["surfaces"]}
    for surface_id in sorted(set(artifact_surfaces) - set(tree_surfaces)):
        problems.append(f"the artifact holds a surface {surface_id!r} the tree no longer declares")
    for surface_id in sorted(set(tree_surfaces) - set(artifact_surfaces)):
        problems.append(f"the tree declares a surface {surface_id!r} the artifact has no row for")
    for surface_id in sorted(set(tree_surfaces) & set(artifact_surfaces)):
        for field in ("title", "contract"):
            was = artifact_surfaces[surface_id][field]
            now = getattr(tree_surfaces[surface_id], field)
            if was != now:
                problems.append(
                    f"surface {surface_id!r}: the artifact's {field} is {was!r}, "
                    f"the tree's is {now!r}"
                )

    tree_profiles = {profile.id: list(profile.elements) for profile in discover_profiles()}
    artifact_profiles = {entry["id"]: list(entry["elements"]) for entry in matrix["profiles"]}
    for profile_id in sorted(set(artifact_profiles) - set(tree_profiles)):
        problems.append(f"the artifact holds a profile {profile_id!r} the tree no longer declares")
    for profile_id in sorted(set(tree_profiles) - set(artifact_profiles)):
        problems.append(
            f"the tree declares a profile {profile_id!r} the artifact has no column for"
        )
    for profile_id in sorted(set(tree_profiles) & set(artifact_profiles)):
        if artifact_profiles[profile_id] != tree_profiles[profile_id]:
            problems.append(
                f"profile {profile_id!r}: the artifact's devices are "
                f"{artifact_profiles[profile_id]}, the tree's are {tree_profiles[profile_id]}"
            )

    for surface_id in sorted(set(tree_surfaces) & set(artifact_surfaces)):
        row = matrix["cells"].get(surface_id, {})
        for profile_id in sorted(set(tree_profiles) & set(artifact_profiles)):
            if profile_id not in row:
                problems.append(f"the artifact has no cell for {surface_id!r} x {profile_id!r}")

    for surface_id in sorted(set(tree_surfaces) - set(VOICE)):
        problems.append(
            f"surface {surface_id!r} has no entry in scripts/render_support_matrix.py's VOICE, "
            f"so the page could only render it as a blank column"
        )
    for surface_id in sorted(set(VOICE) - set(tree_surfaces)):
        problems.append(f"VOICE describes a surface {surface_id!r} the tree no longer declares")
    for profile_id in sorted(tree_profiles):
        if profile_blurb(profile_id) is None:
            problems.append(
                f"profile {profile_id!r} matches no family in "
                f"scripts/render_support_matrix.py's FAMILY_BLURB, so its section could "
                f"only describe nothing"
            )
    return problems


def code_spans(text: str) -> "set[str]":
    """Answer the ``code spans`` in *text* -- what the narrowing pin below is written in.

    A split on backticks rather than a regex, because
    ``tests/unit/test_support_matrix.py`` bans ``re`` from this file outright: a
    renderer that reached for one would be recovering an artifact FIELD by parsing
    English, which is the failure Task 4b made ``observed_cells`` structural to
    prevent. This reads :data:`VOICE`, which is this file's own hand-written prose
    and not a field of anything, and it reads it in order to CHECK it.
    """
    return set(text.split("`")[1::2])


_DOMAIN_PREDICATE = "applicable_cell"
"""The function a contract module declares to say which drawn cells it is about.

Read from the module by name because that is the name
``tests/conformance/conftest.py``'s ``pytest_generate_tests`` looks up; a module that
declares none narrows nothing, and today the three ``exec`` surfaces share such a
module.
"""


@dataclass(frozen=True)
class _Domain:
    """One contract module's domain rule, as text and as the names it reads."""

    source: str
    """The whole ``applicable_cell`` definition, docstring included."""

    reads: "frozenset[str]"
    """The attributes its BODY consults.

    Its own parameter names are excluded, and so is everything outside the body --
    the signature's annotations, the decorators, the docstring. A clause that named
    ``resolved`` or ``ResolvedCell`` would otherwise satisfy the second check below
    while saying nothing about what the rule actually reads, and a docstring
    contributes no ``Name`` or ``Attribute`` node at all.
    """


def _domain_rule(module: Path) -> "_Domain | None":
    """*module*'s ``applicable_cell``, or ``None`` if it declares none.

    Read by AST rather than by import: this runs inside a Sphinx build, and importing
    a conformance module there would drag pytest's collection machinery into the docs
    build for a question about source text.
    """
    text = module.read_text(encoding="utf-8")
    for node in ast.parse(text).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != _DOMAIN_PREDICATE:
            continue
        reads = {
            child.attr if isinstance(child, ast.Attribute) else child.id
            for statement in node.body
            for child in ast.walk(statement)
            if isinstance(child, (ast.Attribute, ast.Name))
        } - {argument.arg for argument in node.args.args}
        return _Domain(source=ast.get_source_segment(text, node) or "", reads=frozenset(reads))
    return None


def narrowing_mismatch(matrix: dict) -> "list[str]":
    """List every way :data:`VOICE`'s narrowing prose has come loose from its RULE.

    ★ WHY THIS EXISTS. A ``not-observable`` cell renders a hand-written clause saying
    WHY a contract put those devices outside its domain, and nothing tied that sentence
    to the predicate it describes. The drift that matters is not a docstring edit: it is
    the DOMAIN RULE changing while the page keeps publishing the old reason, which is
    the one direction a reader cannot detect -- the verdict, the device list and the
    probe would all still be right.

    TWO CHECKS, AND THEY FAIL ON DIFFERENT MISTAKES.

    1. **Every code span in the clause must appear in that contract's own
       ``applicable_cell``.** So the page may not name a mechanism the rule does not
       mention, and renaming what the rule consults reddens the build.
    2. **The clause (with its detail) must name at least one identifier the
       predicate's BODY reads.** Check 1 alone is satisfied by a clause that names
       nothing at all, which is this item's signature defect: a guard that a vacuous
       input passes. Today that identifier is ``remote_scratch`` for both transfer
       surfaces and ``long_running_command`` for timeout.

    A MODULE THAT DECLARES NO ``applicable_cell`` NARROWS NOTHING, so its surfaces can
    never render the clause at all. Those are held to the mirror of the same rule: the
    clause must name no mechanism, and no cell of that surface may carry a
    ``not_observable`` entry -- if one ever does, this file is publishing a reason
    nothing produced.

    THE PROSE ITSELF IS DELIBERATELY NOT PINNED, and
    ``tests/unit/test_docs_gap_sync.py`` is the precedent: pinning paragraphs verbatim
    "would buy a copying ritual rather than a check -- it would redden on a typo fix and
    stay green on a lie".
    """
    problems: "list[str]" = []
    for surface in SURFACES:
        voice = VOICE.get(surface.id)
        if voice is None:
            continue  # `axes_mismatch` owns the missing-entry report.
        module = PROJECT_ROOT / surface.contract.split("::")[0]
        rule = _domain_rule(module)
        spans = code_spans(voice.narrowed)
        if rule is None:
            if spans:
                problems.append(
                    f"surface {surface.id!r}: its narrowing clause names "
                    f"{sorted(spans)}, but {module.name} declares no "
                    f"{_DOMAIN_PREDICATE}() -- it narrows nothing, so that reason "
                    f"describes something no run can produce"
                )
            for profile_id, cell in sorted(matrix["cells"].get(surface.id, {}).items()):
                if cell.get("not_observable"):
                    problems.append(
                        f"{surface.id!r} x {profile_id!r} carries a not_observable "
                        f"entry, but {module.name} declares no {_DOMAIN_PREDICATE}() "
                        f"to have excluded anything"
                    )
            continue
        for span in sorted(spans):
            if span not in rule.source:
                problems.append(
                    f"surface {surface.id!r}: its narrowing clause names `{span}`, "
                    f"which {module.name}'s {_DOMAIN_PREDICATE}() does not mention -- "
                    f"the page would be publishing a reason the rule no longer gives"
                )
        named = code_spans(f"{voice.narrowed} {voice.narrowed_detail}")
        if not named & rule.reads:
            problems.append(
                f"surface {surface.id!r}: its narrowing prose names none of the "
                f"identifiers {module.name}'s {_DOMAIN_PREDICATE}() actually reads "
                f"({sorted(rule.reads)}), so nothing ties the reason to the rule"
            )
    return problems


def promise_mismatch(matrix: dict) -> "list[str]":
    """List every way a surface's PROMISE has come loose from what its contract watches.

    ★ WHY THIS EXISTS. :data:`VOICE`'s ``capability`` was a per-surface CONSTANT, and
    :func:`meaning` composed "**Yes.** You can {capability}" from it for every
    ``measured-ok`` cell. Three published rows therefore promised the OPPOSITE of what
    the cell beside them had stored: `transfer-mode` on all three Zephyr profiles said
    *you can have the permission mode land on the file*, while the cell's own
    ``observable`` said what was watched was otto REFUSING a mode. Nothing in this file
    or in the suite related the sentence to the field, so no guard could see it.

    FOUR CHECKS, EACH FAILING ON A DIFFERENT MISTAKE.

    1. **Every arm's ``marker`` appears verbatim in that contract's own module.** The
       marker is the one question this file asks of an ``observable``, so rewording the
       arm that writes it must fail the build rather than silently returning the page
       to the wrong promise.
    2. **A refusal arm is complete**: it declares the headline, the clause and the grid
       word, and it declares no capability. Half a refusal arm renders an empty bold
       lead or a blank grid cell, which is the missing-entry defect this item keeps
       finding one level down.
    3. **Every code span in a refusal arm's ``instead`` appears in that module.** The
       rule :func:`narrowing_mismatch` applies to the other piece of hand-written prose
       on this page, applied to this one: the page may not name a mechanism the
       contract does not.
    4. **Every measured cell's own ``observable`` matches exactly ONE declared arm** --
       and a measured cell HAS one. Zero arms means a contract grew an arm nobody
       described; two means the markers do not discriminate; no observable at all means
       nothing on the cell says what was watched. In each case the page cannot tell
       which promise the cell licenses, and :func:`promise_of` publishes none -- this
       makes that a build failure rather than a quiet degradation.

    A SURFACE WITH ONE OBSERVABLE DECLARES NO ARMS and is skipped: its promise is
    unconditional, and there is nothing for a cell to disagree with.
    """
    problems: "list[str]" = []
    for surface in SURFACES:
        voice = VOICE.get(surface.id)
        if voice is None or not voice.branches:
            continue  # `axes_mismatch` owns the missing-entry report.
        module = PROJECT_ROOT / surface.contract.split("::")[0]
        source = module.read_text(encoding="utf-8")
        for branch in voice.branches:
            if branch.marker not in source:
                problems.append(
                    f"surface {surface.id!r}: it tells an observable arm apart by "
                    f"{branch.marker!r}, which {module.name} no longer writes -- the "
                    f"page would place every cell on whichever arm still matches"
                )
            if branch.capability:
                if branch.headline or branch.instead or branch.grid_word:
                    problems.append(
                        f"surface {surface.id!r}: the arm {branch.marker!r} declares a "
                        f"capability AND a refusal; a cell licenses one or the other"
                    )
                continue
            if not (branch.headline and branch.instead and branch.grid_word):
                problems.append(
                    f"surface {surface.id!r}: the refusal arm {branch.marker!r} is "
                    f"incomplete -- a refusal needs a headline, a clause and a grid "
                    f"word, or the row and the grid publish a blank"
                )
            for span in sorted(code_spans(branch.instead)):
                if span not in source:
                    problems.append(
                        f"surface {surface.id!r}: the arm {branch.marker!r} tells a "
                        f"reader about `{span}`, which {module.name} does not mention"
                    )
        for profile_id, cell in sorted(matrix["cells"].get(surface.id, {}).items()):
            observable = cell.get("observable")
            if not observable:
                # ★ THE OTHER HALF OF THE FAIL-CLOSED. A bare `continue` here made the
                # missing field the ONE way past both defences at once: `promise_of`
                # fell back to the surface's promise and this check walked past the
                # cell, so a hand-edit deleting the key published the promise with a
                # green build. Fail-closed rendering alone would only downgrade the
                # row; reporting it is what stops `make docs`.
                if cell.get("status") not in _UNWATCHED_STATES:
                    problems.append(
                        f"{surface.id!r} x {profile_id!r}: it is {cell.get('status')!r} "
                        f"and declares no observable, so nothing says which of the "
                        f"{len(voice.branches)} observables VOICE declares for this "
                        f"surface it watched -- the page cannot tell which promise, if "
                        f"any, this cell licenses"
                    )
                continue
            matched = [branch for branch in voice.branches if branch.marker in observable]
            if len(matched) != 1:
                problems.append(
                    f"{surface.id!r} x {profile_id!r}: its observable matches "
                    f"{len(matched)} of the {len(voice.branches)} observables VOICE "
                    f"declares for this surface, not 1 -- the page cannot tell which "
                    f"promise, if any, this cell licenses"
                )
    return problems


def _states_section() -> "list[str]":
    """Render the four states, each answering "what does this mean for me"."""
    return [
        "## How to read a verdict",
        "",
        "`measured-ok`",
        ": A run drove the promise against a device in this profile, watched the thing the",
        "  promise is about, and it held. The cell names that **observable** and the",
        "  **positive control** that proves the observable can go red -- see",
        "  {ref}`what that is worth, and where it stops <matrix-guarantee>`.",
        "",
        "`measured-broken`",
        ": A run drove it and watched it fail, and the cell carries what the failure said.",
        "  Read the row rather than the word: a cell here can be broken over one transport",
        "  and fine over another, and a failure here may be one otto **already has",
        "  registered** rather than news. Each row says which, and the count is below.",
        "",
        "`not-observable`",
        ": **A measurement, not the absence of one.** The contract that owns this surface",
        "  declared the device outside its own domain -- there is nothing on that device the",
        "  promise could be watched against. It does not mean otto refuses, and it does not",
        "  mean the surface is broken; each cell below names the exact probe and what it",
        "  answered, so you can check rather than trust.",
        "",
        "`untested`",
        ": Nobody has run it against this profile. otto does **not** block it -- it runs, and",
        "  the outcome is the measurement this cell is waiting for. **`untested` means",
        "  untested, not unsupported**, the same stance",
        "  {doc}`subsystems/busybox-support` takes: blocking an untested surface would turn",
        '  "we do not know" into "does not work", which is a lie in the expensive direction.',
        "",
        "```{warning}",
        "**Two things one word cannot say, and both are on this page rather than behind it.**",
        "",
        "1. **A verdict is scalar; the measurement is not.** The bed measures an",
        "   `(device, console, transfer backend)` cell, and one device can answer two",
        "   different things -- BusyBox file transfer **works over `shell` and fails over",
        "   `nc`**. So every cell whose drawn cells disagreed says which ones did what,",
        "   read from the artifact's own per-cell fields rather than recovered from prose.",
        "2. **A profile is not capability-uniform.** A verdict may rest on some of a",
        "   profile's devices and not others, and there are two different reasons a device",
        "   can be missing: it **cannot express the observable**, or **no run has drawn it",
        "   yet**. Those are different claims about your hardware and they are never merged",
        "   here -- a blank is not a refusal, and a refusal is not a blank.",
        "```",
        "",
    ]


def _currency_section(matrix: dict, rendered_on: "_datetime.date") -> "list[str]":
    """Render staleness FIRST and loud (ruling of 2026-08-24, Chris).

    Nothing refreshes this artifact. Every row's currency depends on a person running
    ``make conformance-bed`` on the one machine with a lab attached, and a confidently
    rendered three-month-old verdict is worse than a blank -- so the dates come before
    the grid rather than in a footnote after it.
    """
    dates = sorted(
        cell["as_of"]
        for row in matrix["cells"].values()
        for cell in row.values()
        if "as_of" in cell
    )
    counts: "dict[str, int]" = {}
    for row in matrix["cells"].values():
        for cell in row.values():
            counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    total = sum(counts.values())
    spread = (
        "no cell carries a date yet"
        if not dates
        else f"measured **{dates[0]}**"
        if dates[0] == dates[-1]
        else f"measured between **{dates[0]}** and **{dates[-1]}**"
    )
    tally = ", ".join(f"{counts[state]} `{state}`" for state in sorted(counts))
    return [
        "```{warning}",
        "**Read the dates before you read the verdicts. Nothing on this page refreshes",
        "itself.**",
        "",
        f"The {total} cells below were {spread} (**UTC**, not local time -- a run late on one",
        "evening dates to the next day). Today's mix: " + tally + ".",
        "",
        "Every one of them came from `make conformance-bed`, which needs a physical lab of",
        "VMs and QEMU guests and therefore runs on **one developer machine**, by hand. CI",
        "cannot re-measure a single row: it has no lab. So a cell is exactly as current as",
        "the last time somebody ran the bed lane and committed the result, and a date that",
        "has drifted months into the past is telling you the truth about how much you should",
        "trust the verdict beside it.",
        "",
        f"This page was rendered on {rendered_on.isoformat()} from",
        "`schemas/support_matrix.json`; subtract to get the age of any row.",
        "```",
        "",
    ]


def _guarantee_section() -> "list[str]":
    """Render what green means, and three honest limits on it."""
    return [
        "(matrix-guarantee)=",
        "",
        "## What a `measured-ok` cell guarantees",
        "",
        "A cell may not say `measured-ok` without naming two things: the **observable** it",
        "watched, and a **positive control** -- a test that deliberately makes that",
        "observable go red, and that is required to *fail* on a host where the observable",
        "cannot move. Without the control a contract that asserted nothing at all would",
        "publish green, which is the guards-that-cannot-fail defect promoted into a",
        "*published* artifact -- worse than the silent version, because a matrix is what",
        "people read instead of the tests.",
        "",
        "**And the rule follows the claim down to the route.** A `measured-broken` row that",
        "still says *only over `shell`* is making a `measured-ok`-strength promise about one",
        "route, so that route names its own control, on its own drawn cell -- not merely one",
        "run somewhere on the same device. A device can offer four transfer backends, and a",
        "control that proved the check could fail over `scp` says nothing about `nc`. Where a",
        "route passed and no control passed beside it, the collate step refuses the cell",
        "outright; if such a cell ever reaches this page anyway, the row reports what was",
        "seen and stops short of promising it.",
        "",
        "Three limits, stated plainly, because a reference that overstates its own strength",
        "is the exact thing this one was built to prevent:",
        "",
        "- **The controls are protected from vacuity by a construction, not by a",
        "  discovery.** Each control is driven against a fake host in both directions: it",
        "  must pass where the observable moves and *fail* where it cannot. Before that",
        "  harness existed, every vacuous form of every control passed every other guard in",
        '  the suite. "Nothing survives being made vacuous" is therefore a **built** result',
        "  rather than a found one, and the catcher is itself a construction that could be",
        "  wrong.",
        "- **A control vouches for the instrument, not for the product.** It proves the",
        "  assertion beside it can tell a right answer from a wrong one on that cell. It",
        "  does not widen the promise to devices the run never touched -- which is why a",
        "  cell may only cite a control parametrized on a device its own verdict rests on.",
        "- **The check that no verdict was hand-written runs on the dev VM only.** A guard",
        "  re-collates the committed cells from the records a bed run leaves behind and",
        "  requires them to match. Those records live in a git-ignored directory, so in CI",
        "  and in a fresh checkout that guard is **inert** -- it makes a weaker, always-true",
        "  claim rather than reporting success for a check nobody ran. The schema, the",
        "  per-device accounting and the positive-control resolution are checked everywhere;",
        "  reproducibility from raw records is not.",
        "",
    ]


def _registered_gaps_section(matrix: dict) -> "list[str]":
    """Say whose news the broken cells are, and the division of labour that follows."""
    broken = [
        cell
        for row in matrix["cells"].values()
        for cell in row.values()
        if cell["status"] == "measured-broken"
    ]
    predicted = [
        cell
        for cell in broken
        if cell.get("observed_cells")
        and all(
            entry["outcome"] != "failed"
            for entry in cell["observed_cells"]
            if entry["outcome"] != "passed"
        )
        and any(entry["outcome"] == "xfailed" for entry in cell["observed_cells"])
    ]
    return [
        "## Known-broken surfaces, and whose news they are",
        "",
        f"{len(broken)} of these cells read `measured-broken`, and **{len(predicted)} of them",
        "failed in a way the suite predicted** -- a strict `xfail`, which the lane treats as a",
        "hard error if it ever *passes*. A predicted failure means otto already carries the",
        "defect in its own registry ({data}`~otto.host.userland.GAPS`), already documents it,",
        "and already refuses or adapts at the call sites that consult it.",
        "",
        "**So this page did not discover them.** It re-measures them, independently and with a",
        "date, from the opposite end: the registry is what otto *knows*, and these cells are",
        "what a run *saw*. A support matrix that quietly omitted a surface otto knows is broken",
        "would be lying by omission, so they are published here too -- but they are somebody",
        "else's news, and the row says so.",
        "",
        "```{note}",
        "**Division of labour with {doc}`subsystems/busybox-support`, so the two do not",
        "drift.** That page is the readable rendering of {data}`~otto.host.userland.GAPS`: one",
        "record per *surface otto refuses or adapts at a call site*, what otto does about it,",
        "which call sites are wired, and who would close it. It is the authority on **otto's",
        "behaviour when it meets a device it cannot fully serve**.",
        "",
        "This page is a **dated measurement across a grid**: which of otto's conformance",
        "contracts held, on which devices, on which day, with what watched. It is the",
        "authority on **what a run last saw**, and on nothing else.",
        "",
        "The two share two words. `measured-broken` and `untested` are spelled the same in",
        "`otto.host.userland` and in `schemas/support-matrix.schema.json`, and they mean the",
        "same thing in both -- but they belong to different registries and are written by",
        "different things. There, a human records a surface and otto reads the record at a",
        "call site to decide whether to refuse. Here, a collate step writes a state from run",
        "output and nothing in `src/otto/` ever reads it. This page adds two states the gap",
        "registry does not have (`measured-ok`, `not-observable`); the gap registry covers",
        "surfaces this page has no contract for. Neither restates the other, and a fact about",
        "otto's refusal behaviour belongs there, not here.",
        "```",
        "",
    ]


def _deviations_section() -> "list[str]":
    """Name the spec requirement this item consciously does not meet, and why."""
    return [
        "## What this page deliberately does not do",
        "",
        "**Spec §5 asks the collate step to also fold in observation artifacts downloaded",
        "from the nightly `conformance-hermetic` CI job, recorded as `venue: ci-hermetic`.",
        "That is not implemented, and the omission is a decision rather than an oversight.**",
        "",
        "The profile axis on this page is built from the *bed labs'* devices. The hermetic",
        "venue's cells name things that are in no lab at all -- `local`, `loopback`, and a set",
        "of BusyBox *artifacts* that are executed under the runner's own `bash` rather than",
        "being a device with an ash shell. Every hermetic observation therefore names an",
        "element no column on this page holds, and folding one in would mean **inventing a",
        "column, or filing a result about a runner-shell run under a real guest's name**.",
        "Either would publish a verdict about hardware from a run that never touched it,",
        "which is the failure this whole artifact exists to prevent.",
        "",
        "So the collate step **discards hermetic records loudly** -- it prints how many and",
        "why -- and `ci-hermetic`, while still a legal venue in the schema, is produced by no",
        "code path. Every cell here reads `bed`. If the hermetic venue ever gains devices with",
        "a lab entry to derive a userland from, this is the requirement to revisit.",
        "",
    ]


#: profile id (exact) or id PREFIX -> what that userland is, for someone who has one.
#: Prefix-matched so a sixth BusyBox or a fourth Zephyr release needs no edit here --
#: but an id matching NO family is reported by :func:`axes_mismatch` and fails the docs
#: build, rather than rendering a section that describes nothing.
FAMILY_BLURB = {
    "gnu": "a GNU coreutils userland with `bash` -- the world otto grew up in",
    "busybox-": (
        "a BusyBox {version} userland: one multi-call binary supplying `ash` and every "
        "applet, with no `bash` and no coreutils behind it"
    ),
    "zephyr-": (
        "a Zephyr {version} target: an RTOS shell over a console, with no POSIX userland "
        "underneath it at all"
    ),
}


def profile_blurb(profile_id: str) -> "str | None":
    """Describe this userland, or answer ``None`` if the tree grew an undescribed family."""
    if profile_id in FAMILY_BLURB:
        return FAMILY_BLURB[profile_id]
    for prefix, blurb in FAMILY_BLURB.items():
        if prefix.endswith("-") and profile_id.startswith(prefix):
            return blurb.format(version=profile_id[len(prefix) :])
    return None


def _grid(matrix: dict) -> "list[str]":
    """Render the orientation table: profiles down, surfaces across.

    PROFILES ARE THE ROWS on purpose. A reader arrives holding a device, so the thing
    they can name is the userland -- one row, read left to right, is "what otto can do
    with something like mine". Surfaces as rows would make them scan a column for a
    thing they had to identify first.
    """
    heads = [VOICE[surface.id].short for surface in SURFACES]
    lines = [
        "## At a glance",
        "",
        "One row is one kind of device. Follow it across to see what a run last saw, then",
        "read that profile's section for the sentence behind the word -- **the grid is",
        "orientation, not the answer**.",
        "",
        "| Device profile | " + " | ".join(heads) + " |",
        "| --- | " + " | ".join(["---"] * len(heads)) + " |",
    ]
    for profile in matrix["profiles"]:
        cells = []
        for surface in SURFACES:
            cell = matrix["cells"][surface.id][profile["id"]]
            coverage = cell_coverage(matrix, surface.id, profile["id"])
            cells.append(_esc(grid_token(surface.id, cell, coverage)))
        name = "{ref}`" + profile["id"] + " <" + _slug(profile["id"]) + ">`"
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "`works`",
        ": every drawn cell passed, on every device in the profile.",
        "",
        "`` `x` only ``",
        ": it works over `x` and fails elsewhere. **Not a broken surface** -- a surface with",
        "  a working route and a broken one, and the section names both.",
        "",
        "`broken`",
        ": every drawn cell failed. Read the section for whether otto already knew.",
        "",
        "`refused`",
        ": otto **refuses** the request on these devices rather than doing it wrongly, and a",
        "  run confirmed that refusal held. The contract passed -- what it watched was the",
        "  refusal and not the capability -- so this is neither a defect nor a blank. The",
        "  section says what you get instead.",
        "",
        "`not interpreted`",
        ": a run passed, but what it watched matches none of the observables this page",
        "  carries a promise for, so the grid says nothing and the section reproduces the",
        "  record. The documentation build normally fails before publishing one of these.",
        "",
        "`(k of n)`",
        ": a suffix on any word above -- the verdict rests on `k` of the profile's `n`",
        "  devices rather than on all of them. The section says what happened to the rest,",
        "  and whether they *could not* be measured or simply *have not been*.",
        "",
        "`not observable`",
        ": nothing on these devices can express what the promise is about. Not a refusal and",
        "  not a failure.",
        "",
        "`untested`",
        ": no run has looked. **Not** a statement that it does not work.",
        "",
    ]
    return lines


def _profile_section(matrix: dict, profile: dict) -> "list[str]":
    """Render one profile: what the device is, the six answers, then the evidence."""
    profile_id = profile["id"]
    elements = list(profile["elements"])
    lines = [
        f"({_slug(profile_id)})=",
        "",
        f"### {profile_id}",
        "",
        _capitalise(profile_blurb(profile_id) or "")
        + f". Measured on {_devices(len(elements))} in otto's test bed: "
        + f"{_join(_code(elements))}.",
        "",
        "| Surface | Verdict | What it means for you |",
        "| --- | --- | --- |",
    ]
    for surface in SURFACES:
        cell = matrix["cells"][surface.id][profile_id]
        coverage = cell_coverage(matrix, surface.id, profile_id)
        verdict = f"`{cell['status']}`"
        if cell["status"] != "untested":
            # Separated by a literal middot rather than a `<br>` or an entity: raw
            # inline HTML is a builder-dependent node, and `make docs` runs the
            # doctest builder over this page as well as the html one.
            verdict += f" · {_coverage_note(coverage)} · {cell['as_of']}"
        lines.append(
            f"| {_esc(surface.title)} | {verdict} | {_esc(meaning(surface.id, cell, coverage))} |"
        )
    lines += ["", "**What each verdict rests on:**", ""]
    for surface in SURFACES:
        cell = matrix["cells"][surface.id][profile_id]
        coverage = cell_coverage(matrix, surface.id, profile_id)
        lines += _evidence(surface.id, cell, coverage)
    lines.append("")
    return lines


def _provenance_section() -> "list[str]":
    """Explain how a verdict gets here, and what stops a wrong one."""
    return [
        "## How this page is produced",
        "",
        "1. `make conformance-bed` runs otto's conformance contracts against every drawn",
        "   `(device, console, transfer backend)` cell of the lab. Each item leaves one",
        "   observation record carrying its outcome, read from pytest's own report at",
        "   teardown rather than from inside the test body.",
        "2. The collate step (`scripts/collate_support_matrix.py`, also `make",
        "   support-matrix`) folds those records into `schemas/support_matrix.json`. It is",
        "   the **only** thing in the repo that may write a `measured-*` verdict; it cannot",
        "   run `git`; and a run that did not draw a cell leaves that cell byte-identical,",
        "   so a sampled run can never quietly downgrade the matrix.",
        "3. The release commits the result (`make release-matrix`), but only when no cell",
        "   got worse: a new `measured-broken`, or a lost `measured-ok`, is refused and",
        "   stays a person's call. CI never commits a verdict, and no workflow runs the",
        "   collate step -- CI has no lab to run it against.",
        "4. This page is rendered from the committed artifact on every documentation build",
        "   (`scripts/render_support_matrix.py`, hooked from `docs/conf.py`).",
        "",
        "**The renderer refuses to publish a stale grid.** Before writing anything it asks",
        "the tree what surfaces and profiles it declares today -- by walking the conformance",
        "modules, by reading the surface table, and by resolving every lab device's userland",
        "-- and any disagreement with the artifact's axes **fails the documentation build**.",
        "A renamed profile or a deleted contract stops `make docs` rather than leaving a",
        "verdict on this page about something that no longer exists.",
        "",
        "**The one thing on this page that is written by hand is checked too.** Where a row",
        "says a device *could not be measured*, the reason beside it is a sentence somebody",
        "wrote -- and the build requires every mechanism that sentence names to appear in",
        "the contract's own domain rule, and requires the sentence to name at least one",
        "thing that rule really reads. A rule that changes without its reason therefore",
        "fails the build instead of leaving this page explaining a decision otto no longer",
        "makes. What is **not** checked is the wording itself: pinning prose verbatim would",
        "redden on a typo fix and stay green on a lie, so the exact probe is printed beside",
        "every such claim for you to check rather than trust.",
        "",
        "See {doc}`testing` for where the conformance lane sits among otto's other suites.",
        "",
    ]


def render(matrix: dict, *, rendered_on: "_datetime.date | None" = None) -> str:
    """Render the whole page, refusing a matrix whose axes the tree no longer backs."""
    problems = axes_mismatch(matrix)
    if problems:
        raise ValueError(
            "refusing to render a support matrix the tree no longer backs:\n  "
            + "\n  ".join(problems)
        )
    rendered_on = rendered_on or _datetime.datetime.now(_datetime.timezone.utc).date()
    lines = [
        "<!-- GENERATED FILE -- do not edit by hand.",
        "     scripts/render_support_matrix.py renders this from schemas/support_matrix.json",
        "     on every Sphinx build (docs/conf.py, builder-inited). Edits are overwritten. -->",
        "",
        "# Support matrix",
        "",
        "**Can otto do X against a device like mine?** This page is the standing answer for",
        f"{len(matrix['surfaces'])} surfaces across {len(matrix['profiles'])} device profiles,",
        "and none of it is a judgement: every verdict below was produced by a run against a",
        "real target, folded into `schemas/support_matrix.json` by the one script allowed to",
        "write one, and rendered here at build time.",
        "",
        "A **surface** is one promise otto makes -- that an exit code comes back intact, that",
        "a file survives a roundtrip, that a command over budget fails the documented way. A",
        "**profile** is a userland: the GNU world, one of five BusyBox releases, one of three",
        "Zephyr releases. A **device** is one element of otto's test bed standing for that",
        "userland -- a VM or a QEMU guest. Profiles are **not** capability-uniform, which is",
        "why several verdicts below rest on some of a profile's devices and not others, and",
        "say so.",
        "",
        "One device is usually **drawn more than once**. otto offers a device a menu of",
        "consoles and file-transfer backends, and the bed runs every contract against every",
        "combination it offers -- so a profile holding a single device can still have two",
        "drawn cells, and a four-device profile thirty-two. That is why a cell can be green",
        "on one route and red on another, and why the counts below distinguish *devices*",
        "from *drawn cells*.",
        "",
    ]
    lines += _currency_section(matrix, rendered_on)
    lines += _states_section()
    lines += _guarantee_section()
    lines += _registered_gaps_section(matrix)
    lines += _grid(matrix)
    lines += ["## The profiles", "", "One section per userland, in the artifact's own order.", ""]
    for profile in matrix["profiles"]:
        lines += _profile_section(matrix, profile)
    lines += _deviations_section()
    lines += _provenance_section()
    return "\n".join(lines).rstrip() + "\n"


def main(argv: "list[str]") -> int:
    """Render the page, or report why the artifact and the tree disagree."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument("--page", type=Path, default=PAGE_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report axis disagreement and write nothing",
    )
    args = parser.parse_args(argv)
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    problems = axes_mismatch(matrix) + narrowing_mismatch(matrix) + promise_mismatch(matrix)
    if problems:
        print(
            f"{args.matrix} no longer matches the tree it describes:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 1
    if args.check:
        print(f"{args.matrix}: axes agree with the tree; wrote nothing (--check)")
        return 0
    args.page.parent.mkdir(parents=True, exist_ok=True)
    args.page.write_text(render(matrix), encoding="utf-8")
    cells = sum(len(row) for row in matrix["cells"].values())
    print(f"{args.page}: rendered {cells} cells")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
