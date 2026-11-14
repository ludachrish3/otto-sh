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
import datetime as dt
import json
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from scripts.collate_support_matrix import FAILURE_SUMMARY_LIMIT
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.support_matrix import (
    MATRIX_PATH,
    SURFACES,
    CellCoverage,
    cell_coverage,
    discover_contracts,
    discover_profiles,
)
from tests.conformance._entry import entries_declared

if TYPE_CHECKING:
    # The product is reached at RUNTIME only from inside the two functions that
    # need it, for the ``sys.path`` reason the module docstring gives; these are
    # for the annotations alone.
    from collections.abc import Callable, Sequence
    from enum import Enum

    from otto.host.capability_grid import HostCapabilities
    from otto.host.command_frame import CommandFrame
    from otto.host.exec_route import ExecRoute
    from otto.host.limitation import Limitation
    from otto.host.session import RouteRecord
    from otto.result import CommandResult
    from tests.conformance._resolved import ResolvedCell

PAGE_PATH = CHECKOUT_ROOT / "docs" / "architecture" / "support-matrix.md"
"""The rendered page.

GIT-IGNORED and regenerated on every Sphinx build (``docs/conf.py``'s
``builder-inited`` hook, beside ``_generate_docs_media``), the same standing
``docs/_static/generated/`` has. Committing it would put a second copy of every verdict
in the tree, and the copy that goes stale is the one nothing runs.
"""

FAMILIES_PAGE_PATH = CHECKOUT_ROOT / "docs" / "cli" / "host" / "families.md"
"""The rendered host-families page.

COMMITTED, unlike :data:`PAGE_PATH`, and for the opposite reason. Its source is
the tree itself -- the ``capabilities`` declaration on each host class -- so a
reader of the repository can see the promise without building the docs, and
``tests/unit/test_support_matrix.py`` re-renders it and compares byte for byte,
so a declaration edited without regenerating this page reds. It carries no
date and no measurement, which is what makes a byte comparison meaningful.
"""

COMMAND_CONTRACTS_DIR = CHECKOUT_ROOT / "docs" / "command-contracts"
"""The rendered command contract pages: ``index.md``, the overview, and one page per command.

The pages are :func:`render_command_contracts`' answer; which entry goes on
which page is :data:`ENTRY_VOICE`. COMMITTED, for the reason
:data:`FAMILIES_PAGE_PATH` is: their sources are the tree's own declarations
(the ``entries`` marker on each command contract, and every
:class:`~otto.host.limitation.Limitation` on a built-in frame, exec route or
console setting), they carry no date and no measurement, and
``tests/unit/test_support_matrix.py`` compares them byte for byte.
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
    ``declared_by``
        The declaration this arm publishes, as ``module:attr.path``
        (``otto.host.command_frame:ZephyrFrame.exact_output``). Empty for an arm no
        :class:`~otto.host.limitation.Limitation` stands behind. When set it must
        resolve to one: :func:`promise_mismatch` reports a name that does not, so a
        renamed or deleted declaration fails the build instead of leaving the page
        citing nothing. The arm's ``capability`` then states the promise WITHOUT the
        exception, and :func:`branch_capability` appends the declaration's own
        ``affects`` and ``reason``, so correcting the declaration corrects the promise.
    ``kinds``
        The cell kinds (:data:`HOST_KIND_VOICE`'s keys) this arm is watched on, for an
        arm no ``declared_by`` places: the contract page names the device from it.
        PINNED to the matrix: ``tests/unit/test_support_matrix.py`` requires it to be
        exactly the kinds of the drawn cells whose observable carries ``marker``.
    """

    marker: str
    capability: str = ""
    headline: str = ""
    instead: str = ""
    grid_word: str = ""
    declared_by: str = ""
    kinds: "tuple[str, ...]" = ()


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
    reader_narrowed: str = ""
    """:attr:`narrowed` for a first-time user, on the command contract page, where
    the suite's own vocabulary means nothing; empty when :attr:`narrowed` already
    reads plainly. Not pinned to the domain rule's tokens, as :attr:`narrowed` is:
    it is the same fact, rewritten, beside the clause that is pinned, so a change
    to the rule meets both (:func:`reader_narrowed`)."""
    reader_entry_narrowed: str = ""
    """Why the domain drops an ENTRY on hosts that entry reaches, for the same reader;
    empty when :attr:`reader_narrowed` already says it (:func:`reader_entry_narrowed`)."""
    branches: "tuple[Branch, ...]" = ()
    """The observables this surface can offer, when it offers more than one.

    Empty for a surface with a single observable, which is eight of the eleven today.
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
    "exec-output-exact": Voice(
        short="exact output",
        capability=(
            "read a command's output exactly as the command wrote it -- blank lines, "
            "spaces, tabs and carriage returns intact -- with only the one final "
            "newline removed"
        ),
        # The same rule on both arms; what differs is the evidence. A POSIX
        # shell runs the shipped table of chosen bytes, a Zephyr shell only
        # the stock commands its firmware fixes.
        branches=(
            Branch(
                marker="every row of the table",
                capability=(
                    "read a command's output exactly as the command wrote it -- blank "
                    "lines, spaces, tabs and carriage returns intact -- with only the one "
                    "final newline removed"
                ),
            ),
            Branch(
                marker="as a console that does not escape 0xFF carries them",
                # The exception is NOT worded here: :func:`branch_capability`
                # appends the declaration's own `affects` and `reason`.
                capability=(
                    "read a command's output exactly as the command wrote it -- blank "
                    "lines, spaces, tabs and carriage returns intact -- with only the one "
                    "final newline removed"
                ),
                declared_by="otto.host.options:UNESCAPED_IAC",
            ),
            Branch(
                marker="the stock commands whose output this firmware fixes",
                capability=(
                    "read a command's output exactly as the device's firmware wrote it, "
                    "with only the one final newline and the colour codes the shell puts "
                    "around its error and warning lines removed (checked against "
                    "recordings of the firmware's own commands, because the Zephyr shell "
                    "has no command that prints text you choose)"
                ),
                kinds=("bed-zephyr",),
            ),
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
    "exec-signal-exit": Voice(
        short="signal exit",
        capability=(
            "read `retcode` 128+N for a command killed by signal N, as a shell reports it, "
            "and keep using the host afterwards"
        ),
        narrowed=(
            "this suite's vocabulary for such a target declares no `signal_kill_command`, "
            "or `exec` there runs outside `_PROCESS_ROUTES`, typed into a shell session "
            "that the stimulus would end as well"
        ),
        reader_narrowed=(
            "the Zephyr shell runs each command on its own thread rather than as a "
            "process, so there is no command a signal can kill there"
        ),
        reader_entry_narrowed=(
            "on every other host `exec` types the command into a shell session (the "
            "`pooled-shell` or `default-session` route), "
            "and a killed command would take that session down with it, so otto does not "
            "check it there yet"
        ),
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
    "transfer-progress": Voice(
        short="progress",
        capability=(
            "watch a transfer's progress bar and trust that it moves with the bytes -- "
            "it never starts past one stride, never runs ahead of the total, and finishes"
        ),
        narrowed=(
            "otto reports nowhere to put a file on such a device -- its filesystem "
            "backend answers `supports_transfer` False -- so there is no transfer "
            "whose progress could be watched"
        ),
        narrowed_detail=(
            "The same narrowing as the two transfer surfaces above, read from the same "
            "answer: `remote_scratch` is `None` for exactly those hosts."
        ),
    ),
    "transfer-recursive": Voice(
        short="tree roundtrip",
        capability=(
            "put a whole directory tree on the device and get it back with its nesting, "
            "its file contents and its empty directories intact"
        ),
        # ★ THE SECOND SURFACE WHOSE CONTRACT WATCHES TWO DIFFERENT THINGS, and the
        # same shape as `transfer-mode`'s: a device otto walks no tree for is watched
        # REFUSING, and a refusal is not a quiet spelling of the promise.
        branches=(
            Branch(
                marker="get(recursive=True) reads back",
                capability=(
                    "put a whole directory tree on the device and get it back with its "
                    "nesting, its file contents and its empty directories intact"
                ),
            ),
            Branch(
                marker="raises for recursive=True",
                headline="No -- and otto says so rather than pretending.",
                # SAYS ONLY WHAT THAT ARM ASSERTS, which is a `raises` and
                # nothing else: what otto did or did not transfer first is not
                # watched there, so this may not claim it.
                instead=(
                    "otto implements no recursive transfer for such a device: what a run "
                    "watched here is `put(recursive=True)` raising "
                    "`NotImplementedError` rather than a tree landing"
                ),
                grid_word="refused",
            ),
        ),
        narrowed=(
            "otto reports nowhere to put a file on such a device -- its filesystem "
            "backend answers `supports_transfer` False -- so there is no tree to put "
            "and nothing to read back"
        ),
        narrowed_detail=(
            "The same narrowing as the three transfer surfaces above, read from the same "
            "answer: `remote_scratch` is `None` for exactly those hosts."
        ),
    ),
    "transfer-concurrent": Voice(
        short="batch",
        capability=(
            "put a batch of files larger than the protocol's concurrency cap and get every "
            "one back, whether the files move together or one at a time"
        ),
        narrowed=(
            "otto reports nowhere to put a file on such a device -- its filesystem "
            "backend answers `supports_transfer` False -- so there is no batch to put "
            "and nothing to read back"
        ),
        narrowed_detail=(
            "The same narrowing as the transfer surfaces above, read from the same "
            "answer: `remote_scratch` is `None` for exactly those hosts."
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
        reader_narrowed=(
            "otto does not test what happens when a Zephyr command runs longer than its "
            "timeout, so it promises nothing about it. The Zephyr shell runs one command "
            "at a time, on its own thread, so a command made to outlive its budget would "
            "block the very shell the test then has to find still answering"
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
    "entry": "through {}",
}

_AXES = ("element", "term", "transfer")

_ENTRY_AXIS = "entry"
"""The axis a command contract's drawn cells carry, and only theirs: see :func:`split_of`."""

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
    """Group *cell*'s ``observed_cells`` by outcome, and note which axes vary.

    ``entry`` (how a command was issued: ``run``, ``exec``, ``exec-shell``) is an
    axis only when EVERY drawn cell carries one, which is exactly a command
    contract's cell; a transfer cell carries none and splits as it always has.
    """
    entries = list(cell.get("observed_cells", []))
    axes = list(_AXES)
    if entries and all(entry.get(_ENTRY_AXIS) for entry in entries):
        axes.append(_ENTRY_AXIS)
    varying = [axis for axis in axes if len({entry[axis] for entry in entries}) > 1]
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
    if not cell.get("observable"):
        if cell.get("status") in _UNWATCHED_STATES:
            return Promise(capability=voice.capability)
        return _UNINTERPRETED
    arms = arms_of(surface_id, cell)
    if any(arm.promise is _UNINTERPRETED for arm in arms):
        return _UNINTERPRETED
    if len(arms) == 1:
        return arms[0].promise
    return _PER_CELL


@dataclass(frozen=True)
class Arm:
    """The drawn cells of one cell that watched the same :class:`Branch`, and its promise."""

    promise: Promise
    cells: "list[dict]"


_PER_CELL = Promise(
    headline="It depends on how you reach the device.",
    grid_word="varies",
)
"""What :func:`promise_of` answers for a cell whose drawn cells ran different arms.

Not a promise of its own: :func:`meaning` sees it and says one line per
:class:`Arm` instead, each naming the drawn cells it applies to.
"""


def _watched(cell: dict, entry: dict) -> "str | None":
    """Return what drawn cell *entry* of *cell* watched: its own ``observable``.

    Falls back to the cell-level one for an entry that carries none, the
    shape every drawn cell had before each carried its own.
    """
    return entry.get("observable") or cell.get("observable")


def _arm_matching(voice: "Voice", observable: "str | None") -> "Branch | None":
    """Return the ONE declared arm *observable* names, or ``None`` for zero or two."""
    matched = [branch for branch in voice.branches if observable and branch.marker in observable]
    return matched[0] if len(matched) == 1 else None


def arms_of(surface_id: str, cell: dict) -> "list[Arm]":
    """Group *cell*'s drawn cells by the arm each one's own observable names.

    In declaration order; the drawn cells matching no arm, or two, group under
    :data:`_UNINTERPRETED`, last. A cell with no drawn cells is answered from
    its cell-level observable alone, as one arm with no cells.
    """
    voice = VOICE[surface_id]
    entries = list(cell.get("observed_cells", []))
    if not entries:
        branch = _arm_matching(voice, cell.get("observable"))
        return [Arm(promise=_promise_for(branch), cells=[])]
    grouped: "dict[int, list[dict]]" = {}
    for entry in entries:
        branch = _arm_matching(voice, _watched(cell, entry))
        index = len(voice.branches) if branch is None else voice.branches.index(branch)
        grouped.setdefault(index, []).append(entry)
    return [
        Arm(
            promise=_promise_for(voice.branches[index] if index < len(voice.branches) else None),
            cells=grouped[index],
        )
        for index in sorted(grouped)
    ]


def branch_capability(branch: Branch) -> str:
    """*branch*'s promise, with the exception its ``declared_by`` names in that declaration's words.

    An arm with no declaration, or a declaration that resolves to no
    :class:`~otto.host.limitation.Limitation` (which :func:`promise_mismatch`
    reports), is its own ``capability``.
    """
    from otto.host.limitation import Limitation

    declared = _resolve_declaration(branch.declared_by) if branch.declared_by else None
    if not branch.capability or not isinstance(declared, Limitation):
        return branch.capability
    return f"{branch.capability}, except for {declared.affects}: {declared.reason}"


def _promise_for(branch: "Branch | None") -> Promise:
    """Return what one arm licenses; :data:`_UNINTERPRETED` for no arm."""
    if branch is None:
        return _UNINTERPRETED
    return Promise(
        capability=branch_capability(branch),
        headline=branch.headline,
        instead=branch.instead,
        grid_word=branch.grid_word,
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
    if promise is _PER_CELL:
        # ★ A LIMITATION ON ONE TERM OR ONE ENTRY IS PUBLISHED WHERE IT APPLIES. The
        # drawn cells ran different arms, so no one sentence is true of the whole
        # cell; each arm gets its own, naming the drawn cells it covers.
        lines = [_arm_sentence(arm, split.varying, predicted) for arm in arms_of(surface_id, cell)]
        return f"**{promise.headline}** " + " ".join(lines) + tail
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


def _arm_sentence(arm: Arm, varying: "list[str]", predicted: str) -> str:
    """Say what ONE arm of a per-cell cell means, for the drawn cells it covers.

    The same rules as :func:`meaning`'s whole-cell sentences, one arm wide: a
    refusal arm never says "you can", and a pass with no positive control on
    it is an observation, not a promise.
    """
    where = _capitalise(_where(arm.cells, varying))
    passed = [entry for entry in arm.cells if entry["outcome"] == "passed"]
    broken = [entry for entry in arm.cells if entry["outcome"] != "passed"]
    promise = arm.promise
    if not promise.capability:
        if not broken:
            saw = "it passed"
        elif passed:
            saw = (
                f"it held {_where(passed, varying)} and failed {_where(broken, varying)} "
                f"-- {predicted}"
            )
        else:
            saw = f"it failed -- {predicted}"
        return f"{where}: {promise.headline} {_capitalise(promise.instead)}, and {saw}."
    if broken and passed:
        return (
            f"{where}: you can {promise.capability} {_where(passed, varying)}; "
            f"{_where(broken, varying)} it fails -- {predicted}."
        )
    if broken:
        return f"{where}: it fails when asked to {promise.capability} -- {predicted}."
    uncited = [entry for entry in passed if not entry.get("positive_control")]
    if uncited:
        return (
            f"{where}: it passed, but no positive control passed "
            f"{_where(uncited, varying)}, so that is an observation rather than a guarantee."
        )
    return f"{where}: you can {promise.capability}."


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
       input passes. Today that identifier is ``remote_scratch`` for all three
       transfer surfaces and ``long_running_command`` for timeout.

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
        module = CHECKOUT_ROOT / surface.contract.split("::")[0]
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
        module = CHECKOUT_ROOT / surface.contract.split("::")[0]
        source = module.read_text(encoding="utf-8")
        for branch in voice.branches:
            declaration = _declaration_problem(surface.id, branch)
            if declaration:
                problems.append(declaration)
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
            # PER DRAWN CELL: each one's own observable picks its arm, so each must
            # pick exactly one. A cell with no drawn cells is asked as a whole.
            drawn = [
                (f" on {entry['cell_label']!r}", _watched(cell, entry))
                for entry in cell.get("observed_cells", [])
            ] or [("", observable)]
            for where, watched in drawn:
                matched = [b for b in voice.branches if watched and b.marker in watched]
                if len(matched) != 1:
                    problems.append(
                        f"{surface.id!r} x {profile_id!r}{where}: its observable matches "
                        f"{len(matched)} of the {len(voice.branches)} observables VOICE "
                        f"declares for this surface, not 1 -- the page cannot tell which "
                        f"promise, if any, this cell licenses"
                    )
    return problems


def _resolve_declaration(name: str) -> object:
    """Return the object ``module:attr.path`` *name* names; ``None`` if it names nothing."""
    import importlib

    module_name, _, path = name.partition(":")
    attributes = path.split(".")
    if not module_name or not all(attributes):
        return None  # ":X", "m:", "m:a..b": no module, or an empty attribute.
    try:
        target: object = importlib.import_module(module_name)
    except (ImportError, ValueError, TypeError):
        # ImportError: no such module; ValueError: an empty module name
        # ("a..b" -> ""); TypeError: a relative name (".x") with no package.
        return None
    for attribute in attributes:
        target = getattr(target, attribute, None)
        if target is None:
            return None
    return target


def _declaration_problem(surface_id: str, branch: Branch) -> "str | None":
    """Report *branch*'s ``declared_by`` when it names no :class:`Limitation`."""
    from otto.host.limitation import Limitation

    if not branch.declared_by:
        return None
    if isinstance(_resolve_declaration(branch.declared_by), Limitation):
        return None
    return (
        f"surface {surface_id!r}: the arm {branch.marker!r} publishes the declaration "
        f"{branch.declared_by!r}, which does not resolve to a Limitation -- the page "
        f"would cite a limitation nothing declares"
    )


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


def _currency_section(matrix: dict, rendered_on: "dt.date") -> "list[str]":
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


def _progress_promises_section() -> "list[str]":
    """Render each backend's DEFAULT progress stride, read off the registry.

    DERIVED, like every other verdict on this page: one row per registered
    backend, both arms and the note read off the class's own
    ``ProgressGranularity``. ``tests/unit/test_support_matrix.py``'s
    ``test_the_page_renders_every_backends_progress_promise_from_the_registry``
    pins the two against each other in both directions, so a declaration edited
    without this page, or a row typed by hand, reds.

    THE CLASS ATTRIBUTE AND NOT ``effective_progress_granularity()``, and the
    prose below says so rather than leaving a reader to assume otherwise.
    The registry's metadata states the CLASS's declaration (and the registry
    refuses a class that disagrees with it); the instance method needs a built
    backend, which needs a host's option tables, which a page
    rendered at documentation-build time has none of. The DEFAULT promise is
    therefore the only thing this page can honestly publish -- and the one
    backend a configuration moves (``scp``, whose stride is the ``block_size``
    its ``ScpOptions`` hand ``asyncssh``) is named, so the reader is told where
    the default stops being the answer.

    The import is local for the reason the module docstring gives about
    ``sys.path``: every other import here is stdlib, ``scripts`` or ``tests``,
    and this is the only line in the file that reaches into the product.
    """
    from otto.host.transfer import TRANSFER_BACKENDS

    def cell(arm: "int | None") -> str:
        return "one event at completion" if arm is None else f"{arm:,} bytes"

    lines = [
        "(matrix-progress-promises)=",
        "",
        "## Transfer progress: what each backend promises",
        "",
        "The `progress` column measures the bar against a promise each backend makes in",
        "code (a `ProgressGranularity` on the backend class): the **stride**, the most the",
        "bar may advance between two ticks, in each direction. A backend that promises a",
        "stride moves `3 x stride + 17` bytes under the contract and must tick at least",
        "four times, never beginning past one stride and always finishing on the total.",
        "*One event at completion* means the bar goes straight to done when the transfer",
        "does -- a whole-file transfer with nothing to observe in between -- and the note",
        "beside it says why.",
        "",
        "**These are the defaults.** Each row is the declaration on the backend's own",
        "class, which is all a page rendered with no host in hand can know. One of them",
        "moves with your configuration: `scp` promises whatever `block_size` reaches",
        "`asyncssh`, so a value set in `scp_options` -- as the field, or through `extra`,",
        "which is applied last and wins -- replaces the default below, and it is that",
        "configured value a run measures the bar against.",
        "",
        "{doc}`../cli/host/families` is the declared counterpart for the host verbs:",
        "what each host family promises for `user=`, progress and session identity, read",
        "off the host classes the same way this table is read off the backends. That page",
        "changes only when a host class does; this page's *measured* cells change when the",
        "bed does. A declared promise that a device does not keep is a bug in either the",
        "declaration or the device, and the conformance suite tells which.",
        "",
        "| backend | put stride | get stride | note |",
        "|---|---|---|---|",
    ]
    for name in sorted(TRANSFER_BACKENDS.names()):
        granularity = TRANSFER_BACKENDS.peek(name=name).metadata.progress_granularity
        lines.append(
            f"| `{name}` | {cell(granularity.put)} | {cell(granularity.get)} | {granularity.note} |"
        )
    lines.append("")
    return lines


def _registered_gaps_section(matrix: dict) -> "list[str]":
    """Say whose news the broken cells are, and the division of labour that follows.

    RENDERED AT ZERO BROKEN CELLS TOO, with a zero-state rather than by dropping
    the heading. The section's real subject is not the cells: it is the RULE that
    a red cell here is a re-measurement of something otto's own gap registry
    already knows, and the division of labour that keeps this page and
    {doc}`subsystems/busybox-support` from restating each other. A page that
    stopped naming its registry the day everything passed would invite exactly
    the drift that note exists to prevent, and it would take the rule out of the
    reader's hands on the day the next red cell appears.
    """
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
    if broken:
        opening = [
            f"{len(broken)} of these cells read `measured-broken`, and **{len(predicted)} of them",
            "failed in a way the suite predicted** -- a strict `xfail`, which the lane treats as a",
            "hard error if it ever *passes*. A predicted failure means otto already carries the",
            "defect in its own registry ({data}`~otto.host.userland.GAPS`), already documents it,",
            "and already refuses or adapts at the call sites that consult it.",
            "",
            "**So this page did not discover them.** It re-measures them, independently and",
            "with a date, from the opposite end: the registry is what otto *knows*, and these",
            "cells are what a run *saw*. A support matrix that quietly omitted a surface otto",
            "knows is broken would be lying by omission, so they are published here too -- but",
            "they are somebody else's news, and the row says so.",
        ]
    else:
        opening = [
            "**None of these cells reads `measured-broken`.** Every contract held on every",
            "device the last run drew it on, so there is nothing in the grid below for this",
            "section to attribute -- and the rule it states is what the day one of them turns",
            "red will be read against, so it stays.",
            "",
            "**That day, this page will not have discovered the defect.** A red cell here is",
            "normally a strict `xfail` -- a failure the suite predicted, which the lane treats",
            "as a hard error if it ever *passes* -- and a prediction means otto already carries",
            "the defect in its own registry ({data}`~otto.host.userland.GAPS`), already",
            "documents it, and already refuses or adapts at the call sites that consult it. This",
            "page would be re-measuring it, independently and with a date, from the opposite",
            "end: the registry is what otto *knows*, a cell is what a run *saw*. A matrix that",
            "quietly omitted a surface otto knows is broken would be lying by omission, so such",
            "a cell is published here too -- but it is somebody else's news, and the row says",
            "so.",
        ]
    return [
        "## Known-broken surfaces, and whose news they are",
        "",
        *opening,
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
        "`partly broken`",
        ": some drawn cells passed and some failed, and no one transport, console or device",
        "  separates them (or too many do to fit here). **Not a broken surface** either --",
        "  the section names every drawn cell on each side.",
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
        "`varies`",
        ": the verdict differs between the drawn cells of this device -- one console,",
        "  transport or way of issuing a command (`run`, `exec`, `exec-shell`) gets one",
        "  answer and another gets a different one. The device's section gives one line per",
        "  answer and names where each applies.",
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
    """Render one profile: what the device is, the seven answers, then the evidence."""
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
        "   the **only** thing in the codebase that may write a `measured-*` verdict; it cannot",
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


def _transfer_cell(capabilities: "HostCapabilities") -> str:
    """Name what moves this family's bytes, preferring the registry's own answer.

    ``transfer_family`` names a ``BaseFileTransfer.host_families`` key, so the
    backends are LISTED off the registry and a backend added to a family shows
    up here with no edit. ``transfer`` is the declared fallback for the two
    families that select no registered backend by name — see the field
    docstrings on ``HostCapabilities``.
    """
    from otto.host.transfer import TRANSFER_BACKENDS

    if not capabilities.transfer_family:
        return capabilities.transfer
    names = [
        name
        for name in sorted(TRANSFER_BACKENDS.names())
        if capabilities.transfer_family in TRANSFER_BACKENDS.peek(name=name).metadata.host_families
    ]
    return _join(_code(names))


def _meaning_rows(members: "Sequence[Enum]") -> "list[str]":
    """One ``| value | meaning |`` row per enum member, read off member ``__doc__``.

    The meanings are NOT retyped here. Each member carries its own sentence
    (``UserSupport.__new__`` attaches it), which is the single place the
    vocabulary is defined; this renders it.
    """
    return [f"| `{member.value}` | {(member.__doc__ or '').strip()} |" for member in members]


_FAMILY_HOME_DOCS = {"container": "../docker/index"}
"""Families whose selection has a CLI home page, linked from the "how selected" cell."""


def render_families() -> str:
    """Render the host-families page from the ``capabilities`` on each host class.

    DECLARED, not measured — the counterpart to everything else this module
    renders. Every cell below is read off a frozen ``HostCapabilities`` on a
    host class; nothing here consults a run, an artifact or a date, which is
    why this page can be committed and compared byte for byte.
    """
    from otto.host.capability_grid import SessionIdentity, UserSupport, shipped_host_families

    families = shipped_host_families()
    lines = [
        "<!-- GENERATED FILE -- do not edit by hand.",
        "     scripts/render_support_matrix.py renders this from the `capabilities`",
        "     declaration on each host class, on every Sphinx build (docs/conf.py,",
        "     builder-inited). Edits are overwritten. -->",
        "",
        "# Host families",
        "",
        "otto ships several kinds of host, and they do not all answer the four verbs the",
        "same way. A serial console has no second user to become; a container has no",
        "credentials for one; a unix box has both. Each row below is what that family's",
        "host class **declares** in its `capabilities` object. What a run **measured**",
        "against real hardware, cell by cell, is in {doc}`../../architecture/support-matrix`.",
        "What a command's exit code, output and timeout mean on each kind of host is on",
        "{ref}`Command contracts <command-contracts>`.",
        "",
        "A class registered from your own repository must declare one too:",
        "`register_host_class` refuses a class without it.",
        "",
        "## What the `user=` answers mean",
        "",
        "| answer | what it means |",
        "|---|---|",
    ]
    lines += _meaning_rows(list(UserSupport))
    lines += [
        "",
        "## Where a session's identity comes from",
        "",
        "`run` drives a **persistent** session; `exec`, `put` and `get` are stateless. So",
        "`run(user=)` is a question about an identity that already exists, and each family",
        "answers it one of the ways below.",
        "",
        "| identity | what it means |",
        "|---|---|",
    ]
    lines += _meaning_rows(list(SessionIdentity))
    lines += [
        "",
        "## The families",
        "",
        "**Progress bar** is about the family's own transfer leg: whether the bytes move",
        "through a backend that can report while they are moving. Per-backend strides",
        "are published with the backends themselves, in {ref}`matrix-progress-promises`.",
        "",
        (
            "| family | how selected | `run(user=)` | `exec(user=)` | `put(user=)` "
            "| `get(user=)` | progress bar | session identity | transfer | note |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for family in families:
        caps = family.capabilities
        selector = family.selector
        if family.name in _FAMILY_HOME_DOCS:
            selector += f" (see {{doc}}`{_FAMILY_HOME_DOCS[family.name]}`)"
        lines.append(
            f"| `{family.name}` | {selector} "
            f"| `{caps.run_user.value}` | `{caps.exec_user.value}` "
            f"| `{caps.put_user.value}` | `{caps.get_user.value}` "
            f"| {'yes' if caps.show_progress else 'no'} "
            f"| {caps.session_identity.value} | {_transfer_cell(caps)} | {caps.note} |"
        )
    lines += [
        "",
        "The CLI pages for the verbs themselves — {doc}`exec`,",
        "{doc}`put`, {doc}`get` — say how to pass `--user`; this",
        "page says what each family will do with it.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class ContractRow:
    """One command contract: the surface it backs, and the entries it is checked through."""

    surface_id: str
    capability: str
    contract: str
    entries: "list[str]"
    notes: "list[NotChecked]" = dataclass_field(default_factory=list)
    """Where the promise is NOT checked: the kinds of host outside the contract's
    domain (for every entry), and each declared entry's ineligible hosts (for that
    entry). Empty only when every host the suite reaches is checked through every entry."""

    @property
    def not_checked(self) -> "list[str]":
        """Each of :attr:`notes`' full sentences, in a reader's words."""
        return [note.text for note in self.notes]

    @property
    def not_checked_short(self) -> "list[str]":
        """The same places, one per :attr:`not_checked` note, in the table's few words."""
        return [note.short for note in self.notes]

    def notes_for(self, entry: str) -> "list[NotChecked]":
        """Return the notes that apply to *entry*: the domain's, and *entry*'s own.

        A "not on" note is left out when one of *entry*'s "only on" notes
        already rules its hosts out: a reader told `exec-shell` runs only on
        SSH hosts needs no note that it skips a Zephyr board.
        """
        mine = [note for note in self.notes if note.entry in (None, entry)]
        only = [note.hosts for note in mine if note.only]
        return [
            note
            for note in mine
            if note.only or not any(not (note.hosts & hosts) for hosts in only)
        ]


#: The kinds of host the conformance suite reaches, as a reader names them. Keyed
#: by a cell's ``kind`` (``tests/conformance/_cells.py``, ``_bed.py``);
#: :func:`contract_rows` raises on a kind with no words here, so a new venue kind
#: cannot reach the page unnamed.
HOST_KIND_VOICE = {
    "bed-unix": "a GNU/Linux machine",
    "bed-busybox": "a BusyBox device",
    "bed-zephyr": "a Zephyr board",
    "local": "the local machine",
    "loopback-ssh": "the local machine logged in to over SSH",
    "busybox-artifact": "BusyBox's applets run on the local machine",
}


def reader_entry_narrowed(surface_id: str) -> str:
    """Say why a domain drops an entry where that entry reaches, in a reader's words."""
    return VOICE[surface_id].reader_entry_narrowed or reader_narrowed(surface_id)


def reader_narrowed(surface_id: str) -> str:
    """Why *surface_id*'s contract leaves some hosts out, in a first-time user's words."""
    voice = VOICE[surface_id]
    return voice.reader_narrowed or voice.narrowed


def _host_phrase(kind: str, term: str) -> str:
    """Name one ``(kind, term)`` for a reader: "a BusyBox device over `telnet`"."""
    words = HOST_KIND_VOICE.get(kind)
    if words is None:
        raise ValueError(f"cell kind {kind!r} has no words in HOST_KIND_VOICE")
    if term == "local" or kind == "loopback-ssh":
        return words
    return f"{words} over `{term}`"


def _suite_cells() -> "list[ResolvedCell]":
    """Every cell the conformance suite can draw: the hermetic venue's and the bed's."""
    from tests.conformance._bed import bed_space
    from tests.conformance._cells import hermetic_space

    return [*hermetic_space(), *bed_space()]


def _domain_of(contract: str) -> "Callable[[ResolvedCell], bool] | None":
    """*contract*'s module's ``applicable_cell``: the predicate the suite applies."""
    import importlib

    module = contract.split("::", maxsplit=1)[0].removesuffix(".py").replace("/", ".")
    return getattr(importlib.import_module(module), _DOMAIN_PREDICATE, None)


@dataclass(frozen=True)
class NotChecked:
    """One place a promise is not checked: a table cell's few words, and its note."""

    short: str
    """The words the promise table's **Not checked** cell carries, beside the note number."""

    text: str
    """The note's full sentence, with the reason."""

    entry: "str | None" = None
    """The one entry this note is about; ``None`` for a domain note, which holds for every entry."""

    hosts: "frozenset[tuple[str, str]]" = frozenset()
    """The ``(kind, term)`` pairs the note names: where it is checked for an
    :attr:`only` note, else where it is not."""

    only: bool = False
    """Whether the note says where the promise IS checked ("Checked only on ...")."""


def collected_cells(
    contract: str,
    cells: "list[ResolvedCell]",
    *,
    applies: "Callable[[ResolvedCell], bool] | None",
) -> "list[ResolvedCell]":
    """Return the cells *contract* is collected on: crossed with its entries, then its domain."""
    from tests.conformance._entry import space_for

    return [cell for cell in space_for(contract, cells) if applies is None or applies(cell)]


def not_checked(
    surface_id: str,
    contract: str,
    cells: "list[ResolvedCell]",
    *,
    applies: "Callable[[ResolvedCell], bool] | None",
) -> "list[str]":
    """Say where *contract*'s promise is not checked: :func:`not_checked_notes`' sentences."""
    return [note.text for note in not_checked_notes(surface_id, contract, cells, applies=applies)]


def not_checked_notes(
    surface_id: str,
    contract: str,
    cells: "list[ResolvedCell]",
    *,
    applies: "Callable[[ResolvedCell], bool] | None",
) -> "list[NotChecked]":
    """Say where *contract*'s promise is not checked, from what the suite collects.

    Read through the suite's own functions: :func:`~tests.conformance._entry.space_for`
    crosses each cell with the declared entries it admits (``entries_of``), and
    *applies* is the contract's domain rule, applied to that space as
    ``tests/conformance/conftest.py``'s ``pytest_generate_tests`` does. A host
    kind no collected cell reaches is outside the domain, and the clause says
    why in :func:`reader_narrowed`'s words; an entry that reaches fewer hosts
    than the domain names where it is, or is not, checked.
    """
    from tests.conformance._entry import ENTRY_REASON, project, space_for

    declared = entries_declared(contract)
    universe = project(cells)
    reachable = space_for(contract, cells)
    collected = collected_cells(contract, cells, applies=applies)
    notes: "list[NotChecked]" = []
    every = {(cell.kind, cell.cell.term) for cell in universe}
    domain = {(cell.kind, cell.cell.term) for cell in collected}
    outside_kinds = sorted(
        {kind for kind, _ in every} - {kind for kind, _ in domain}, key=list(HOST_KIND_VOICE).index
    )
    partly = sorted({pair for pair in every - domain if pair[0] not in outside_kinds})
    if outside_kinds or partly:
        names = [HOST_KIND_VOICE[kind] for kind in outside_kinds]
        names += [_host_phrase(*pair) for pair in partly]
        notes.append(
            NotChecked(
                hosts=frozenset(every - domain),
                short=f"not on {_join(names)}",
                text=(
                    f"Not checked on {_join(names)}, so not promised there: "
                    f"{reader_narrowed(surface_id)}."
                ),
            )
        )
    for entry in declared:
        checked = {(cell.kind, cell.cell.term) for cell in collected if cell.entry == entry}
        missing = domain - checked
        if not missing:
            continue
        # A host the entry reaches but the domain drops for that entry is not
        # checked for the domain's reason, not the entry's: each gets its note.
        admitted = {(cell.kind, cell.cell.term) for cell in reachable if cell.entry == entry}
        for left_out, among, reason in (
            (missing - admitted, admitted, ENTRY_REASON.get(entry)),
            (missing & admitted, checked, reader_entry_narrowed(surface_id)),
        ):
            if left_out:
                notes.append(_entry_note(entry, among=among, left_out=left_out, reason=reason))
    return notes


def _entry_note(
    entry: str,
    *,
    among: "set[tuple[str, str]]",
    left_out: "set[tuple[str, str]]",
    reason: "str | None",
) -> NotChecked:
    """Say that *entry* is checked on *among* and not on *left_out*, by the shorter list.

    The note sits in *entry*'s own table, so it does not name the entry. An
    "only on" list is named in the note alone, and the table cell says "only on
    some hosts" beside its number: a list of hosts appears once on a page.
    """
    order = list(HOST_KIND_VOICE)
    only = bool(among) and len(among) <= len(left_out)
    pairs = among if only else left_out
    phrases = [
        _host_phrase(*pair) for pair in sorted(pairs, key=lambda p: (order.index(p[0]), p[1]))
    ]
    text, short = (
        (f"Checked only on {_join(phrases)}", "only on some hosts")
        if only
        else (f"Not checked on {_join(phrases)}", f"not on {_join(phrases)}")
    )
    return NotChecked(
        short=short,
        text=f"{text}{f': {reason}' if reason else ''}.",
        entry=entry,
        hosts=frozenset(pairs),
        only=only,
    )


def contract_rows(cells: "list[ResolvedCell] | None" = None) -> "list[ContractRow]":
    """Every surface whose contract declares entries, in :data:`SURFACES` order.

    The entries are :func:`~tests.conformance._entry.entries_declared`'s answer,
    the one reader of the ``entries`` marker; where each is not checked is
    :func:`not_checked`'s, over *cells* (the suite's own, by default).
    """
    cells = _suite_cells() if cells is None else cells
    return [
        ContractRow(
            surface_id=surface.id,
            capability=VOICE[surface.id].capability,
            contract=surface.contract,
            entries=entries,
            notes=notes,
        )
        for surface in SURFACES
        if (entries := entries_declared(surface.contract))
        for notes in [
            not_checked_notes(
                surface.id, surface.contract, cells, applies=_domain_of(surface.contract)
            )
        ]
    ]


@dataclass(frozen=True)
class DeclaredLimitation:
    """One :class:`~otto.host.limitation.Limitation`, and every place that declares it.

    One object declared in two places (the same line-budget limitation on two
    routes) is ONE record naming both, so a reader meets it once.
    """

    frames: "list[str]"
    """The dialect names of the command frames declaring it as ``exact_output``."""

    routes: "list[str]"
    """The :class:`~otto.host.exec_route.ExecRoute` values whose record declares it."""

    limitation: "Limitation"

    consoles: "list[str]"
    """The ``console_options`` settings declaring it, as ``escapes_iac: false``
    (:attr:`~otto.host.options.ConsoleOptions.declared_limitations`)."""

    @property
    def where(self) -> str:
        """Name the declaring frames, routes and console settings for a reader."""
        parts = []
        for names, noun in ((self.frames, "command frame"), (self.routes, "route")):
            if names:
                plural = "s" if len(names) > 1 else ""
                parts.append(f"the {_join(_code(names))} {noun}{plural}")
        if self.consoles:
            settings = _join(_code([f"console_options.{name}" for name in self.consoles]))
            parts.append(f"a console whose host record sets {settings}")
        return _join(parts)


def _builtin_frames() -> "list[type[CommandFrame]]":
    """Resolve the frame classes otto ships, read off ``BUILTIN_FRAMES`` alone.

    Never off ``FRAME_CLASSES``: that registry also holds a caller's frames,
    and a built-in name a caller overwrote would answer with the caller's class.
    """
    from otto.host.command_frame import BUILTIN_FRAMES

    return [cast("type[CommandFrame]", frame.ref.resolve()) for frame in BUILTIN_FRAMES]


def declared_limitations(
    *,
    frames: "list[type[CommandFrame]] | None" = None,
    routes: "dict[ExecRoute, RouteRecord] | None" = None,
    consoles: "dict[str, Limitation] | None" = None,
) -> "list[DeclaredLimitation]":
    """Every limitation declared on *frames*, *routes* and *consoles*: otto's own by default.

    Grouped by object identity, in first-declared order (frames, then
    routes, then console settings): a frame's is its ``exact_output``, a
    route's are its record's ``limitations``, and a console setting's is
    :attr:`ConsoleOptions.declared_limitations
    <otto.host.options.ConsoleOptions.declared_limitations>`.
    """
    from otto.host.options import ConsoleOptions
    from otto.host.session import EXEC_ROUTES

    frames = _builtin_frames() if frames is None else frames
    routes = EXEC_ROUTES if routes is None else routes
    consoles = ConsoleOptions.declared_limitations if consoles is None else consoles
    grouped: "dict[int, DeclaredLimitation]" = {}

    def record(limitation: "Limitation") -> DeclaredLimitation:
        return grouped.setdefault(
            id(limitation),
            DeclaredLimitation(frames=[], routes=[], consoles=[], limitation=limitation),
        )

    for frame in frames:
        if frame.exact_output is not None:
            record(frame.exact_output).frames.append(frame.type_name)
    for route, route_record in routes.items():
        for limitation in route_record.limitations.values():
            record(limitation).routes.append(route.value)
    for name, limitation in consoles.items():
        value = "true" if ConsoleOptions.declaring_value(name) else "false"
        record(limitation).consoles.append(f"{name}: {value}")
    return list(grouped.values())


_KIND_VOICE = {
    "device": "device: the device cannot do it",
    "library": "library: a library otto builds on behaves this way",
}


def exception_label(item: "DeclaredLimitation") -> str:
    """Name the page label of *item*'s section, read off its title (what, and where)."""
    title = exception_title(item).lower()
    return "-".join(["exception", *"".join(c if c.isalnum() else " " for c in title).split()])


def exception_title(item: DeclaredLimitation) -> str:
    """Title *item*'s section with the promise it affects, and where."""
    return f"{_capitalise(item.limitation.affects)}, on {item.where}"


def _not_checked_cell(row: ContractRow, entry: str, notes: "list[str]") -> str:
    """Say in *entry*'s table where *row* is not checked: each place in words, then its note.

    "`exec-shell` only on ... (note 2); not on a Zephyr board (note 3)", read off
    :meth:`ContractRow.notes_for` *entry*, numbered by their place in the page's
    *notes*; "--" for none.
    """
    own = row.notes_for(entry)
    if not own:
        return "--"
    return "; ".join(f"{note.short} (note {notes.index(note.text) + 1})" for note in own)


def _narrower_promise(
    surface_id: str, branch: Branch, limitations: "list[DeclaredLimitation]"
) -> str:
    """One bullet: the device *branch* applies to, then its promise.

    The device is the declaration's own place (:attr:`DeclaredLimitation.where`)
    for an arm a declaration stands behind, else its :attr:`Branch.kinds` in
    :data:`HOST_KIND_VOICE`'s words. The exception's own words stay in its
    section, reached by link; an arm whose declaration is not among
    *limitations* (a test's override) keeps them inline instead, through
    :func:`branch_capability`. On a Zephyr board the bullet also names the
    exact colour sequences the frame removes
    (:data:`~otto.host.command_frame.ZEPHYR_LEVEL_SEQUENCES`), which is what
    otto always does there, not something unmeasured.
    """
    from otto.host.command_frame import ZEPHYR_LEVEL_SEQUENCES
    from otto.host.limitation import Limitation

    declared = _resolve_declaration(branch.declared_by) if branch.declared_by else None
    item = next((i for i in limitations if i.limitation is declared), None)
    if isinstance(declared, Limitation) and item is not None:
        return (
            f"On {item.where}, `{surface_id}` does not cover {declared.affects}.\n"
            f"  - Otherwise you can {_esc(branch.capability.rstrip('.'))}.\n"
            f"  - What is lost: {{ref}}`{_capitalise(declared.affects)} <{exception_label(item)}>`."
        )
    where = _join([HOST_KIND_VOICE[kind] for kind in branch.kinds]) or "some devices"
    bullet = (
        f"On {where}, `{surface_id}` holds in a narrower form.\n"
        f"  - You can {_esc(branch_capability(branch).rstrip('.'))}."
    )
    if "bed-zephyr" in branch.kinds:
        sequences = _join(
            [f"`{seq.encode('unicode_escape').decode('ascii')}`" for seq in ZEPHYR_LEVEL_SEQUENCES]
        )
        bullet += (
            f"\n  - The colour codes removed are exactly {sequences}, wherever they occur in "
            "the output, so a command that writes one of those sequences itself loses it."
        )
    return bullet


@dataclass(frozen=True)
class ZephyrRecorded:
    """What the recorded Zephyr transcripts show, read off the recordings themselves."""

    releases: "list[str]"
    """Every Zephyr release a recording was made on."""

    empty_on: "list[str]"
    """The releases on which some recorded command's ``value`` is empty."""

    leading_spaces: bool
    """Whether some recorded ``value`` has a line that starts with a space."""

    trailing_spaces: bool
    """Whether some recorded ``value`` has a line that ends with a space."""


def zephyr_recorded() -> ZephyrRecorded:
    """Read :class:`ZephyrRecorded` off ``tests/unit/host/shell_transcripts/*.json``."""
    from tests._fixtures.shell_transcripts import recordings

    zephyr = [data for data in recordings().values() if data["release"].startswith("zephyr-")]
    values = [(data["release"], entry["value"]) for data in zephyr for entry in data["commands"]]
    lines = [line for _, value in values for line in value.split("\n")]
    return ZephyrRecorded(
        releases=sorted({data["release"] for data in zephyr}),
        empty_on=sorted({release for release, value in values if value == ""}),
        leading_spaces=any(line.startswith(" ") for line in lines),
        trailing_spaces=any(line.endswith(" ") for line in lines),
    )


def _release_words(releases: "list[str]") -> str:
    """``["zephyr-2.7", "zephyr-3.7"]`` as "Zephyr 2.7 and 3.7"."""
    numbers = [release.removeprefix("zephyr-") for release in releases]
    return f"Zephyr {_join(numbers)}" if numbers else ""


def _zephyr_unrecorded(recorded: "ZephyrRecorded | None" = None) -> str:
    """Say what output on a Zephyr board is undefined, from what the recordings show.

    The caveats of ``tests/unit/host/shell_transcripts/README.md``, said from the
    recordings' own data where they are data: on which releases empty output
    was recorded, and whether a line with spaces at either end ever was.
    """
    recorded = zephyr_recorded() if recorded is None else recorded
    unmeasured = [
        "a `\\r` the program itself writes",
        "output that does not end with a newline",
    ]
    without_empty = [r for r in recorded.releases if r not in recorded.empty_on]
    if without_empty:
        seen = (
            f" (on {_release_words(recorded.empty_on)} a command with no output was "
            "recorded, and it arrives as nothing)"
            if recorded.empty_on
            else ""
        )
        unmeasured.append(f"empty output on {_release_words(without_empty)}{seen}")
    if not recorded.trailing_spaces:
        kept = (
            " (spaces at the start of a line, as `help` writes, are recorded and kept)"
            if recorded.leading_spaces
            else ""
        )
        unmeasured.append(f"spaces at the end of a line{kept}")
    if not recorded.leading_spaces:
        unmeasured.append("spaces at the start of a line")
    unmeasured += [
        "an info-level coloured line",
        "plain output followed by an error line",
        (
            "a `\\r\\n` the command writes itself (seen once, not recorded: `log status` "
            "arrives as `\\r\\r\\n`)"
        ),
    ]
    return (
        "- On a Zephyr board, output that no recording of a firmware command shows "
        "(`tests/unit/host/shell_transcripts/README.md`): "
        + "; ".join(unmeasured)
        + ". Exact output is promised there only for the firmware's own commands, as above."
    )


@dataclass(frozen=True)
class EntryVoice:
    """How one entry reaches the command contract pages: on whose page, in what words."""

    page: str
    """The :data:`COMMAND_PAGES` key whose page carries this entry's promises."""

    lead: str
    """The words a promise row on that page reads under: "With `exec` you can"."""

    heading: str = ""
    """The section of the page this entry's table sits under; empty for the page's own command."""

    about: "tuple[str, ...]" = ()
    """What this entry is, said under :attr:`heading` before its table."""


#: Which command page each entry's promises are on: THE entry -> page mapping.
#: Keyed by the entry vocabulary of ``tests/conformance/_entry.py``
#: (``ALL_ENTRIES``); :func:`render_command_contracts` raises on an entry with no
#: line here, so a new entry cannot reach the suite without a page.
ENTRY_VOICE = {
    "run": EntryVoice(page="run", lead="With `run` you can"),
    "exec": EntryVoice(page="exec", lead="With `exec` you can"),
    "exec-shell": EntryVoice(
        page="exec",
        lead="With `exec` sent through a shell session you can",
        heading="Shell-routed `exec`",
        about=(
            "This section is about one case only: an `exec` on a host reached over",
            "`ssh` that the cases above send through a shell session (`pooled-shell`)",
            "instead of its own channel. The conformance suite checks that path as an",
            "entry of its own, `exec-shell`. A host reached over `telnet` or `console`",
            "always sends `exec` through a shell session: `pooled-shell` over `telnet`,",
            "`default-session` (the session `run` uses) over `console`. Their `exec`",
            "promises are listed and checked in the main `exec` table above, and the",
            "notes on the table below do not apply to them.",
        ),
    ),
}


@dataclass(frozen=True)
class CommandPage:
    """One command's page under ``docs/command-contracts/``: the words only it says."""

    title: str
    """The page's heading."""

    summary: str
    """What the command does, in the overview's list of commands."""

    opening: "tuple[str, ...]"
    """The page's first sentences: what the command is, and how you issue it."""

    routes: "tuple[str, ...]"
    """The :class:`~otto.host.exec_route.ExecRoute` values this command can take, in
    :data:`ROUTE_VOICE`; a limitation declared on one of them applies to it."""

    route_rules: "tuple[str, ...]" = ()
    """When the command takes each of :attr:`routes`, as markdown lines."""

    route_cases: "tuple[RouteCase, ...]" = ()
    """The same, as one case per line: rendered under their groups after
    :attr:`route_rules`, and each driven through otto's real route decision by
    ``tests/unit/host/test_exec_plan.py``, so a routing change fails a test."""


@dataclass(frozen=True)
class RouteCase:
    """One case a command page lists: when it applies, and the route it takes."""

    id: str
    """The case's name, the key ``tests/unit/host/test_exec_plan.py`` builds a host by."""

    group: str
    """The heading line the case sits under on the page."""

    when: str
    """The case, in a reader's words."""

    route: str
    """The :class:`~otto.host.exec_route.ExecRoute` value it takes."""

    route_words: str = ""
    """What to print for the route instead of the bare value (a container's prefix form)."""

    note: str = ""
    """A sentence printed after the route, such as that the case is not tested."""


#: The cases the ``exec`` page lists, each a line of that page and a test case.
EXEC_ROUTE_CASES = (
    RouteCase(
        "ssh",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "reached over `ssh`, `exec` gets a bare SSH channel of its own, unless a case "
        "below applies",
        "ssh-channel",
    ),
    RouteCase(
        "telnet",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "reached over `telnet`, it goes through a shell session",
        "pooled-shell",
    ),
    RouteCase(
        "proxied",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "the host's login is **proxied** (otto logs in as one account and then switches "
        "to the one it runs as; [Credentials and login proxies]"
        "(../configuration/host-sources.md#credentials-and-login-proxies)): a shell "
        "session",
        "pooled-shell",
        note=(
            "No host the suite tests has a proxied login "
            "({ref}`What is not covered at all <command-contracts-not-covered>`)."
        ),
    ),
    RouteCase(
        "hook",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "the host declares a **session-setup hook** (project code otto runs on every "
        "shell session it opens; {ref}`Session setup <per-host-session-setup>`): a shell "
        "session",
        "pooled-shell",
    ),
    RouteCase(
        "needs-shell",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "the call needs a shell: it passes `expects` (patterns otto answers while the "
        "command runs, such as a password prompt), `sudo=True`, or a `user` otto cannot "
        "log in as directly",
        "pooled-shell",
    ),
    RouteCase(
        "own-ssh-user",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "`exec(user=...)` over `ssh` for a user with SSH credentials of its own, and no "
        "`expects` or `sudo`: an SSH connection as that user, even when the login is "
        "proxied or the host declares a session-setup hook",
        "ssh-channel",
    ),
    RouteCase(
        "console",
        "On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):",
        "reached over `console`, it uses the same session as `run`: a console serves one client",
        "default-session",
    ),
    RouteCase(
        "embedded",
        "On other hosts:",
        "on an embedded host, such as a Zephyr board, its one console session, as `run`",
        "default-session",
    ),
    RouteCase(
        "local", "On other hosts:", "on the local host, a fresh local process", "local-subprocess"
    ),
    RouteCase(
        "container",
        "On other hosts:",
        "in a docker container, `docker exec` through its parent host",
        "container",
        route_words="`container/<the parent's route>`",
    ),
)


def _route_case_lines(cases: "tuple[RouteCase, ...]") -> "list[str]":
    """*cases* as markdown: each group's heading line, then one bullet per case."""
    lines: "list[str]" = []
    group = None
    for case in cases:
        if case.group != group:
            group = case.group
            lines += ["", group, ""] if lines else [group, ""]
        route = case.route_words or f"`{case.route}`"
        lines.append(f"- {_capitalise(case.when)} ({route}).{f' {case.note}' if case.note else ''}")
    return lines


#: Every command with a contract page, in the order the overview lists them. A
#: command's page carries the entries :data:`ENTRY_VOICE` maps to its key.
COMMAND_PAGES = {
    "run": CommandPage(
        title="`run`: a command in the host's session",
        summary="sends a command to the host's one persistent shell session",
        opening=(
            "`run` sends a command to the host's one persistent shell session, so what",
            "one command changes there (the working directory, a variable) is still",
            "there for the next. You call it from Python, as",
            "{meth}`Host.run <otto.host.host.Host.run>`; the CLI has no `run`.",
        ),
        routes=("default-session",),
        route_rules=(
            "`run` always uses the host's one persistent shell session, the",
            "`default-session` route, whatever the host's term. On an embedded host,",
            "such as a Zephyr board, that session is the board's one console session.",
            "So `result.route` is `default-session` for every `run`.",
        ),
    ),
    "exec": CommandPage(
        title="`exec`: a one-off command",
        summary="runs a one-off command, with no state carried from one call to the next",
        opening=(
            "`exec` runs a one-off command: nothing one call changes is carried to the",
            "next. From the CLI it is `otto host <id> exec` ({doc}`/cli/host/exec`); from",
            "Python, {meth}`Host.exec <otto.host.host.Host.exec>`.",
        ),
        routes=("ssh-channel", "pooled-shell", "default-session", "local-subprocess", "container"),
        route_cases=EXEC_ROUTE_CASES,
    ),
}

#: What each route a command can take is, in a reader's words, in the order the
#: overview lists them. Keyed by :class:`~otto.host.exec_route.ExecRoute` value.
ROUTE_VOICE = {
    "default-session": "the host's one persistent shell session, the one `run` uses",
    "pooled-shell": "a shell session otto keeps for `exec` beside the one `run` uses",
    "ssh-channel": "a bare SSH channel of the command's own, with no terminal",
    "local-subprocess": "a fresh process on the local machine",
    "container": (
        "a docker container's command, run as `docker exec` through its parent host; "
        "its result reports `container/` and the route the parent took"
    ),
}

COMMAND_CONTRACTS_LABEL = "command-contracts"
"""The overview page's label: what another page links to for the general rules."""


def command_page_label(command: str) -> str:
    """Name the label of *command*'s page: ``command-contract-exec``."""
    return f"command-contract-{command}"


def _contract_page_header(source: str) -> "list[str]":
    """Return the generated-file comment every command contract page opens with."""
    return [
        "<!-- GENERATED FILE -- do not edit by hand.",
        "     scripts/render_support_matrix.py (render_command_contracts) renders this",
        f"     from {source}.",
        "     Regenerate with `uv run python -m scripts.render_support_matrix`;",
        "     edits are overwritten. -->",
        "",
    ]


def entries_on_page(command: str) -> "list[str]":
    """Return the entries :data:`ENTRY_VOICE` puts on *command*'s page, in entry order."""
    from tests.conformance._entry import ALL_ENTRIES

    return [entry for entry in ALL_ENTRIES if ENTRY_VOICE[entry].page == command]


def _check_page_mapping() -> None:
    """Refuse an entry with no page, a page no entry reaches, or a route with no words."""
    from tests.conformance._entry import ALL_ENTRIES

    problems = [
        f"entry {e!r} has no line in ENTRY_VOICE" for e in ALL_ENTRIES if e not in ENTRY_VOICE
    ]
    problems += [
        f"entry {entry!r} maps to {voice.page!r}, which COMMAND_PAGES does not hold"
        for entry, voice in ENTRY_VOICE.items()
        if voice.page not in COMMAND_PAGES
    ]
    problems += [
        f"command page {command!r} carries no entry"
        for command in COMMAND_PAGES
        if not any(voice.page == command for voice in ENTRY_VOICE.values())
    ]
    from otto.host.exec_route import ExecRoute

    # Every route a result can report has words, and nothing else does: bare
    # `container` stands for its `container/<parent route>` form, and
    # `unsupported` is never reported (an unsupported term raises first).
    reported = {route.value for route in ExecRoute} - {ExecRoute.UNSUPPORTED.value}
    if set(ROUTE_VOICE) != reported:
        problems.append(
            f"ROUTE_VOICE {sorted(ROUTE_VOICE)} != the ExecRoute values {sorted(reported)}"
        )
    problems += [
        f"command page {command!r} names route {route!r}, which ROUTE_VOICE does not hold"
        for command, page in COMMAND_PAGES.items()
        for route in page.routes
        if route not in ROUTE_VOICE
    ]
    if problems:
        raise ValueError(
            "the command contract pages cannot be laid out:\n  " + "\n  ".join(problems)
        )


def _applies_to(item: DeclaredLimitation, page: CommandPage) -> bool:
    """Whether *item* reaches a command taking *page*'s routes.

    A frame's or a console setting's limitation holds on every route that types
    into the session or reads that console, so on every command; a route's only
    on the commands that can take that route.
    """
    return bool(item.frames or item.consoles or set(item.routes) & set(page.routes))


def _narrowed(rows: "list[ContractRow]") -> "list[tuple[str, Branch]]":
    """Every ``(surface id, arm)`` whose promise is narrower on some device than its row says."""
    return [
        (row.surface_id, branch)
        for row in rows
        for branch in VOICE[row.surface_id].branches[1:]
        if branch.capability
    ]


def render_command_contracts(
    *,
    rows: "list[ContractRow] | None" = None,
    limitations: "list[DeclaredLimitation] | None" = None,
    cells: "list[ResolvedCell] | None" = None,
) -> "dict[str, str]":
    """Render every page of ``docs/command-contracts/``, keyed by file name.

    DECLARED, like :func:`render_families`: read off the ``entries`` marker on
    each command contract (:func:`contract_rows`) and the limitations the
    built-in frames, exec routes and console settings declare
    (:func:`declared_limitations`). ``index.md`` is the overview, holding what
    every command shares; each :data:`COMMAND_PAGES` key gets a page carrying
    the entries :data:`ENTRY_VOICE` maps to it. *rows*, *limitations* and
    *cells* (the suite's cells, which *rows* and the proxied-login sentence are
    read from) replace a source, for a test.
    """
    _check_page_mapping()
    cells = _suite_cells() if cells is None else cells
    rows = contract_rows(cells) if rows is None else rows
    limitations = declared_limitations() if limitations is None else limitations
    labels = [exception_label(item) for item in limitations]
    if len(set(labels)) != len(labels):
        raise ValueError(f"two declared limitations share a section label: {labels}")
    pages = {"index.md": render_contracts_overview(rows, limitations, cells)}
    for command in COMMAND_PAGES:
        pages[f"{command}.md"] = render_command_page(command, rows, limitations)
    return pages


#: What each kind of host the suite reaches is, for a first-time reader, and
#: whether it needs the lab (``True``: on the lab's test bed) or runs on the
#: machine running the suite (``False``). Keyed as :data:`HOST_KIND_VOICE`;
#: :func:`render_contracts_overview` raises on a kind with no line here.
HOST_KIND_GLOSS = {
    "bed-unix": ("a full Linux system, with the GNU tools and bash", True),
    "bed-busybox": (
        "a small Linux system whose commands and shell (ash) all come from BusyBox",
        True,
    ),
    "bed-zephyr": (
        "a small board running the Zephyr operating system, driven through its own shell",
        True,
    ),
    "local": ("the machine otto itself runs on", False),
    "loopback-ssh": ("the same machine, reached over SSH as if it were remote", False),
    "busybox-artifact": (
        (
            "BusyBox's commands (its applets) put on the `PATH` of a `run` session on the "
            "local machine, standing in for a BusyBox device without a lab"
        ),
        False,
    ),
}


def _host_kind_lines(cells: "list[ResolvedCell]") -> "list[str]":
    """List the overview's kinds of host, glossed and tagged with where each runs.

    Each names the terms the suite reaches it over, read from *cells*.
    """
    missing = [kind for kind in HOST_KIND_VOICE if kind not in HOST_KIND_GLOSS]
    if missing:
        raise ValueError(f"host kinds with no words in HOST_KIND_GLOSS: {missing}")
    terms: "dict[str, set[str]]" = {}
    for cell in cells:
        if cell.cell.term != "local" and cell.kind != "loopback-ssh":
            terms.setdefault(cell.kind, set()).add(cell.cell.term)
    lines = []
    for needs_lab, place in ((False, "Needing no lab:"), (True, "On the lab's test bed:")):
        lines += [f"{place}", ""]
        for kind, words in HOST_KIND_VOICE.items():
            if HOST_KIND_GLOSS[kind][1] is not needs_lab:
                continue
            over = f" Reached over {_join(_code(sorted(terms[kind])))}." if kind in terms else ""
            lines.append(f"- **{_capitalise(words)}**: {HOST_KIND_GLOSS[kind][0]}.{over}")
        lines.append("")
    return lines


#: Words the pages use that a newcomer may not know, glossed once on the overview.
GLOSSARY = {
    "lab": "the hosts a project describes in its lab file, which otto connects to",
    "test bed": "the virtual machines and emulated boards otto's own lab tests against",
    "ash": "BusyBox's shell",
    "BusyBox applets": "the small commands BusyBox provides, one program standing in for many",
    "echo": "a terminal sending back each character typed, so it shows on screen",
    "IAC": (
        "the telnet byte 0xFF, which starts a telnet command; `escapes_iac` in a "
        "console's settings says whether its server doubles a 0xFF the command wrote"
    ),
    "proxied login": (
        "otto logs in as one account, then switches to the one it runs as "
        "([Credentials and login proxies]"
        "(../configuration/host-sources.md#credentials-and-login-proxies))"
    ),
    "session-setup hook": (
        "project code otto runs on every shell session it opens "
        "({ref}`Session setup <per-host-session-setup>`)"
    ),
}


def _reaches_a_container(cells: "list[ResolvedCell]") -> bool:
    """Whether some cell's command, through some entry it has, reports a container route."""
    from tests.conformance._entry import entries_of, expected_route, with_entry

    return any(
        expected_route(with_entry(cell, entry)).startswith("container")
        for cell in cells
        for entry in entries_of(cell)
    )


def _signalled() -> "CommandResult":
    """Build the failed result of a command killed by SIGTERM, as a shell reports it."""
    import signal as _signal

    from otto.host.exit_status import exit_status_of
    from otto.result import CommandResult
    from otto.utils import Status

    # A nonzero exit status means `Failed`: product code has no shared owner for that decision yet.
    return CommandResult(status=Status.Failed, retcode=exit_status_of(-_signal.SIGTERM))


def _timed_out_example() -> "CommandResult":
    """Build a timed-out result with the one function that builds every one."""
    from otto.host.exec_route import ExecRoute
    from otto.host.session import ExecOutput, timed_out_result

    return timed_out_result(
        cmd="sleep 60", timeout=5.0, output=ExecOutput(), route=ExecRoute.SSH_CHANNEL
    )


def exception_link(item: DeclaredLimitation) -> str:
    """Link *item*'s section on the overview, with readable text.

    A role's explicit text cannot carry a code span, so the text is the
    section's title with its backticks dropped.
    """
    return f"{{ref}}`{exception_title(item).replace('`', '')} <{exception_label(item)}>`"


def _line_budget_link(limitations: "list[DeclaredLimitation]") -> str:
    """Link the line-length exception's section: the one place its rule is stated."""
    from otto.host.exec_route import ExecRoute
    from otto.host.session import EXEC_ROUTES

    budget = EXEC_ROUTES[ExecRoute.POOLED_SHELL].limitations.get("line_budget")
    item = next((i for i in limitations if i.limitation is budget), None)
    return exception_link(item) if item else "[Declared exceptions](#declared-exceptions)"


def _not_checked_example(rows: "list[ContractRow]") -> str:
    """Say where one promise is or is not checked, read off the first note in *rows* with hosts.

    An "only on" note is preferred, since it lists where the promise holds.
    """
    order = list(HOST_KIND_VOICE)
    found = [(r, n) for r in rows for n in r.notes if n.hosts]
    found.sort(key=lambda pair: not pair[1].only)
    if not found:
        return "a promise a command's page marks as not checked somewhere."
    row, note = found[0]
    pairs = sorted(note.hosts, key=lambda p: (order.index(p[0]), p[1]))
    where = _join([_host_phrase(*pair) for pair in pairs])
    through = f" through `{note.entry}`" if note.entry else ""
    if note.only:
        return f"`{row.surface_id}` is checked{through} only on {where}."
    return f"`{row.surface_id}` is not checked{through} on {where}."


def render_contracts_overview(
    rows: "list[ContractRow]",
    limitations: "list[DeclaredLimitation]",
    cells: "list[ResolvedCell]",
) -> str:
    """Render ``docs/command-contracts/index.md``: what every command's contract shares."""
    from otto.host.host import DEFAULT_COMMAND_TIMEOUT

    timed_out = _timed_out_example()
    signalled = _signalled()
    lines = _contract_page_header(
        "the `entries` marker on each command contract and the limitations\n"
        "     declared on otto's built-in command frames, exec routes and console settings"
    )
    lines += [
        f"({COMMAND_CONTRACTS_LABEL})=",
        "",
        "# Command contracts",
        "",
        "A **command contract** is a promise otto makes about the result of a",
        "command you run on a host -- its exit code, its output, its timeout --",
        "together with the test that checks it. This page holds what every command",
        "shares. Each command's own page, listed below, says which promises are",
        "checked through it, and where.",
        "",
        "## Three words",
        "",
        "| Word | Means | Examples |",
        "| --- | --- | --- |",
        "| **term** | how otto reaches the host | `ssh`, `telnet`, `console` |",
        "| **entry** | which call you made | `run`, `exec`, shell-routed `exec` |",
        (
            "| **route** | which path the command then took | listed in "
            "[Which route your command takes](#which-route-your-command-takes) |"
        ),
        "",
        "A host's term is set in its record in the lab file",
        "({doc}`Host options </configuration/host-options>`). The route follows from",
        "the term, the entry and the call's arguments; each command's page says how.",
        "Other words these pages use:",
        "",
        *[f"- **{word}**: {meaning}." for word, meaning in GLOSSARY.items()],
        "",
        "## What you see",
        "",
        "From the CLI, `otto host <id> exec` shows the command's output as it runs and",
        (
            f"exits with the command's exit code, or {timed_out.exit_code} when the "
            "command never finished."
        ),
        "A timed-out command prints no message of its own: you see the output it wrote",
        f"before the budget expired, and the exit code {timed_out.exit_code}",
        "({doc}`/cli/host/exec`, {ref}`the host exit codes <host-exit-codes>`). From",
        "Python, `run` and `exec` hand back a {class}`~otto.result.CommandResult` whose",
        "fields hold the same answers: `retcode` is the command's own return code,",
        "`exit_code` is what the CLI exits with, and `value` is the output. For",
        "example (the result built here stands for what `await host.exec(...)`",
        "returns):",
        "",
        "```{doctest}",
        ">>> from otto.result import CommandResult",
        ">>> from otto.utils import Status",
        ">>> result = CommandResult(",
        '...     status=Status.Success, retcode=0, value="hello", route="ssh-channel"',
        "... )",
        ">>> result.route, result.timed_out, result.value",
        "('ssh-channel', False, 'hello')",
        "```",
        "",
        "## The commands covered",
        "",
        *[
            f"- {{doc}}`{command} <{command}>`: {page.summary}."
            for command, page in COMMAND_PAGES.items()
        ],
        "",
        "A command not listed here has no contract yet: nothing on these pages is",
        "promised for it.",
        "",
        "```{toctree}",
        ":hidden:",
        "",
        *COMMAND_PAGES,
        "```",
        "",
        "(command-contracts-checked)=",
        "",
        "## How otto checks a contract",
        "",
        "Each promise has a test in otto's conformance suite. The suite runs the test",
        "once through each entry it declares, on each **kind of host** below. Some",
        "need no lab and run on the machine running the suite; the rest are on the",
        "lab's **test bed**, the virtual machines and emulated boards otto's own lab",
        "tests against.",
        "",
        *_host_kind_lines(cells),
        "Whether a promise held, device by device, is in the",
        "{doc}`support matrix </architecture/support-matrix>`.",
        "",
        "(command-contracts-promise-id)=",
        "",
        "Each promise has a **Promise id**: a fixed name, shared by every command's",
        "page and by the support matrix. The `exec-` at the start of most ids is",
        "historical; it does not mean the `exec` command.",
        "",
        "## Which route your command takes",
        "",
        "otto picks the route for each call. A command takes one of these:",
        "",
        *[f"- `{route}`: {words}." for route, words in ROUTE_VOICE.items()],
        "",
        "Each command's page says when it takes each. From Python, read the result's",
        "`route` field",
        (
            "([Which route ran a command]"
            "(../architecture/utilities/results.md#which-route-ran-a-command));"
        ),
        "`otto host <id> exec` does not print it.",
        "",
        "(command-contracts-not-promised)=",
        "",
        "## How to read what is not promised",
        "",
        "A promise holds where it is checked, unless a declared exception says",
        "otherwise. Anything else is not promised, in one of three ways:",
        "",
        "1. **Not checked.** The promise exists, but no test covers this kind of host",
        "   or this entry, so it is not promised there. A command's page says where,",
        "   beside each promise, with a numbered note saying why. For example,",
        f"   {_not_checked_example(rows)}",
        "2. **A declared exception.** The device, or a library otto is built on (such",
        "   as telnetlib3), makes the promise impossible, and otto says so in its",
        "   code. Each is listed [below](#declared-exceptions) with its measurements.",
        "   For example, a long line typed into a terminal is cut.",
        "3. **Undefined.** There is no promise at all, because no device has been",
        "   measured behaving this way. Each is listed under",
        "   [Undefined](#undefined-never-measured-so-not-promised). For example, what",
        "   a tab looks like on a terminal that turns tabs into spaces.",
        "",
        "## What the output and timeout promises mean",
        "",
        "What a command's output holds, line by line, is defined in",
        "{ref}`What a command's value holds <command-output-contract>`.",
        "",
        f"The time budget is the call's `timeout`: {DEFAULT_COMMAND_TIMEOUT:g} seconds by",
        "default ({data}`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`), for `run` and",
        "`exec` alike, and `--timeout` on the CLI. A command that outlives it does",
        f"not raise. Its result has `timed_out` set to `{timed_out.timed_out}`, `status`",
        f"`Status.{timed_out.status.name}`, `retcode` `{timed_out.retcode}` (the command",
        "never finished, so it has no exit code of its own) and `exit_code`",
        f"`{timed_out.exit_code}`; `otto host <id> exec` exits {timed_out.exit_code}.",
        "Its `value` begins with the timeout line (for a 5-second budget,",
        f"`{timed_out.value.splitlines()[0]}`); the rest of its shape is in",
        "{ref}`Timed out <timed-out-command-result>`. `retcode` `-1` also means",
        "other things, so `timed_out` is the field that tells a timeout apart",
        "([The Result family](../architecture/utilities/results.md#the-result-family)).",
        "",
        "On a Zephyr board a command's `retcode` is the firmware's own return value,",
        "in negative-errno style (`-8` for an unknown command), not a POSIX exit",
        "status, and a failed command's `exit_code` is that same negative value",
        "([Exit codes](../architecture/utilities/results.md#exit-codes) says the one",
        "value it cannot carry).",
        "",
        f"A command that fails has `status` `Status.{signalled.status.name}`, and its `exit_code`",
        "is its `retcode`. One killed by signal N fails the same way, with `retcode`",
        f"128+N: `kill -TERM` reads `{signalled.retcode}`, and `exit_code`",
        f"`{signalled.exit_code}`",
        "({ref}`A command killed, timed out or interrupted <command-ends-abnormally>`).",
        "",
    ]
    narrower = _narrowed(rows)
    if narrower:
        lines += [
            "(command-contracts-narrower)=",
            "",
            "## Where a device gets a narrower promise",
            "",
            "On these devices the promise a test can check is narrower than a command's",
            "page says, and only the narrower one is promised there, for every command:",
            "",
        ]
        lines += [
            f"- {_narrower_promise(surface_id, branch, limitations)}"
            for surface_id, branch in narrower
        ]
        lines.append("")
    lines += ["(command-contracts-not-covered)=", "", "## What is not covered at all", ""]
    if not any(cell.proxied_login for cell in cells):
        lines += [
            "No host the conformance suite reaches has a **proxied** login, so the",
            "route a proxied login takes is covered by unit tests only, and none of",
            "the promises on these pages is checked through it.",
            "",
        ]
    if not _reaches_a_container(cells):
        lines += [
            "A docker container's `exec` is designed to follow its parent host's route",
            "({ref}`What a command's value holds <command-output-contract>`), but no",
            "host the conformance suite reaches is a container, so nothing on these",
            "pages is promised for one.",
            "",
        ]
    lines += [
        "## Declared exceptions",
        "",
        "Where a device's firmware or shell, or a library otto is built on, makes a",
        "promise impossible, otto declares the exception in its code, next to what",
        "it applies to. Each is described once, here; a command's page links the ones",
        "that apply to it.",
        "",
    ]
    if limitations:
        kinds = list(dict.fromkeys(item.limitation.kind for item in limitations))
        # A Kind column only while the kinds differ: one kind on every row says nothing.
        mixed = len(kinds) > 1
        lines += [
            "| Exception |" + (" Kind |" if mixed else "") + " In short |",
            "| --- |" + (" --- |" if mixed else "") + " --- |",
        ]
        lines += [
            f"| {exception_link(item)} |"
            + (f" {_KIND_VOICE.get(item.limitation.kind, item.limitation.kind)} |" if mixed else "")
            + f" {_esc(item.limitation.summary or item.limitation.reason)} |"
            for item in limitations
        ]
        lines += [
            "",
            "Each section below gives the exception's **kind**: "
            + "; ".join(f"{_KIND_VOICE.get(kind, kind)}" for kind in _KIND_VOICE)
            + ".",
        ]
        for item in limitations:
            lines += [
                "",
                f"({exception_label(item)})=",
                "",
                f"### {exception_title(item)}",
                "",
                (
                    f"Where: {item.where}. Kind: "
                    f"{_KIND_VOICE.get(item.limitation.kind, item.limitation.kind)}."
                ),
                "",
                item.limitation.reason,
                "",
                "Measured (the lab detail, for the evidence):",
                "",
            ]
            lines += [f"- {measurement}" for measurement in item.limitation.measured_on]
    else:
        lines.append("None declared.")
    lines += [
        "",
        "(command-contracts-undefined)=",
        "",
        "## Undefined: never measured, so not promised",
        "",
        "These are not declared exceptions, because no device has been measured",
        "producing them:",
        "",
        "- A terminal that changes line endings in a way otto cannot undo exactly.",
        "  The session reports its line-ending mapping as `unknown(...)`",
        "  (`otto host <id> probe` prints it), returns the output as it arrived, and",
        "  logs a warning once. What a command's output looks like there is not",
        "  promised.",
        "- A tab a command writes, on a terminal that turns tabs into spaces, where",
        "  otto could not switch that off (with `stty tab0`; the device has no `stty`,",
        "  or its `stty` refuses). The session reports its mapping with",
        "  `, tabs expanded` after it, logs a warning once, and the tab arrives as",
        "  spaces.",
        "- Output after a command changes its own terminal settings on a shell",
        "  session, whether `run` or a shell-routed `exec` reached it: for example",
        "  `stty -onlcr`, which stops the terminal turning a newline into `\\r\\n`, or",
        "  a program that leaves the terminal in raw mode, passing every byte through",
        "  unchanged. The session measured its terminal when it opened and does not",
        "  measure again until a new session opens",
        "  ({ref}`What a command's value holds <command-output-contract>`).",
        _zephyr_unrecorded(),
        "- How long a line the Zephyr shell itself accepts. Its command buffer is a",
        "  firmware build setting (`CONFIG_SHELL_CMD_BUFF_SIZE`) and has never been",
        "  measured. Where otto sizes its own lines on a Zephyr board it uses the",
        f"  Linux terminal's limit ({_line_budget_link(limitations)}), which is not a",
        "  measurement of any Zephyr board.",
    ]
    if rows:
        lines += [
            "",
            "## For contributors: which test checks each promise",
            "",
            "By promise id:",
            "",
        ]
        lines += [f"- `{row.surface_id}`: `{row.contract}`" for row in rows]
        lines += [
            "",
            "The suite checks shell-routed `exec` (the `exec-shell` entry) on an SSH host",
            "whose `exec` would otherwise get a channel of its own, by declaring a",
            "session-setup hook that does nothing: declaring any hook sends `exec` to a",
            "shell session.",
        ]
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _entry_table(entry: str, rows: "list[ContractRow]", notes: "list[str]") -> "list[str]":
    """*entry*'s promise table: one row per contract checked through it, under its lead-in."""
    own = [row for row in rows if entry in row.entries]
    if not own:
        return [f"No promise is checked through `{entry}` yet."]
    lines = [
        (
            f"| {ENTRY_VOICE[entry].lead} | Not checked "
            "| {ref}`Promise id <command-contracts-promise-id>` |"
        ),
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {_esc(row.capability)} | {_esc(_not_checked_cell(row, entry, notes))} "
        f"| `{row.surface_id}` |"
        for row in own
    ]
    if _narrowed(own):
        lines += [
            "",
            "On some devices a promise above is narrower: see",
            "{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.",
        ]
    return lines


def render_command_page(
    command: str, rows: "list[ContractRow]", limitations: "list[DeclaredLimitation]"
) -> str:
    """Render *command*'s page: its routes, and the promises of each entry mapped to it."""
    page = COMMAND_PAGES[command]
    entries = entries_on_page(command)
    notes = list(
        dict.fromkeys(
            note.text
            for entry in entries
            for row in rows
            if entry in row.entries
            for note in row.notes_for(entry)
        )
    )
    lines = _contract_page_header(
        f"the `entries` marker on each command contract checked\n     through `{command}`"
    )
    lines += [
        f"({command_page_label(command)})=",
        "",
        f"# {page.title}",
        "",
        *page.opening,
        "The rules every command shares -- what a contract is, what its promises",
        "mean, and how to read what is not promised -- are on",
        f"{{ref}}`Command contracts <{COMMAND_CONTRACTS_LABEL}>`.",
        "",
        f"## Which route `{command}` takes",
        "",
        *page.route_rules,
        *_route_case_lines(page.route_cases),
        "",
        f"## What `{command}` promises",
        "",
        f"Each promise below is checked through `{command}` on every kind of host the",
        "conformance suite reaches ({ref}`the list <command-contracts-checked>`), except",
        "where its **Not checked** column says otherwise; the numbered notes below say why.",
        "",
        *_entry_table(command, rows, notes),
    ]
    for entry in entries:
        voice = ENTRY_VOICE[entry]
        if entry == command:
            continue
        lines += [
            "",
            f"## {voice.heading}",
            "",
            *voice.about,
            "",
            *_entry_table(entry, rows, notes),
        ]
    lines += ["", "## Where a promise is not checked", ""]
    lines += [f"{number}. {note}" for number, note in enumerate(notes, start=1)]
    if not notes:
        lines.append(
            "Every promise on this page is checked on every kind of host the suite reaches."
        )
    applying = [item for item in limitations if _applies_to(item, page)]
    lines += [
        "",
        "## Declared exceptions and undefined behaviour",
        "",
        f"These declared exceptions apply to `{command}`; each is described on",
        f"{{ref}}`Command contracts <{COMMAND_CONTRACTS_LABEL}>`:",
        "",
    ]
    lines += [f"- {exception_link(item)}" for item in applying] or ["- None declared."]
    lines += [
        "",
        f"What is never measured, so not promised, for `{command}` as for every command:",
        "{ref}`Undefined <command-contracts-undefined>`.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render(matrix: dict, *, rendered_on: "dt.date | None" = None) -> str:
    """Render the whole page, refusing a matrix whose axes the tree no longer backs."""
    problems = axes_mismatch(matrix)
    if problems:
        raise ValueError(
            "refusing to render a support matrix the tree no longer backs:\n  "
            + "\n  ".join(problems)
        )
    rendered_on = rendered_on or dt.datetime.now(dt.timezone.utc).date()
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
        "What each command promise means for a command's result, and the declared",
        "exceptions to it, are on {ref}`Command contracts <command-contracts>`.",
        "",
    ]
    lines += _currency_section(matrix, rendered_on)
    lines += _states_section()
    lines += _guarantee_section()
    lines += _progress_promises_section()
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
    parser.add_argument("--families-page", type=Path, default=FAMILIES_PAGE_PATH)
    parser.add_argument("--contracts-dir", type=Path, default=COMMAND_CONTRACTS_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report axis disagreement and a stale committed page, and write nothing",
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
    committed = {
        args.families_page: render_families(),
        **{args.contracts_dir / name: text for name, text in render_command_contracts().items()},
    }
    if args.check:
        # The families and contract pages are COMMITTED, so --check can compare
        # them; the matrix page is git-ignored and regenerated, so there is
        # nothing to compare.
        stale = [
            path
            for path, text in committed.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        # A page no command generates any more (a renamed or removed one) is stale too.
        if args.contracts_dir.is_dir():
            stale += sorted(set(args.contracts_dir.glob("*.md")) - set(committed))
        for path in stale:
            print(
                f"{path} is not what the tree would generate; "
                f"re-run `uv run python -m scripts.render_support_matrix` and commit it",
                file=sys.stderr,
            )
        if stale:
            return 1
        print(f"{args.matrix}: axes agree with the tree; wrote nothing (--check)")
        return 0
    args.page.parent.mkdir(parents=True, exist_ok=True)
    args.page.write_text(render(matrix), encoding="utf-8")
    cells = sum(len(row) for row in matrix["cells"].values())
    print(f"{args.page}: rendered {cells} cells")
    for path, text in committed.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"{path}: rendered from the tree's declarations")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
