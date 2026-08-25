"""What a conformance run LEAVES BEHIND: one JSON record per cell it exercised.

Spec 2026-08-22 §5 asks a conformance run to "emit an observation record per
cell it exercised (JSON, into the run's xdir output)", which a later collate
step folds into ``schemas/support_matrix.json``. Those records are the ONLY
path by which a cell may become ``measured-*``, so this module's whole job is
to be un-fabricatable: it reports what the run actually did, at a point where
that is already decided.

**THERE IS NO XDIR HERE, AND THAT IS MEASURED, NOT ASSUMED.** ``--xdir``
(``OTTO_XDIR``, default ``Path()`` = CWD, ``otto.cli.main``) is a property of
an *otto CLI invocation*: ``otto.cli.invoke`` hands it to
``otto.logger.management.init_cli_logging``, and ``create_output_dir`` then
makes ``<xdir>/<command>/<stamp>/`` under it. This suite never invokes the
CLI -- it builds hosts directly, through ``otto.host.factory`` and
``LocalHost`` -- so no such directory is ever created. Two further facts
close the question:

* ``OTTO_XDIR`` is NOT in :data:`tests._ambient_env.AMBIENT_OPT_INS`, so
  ``tests/conftest.py`` STRIPS it at import. A run cannot be pointed at an
  xdir even deliberately.
* MEASURED 2026-08-24: one hermetic contract over all 8 cells (0.73s) added
  ZERO paths to the repo tree -- a ``find -maxdepth 3`` listing diffed before
  and after came back identical. The ``run/`` and ``logs/`` directories a dev
  checkout carries are an otto CLI invocation's leavings from the e2e tree
  (``<xdir>/<command>/<stamp>/`` per ``create_output_dir``, and
  ``<xdir>/logs/<host id>/`` per ``BaseHost.log_dest``), not this suite's.

So the run output directory is CHOSEN here rather than inherited:
:func:`observations_dir`, ``reports/conformance-observations/`` under
``PROJECT_ROOT``. ``reports/`` is where every lane already writes its
machine-readable output (``Makefile``'s ``JUNIT_DIR``), it is git-ignored, and
``make clean`` removes it. Anchored at ``PROJECT_ROOT`` and not at CWD: a
lane invoked from a subdirectory would otherwise scatter records where the
collator does not look.

WHY THE OUTCOME IS READ FROM THE REPORT AND NOT FROM THE TEST BODY. A record
written inside a contract, before its assertions run, reports success for a
test that then fails -- a green record vouching for a red test, which is the
exact fabrication the matrix exists to prevent. ``pytest_runtest_makereport``
is where the outcome exists, and this module waits for the TEARDOWN report so
that a setup error, a skip and a strict xfail are each distinguishable from a
pass rather than collapsing into one.

A SKIP IS NEVER A PASS. ``tests/conformance/conftest.py`` refuses to skip a
drawn cell precisely because "a skip inside a drawn cell reports success for a
contract nobody ran"; this module carries that rule into the artifact by
giving ``skipped`` its own outcome, which no collation may read as evidence.

THREE RECORD KINDS. The first two exist because a cell can fail to be measured
in two very different ways and only one of them produces a test item; the third
exists because a contract's pass is only evidence if the instrument that
measured it could have said no.

``observation``
    A contract RAN against a drawn cell. Carries the outcome, and the
    OBSERVABLE the contract declared for that cell
    (``tests/conformance/_observable.py``) -- ``null`` for a contract that
    declares none, which is what stops the collator marking that surface
    ``measured-ok`` at all.

``domain-exclusion``
    A contract declared a drawn cell outside its applicable domain
    (``applicable_cell``), so no item was ever generated for it and no report
    hook can see it. Without this record the artifact could never hold an
    honest ``not_observable`` list. RE-MEASURED 2026-08-24 by applying each
    predicate to ``bed_space()``'s 49 cells: transfer excludes 3 (elements
    ``zephyr37_nofs``, ``zephyr37_llext``, ``zephyr44_llext``) and timeout
    excludes 7 -- every element of all three Zephyr profiles, so those matrix
    cells need an EMPTY ``observed_on``, a state only ``not-observable``
    permits.

    Deliberately NOT emitted as ``not-observable`` outright. Whether a
    contract's domain narrowing earns that verdict is a collation decision
    (the two live narrowings differ: transfer's reads otto's own
    ``supports_transfer``, timeout's is this suite's judgement -- see each
    ``applicable_cell`` docstring). This module states the fact it measured --
    the predicate was evaluated on this cell and answered False -- and invents
    no reason of its own.

``control``
    A POSITIVE CONTROL ran against a drawn cell. Its outcome NEVER becomes a
    cell's verdict and never enters an ``observed_on`` -- Task 3 of this item
    established that a control's result is a statement about the INSTRUMENT and
    not about the host -- but the collator must be able to ask whether the
    instrument proved itself on the very cell an observation came from before
    writing ``measured-ok``. See :func:`control_record` for the gap this
    closes: a collator that only CONSTRUCTS a positive-control nodeid publishes
    wiring, not evidence.

A cell the venue could not BUILD produces neither record, and needs no code to
say so: it never enters the space, so it is never drawn, never parametrized,
and no hook here ever sees it. That is the "dropped, not skipped" rule of
``tests/conformance/_cells.py`` holding on its own.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

import pytest

from tests._ambient_env import ambient
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.support_matrix import SURFACES, Surface, discover_profiles
from tests.conformance._controls import control_surface_of_item, marks_a_positive_control
from tests.conformance._observable import observable_of
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label

_T = TypeVar("_T")
"""What a stash slot holds: the key's type and its factory's return, tied together."""

FORMAT = 1
"""The record's document version, spelled the way the matrix artifact spells
its own: required, no default, so an unversioned record fails loud in the
collator rather than being read as an empty modern one."""

OBSERVATION = "observation"
DOMAIN_EXCLUSION = "domain-exclusion"
CONTROL = "control"

OBSERVATIONS_ENV_VAR = "OTTO_CONFORMANCE_OBSERVATIONS"
"""Redirects :func:`observations_dir`. Declared in
:data:`tests._ambient_env.AMBIENT_OPT_INS`, without which
``tests/conftest.py`` would strip it and every run would silently write to the
default -- issue #192's shape exactly."""

DEFAULT_OBSERVATIONS_DIR = PROJECT_ROOT / "reports" / "conformance-observations"

RECORD_SUMMARY_LIMIT = 2400
"""Cap on ONE record's failure summary. The nodeid a record also carries is how
a reader reproduces the failure in full, so this exists to bound a pathological
``longrepr`` line rather than to shorten a real reason.

★ IT MUST STAY LARGER THAN ``scripts/collate_support_matrix.py``'s
``FAILURE_SUMMARY_LIMIT``, and that ordering is a correctness property rather
than a preference. The collate step joins ``"<cell label>: <this summary>"``
segments and caps the JOIN, and the rendered page marks a cell whose summary
reached THAT cap -- a length comparison against that one constant. So a cut
made HERE, below the join's cap, would reach the page unannounced: the reader
would meet a reason trailing off mid-word with nothing saying it had been cut.
Because a single segment carries its label and separator on top of this text,
any record clipped here necessarily overflows the smaller join cap, and the
page announces it. ``tests/unit/test_support_matrix.py`` pins the ordering.

MEASURED 2026-08-25, which is why 500 was wrong: the longest reason this tree
declares is the transfer module's ``nc``-on-BusyBox ``expected_failure``, and a
record of it is 749 characters. At 500 every one of the ten ``measured-broken``
cells stopped mid-sentence, discarding the half a reader most needs -- that
``shell`` transfer passes on those same five guests and ``nc`` passes on every
GNU cell, i.e. exactly how narrow the gap is.
"""


def observations_dir() -> Path:
    """Where this run writes its observation records.

    See the module docstring for why this is chosen rather than read off an
    xdir that does not exist.
    """
    raw = ambient(OBSERVATIONS_ENV_VAR, "").strip()
    return Path(raw) if raw else DEFAULT_OBSERVATIONS_DIR


def surface_for(contract: str) -> "Surface | None":
    """The matrix ROW *contract* is, or None if the table does not name it.

    None is unreachable in a committed tree -- ``tests/unit/test_support_matrix.py``
    asserts :data:`~tests._fixtures.support_matrix.SURFACES` equals the
    contracts the tree declares, in both directions -- and is answered rather
    than raised so that a tree caught mid-edit reports a test's real outcome
    instead of an INTERNALERROR from a reporting hook. A record carrying
    ``surface: null`` is one the collator must refuse, loudly.
    """
    return next((surface for surface in SURFACES if surface.contract == contract), None)


@lru_cache(maxsize=1)
def _profile_by_element() -> "Mapping[str, str]":
    """element -> profile id, over every column the artifact's axis declares.

    Memoised per process because :func:`~tests._fixtures.support_matrix.discover_profiles`
    builds hosts through otto's factory to read their axes, and this is asked
    once per emitted record. Read-only, so the memo cannot be edited by a
    caller into disagreeing with the axis it came from.
    """
    return MappingProxyType(
        {member: profile.id for profile in discover_profiles() for member in profile.elements}
    )


def profile_for(element: str) -> "str | None":
    """The matrix COLUMN holding *element*, or None if no column does.

    Asks the artifact's own axis (``discover_profiles``) rather than
    re-deriving a userland, so this cannot disagree with the committed
    columns about which one an element belongs to.

    **None IS THE COMMON ANSWER TODAY, AND IT IS A FINDING RATHER THAN A
    BUG HERE.** MEASURED 2026-08-24: the profile axis is built from the BED
    labs' 16 elements (``bb1161``, ``test1``, ``zephyr37_fat``, ...), while
    the HERMETIC venue's cells carry ``local``, ``loopback`` and
    ``busybox-1.16.1``-style element names that are in no lab at all --
    ``axes_for('busybox-1.16.1')`` raises ``KeyError: 'busybox-1.16.1' is not
    in the tech1 lab data``. So every hermetic observation names an element
    that no profile holds. Guessing a column from the element's SPELLING is
    exactly the sniff ``tests/conformance/_bed.py``'s ``_kind_for_userland``
    docstring rejects, and guessing wrong would publish a bed verdict earned
    on a runner. The record therefore says ``null`` and the collation step
    decides -- with a ruling -- what a hermetic observation may claim.
    """
    return _profile_by_element().get(element)


PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
XFAILED = "xfailed"
XPASSED = "xpassed"
NOT_RUN = "not-run"

_EVIDENTIAL = frozenset({PASSED, FAILED, XFAILED})
"""The outcomes that say something about the CONTRACT.

Everything else says something about the run: a setup error, a skip, or an
item whose call phase never happened. The collator must not read those as
evidence, and this set is where that list is written down once.
"""


def is_evidential(outcome: str) -> bool:
    """Whether *outcome* is a statement about the contract rather than the run."""
    return outcome in _EVIDENTIAL


def outcome_of(reports: "dict[str, pytest.TestReport]") -> str:
    """One word for what happened to an item, from its per-phase reports.

    Ordered so that the run-level outcomes cannot be mistaken for contract
    ones. A failure in setup or teardown is :data:`ERROR`, not
    :data:`FAILED` -- the contract never got to speak. A skip is
    :data:`SKIPPED` and never a pass, whatever phase raised it. A strict
    xfail that fired is :data:`XFAILED`, which is an ASSERTION about a known
    product defect and so is real evidence, distinct from a skip.
    """
    for phase in ("setup", "call", "teardown"):
        report = reports.get(phase)
        if report is not None and report.failed:
            # A strict xfail that PASSED arrives here: pytest turns an XPASS
            # under strict into a call-phase failure. Named as what it is, so
            # a collator never files it as a pass.
            if phase == "call" and hasattr(report, "wasxfail"):
                return XPASSED
            return FAILED if phase == "call" else ERROR
    setup = reports.get("setup")
    if setup is not None and setup.skipped:
        return SKIPPED
    call = reports.get("call")
    if call is None:
        return NOT_RUN
    if call.skipped:
        return XFAILED if hasattr(call, "wasxfail") else SKIPPED
    return PASSED


def failure_summary(reports: "dict[str, pytest.TestReport]") -> "str | None":
    """One line saying how the failing phase failed, or None if none did.

    A STRICT XFAIL IS HANDLED FIRST and separately: it is real evidence about
    the contract (an assertion about a known product defect) but it produces no
    longrepr, so the loop below would answer None and a ``measured-broken``
    cell would carry no summary at all.

    Prefers the LAST ``E``-marked line of the longrepr -- pytest marks the
    raised exception's own lines with ``E``, and the last of them carries the
    message. Falls back to the last line of all, which is pytest's
    ``file:line: ExceptionType`` locator: less informative, but never empty
    and never invented. MEASURED against a deliberately failed cell: without
    the preference the summary was the locator alone, which names the file
    that raised and not what it said.
    """
    call = reports.get("call")
    if call is not None and getattr(call, "wasxfail", None) is not None and call.skipped:
        # A STRICT XFAIL THAT FIRED, and it has no failure text of its own --
        # pytest records the DECLARATION instead. MEASURED on the first real
        # bed collation: without this the matrix published
        # `measured-broken ... "bed-busybox[bb1161:telnet:nc]: xfailed"`, which
        # names the cell and not the defect, while the reason otto's `nc`
        # listener never binds was sitting in `wasxfail` unread. A
        # `measured-broken` cell whose summary is the word "xfailed" tells a
        # reader nothing they could not see from the status.
        return f"call: expected failure -- {call.wasxfail}"[:RECORD_SUMMARY_LIMIT]
    for phase in ("setup", "call", "teardown"):
        report = reports.get(phase)
        if report is None or not report.failed:
            continue
        lines = [line.strip() for line in report.longreprtext.splitlines() if line.strip()]
        if not lines:
            return f"{phase}: failed with no representation"
        marked = [line for line in lines if line.startswith("E ")]
        return f"{phase}: {(marked or lines)[-1]}"[:RECORD_SUMMARY_LIMIT]
    return None


def today() -> str:
    """The measurement date, ISO, matching the artifact's ``as_of`` pattern.

    UTC, following every other stamp otto writes (``create_output_dir``,
    ``otto.coverage.validity``). A local date would make the same run stamp
    two different days depending on the runner's zone, and the matrix is read
    across machines.
    """
    return datetime.now(tz=timezone.utc).date().isoformat()


@dataclass(frozen=True)
class CellFacts:
    """The axis values one record names, all read off the cell rather than parsed."""

    element: str
    profile: "str | None"
    term: str
    transfer: str
    cell_kind: str
    label: str


def cell_facts(resolved: ResolvedCell) -> CellFacts:
    """Everything a record says about WHICH cell it is about.

    ``element`` and not merely ``profile``: the matrix's ``observed_on`` and
    ``not_observable`` are per-ELEMENT because two profiles are internally
    split, and a record naming only the column could never populate them --
    the rendered page could then not say "measured on 2 of 4" honestly.
    """
    cell = resolved.cell
    return CellFacts(
        element=cell.element,
        profile=profile_for(cell.element),
        term=cell.term,
        transfer=cell.transfer,
        cell_kind=resolved.kind,
        label=cell_label(resolved),
    )


def _common(kind: str, resolved: ResolvedCell, venue: str, contract: str) -> dict:
    surface = surface_for(contract)
    facts = cell_facts(resolved)
    return {
        "format": FORMAT,
        "kind": kind,
        "surface": surface.id if surface is not None else None,
        "contract": contract,
        "venue": venue,
        "as_of": today(),
        "element": facts.element,
        "profile": facts.profile,
        "term": facts.term,
        "transfer": facts.transfer,
        "cell_kind": facts.cell_kind,
        "cell_label": facts.label,
    }


def observation_record(
    *,
    resolved: ResolvedCell,
    venue: str,
    contract: str,
    nodeid: str,
    outcome: str,
    summary: "str | None" = None,
    observable: "str | None" = None,
) -> dict:
    """One contract's result against one drawn cell.

    ``contract`` is the unparametrized nodeid (the matrix row's own id) and
    ``nodeid`` the parametrized one that names this very cell. BOTH, because
    they are not interchangeable and only one of them is usable today:
    ``schemas/support-matrix.schema.json``'s ``NodeId`` pattern ends
    ``(\\[[^\\]]*\\])?$``, which MEASURED cannot match a real conformance
    nodeid -- ``cell_label`` puts brackets INSIDE the parametrization
    (``...[local[local:local:local]]``), so the inner ``]`` defeats
    ``[^\\]]*``. The unparametrized form validates; the parametrized one is
    what a reader reproduces a cell with.
    """
    record = _common(OBSERVATION, resolved, venue, contract) | {
        "nodeid": nodeid,
        "outcome": outcome,
        "evidential": is_evidential(outcome),
        "observable": observable,
    }
    if summary is not None:
        record["failure_summary"] = summary
    return record


def control_record(
    *,
    resolved: ResolvedCell,
    venue: str,
    surface: str,
    control: str,
    nodeid: str,
    outcome: str,
    summary: "str | None" = None,
) -> dict:
    """A POSITIVE CONTROL's result against one drawn cell.

    A THIRD KIND, and it is emphatically not an observation. Task 3 of this
    item established why a control must leave no OBSERVATION record: a
    control's outcome is a statement about the INSTRUMENT, never about the
    host, and folding it in beside a contract's own result would let a cell's
    verdict rest on a test that asserted nothing about the far side. That
    holds, and this record does not touch it -- nothing here ever enters a
    cell's ``observed_on``.

    WHAT IT CLOSES is the other half, and without it the artifact's central
    guarantee is only a shape check. ``schemas/support_matrix.json`` asks a
    ``measured-ok`` cell to name the control that proved its observable can go
    red; a collator that merely CONSTRUCTS that nodeid publishes "a test with
    this name exists and is collected on this cell", which is wiring, not
    evidence. A control that ran on the cell and FAILED means the instrument
    could not tell a wrong answer from a right one -- and the contract's pass
    beside it then proves nothing, which is precisely the defect this item
    exists to expose. So the collator requires a control record that PASSED,
    on the very cell the observation came from, and this is where that fact
    comes from.

    ``surface`` is read off the control's own marker
    (``tests/conformance/_controls.py``), never inferred from the module it
    lives in, so a control moved between modules keeps vouching for the same
    row and one pointed at a row that no longer exists fails the tree's
    cross-check instead of quietly vouching for its neighbour.
    """
    record = _common(CONTROL, resolved, venue, control) | {
        "surface": surface,
        "nodeid": nodeid,
        "outcome": outcome,
        "evidential": is_evidential(outcome),
    }
    if summary is not None:
        record["failure_summary"] = summary
    return record


def exclusion_record(*, resolved: ResolvedCell, venue: str, contract: str) -> dict:
    """A drawn cell a contract declared outside its applicable domain.

    ``probed`` / ``probe_result`` are named for the fields
    ``schemas/support-matrix.schema.json``'s ``ElementProbe`` requires, and
    hold only what was actually evaluated. The REASON lives in the
    predicate's own docstring and is deliberately not copied here: the two
    live narrowings mean different things, and a summary written by this
    module beside a machine-written verdict is the fabrication path §5's
    guards exist to close.
    """
    module = contract.split("::", 1)[0]
    return _common(DOMAIN_EXCLUSION, resolved, venue, contract) | {
        "probed": f"{module}::applicable_cell({cell_label(resolved)})",
        "probe_result": (
            "False -- the contract declares this drawn cell outside its applicable domain"
        ),
    }


def record_filename(record: dict) -> str:
    """A name that is stable per (surface, cell, venue) and unique across workers.

    Stable so that re-running a cell REPLACES its record rather than growing
    a pile whose newest member has to be guessed at; unique so that the
    suite's default ``-n auto`` cannot have two workers write one file. The
    digest covers the identity fields rather than the whole record, because
    ``as_of`` and the outcome are exactly the parts that should overwrite.
    """
    identity = "\x1f".join(
        str(record.get(field))
        for field in ("kind", "contract", "venue", "cell_label", "element", "term", "transfer")
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    surface = record.get("surface") or "unmapped"
    return f"{record['kind']}-{surface}-{digest}.json"


def write_record(directory: Path, record: dict) -> Path:
    """Write *record* into *directory*, creating it, and answer the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / record_filename(record)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def read_records(directory: Path) -> "list[dict]":
    """Every record in *directory*, sorted by filename.

    The read the collate step will make. Here rather than there so that the
    write and the read of this format stay in one file and cannot drift into
    two spellings of it.
    """
    if not directory.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


# ── pytest wiring ────────────────────────────────────────────────────────────
#
# The hooks themselves are one-liners in ``tests/conformance/conftest.py``, the
# way ``tests/conftest.py`` wires the retry wrapper: a hook body is the one
# place a unit test cannot reach, so everything a unit test must be able to
# assert about lives here as a plain function.

_PHASE_REPORTS = pytest.StashKey["dict[str, pytest.TestReport]"]()
"""Per ITEM: its setup/call/teardown reports, accumulated until all three exist."""

_EXCLUSIONS = pytest.StashKey["dict[str, list[ResolvedCell]]"]()
"""Per CONFIG: contract nodeid -> the drawn cells its domain excluded.

Captured at parametrization because that is the only moment the information
exists: an excluded cell generates NO item, so no report hook can ever see it.
"""

_OBSERVED_CONTRACTS = pytest.StashKey["set[str]"]()
"""Per CONFIG: the contracts this PROCESS actually produced an observation for.

What gates the exclusion records. An exclusion says "this run exercised
contract C, and cell X was outside its domain"; a process that ran none of C's
items has not earned that sentence -- and ``make coverage``'s path-less legs
collect this tree and then deselect every item of it, so the ungated version
would have every unit lane in the repo writing conformance evidence.
"""


def contract_of(nodeid: str) -> str:
    """*nodeid* without its parametrization -- the matrix row's own id.

    Splits on the FIRST ``[``: a cell label contains brackets of its own
    (``...[bed-busybox[bb1161:telnet:nc]]``), so anything anchored at the last
    one would keep half the label.
    """
    return nodeid.split("[", 1)[0]


def _slot(config: "pytest.Config", key: "pytest.StashKey[_T]", empty: "Callable[[], _T]") -> "_T":
    """Answer *config*'s stash entry for *key*, creating it from *empty* the first time.

    ANNOTATED RATHER THAN SUPPRESSED. This carried a bare
    ``# type: ignore[no-untyped-def]`` from the task that wrote it, which is a
    suppression with no justification beside it -- and it survived because that
    task's own audit ran a bare ``git diff`` AFTER committing, so it was
    grepping an empty diff. The generic is what the ignore was standing in for:
    the key's parameter and the factory's return are the same type, which is
    exactly what makes the two call sites below safe.
    """
    value = config.stash.get(key, None)
    if value is None:
        value = empty()
        config.stash[key] = value
    return value


def note_domain_exclusions(
    config: pytest.Config, contract: str, excluded: "list[ResolvedCell]"
) -> None:
    """Remember which drawn cells *contract* declared outside its domain."""
    if excluded:
        _slot(config, _EXCLUSIONS, dict)[contract] = list(excluded)


def domain_exclusions(config: pytest.Config) -> "dict[str, list[ResolvedCell]]":
    """What :func:`note_domain_exclusions` recorded on *config*, contract by contract."""
    return config.stash.get(_EXCLUSIONS, None) or {}


def record_phase(
    item: pytest.Item,
    report: "pytest.TestReport",
    resolved: "ResolvedCell | None",
    venue: str,
) -> "Path | None":
    """Accumulate *report*, and on TEARDOWN write this item's observation.

    Teardown and not call, so that :func:`outcome_of` sees every phase: an
    item that errored in setup never reached its contract, and one that
    passed its call and then failed teardown did not leave the cell as it
    found it. Either read from the call phase alone would report a pass.

    A POSITIVE CONTROL LEAVES NO **OBSERVATION** RECORD, and that exclusion is
    not tidiness. A control takes ``resolved_cell`` exactly as a contract does
    -- it has to run on the cell it vouches for -- so without this it would
    emit an observation whose ``contract`` names no matrix row and whose
    ``surface`` is therefore ``null``, the very shape :func:`surface_for`
    documents as "unreachable in a committed tree" and the collator must
    refuse. Worse, it would be evidence of the wrong KIND: a control's outcome
    is a statement about the INSTRUMENT, and folding it in beside the
    contract's own result would let a cell's verdict rest on a test that never
    asserted anything about the host.

    It leaves a ``control`` RECORD instead, which is a different claim filed
    under a different kind: *the instrument for surface S proved itself on cell
    X, or did not*. Nothing downstream may read it as an observation -- the
    collator never puts a control's element in ``observed_on`` -- and what it
    buys is the one question a constructed nodeid cannot answer. See
    :func:`control_record` and ``tests/conformance/_controls.py``.

    Answers the path written, or None when there is nothing to record: an item
    with no cell (this tree's bed-opener witness), a control whose marker names
    no readable surface, or a non-teardown phase.
    """
    reports = item.stash.get(_PHASE_REPORTS, None)
    if reports is None:
        reports = {}
        item.stash[_PHASE_REPORTS] = reports
    reports[report.when] = report
    if report.when != "teardown" or resolved is None:
        return None
    if marks_a_positive_control(item):
        surface = control_surface_of_item(item)
        if surface is None:
            return None
        return write_record(
            observations_dir(),
            control_record(
                resolved=resolved,
                venue=venue,
                surface=surface,
                control=contract_of(item.nodeid),
                nodeid=item.nodeid,
                outcome=outcome_of(reports),
                summary=failure_summary(reports),
            ),
        )
    contract = contract_of(item.nodeid)
    record = observation_record(
        resolved=resolved,
        venue=venue,
        contract=contract,
        nodeid=item.nodeid,
        outcome=outcome_of(reports),
        summary=failure_summary(reports),
        observable=observable_of(item, resolved),
    )
    written = write_record(observations_dir(), record)
    _slot(item.config, _OBSERVED_CONTRACTS, set).add(contract)
    return written


def write_domain_exclusions(config: pytest.Config, venue: str) -> "list[Path]":
    """Write one record per drawn cell excluded by a contract this process RAN."""
    observed = config.stash.get(_OBSERVED_CONTRACTS, None) or set()
    exclusions = domain_exclusions(config)
    directory = observations_dir()
    return [
        write_record(directory, exclusion_record(resolved=resolved, venue=venue, contract=contract))
        for contract in sorted(observed)
        for resolved in exclusions.get(contract, [])
    ]
