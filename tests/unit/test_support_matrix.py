"""Guards on the committed ``{surface} x {profile}`` support matrix (spec §5).

TWO JOBS. The first is that the committed artifact agrees with the tree: every
conformance contract has a row, every ``userland`` the bed labs resolve to has
a column, and the grid names nothing the tree does not declare.

The second is the one the item exists for, and it is asserted by INJECTION
rather than by inspection: a ``measured-ok`` cell that omits its positive
control, a ``not-observable`` cell that omits its probe, a ``measured-ok``
that hides the elements it could not measure -- each is written into a copy of
the real artifact and the SCHEMA is required to reject it. The rejection has
to come from the schema and not from Python, because §5 wants a hand-edit of
the JSON to fail, and a hand-editor does not run this file.

**EVERY REJECTION TEST NAMES THE CONSTRAINT IT MEANS.** A bare "some error was
raised" would pass on an unrelated defect in the injected fixture -- the same
reason ``.ruff.toml``'s ``raises-extend-require-match-for`` forces a ``match=``
on every ``*ValidationError``. And the rejections are backed by
:func:`test_a_fully_evidenced_measured_ok_cell_is_accepted` and its
``not-observable`` twin: a schema that rejected EVERYTHING would satisfy every
negative test in this file and nothing else here would notice.
"""

import contextlib
import copy
import dataclasses
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from otto.result import CommandResult, Result, Results
from otto.utils import Status
from scripts.check_matrix_downgrades import main as gate_main
from scripts.collate_support_matrix import BED, FAILURE_SUMMARY_LIMIT, collate, report
from scripts.collate_support_matrix import main as collate_main
from scripts.render_support_matrix import (
    _UNINTERPRETED,
    _UNWATCHED_STATES,
    FAMILY_BLURB,
    PAGE_PATH,
    VOICE,
    Promise,
    _domain_rule,
    axes_mismatch,
    code_spans,
    narrowing_mismatch,
    promise_mismatch,
    promise_of,
    render,
)
from scripts.render_support_matrix import main as render_main
from tests._ambient_env import ambient_opt_ins
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.profiles import Cell
from tests._fixtures.support_matrix import (
    CELL_FIXTURE,
    CONFORMANCE_ROOT,
    MATRIX_PATH,
    SCHEMA_PATH,
    SURFACES,
    acceptable_controls,
    build_matrix,
    cell_coverage,
    cell_outcome_errors,
    collect_conformance_nodeids,
    discover_contracts,
    discover_profiles,
    element_accounting_errors,
    positive_control_errors,
)
from tests.conformance import conftest as conformance_conftest
from tests.conformance import test_exec_contract as _exec_contract
from tests.conformance import test_timeout_contract as _timeout_contract
from tests.conformance import test_transfer_contract as _transfer_contract
from tests.conformance._controls import (
    CONTROL_MARK,
    discover_controls,
    positive_control_for,
)
from tests.conformance._observable import (
    OBSERVABLE_MARK,
    discover_observables,
    observable_template_for,
    render_observable,
)
from tests.conformance._observation import (
    CONTROL,
    DOMAIN_EXCLUSION,
    ERROR,
    FAILED,
    NOT_RUN,
    OBSERVATIONS_ENV_VAR,
    PASSED,
    RECORD_SUMMARY_LIMIT,
    SKIPPED,
    XFAILED,
    XPASSED,
    failure_summary,
    is_evidential,
    observation_record,
    observations_dir,
    outcome_of,
    profile_for,
    read_records,
    record_filename,
)
from tests.conformance._resolved import ResolvedCell
from tests.conformance._vocabulary import POSIX, vocabulary_for_userland
from tests.conformance.test_transfer_contract import _MODE as _TRANSFER_MODE
from tests.conformance.test_transfer_contract import _PAYLOAD


def _fabricated_cell(element: str, term: str, transfer: str, *, kind: str) -> ResolvedCell:
    """A cell that names an element without standing anything up.

    Every guard below is about what a record SAYS, so a real host would add
    minutes of machinery to prove nothing extra. The opener is deliberately
    inert: any test that called it would be testing the venue, not the record.
    """
    return ResolvedCell(
        cell=Cell(element, term, transfer),
        kind=kind,
        open_host=lambda: None,
        remote_scratch=None,
        vocabulary=POSIX,
    )


COLLATOR_PATH = PROJECT_ROOT / "scripts" / "collate_support_matrix.py"
DOWNGRADE_GATE_PATH = PROJECT_ROOT / "scripts" / "check_matrix_downgrades.py"
RENDERER_PATH = PROJECT_ROOT / "scripts" / "render_support_matrix.py"
"""The collate step: the ONLY writer of a ``measured-*`` verdict (spec §5)."""

GAP_REGISTRY_PATH = PROJECT_ROOT / "src" / "otto" / "host" / "userland.py"
"""The ONE other file spelling ``measured-broken``, for a DIFFERENT artifact.

otto's gap registry has its own two-state vocabulary rendered into
``docs/architecture/subsystems/busybox-support.md``. Allow-listed by name
rather than excluded by scope, so the collision stays visible -- see
``test_the_gap_registry_uses_the_same_two_status_words_for_a_different_artifact``.
"""

_VERDICT_LITERALS = ('"measured-ok"', "'measured-ok'", '"measured-broken"', "'measured-broken'")
"""How a verdict is MINTED in python. Shape-only, and that is the point: this
is a guard on the CODE, and it cannot see a hand-edit of the JSON."""

_REGENERATE = (
    "regenerate with: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c "
    "'from tests._fixtures.support_matrix import rewrite_matrix_axes; print(rewrite_matrix_axes())'"
)

#: WHERE each fixture below is injected. Not an arbitrary cell: these are the
#: two real non-uniform cells this item was designed around, so the fixtures
#: are the measured splits rather than plausible-looking stand-ins, and the
#: per-element guards below have real element lists to check against.
TRANSFER_X_ZEPHYR37 = ("transfer-roundtrip", "zephyr-3.7")
TIMEOUT_X_ZEPHYR27 = ("timeout", "zephyr-2.7")

#: The third real non-uniform cell, and the one the PER-TRANSPORT breakdown
#: exists for. `bb1161` has exactly two bed cells and they disagree: the
#: roundtrip passes over `shell` and xfails over `nc`, against otto's
#: registered `nc-transfer` gap. The profile has one element, so `observed_on`
#: and `not_observable` say nothing at all about the split -- only
#: `observed_cells` can.
TRANSFER_X_BUSYBOX1161 = ("transfer-roundtrip", "busybox-1.16.1")

#: A cell carrying every field ``measured-ok`` demands, holding the split
#: MEASURED 2026-08-24 from ``test_transfer_contract.applicable_cell`` over the
#: bed space: observable on ``zephyr37_fat``/``zephyr37_lfs``, not on
#: ``zephyr37_nofs``/``zephyr37_llext``. The negative tests below each take
#: this apart in exactly one place, so what they prove is that the removed
#: field is what the schema refused -- not that the shape was unacceptable all
#: along.
COMPLETE_MEASURED_OK = {
    "status": "measured-ok",
    "nodeid": (
        "tests/conformance/test_transfer_contract.py::test_put_get_roundtrip_preserves_content"
    ),
    "venue": "bed",
    "as_of": "2026-08-24",
    "observable": "the bytes read back by get() after put() of a known payload",
    # A REAL control nodeid, parametrized on a cell of an element this fixture
    # says it observed. Every one of those three properties is checked below
    # -- right surface, right element, really collected -- so a plausible
    # stand-in here would make the positive-control guards prove nothing.
    "positive_control": (
        "tests/conformance/test_transfer_contract.py"
        "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
        "[bed-zephyr[zephyr37_fat:telnet:console]]"
    ),
    "observed_on": ["zephyr37_fat", "zephyr37_lfs"],
    # REAL bed cell labels, and the guards below resolve them against the bed
    # space rather than accepting the spelling: an invented label is the same
    # class of defect as the invented positive control this item's own Task 1
    # fixture once carried. Each Zephyr element draws exactly one cell.
    # EVERY PASSING ROUTE CARRIES ITS OWN CONTROL, on its own cell -- the
    # schema requires it there, and the guards below resolve each one against
    # `positive_control_for` joined to that entry's own label. A citation
    # copied from the cell-level field above would name the `fat` control on
    # the `lfs` route, which is the per-element weakening the split retires.
    "observed_cells": [
        {
            "cell_label": "bed-zephyr[zephyr37_fat:telnet:console]",
            "element": "zephyr37_fat",
            "term": "telnet",
            "transfer": "console",
            "outcome": "passed",
            "positive_control": (
                "tests/conformance/test_transfer_contract.py"
                "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
                "[bed-zephyr[zephyr37_fat:telnet:console]]"
            ),
        },
        {
            "cell_label": "bed-zephyr[zephyr37_lfs:telnet:console]",
            "element": "zephyr37_lfs",
            "term": "telnet",
            "transfer": "console",
            "outcome": "passed",
            "positive_control": (
                "tests/conformance/test_transfer_contract.py"
                "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
                "[bed-zephyr[zephyr37_lfs:telnet:console]]"
            ),
        },
    ],
    "not_observable": [
        {
            "element": "zephyr37_nofs",
            "probed": "ResolvedCell.remote_scratch",
            "probe_result": "None -- EmbeddedFileSystem reports supports_transfer False",
        },
        {
            "element": "zephyr37_llext",
            "probed": "ResolvedCell.remote_scratch",
            "probe_result": "None -- EmbeddedFileSystem reports supports_transfer False",
        },
    ],
}

#: The ``not-observable`` twin: a whole profile outside a contract's domain.
COMPLETE_NOT_OBSERVABLE = {
    "status": "not-observable",
    "venue": "bed",
    "as_of": "2026-08-24",
    "probed": "test_timeout_contract.applicable_cell over this profile's cells",
    "probe_result": "no cell is in the domain -- no zephyr command can be made to outlive a budget",
    "observed_on": [],
    "observed_cells": [],
    "not_observable": [
        {
            "element": "zephyr27_fat",
            "probed": "test_timeout_contract.applicable_cell",
            "probe_result": "False",
        }
    ],
}


#: The ``measured-broken`` twin, and the case the per-transport breakdown was
#: added for. MEASURED on the bed 2026-08-25: ``bb1161`` moves files fine over
#: ``shell`` and xfails over ``nc``. The aggregate is honestly ``broken`` --
#: an element is ok only if every cell of it passed -- and this fixture is the
#: proof that "broken" is not the same as "cannot transfer at all", which is
#: what a reader takes from the scalar alone.
COMPLETE_MEASURED_BROKEN = {
    "status": "measured-broken",
    "nodeid": (
        "tests/conformance/test_transfer_contract.py::test_put_get_roundtrip_preserves_content"
    ),
    "venue": "bed",
    "as_of": "2026-08-25",
    "observable": "the bytes get() reads back after put() of a known payload",
    "failure_summary": "bed-busybox[bb1161:telnet:nc]: call: expected failure -- otto's gap",
    "observed_on": ["bb1161"],
    # ★ THE ASYMMETRY THAT MAKES THIS FIXTURE THE ONE THAT MATTERS. The `shell`
    # route PASSED, so the page says "you can do this over `shell`" and the
    # entry must name the control that proved the check could go red there.
    # The `nc` route xfailed and names nothing: it claims nothing positive, and
    # a strict xfail xfails the control beside the contract, so there is no
    # passing record to cite. MEASURED on the bed 2026-08-25, and the ten real
    # mixed cells cited NOTHING AT ALL until this field existed.
    "observed_cells": [
        {
            "cell_label": "bed-busybox[bb1161:telnet:nc]",
            "element": "bb1161",
            "term": "telnet",
            "transfer": "nc",
            "outcome": "xfailed",
        },
        {
            "cell_label": "bed-busybox[bb1161:telnet:shell]",
            "element": "bb1161",
            "term": "telnet",
            "transfer": "shell",
            "outcome": "passed",
            "positive_control": (
                "tests/conformance/test_transfer_contract.py"
                "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
                "[bed-busybox[bb1161:telnet:shell]]"
            ),
        },
    ],
    "not_observable": [],
}


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    """The schema, checked for well-formedness before it is trusted to judge."""
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def committed() -> dict:
    """The committed artifact, as it stands on disk."""
    return json.loads(MATRIX_PATH.read_text())


def _with_cell(committed: dict, cell: dict, at: "tuple[str, str]") -> "tuple[dict, tuple]":
    """*committed* with the cell at *at* replaced by *cell* -- a real hand-edit.

    Injected into the genuine artifact rather than into a minimal stub, so the
    rejection is proved on the document the guard actually reads.

    Returns the JSON POINTER of the edit alongside it, and every assertion
    below is scoped to that pointer. MEASURED, and it is why the scoping was
    added: with an unscoped check, writing an unrelated bad cell into the real
    artifact turned :func:`test_a_fully_evidenced_measured_ok_cell_is_accepted`
    RED -- a positive control that can fail for a reason it is not about
    invites someone to "fix" it by weakening the schema. The artifact's own
    validity is :func:`test_the_committed_matrix_validates_against_its_schema`'s
    job, and only its.
    """
    edited = copy.deepcopy(committed)
    surface, profile = at
    assert profile in edited["cells"][surface], f"no such cell: {at}"
    edited["cells"][surface][profile] = cell
    return edited, ("cells", surface, profile)


def _messages(validator: Draft202012Validator, document: dict, under: "tuple" = ()) -> "list[str]":
    """Validation messages, optionally only those raised at or below *under*."""
    return [
        error.message
        for error in validator.iter_errors(document)
        if tuple(error.absolute_path)[: len(under)] == under
    ]


def _accepts(validator: Draft202012Validator, injected: "tuple[dict, tuple]") -> None:
    """Assert the injected cell raises nothing. The control for every ``_rejects``."""
    document, under = injected
    assert _messages(validator, document, under) == []


def _rejects(validator: Draft202012Validator, injected: "tuple[dict, tuple]", needle: str) -> None:
    """Assert the injected cell is rejected, and rejected FOR THE STATED REASON."""
    document, under = injected
    messages = _messages(validator, document, under)
    assert messages, "the schema accepted a cell it must reject"
    assert any(needle in message for message in messages), (
        f"rejected, but not for {needle!r}: {messages}"
    )


def _at(errors: "list[str]", at: "tuple[str, str]") -> "list[str]":
    """Only the accounting errors raised AGAINST the injected cell.

    THE SAME SCOPING `_rejects` USES, and for the reason task 1 measured: an
    unscoped assertion turns the positive controls red for an unrelated defect
    elsewhere in the artifact, and a control that can fail for a reason it is
    not about invites someone to "fix" it by weakening the check. The artifact's
    own cleanliness is a separate guard's job, and only its.
    """
    surface, profile = at
    return [error for error in errors if error.startswith(f"cells.{surface}.{profile}:")]


# --------------------------------------------------------------------------
# The artifact agrees with the tree
# --------------------------------------------------------------------------


def test_the_committed_matrix_validates_against_its_schema(validator, committed):
    assert _messages(validator, committed) == []


def test_every_contract_the_tree_declares_has_a_surface_row():
    """Both directions: a contract added, renamed or deleted fails here."""
    declared = set(discover_contracts())
    tabulated = {surface.contract for surface in SURFACES}
    assert declared == tabulated, (
        f"tests/conformance declares {sorted(declared - tabulated)} with no matrix row, "
        f"and the matrix names {sorted(tabulated - declared)} that no longer exists"
    )


def test_no_conformance_contract_declares_its_cell_via_usefixtures():
    """The blind spot in :func:`discover_contracts`, refused rather than risked.

    Discovery reads function SIGNATURES for ``resolved_cell``. A contract that
    requested it through ``@pytest.mark.usefixtures`` would be invisible, and
    an invisible contract is a missing matrix row that looks like a complete
    matrix. Measured 2026-08-24: no module under ``tests/conformance`` contains
    the string at all, so this refuses a spelling nothing uses rather than
    breaking one that does.
    """
    offenders = [
        path.name
        for path in sorted(CONFORMANCE_ROOT.glob("*.py"))
        if "usefixtures" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} use `usefixtures`; tests/_fixtures/support_matrix.py discovers "
        f"contracts by their `{CELL_FIXTURE}` PARAMETER and would not see one declared "
        f"that way. Take the fixture as an argument, or teach discovery to read marks."
    )


def test_every_profile_the_tree_resolves_has_a_column(committed):
    declared = {profile.id for profile in discover_profiles()}
    published = {profile["id"] for profile in committed["profiles"]}
    assert declared == published, f"profile axis drifted from the tree; {_REGENERATE}"


def test_each_profile_column_lists_the_elements_it_stands_for(committed):
    """The plural is load-bearing -- a cell aggregates over these."""
    declared = {profile.id: list(profile.elements) for profile in discover_profiles()}
    published = {profile["id"]: profile["elements"] for profile in committed["profiles"]}
    assert declared == published, f"profile membership drifted from the tree; {_REGENERATE}"


def test_the_grid_is_complete_and_names_nothing_undeclared(committed):
    """Every (surface, profile) pair has exactly one cell, and no pair is invented."""
    expected = {
        (surface["id"], profile["id"])
        for surface in committed["surfaces"]
        for profile in committed["profiles"]
    }
    actual = {
        (surface_id, profile_id)
        for surface_id, row in committed["cells"].items()
        for profile_id in row
    }
    assert actual == expected, (
        f"missing cells {sorted(expected - actual)}, "
        f"undeclared cells {sorted(actual - expected)}; {_REGENERATE}"
    )


def test_the_committed_artifact_is_what_the_tree_would_generate(committed):
    """The whole document, axes and grid keys alike, re-derived and compared.

    Verdicts are NOT compared: collation writes those, and re-deriving them
    here would make the guard a copy of the collator.
    """
    rebuilt = build_matrix(existing=committed)
    assert rebuilt == committed, f"the artifact no longer matches the tree; {_REGENERATE}"


# --------------------------------------------------------------------------
# The schema can go green -- the positive controls for every rejection below
# --------------------------------------------------------------------------


def test_a_fully_evidenced_measured_ok_cell_is_accepted(validator, committed):
    """Without this, a schema that rejected everything would pass every test below."""
    _accepts(validator, _with_cell(committed, COMPLETE_MEASURED_OK, TRANSFER_X_ZEPHYR37))


def test_a_fully_evidenced_not_observable_cell_is_accepted(validator, committed):
    _accepts(validator, _with_cell(committed, COMPLETE_NOT_OBSERVABLE, TIMEOUT_X_ZEPHYR27))


def test_a_fully_evidenced_measured_broken_cell_is_accepted(validator, committed):
    """The control for every per-transport rejection below.

    Without it, a schema whose ``observed_cells`` rules rejected any breakdown
    at all would satisfy the four negative tests that follow and nothing here
    would notice -- the same reason the two fixtures above have controls.
    """
    _accepts(validator, _with_cell(committed, COMPLETE_MEASURED_BROKEN, TRANSFER_X_BUSYBOX1161))


# --------------------------------------------------------------------------
# ...and cannot go green on incomplete evidence
# --------------------------------------------------------------------------


def test_measured_ok_without_a_positive_control_is_rejected(validator, committed):
    """THE CENTRAL GUARANTEE (§5). A hand-edit that claims a verdict the

    observable was never shown able to contradict must not validate.
    """
    cell = {k: v for k, v in COMPLETE_MEASURED_OK.items() if k != "positive_control"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'positive_control' is a required property",
    )


def test_measured_ok_without_an_observable_is_rejected(validator, committed):
    cell = {k: v for k, v in COMPLETE_MEASURED_OK.items() if k != "observable"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'observable' is a required property",
    )


def test_measured_ok_without_a_date_is_rejected(validator, committed):
    cell = {k: v for k, v in COMPLETE_MEASURED_OK.items() if k != "as_of"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'as_of' is a required property",
    )


def test_measured_ok_hiding_the_per_element_breakdown_is_rejected(validator, committed):
    """Chris's ruling, 2026-08-24: a cell may not publish ``measured-ok`` while

    silently omitting that some of its elements could not be measured. Absence
    of the key is not the same statement as ``not_observable: []``.
    """
    cell = {k: v for k, v in COMPLETE_MEASURED_OK.items() if k != "not_observable"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'not_observable' is a required property",
    )


def test_measured_ok_naming_no_element_it_was_observed_on_is_rejected(validator, committed):
    """An empty ``observed_on`` is a verdict resting on nothing at all."""
    cell = COMPLETE_MEASURED_OK | {"observed_on": []}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "should be non-empty")


def test_measured_ok_carrying_a_failure_summary_is_rejected(validator, committed):
    """Evidence from another state, which would render as a contradiction."""
    cell = COMPLETE_MEASURED_OK | {"failure_summary": "it broke"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'failure_summary' is not one of",
    )


def test_measured_broken_without_a_failure_summary_is_rejected(validator, committed):
    """Taken apart from the BROKEN fixture, so this proves what it says.

    It used to be built by restatusing ``COMPLETE_MEASURED_OK``, which was
    fine while the two states differed only in this field. It is not fine now:
    such a cell also violates the per-transport rule (its breakdown says
    everything passed), so the test would have been rejected for two reasons
    and would still pass if the ``failure_summary`` requirement were dropped
    -- `_rejects` only needs SOME message to match, and the needle would have
    to compete with a second, unrelated failure.
    """
    cell = {k: v for k, v in COMPLETE_MEASURED_BROKEN.items() if k != "failure_summary"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_BUSYBOX1161),
        "'failure_summary' is a required property",
    )


def test_measured_ok_hiding_the_per_transport_breakdown_is_rejected(validator, committed):
    """The ruling of 2026-08-24, one level below Chris's per-element one.

    The matrix's axis is the profile, but the bed measures an
    (element, term, transfer) CELL. A cell that omits ``observed_cells``
    publishes a scalar over a space that is not uniform, and a renderer would
    have no way back to the split except by parsing the ``observable``'s pipes
    and the ``failure_summary``'s English. A field that CAN be omitted is a
    field a future collator will omit, so the omission fails here.
    """
    cell = {k: v for k, v in COMPLETE_MEASURED_OK.items() if k != "observed_cells"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'observed_cells' is a required property",
    )


def test_measured_broken_hiding_the_per_transport_breakdown_is_rejected(validator, committed):
    """★ THE CASE THE RULING NAMES. ``measured-broken`` must say WHICH cells broke.

    "transfer roundtrip is broken on BusyBox 1.16.1" is the reading a scalar
    invites and it is FALSE -- ``shell`` works and only ``nc`` fails. A broken
    cell that cannot name its failing transports is the misleading row this
    field exists to retire, so it is refused rather than rendered.
    """
    cell = {k: v for k, v in COMPLETE_MEASURED_BROKEN.items() if k != "observed_cells"}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_BUSYBOX1161),
        "'observed_cells' is a required property",
    )


def test_measured_broken_whose_breakdown_says_everything_passed_is_rejected(validator, committed):
    """The status and the breakdown cannot disagree -- direction one.

    A ``measured-broken`` cell whose every entry passed names no failing cell
    at all, which is the omission above wearing the field's own clothes: the
    key is present, and it still cannot tell a reader what broke.
    """
    cell = COMPLETE_MEASURED_BROKEN | {
        "observed_cells": [
            entry | {"outcome": "passed"} for entry in COMPLETE_MEASURED_BROKEN["observed_cells"]
        ]
    }
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_BUSYBOX1161),
        "does not contain items matching the given schema",
    )


def test_measured_ok_whose_breakdown_holds_a_failing_cell_is_rejected(validator, committed):
    """The status and the breakdown cannot disagree -- direction two.

    Written because the first direction alone would be satisfied by a schema
    that simply demanded a failing entry everywhere. ``measured-ok`` beside a
    cell that did not pass is the uniform-reading defect inverted: the
    aggregate would be flattering the one transport that broke.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    entries[0]["outcome"] = "failed"
    cell = COMPLETE_MEASURED_OK | {"observed_cells": entries}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "'passed' was expected")


def test_a_cell_outcome_that_does_not_name_its_transport_is_rejected(validator, committed):
    """The transport is the axis the scalar hid, so an entry without it is inert.

    A renderer could split it back out of ``cell_label``, and that is exactly
    what must not be necessary: the label's spelling belongs to
    ``tests/conformance/_sample.py::cell_label``, and a parser aimed at it
    would keep succeeding against the wrong fields the day it changes.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    del entries[0]["transfer"]
    cell = COMPLETE_MEASURED_OK | {"observed_cells": entries}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'transfer' is a required property",
    )


def test_a_cell_outcome_carrying_a_non_evidential_outcome_is_rejected(validator, committed):
    """A skip is not a measurement, and the artifact may not hold one as evidence.

    The collate step already discards a non-evidential record with a reason.
    Enumerating the three evidential outcomes HERE is the second half: one
    that reached the artifact by another route fails validation instead of
    rendering beside three real results as though it were a fourth.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    entries[0]["outcome"] = "skipped"
    cell = COMPLETE_MEASURED_OK | {"observed_cells": entries}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "is not one of")


def test_a_route_that_passed_may_not_omit_its_positive_control(validator, committed):
    """★ THE CLAIM FOLLOWS THE ROUTE, AND SO MUST THE CONTROL.

    ``observed_cells`` exists so a mixed cell can say *works over ``shell``,
    broken over ``nc``* -- and MEASURED 2026-08-25, the first half of that
    sentence is what all ten real broken cells published while citing no
    control at all. A ``measured-ok`` cell may not exist without naming the
    test that proved its observable can go red; a passing ROUTE makes an
    equally positive claim, so the same requirement is enforced one level down
    rather than left to a renderer to notice.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    del entries[0]["positive_control"]
    cell = COMPLETE_MEASURED_OK | {"observed_cells": entries}
    _rejects(
        validator,
        _with_cell(committed, cell, TRANSFER_X_ZEPHYR37),
        "'positive_control' is a required property",
    )


def test_a_route_that_failed_need_not_name_a_control(validator, committed):
    """★ THE CONTROL FOR THE RULE ABOVE, and the reason it is conditional.

    ``COMPLETE_MEASURED_BROKEN``'s ``nc`` route xfailed and cites nothing. It
    claims nothing positive, and a strict xfail xfails the CONTROL beside the
    contract -- MEASURED on the bed, the ten ``nc`` control records are
    ``xfailed`` too -- so no passing record exists to name. A schema that
    required the field on every entry would make an honest broken cell
    unwritable, and this row is what stops the requirement above being
    tightened into that.
    """
    assert not any(
        entry.get("positive_control")
        for entry in COMPLETE_MEASURED_BROKEN["observed_cells"]
        if entry["outcome"] != "passed"
    ), "this guard is vacuous unless the fixture really has an uncited failing route"
    _accepts(validator, _with_cell(committed, COMPLETE_MEASURED_BROKEN, TRANSFER_X_BUSYBOX1161))


def test_a_route_may_cite_a_control_even_where_it_failed(validator, committed):
    """PERMITTED, never required. A failing route with a PASSING control is a real state.

    It says the instrument worked on that route and the contract failed
    anyway, which is a stronger statement than "everything failed here" -- so
    the schema permits the citation rather than whitelisting it out of the
    failing case. Refusing it would make the field's presence mean "this route
    passed", which is what ``outcome`` already says.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_BROKEN["observed_cells"])
    entries[0]["positive_control"] = (
        "tests/conformance/test_transfer_contract.py"
        "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
        "[bed-busybox[bb1161:telnet:nc]]"
    )
    cell = COMPLETE_MEASURED_BROKEN | {"observed_cells": entries}
    _accepts(validator, _with_cell(committed, cell, TRANSFER_X_BUSYBOX1161))


def test_a_routes_positive_control_that_is_not_a_nodeid_is_rejected(validator, committed):
    """Shape, checked at the route the same way it is checked at the cell.

    Nowhere near sufficient -- a well-formed string naming a test that never
    ran is this item's signature defect, and
    :func:`~tests._fixtures.support_matrix.positive_control_errors` is what
    resolves it. But a field that accepted any string at all would let the
    resolution guard be the only thing standing between a hand-edit and a
    published citation, and this artifact is documentation.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    entries[0]["positive_control"] = "the shell control, obviously"
    cell = COMPLETE_MEASURED_OK | {"observed_cells": entries}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "does not match")


def test_not_observable_claiming_a_measured_cell_is_rejected(validator, committed):
    """``observed_cells: []`` is a STATEMENT, and it has to stay true.

    ``not-observable`` means no cell of this profile produced a result. A
    breakdown holding one contradicts the verdict beside it in the same way a
    non-empty ``observed_on`` does, and the twin rules are required so that
    neither half can drift.
    """
    cell = COMPLETE_NOT_OBSERVABLE | {"observed_cells": COMPLETE_MEASURED_OK["observed_cells"][:1]}
    _rejects(validator, _with_cell(committed, cell, TIMEOUT_X_ZEPHYR27), "is expected to be empty")


def test_not_observable_without_what_was_probed_is_rejected(validator, committed):
    cell = {k: v for k, v in COMPLETE_NOT_OBSERVABLE.items() if k != "probed"}
    _rejects(
        validator,
        _with_cell(committed, cell, TIMEOUT_X_ZEPHYR27),
        "'probed' is a required property",
    )


def test_not_observable_without_the_probe_result_is_rejected(validator, committed):
    cell = {k: v for k, v in COMPLETE_NOT_OBSERVABLE.items() if k != "probe_result"}
    _rejects(
        validator,
        _with_cell(committed, cell, TIMEOUT_X_ZEPHYR27),
        "'probe_result' is a required property",
    )


def test_not_observable_without_the_per_element_probes_is_rejected(validator, committed):
    cell = COMPLETE_NOT_OBSERVABLE | {"not_observable": []}
    _rejects(validator, _with_cell(committed, cell, TIMEOUT_X_ZEPHYR27), "should be non-empty")


def test_not_observable_claiming_an_observation_is_rejected(validator, committed):
    """If any element expressed the observable, the cell is not un-observable."""
    cell = COMPLETE_NOT_OBSERVABLE | {"observed_on": ["zephyr27_fat"]}
    _rejects(validator, _with_cell(committed, cell, TIMEOUT_X_ZEPHYR27), "is expected to be empty")


def test_an_element_probe_without_its_result_is_rejected(validator, committed):
    cell = COMPLETE_NOT_OBSERVABLE | {
        "not_observable": [{"element": "zephyr27_fat", "probed": "applicable_cell"}]
    }
    _rejects(
        validator,
        _with_cell(committed, cell, TIMEOUT_X_ZEPHYR27),
        "'probe_result' is a required property",
    )


def test_untested_carrying_stale_evidence_is_rejected(validator, committed):
    """Downgrading a verdict must take its evidence with it."""
    cell = {"status": "untested", "as_of": "2026-08-24", "nodeid": COMPLETE_MEASURED_OK["nodeid"]}
    _rejects(
        validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "is not one of ['status']"
    )


def test_a_positive_control_that_is_not_a_nodeid_is_rejected(validator, committed):
    """Shape only, but it is the difference between a nodeid and a sentence."""
    cell = COMPLETE_MEASURED_OK | {"positive_control": "we checked it by hand, it was fine"}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "does not match")


def test_an_unknown_status_is_rejected(validator, committed):
    """A fifth state would slip past every conditional above -- none would fire."""
    cell = {"status": "supported"}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "is not one of")


def test_an_unknown_venue_is_rejected(validator, committed):
    cell = COMPLETE_MEASURED_OK | {"venue": "somebody's laptop"}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "is not one of")


def test_a_non_iso_date_is_rejected(validator, committed):
    """`format: date` is an annotation; the `pattern` beside it is the assertion."""
    cell = COMPLETE_MEASURED_OK | {"as_of": "August 2026"}
    _rejects(validator, _with_cell(committed, cell, TRANSFER_X_ZEPHYR37), "does not match")


def test_an_unversioned_document_is_rejected(validator, committed):
    document = {k: v for k, v in copy.deepcopy(committed).items() if k != "format"}
    _rejects(validator, (document, ()), "'format' is a required property")


def test_an_unknown_top_level_key_is_rejected(validator, committed):
    document = copy.deepcopy(committed) | {"verdicts": []}
    _rejects(validator, (document, ()), "Additional properties are not allowed")


# --------------------------------------------------------------------------
# Per-element evidence: what the rendered page has to be able to say
# --------------------------------------------------------------------------
#
# The page is a LOOKUP REFERENCE, so its plain-language column is DERIVED from
# (status, surface, observed_on, not_observable) rather than stored -- a
# hand-written sentence beside a machine-written verdict is the fabrication
# path §5's guards close. These tests are what make that derivation safe: the
# JSON Schema refuses a cell that omits the breakdown, and the checks below
# refuse a breakdown that contradicts the profile it sits under. A schema
# cannot do the second: it is a cross-reference from a cell to its `profiles`
# entry, and JSON Schema cannot follow that pointer.


def test_the_committed_matrix_holds_no_element_accounting_contradiction(committed):
    """LIVE since task 4, and it was honestly declared vacuous before that.

    This shipped with task 1 saying it searched an empty set -- every cell was
    ``untested`` and carried no element lists at all. Collation populated all
    54, so it now really reads two lists per cell. The docstring is corrected
    here rather than left: a guard that describes itself as weaker than it is
    invites being deleted, and one that describes itself as stronger is worse.
    The INJECTION tests below are still what prove it can go red.
    """
    assert element_accounting_errors(committed) == []


def test_a_cell_naming_an_element_from_another_profile_is_caught(committed):
    """A rendered sentence naming `test1` under `zephyr-3.7` would be FALSE."""
    document, _ = _with_cell(
        committed,
        COMPLETE_MEASURED_OK | {"observed_on": ["zephyr37_fat", "test1"]},
        TRANSFER_X_ZEPHYR37,
    )
    errors = element_accounting_errors(document)
    assert any("observed_on names 'test1'" in error for error in errors), errors


def test_an_element_filed_as_both_observed_and_not_observable_is_caught(committed):
    """Whichever half a renderer reached first would contradict the other."""
    document, _ = _with_cell(
        committed,
        COMPLETE_MEASURED_OK
        | {
            "not_observable": [
                {"element": "zephyr37_fat", "probed": "remote_scratch", "probe_result": "None"}
            ]
        },
        TRANSFER_X_ZEPHYR37,
    )
    errors = element_accounting_errors(document)
    assert any("BOTH observed and not-observable" in error for error in errors), errors


def test_an_element_listed_twice_as_not_observable_is_caught(committed):
    """A duplicate would double-count in any "N of M" the page renders."""
    probe = {"element": "zephyr37_nofs", "probed": "remote_scratch", "probe_result": "None"}
    document, _ = _with_cell(
        committed,
        COMPLETE_MEASURED_OK | {"not_observable": [probe, probe]},
        TRANSFER_X_ZEPHYR37,
    )
    errors = element_accounting_errors(document)
    assert any("more than once" in error for error in errors), errors


# --------------------------------------------------------------------------
# ...and per-CELL evidence, because the element is not the measurement unit
# --------------------------------------------------------------------------
#
# The bed measures an (element, term, transfer) cell. `observed_cells` records
# each one and what it answered, and the SCHEMA ties those outcomes to the
# status in both directions. Below are the three cross-references it cannot
# make -- each is a pointer from a cell to something outside it (its own
# `observed_on`, or the cell space the bed venue draws), and JSON Schema cannot
# follow a pointer.


def test_the_committed_matrix_holds_no_per_cell_contradiction(committed):
    """LIVE, not vacuous: all 54 committed cells carry a breakdown.

    Stated because task 1 shipped this section's element twin as an honestly
    declared vacuous guard, and the difference matters when reading which of
    these has teeth today.
    """
    assert cell_outcome_errors(committed) == []


def test_a_breakdown_naming_an_element_the_cell_did_not_observe_is_caught(committed):
    """A transport result attributed to a device the verdict does not rest on.

    The rendered sentence would say "on ``zephyr37_nofs`` it works over
    ``console``" about an element this cell filed as un-observable.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    entries[0] = entries[0] | {
        "element": "zephyr37_nofs",
        "cell_label": "bed-zephyr[zephyr37_nofs:telnet:console]",
    }
    document, _ = _with_cell(
        committed, COMPLETE_MEASURED_OK | {"observed_cells": entries}, TRANSFER_X_ZEPHYR37
    )
    errors = _at(cell_outcome_errors(document), TRANSFER_X_ZEPHYR37)
    assert any("not in observed_on" in error for error in errors), errors


def test_a_breakdown_that_omits_an_observed_element_is_caught(committed):
    """★ THE ONE A SCHEMA CAN NEVER SEE, and the one that matters most.

    A breakdown holding one entry for a two-element cell validates perfectly:
    the key is there, it is non-empty, and every entry passed. It is also the
    uniform reading this field exists to retire, wearing the field's own
    clothes -- a reader would take the one transport shown as the whole story.
    ``observed_on`` and ``observed_cells`` must name the SAME element set.
    """
    document, _ = _with_cell(
        committed,
        COMPLETE_MEASURED_OK | {"observed_cells": COMPLETE_MEASURED_OK["observed_cells"][:1]},
        TRANSFER_X_ZEPHYR37,
    )
    errors = _at(cell_outcome_errors(document), TRANSFER_X_ZEPHYR37)
    assert any("which observed_cells does not break down" in error for error in errors), errors


def test_a_breakdown_naming_a_cell_the_bed_never_draws_is_caught(committed):
    """A plausible-looking label is exactly what this artifact must not accept.

    ``bed-zephyr[zephyr37_fat:ssh:scp]`` is well-formed, names a real element
    and a real transport pair, and is not a cell the bed venue draws for that
    guest. Resolved against ``bed_space()`` for the same reason a
    ``positive_control`` is resolved against real collection: this item's own
    task 1 fixture cited a control that could never resolve, and it satisfied
    every shape check in this file.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    entries[0] = entries[0] | {
        "cell_label": "bed-zephyr[zephyr37_fat:ssh:scp]",
        "term": "ssh",
        "transfer": "scp",
    }
    document, _ = _with_cell(
        committed, COMPLETE_MEASURED_OK | {"observed_cells": entries}, TRANSFER_X_ZEPHYR37
    )
    errors = _at(cell_outcome_errors(document), TRANSFER_X_ZEPHYR37)
    assert any("not a cell the bed venue draws" in error for error in errors), errors


def test_a_breakdown_whose_axes_disagree_with_its_own_label_is_caught(committed):
    """The label and the axes beside it are two spellings of one fact.

    A renderer reads the axes; a person reproducing the measurement reads the
    label. If they disagree, one of the two readers is being lied to, and
    which one depends on which field they happened to trust.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    entries[0]["transfer"] = "nc"
    document, _ = _with_cell(
        committed, COMPLETE_MEASURED_OK | {"observed_cells": entries}, TRANSFER_X_ZEPHYR37
    )
    errors = _at(cell_outcome_errors(document), TRANSFER_X_ZEPHYR37)
    assert any("disagrees with the axes beside it" in error for error in errors), errors


def test_a_breakdown_listing_one_cell_twice_is_caught(committed):
    """A duplicate would double-count in any "N transports" the page renders."""
    entry = COMPLETE_MEASURED_OK["observed_cells"][0]
    document, _ = _with_cell(
        committed,
        COMPLETE_MEASURED_OK
        | {"observed_cells": [entry, entry, *COMPLETE_MEASURED_OK["observed_cells"][1:]]},
        TRANSFER_X_ZEPHYR37,
    )
    errors = _at(cell_outcome_errors(document), TRANSFER_X_ZEPHYR37)
    assert any("more than once" in error for error in errors), errors


def test_the_committed_breakdown_can_go_green_on_a_real_cell(committed):
    """The control for the five injections above: an untouched cell raises nothing.

    Scoped to the artifact as committed, so a guard that flagged EVERY cell
    would satisfy all five negative tests and be caught only here.
    """
    document, _ = _with_cell(committed, COMPLETE_MEASURED_OK, TRANSFER_X_ZEPHYR37)
    assert _at(cell_outcome_errors(document), TRANSFER_X_ZEPHYR37) == []


def test_a_measured_ok_cell_reports_which_elements_it_did_not_cover(committed):
    """The arithmetic a "what it means for you" column needs, without a lookup.

    ``zephyr-3.7`` holds four elements and the transfer contract is observable
    on two of them (measured 2026-08-24 against
    ``test_transfer_contract.applicable_cell``). A cell that measured only one
    of the two must not be renderable as covering the profile: the element it
    did not reach is neither observed nor un-observable, and `unaccounted` is
    where a renderer finds it.
    """
    document, _ = _with_cell(
        committed, COMPLETE_MEASURED_OK | {"observed_on": ["zephyr37_fat"]}, TRANSFER_X_ZEPHYR37
    )
    coverage = cell_coverage(document, *TRANSFER_X_ZEPHYR37)
    assert coverage.elements == ["zephyr37_fat", "zephyr37_lfs", "zephyr37_nofs", "zephyr37_llext"]
    assert coverage.observed_on == ["zephyr37_fat"]
    assert coverage.not_observable == ["zephyr37_nofs", "zephyr37_llext"]
    assert coverage.unaccounted == ["zephyr37_lfs"], (
        "an element in neither list is not covered by the verdict, and a page that "
        "renders the cell as covering its whole profile would mislead a reader"
    )


def test_a_fully_accounted_cell_reports_nothing_unaccounted(committed):
    """The control: `unaccounted` is not simply always non-empty."""
    coverage = cell_coverage(
        _with_cell(committed, COMPLETE_MEASURED_OK, TRANSFER_X_ZEPHYR37)[0], *TRANSFER_X_ZEPHYR37
    )
    assert coverage.unaccounted == []


# --------------------------------------------------------------------------
# Observation records: what a run leaves behind, and why it cannot flatter
# --------------------------------------------------------------------------

# THE ONLY PATH BY WHICH A CELL MAY BECOME `measured-*` (spec §5), so the
# question these guards answer is not "does it write a file" but "can the file
# it writes disagree with what the run did". The two that matter are the
# INJECTION pair below: a contract made to FAIL must produce a `failed` record,
# and one made to SKIP must not produce a passing one -- a skip inside a drawn
# cell "reports success for a contract nobody ran", which is the failure the
# whole venue is built against (tests/conformance/conftest.py).
#
# Both run a REAL conformance session in a subprocess. In-process would be
# cheaper and would prove less: the emitter is a pytest hook, and a hook's
# behaviour is a property of a session, not of a function call.


def _report(when: str, outcome: str, *, wasxfail: "str | None" = None, longrepr=None):
    """A real :class:`pytest.TestReport`, not a stand-in.

    ``outcome_of`` reads ``.failed`` / ``.skipped`` and ``hasattr(...,
    'wasxfail')``; a hand-rolled double would be free to disagree with pytest
    about what those mean, which is precisely the disagreement these tests
    exist to rule out.
    """
    report = pytest.TestReport(
        nodeid="tests/conformance/test_exec_contract.py::test_x[cell]",
        location=("tests/conformance/test_exec_contract.py", 1, "test_x"),
        keywords={},
        outcome=outcome,
        longrepr=longrepr,
        when=when,
    )
    if wasxfail is not None:
        report.wasxfail = wasxfail
    return report


@pytest.mark.parametrize(
    ("reports", "expected"),
    [
        pytest.param(
            {"setup": ("passed",), "call": ("passed",), "teardown": ("passed",)},
            PASSED,
            id="clean-pass",
        ),
        pytest.param(
            {"setup": ("passed",), "call": ("failed",), "teardown": ("passed",)},
            FAILED,
            id="contract-failed",
        ),
        pytest.param({"setup": ("failed",)}, ERROR, id="setup-error"),
        pytest.param(
            {"setup": ("passed",), "call": ("passed",), "teardown": ("failed",)},
            ERROR,
            id="teardown-error",
        ),
        pytest.param({"setup": ("skipped",)}, SKIPPED, id="skipped-at-setup"),
        pytest.param(
            {"setup": ("passed",), "call": ("skipped",), "teardown": ("passed",)},
            SKIPPED,
            id="skipped-in-body",
        ),
        pytest.param(
            {"setup": ("passed",), "call": ("skipped", "known nc gap"), "teardown": ("passed",)},
            XFAILED,
            id="strict-xfail-fired",
        ),
        pytest.param(
            {"setup": ("passed",), "call": ("failed", "known nc gap"), "teardown": ("passed",)},
            XPASSED,
            id="strict-xfail-xpassed",
        ),
        pytest.param({"setup": ("passed",)}, NOT_RUN, id="call-never-happened"),
    ],
)
def test_the_outcome_is_read_from_every_phase(reports, expected):
    """Each phase can veto a pass, and a skip is never one of the passing words."""
    built = {
        when: _report(when, spec[0], wasxfail=spec[1] if len(spec) > 1 else None)
        for when, spec in reports.items()
    }
    assert outcome_of(built) == expected


def test_only_a_real_contract_result_counts_as_evidence():
    """A run-level outcome must not be foldable into a verdict.

    ``xfailed`` IS evidence -- a strict xfail is an assertion about a known
    product defect, which is what ``measured-broken`` records. A skip, a setup
    error and an item whose body never ran are statements about the RUN.
    """
    assert [o for o in (PASSED, FAILED, XFAILED) if not is_evidential(o)] == []
    assert [o for o in (SKIPPED, ERROR, NOT_RUN, XPASSED) if is_evidential(o)] == []


def test_a_record_names_the_element_and_not_only_the_profile():
    """``observed_on`` is per-ELEMENT, so a record that named only a column
    could never populate it -- and the page could not say "2 of 4" honestly."""
    resolved = _fabricated_cell("bb1161", "telnet", "shell", kind="bed-busybox")
    record = observation_record(
        resolved=resolved,
        venue="bed",
        contract=SURFACES[0].contract,
        nodeid=f"{SURFACES[0].contract}[bed-busybox[bb1161:telnet:shell]]",
        outcome=PASSED,
    )
    assert record["element"] == "bb1161"
    assert record["profile"] == "busybox-1.16.1"
    assert record["surface"] == SURFACES[0].id


def test_the_venue_is_recorded_as_given_and_never_inferred_from_the_cells():
    """A bed run that drew only hermetic-resolvable cells must still say ``bed``.

    The label is what the collate step writes into the evidence, so inferring
    it from which cells appeared would mislabel exactly the run whose cells
    were unrepresentative -- and nothing downstream could ever notice.
    """
    hermetic_shaped = _fabricated_cell("local", "local", "local", kind="local")
    record = observation_record(
        resolved=hermetic_shaped,
        venue="bed",
        contract=SURFACES[0].contract,
        nodeid=f"{SURFACES[0].contract}[local[local:local:local]]",
        outcome=PASSED,
    )
    assert record["venue"] == "bed"


def test_the_hook_reads_the_venue_from_the_knob_the_venue_itself_reads():
    """Pinned on the hook's own code, because no hermetic test can observe a
    ``bed`` label being produced -- the only way to make ``current_venue()``
    answer ``bed`` is to run against the real lab."""
    names = conformance_conftest.pytest_runtest_makereport.__code__.co_names
    assert "current_venue" in names, (
        "tests/conformance/conftest.py's report hook must call current_venue() "
        "(tests/conformance/_venue.py) rather than deriving the venue itself"
    )


def test_hermetic_elements_belong_to_no_profile_column():
    """MEASURED, and it is a finding rather than a detail.

    The profile axis is built from the BED labs' elements; the HERMETIC
    venue's cells carry element names that are in no lab at all. So a
    hermetic observation names an element no column holds, and the record
    says ``profile: null`` rather than guessing one from its spelling. Spec
    §5 expects hermetic rows to carry CI-measured dates, so COLLATION needs a
    ruling on this -- this test is what makes the gap fail loudly the day
    someone closes it by hand instead.
    """
    assert profile_for("bb1161") == "busybox-1.16.1"
    assert profile_for("zephyr37_nofs") == "zephyr-3.7"
    unplaceable = {name: profile_for(name) for name in ("local", "loopback", "busybox-1.16.1")}
    assert set(unplaceable.values()) == {None}, (
        f"a hermetic element now resolves to a matrix column: {unplaceable}. That is the "
        "gap this test records; closing it is a collation decision, so update Task 4 and "
        "this test together."
    )


def test_the_observations_directory_is_anchored_at_the_project_root(monkeypatch):
    """Never CWD-relative. A lane invoked from a subdirectory would otherwise
    scatter records where the collate step does not look."""
    monkeypatch.delenv(OBSERVATIONS_ENV_VAR, raising=False)
    assert observations_dir() == PROJECT_ROOT / "reports" / "conformance-observations"
    monkeypatch.setenv(OBSERVATIONS_ENV_VAR, "/tmp/elsewhere")
    assert observations_dir() == Path("/tmp/elsewhere")


def test_the_observations_knob_survives_the_ambient_env_strip():
    """Undeclared, ``tests/conftest.py`` would strip it and a redirected run
    would write into the repo's real directory in silence -- issue #192."""
    assert OBSERVATIONS_ENV_VAR in ambient_opt_ins()


def test_two_cells_of_one_contract_do_not_share_a_record_file():
    """The default lane is ``-n auto``; two workers writing one path would
    leave one cell's verdict on top of another's."""
    contract = SURFACES[0].contract
    names = {
        record_filename(
            observation_record(
                resolved=_fabricated_cell(element, "telnet", "shell", kind="bed-busybox"),
                venue="bed",
                contract=contract,
                nodeid=f"{contract}[bed-busybox[{element}:telnet:shell]]",
                outcome=PASSED,
            )
        )
        for element in ("bb1161", "bb1211")
    }
    assert len(names) == 2


def test_re_measuring_one_cell_replaces_its_record_rather_than_adding_one():
    """Identity is (kind, contract, venue, cell); the outcome and the date are
    what a re-run is meant to overwrite."""
    contract = SURFACES[0].contract
    both = {
        record_filename(
            observation_record(
                resolved=_fabricated_cell("bb1161", "telnet", "shell", kind="bed-busybox"),
                venue="bed",
                contract=contract,
                nodeid=f"{contract}[bed-busybox[bb1161:telnet:shell]]",
                outcome=outcome,
            )
        )
        for outcome in (PASSED, FAILED)
    }
    assert len(both) == 1


def test_the_schema_holds_a_parametrized_conformance_nodeid():
    """THE POSITIVE TWIN of a limitation Task 2 pinned and Task 3 removed.

    Task 1's ``NodeId`` pattern ended ``(\\[[^\\]]*\\])?$``, and MEASURED it
    could not match a single parametrized nodeid this tree produces:
    ``cell_label`` is ``kind[element:term:transfer]`` and pytest wraps it in
    brackets again, so the inner ``]`` closed the character class early. Task 2
    left a test asserting that limitation, with instructions to delete it and
    pin the positive; this is that pin. It BLOCKED this task outright -- a
    ``positive_control`` is a parametrized nodeid by construction, because a
    control is only ever evidence about the cell it ran on.

    Both directions, and the negative half is the one that keeps the widening
    honest: a pattern relaxed to ``.*`` would satisfy the first assertion and
    accept anything.
    """
    pattern = re.compile(json.loads(SCHEMA_PATH.read_text())["$defs"]["NodeId"]["pattern"])
    contract = SURFACES[0].contract
    assert pattern.match(contract), "the unparametrized contract nodeid must validate"
    for label in ("local[local:local:local]", "bed-busybox[bb1161:telnet:nc]"):
        assert pattern.match(f"{contract}[{label}]"), (
            f"the schema still cannot hold a nodeid parametrized on {label!r}, which is "
            f"every nodeid this suite produces"
        )
    for junk in (f"{contract}[a[b[c]]]", f"{contract}[a]b]", "src/otto/x.py::test_x"):
        assert not pattern.match(junk), (
            f"the widened pattern accepts {junk!r}, so it no longer says anything"
        )


# --------------------------------------------------------------------------
# ...and the same thing proved against a real session
# --------------------------------------------------------------------------

_EXEC_CONTRACT = (
    "tests/conformance/test_exec_contract.py::test_exec_reports_the_documented_exit_code"
)
_TRANSFER_CONTRACT = (
    "tests/conformance/test_transfer_contract.py::test_put_get_roundtrip_preserves_content"
)

#: Injected into ONE real run: four drawn cells are made to end four different
#: ways -- fail, skip in the body, skip before the body, and pass the contract
#: but fail TEARDOWN. Every condition is INJECTED rather than found: a guard
#: that inherits a condition already true passes either way and proves nothing.
#: The last two are what make "read the outcome at teardown" a claim with teeth
#: -- MEASURED, an emitter reading the CALL phase instead reports the
#: teardown-error cell as ``passed`` and emits no record at all for the cell
#: whose setup skipped.
_INJECT_FOUR_ENDINGS = """
import pytest

FAILS = "busybox-1.16.1"
SKIPS_IN_BODY = "busybox-1.21.1"
FAILS_AT_TEARDOWN = "busybox-1.28.1"
SKIPS_AT_SETUP = "busybox-1.31.0"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    if FAILS in item.nodeid:
        yield
        raise AssertionError("INJECTED: this cell's contract is made to fail")
    if SKIPS_IN_BODY in item.nodeid:
        yield
        pytest.skip("INJECTED: this cell's contract is made to skip")
    return (yield)


def pytest_runtest_setup(item):
    if SKIPS_AT_SETUP in item.nodeid:
        pytest.skip("INJECTED: this cell never reaches its contract")


@pytest.fixture(autouse=True)
def _inject_teardown_failure(request):
    yield
    if FAILS_AT_TEARDOWN in request.node.nodeid:
        raise RuntimeError("INJECTED: this cell's teardown fails")
"""

#: Injected into a real run: the transfer contract's applicable domain is
#: narrowed to exclude the BusyBox cells, which generates NO items for them --
#: the shape the bed's real Zephyr narrowings have.
_INJECT_DOMAIN_NARROWING = """
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_generate_tests(metafunc):
    if metafunc.module.__name__.endswith("test_transfer_contract"):
        metafunc.module.applicable_cell = lambda resolved: "busybox" not in resolved.cell.element
"""


#: Rides along in every inner run and writes down exactly which items that
#: session SELECTED. The alternative -- counting words in pytest's summary
#: line, or reading its JUnit XML -- answers "how many" when the question is
#: "which", and an emitter that recorded the right NUMBER of the wrong cells
#: would satisfy the first and not the second.
_CENSUS = """
import json
import os


def pytest_collection_finish(session):
    with open(os.environ["_CONFORMANCE_CENSUS"], "w", encoding="utf-8") as handle:
        json.dump([item.nodeid for item in session.items], handle)
"""

_CENSUS_ENV_VAR = "_CONFORMANCE_CENSUS"
"""Deliberately not ``OTTO_``-prefixed: ``tests/conftest.py`` would strip it."""


def _run_conformance(
    tmp_path: Path, contract: str, plugin: str = "", *, also: "tuple[str, ...]" = ()
) -> "tuple[list[str], list]":
    """Run one hermetic contract over EVERY cell; answer (selected nodeids, records).

    *also* names further node targets for the SAME session, which is what lets
    a contract and its positive control be observed in one run -- the only
    place the emitter's treatment of the two can be compared without a second
    session's differences confounding it.
    """
    observations = tmp_path / "observations"
    census = tmp_path / "census.json"
    (tmp_path / "injected_plugin.py").write_text(_CENSUS + plugin, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "OTTO_CONFORMANCE_OBSERVATIONS": str(observations),
        # `all`, so the record set is the whole hermetic space rather than a
        # seeded sample -- an assertion over a random draw would be a
        # different assertion on every run.
        "OTTO_CONFORMANCE_CELLS": "all",
        _CENSUS_ENV_VAR: str(census),
        "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            contract,
            *also,
            "-m",
            "conformance and not conformance_bed",
            "--no-cov",
            "-p",
            "no:randomly",
            "-n0",
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            "-p",
            "injected_plugin",
        ],
        env=env,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert census.exists(), f"the inner run never collected:\n{result.stdout}\n{result.stderr}"
    selected = json.loads(census.read_text(encoding="utf-8"))
    assert selected, f"the inner run selected nothing:\n{result.stdout}\n{result.stderr}"
    return selected, read_records(observations)


def test_a_real_run_leaves_one_record_per_cell_it_exercised(tmp_path):
    """The base claim, against a real session: every item, one well-formed record."""
    selected, records = _run_conformance(tmp_path, _EXEC_CONTRACT)
    observations = [r for r in records if r["kind"] == "observation"]
    assert {r["nodeid"] for r in observations} == set(selected)
    assert {r["outcome"] for r in observations} == {PASSED}
    assert {r["venue"] for r in observations} == {"hermetic"}
    assert {r["surface"] for r in observations} == {"exec-exit-code"}
    assert all(r["contract"] == _EXEC_CONTRACT for r in observations)
    assert all(r["nodeid"].startswith(f"{_EXEC_CONTRACT}[") for r in observations)
    assert all(r["element"] for r in observations), "every record must name its element"
    # One file per cell, not one file overwritten by the last cell.
    assert len({r["cell_label"] for r in observations}) == len(observations)


def test_a_failing_cell_cannot_leave_a_passing_record(tmp_path):
    """★ THE MUTATION THAT MATTERS. A record written from inside a contract,
    before its assertions run, would say ``passed`` here -- a green record
    vouching for a red test, which is the fabrication the matrix exists to
    prevent.

    Scoped to the injected cell in both directions: the failure must appear on
    exactly that cell, and the untouched cells must still read ``passed``. An
    assertion that merely counted failures would also pass if the emitter had
    turned every cell red.
    """
    selected, records = _run_conformance(tmp_path, _EXEC_CONTRACT, plugin=_INJECT_FOUR_ENDINGS)
    by_cell = {r["cell_label"]: r for r in records if r["kind"] == "observation"}
    assert len(by_cell) == len(selected)
    failed = [label for label, r in by_cell.items() if r["outcome"] == FAILED]
    assert len(failed) == 1, f"the injected failure did not land on exactly one cell: {failed}"
    assert "busybox-1.16.1" in failed[0], f"it landed on the wrong cell: {failed}"
    assert "INJECTED" in by_cell[failed[0]]["failure_summary"]
    assert by_cell[failed[0]]["evidential"] is True
    injected = ("busybox-1.16.1", "busybox-1.21.1", "busybox-1.28.1", "busybox-1.31.0")
    untouched = {
        label: r["outcome"]
        for label, r in by_cell.items()
        if not any(marker in label for marker in injected)
    }
    assert set(untouched.values()) == {PASSED}, f"unrelated cells moved too: {untouched}"


def test_a_skipped_cell_cannot_leave_a_passing_record(tmp_path):
    """A skip inside a drawn cell reports success for a contract nobody ran.

    Same session as the failure injection above -- one subprocess, two
    injected conditions -- so this costs a dictionary lookup rather than
    another real run.
    """
    _, records = _run_conformance(tmp_path, _EXEC_CONTRACT, plugin=_INJECT_FOUR_ENDINGS)
    by_cell = {r["cell_label"]: r for r in records if r["kind"] == "observation"}
    in_body = next(label for label in by_cell if "busybox-1.21.1" in label)
    assert by_cell[in_body]["outcome"] == SKIPPED, (
        f"the injected skip did not reach its own cell: {by_cell[in_body]}"
    )
    assert by_cell[in_body]["evidential"] is False, (
        "a skipped cell must not be foldable into a verdict"
    )
    # Scoped: the injection must not have skipped anything it was not aimed at.
    untouched = {label: r["outcome"] for label, r in by_cell.items() if "busybox-" not in label}
    assert set(untouched.values()) == {PASSED}, f"unrelated cells moved too: {untouched}"


def test_a_cell_that_passed_its_body_and_failed_teardown_is_not_a_pass(tmp_path):
    """WHY THE RECORD IS WRITTEN AT TEARDOWN AND NOT AT CALL.

    This cell's contract ran and its assertions held; its teardown then
    raised, so the cell was not left as it was found. An emitter reading the
    CALL phase sees ``passed`` and has no way to learn otherwise -- MEASURED,
    that mutation turns this test red and nothing else in this file notices.
    """
    _, records = _run_conformance(tmp_path, _EXEC_CONTRACT, plugin=_INJECT_FOUR_ENDINGS)
    by_cell = {r["cell_label"]: r for r in records if r["kind"] == "observation"}
    errored = [label for label, r in by_cell.items() if r["outcome"] == ERROR]
    assert len(errored) == 1, f"the injected teardown failure hit {errored}, not one cell"
    assert "busybox-1.28.1" in errored[0], f"it landed on the wrong cell: {errored}"
    assert by_cell[errored[0]]["evidential"] is False


def test_a_cell_skipped_before_its_body_still_leaves_a_record(tmp_path):
    """Silence would be indistinguishable from "never drawn".

    A cell skipped at setup was DRAWN and then not measured, which is a
    different sentence from one the run never reached -- and the second is
    what the collate step must be told, or an undrawn cell and a suppressed
    one fold identically.
    """
    selected, records = _run_conformance(tmp_path, _EXEC_CONTRACT, plugin=_INJECT_FOUR_ENDINGS)
    by_cell = {r["cell_label"]: r for r in records if r["kind"] == "observation"}
    assert {r["nodeid"] for r in by_cell.values()} == set(selected), (
        "a cell that never reached its body left no record at all; "
        f"{len(selected)} items ran and {len(by_cell)} were recorded"
    )
    at_setup = next(label for label in by_cell if "busybox-1.31.0" in label)
    assert by_cell[at_setup]["outcome"] == SKIPPED
    assert by_cell[at_setup]["evidential"] is False


def test_a_cell_a_contract_excludes_leaves_an_exclusion_record_not_silence(tmp_path):
    """The only source the artifact's ``not_observable`` list can have.

    An excluded cell generates no item, so no report hook can ever see it --
    and RE-MEASURED 2026-08-24 over ``bed_space()``'s 49 cells, the bed's real
    narrowings put 3 cells outside transfer and 7 outside timeout, the latter
    covering every element of all three Zephyr profiles. Those rows therefore
    need an EMPTY ``observed_on``, a state only ``not-observable`` permits.
    """
    selected, records = _run_conformance(
        tmp_path, _TRANSFER_CONTRACT, plugin=_INJECT_DOMAIN_NARROWING
    )
    observations = [r for r in records if r["kind"] == "observation"]
    exclusions = [r for r in records if r["kind"] == DOMAIN_EXCLUSION]
    assert {r["nodeid"] for r in observations} == set(selected)
    assert exclusions, "the narrowed-away cells left no trace at all"
    assert all("busybox" in r["element"] for r in exclusions)
    assert all("busybox" not in r["element"] for r in observations)
    assert all(r["contract"] == _TRANSFER_CONTRACT for r in exclusions)
    assert all("applicable_cell" in r["probed"] for r in exclusions)
    assert all(r["probe_result"].startswith("False") for r in exclusions)


def test_a_run_that_exercises_no_contract_writes_nothing(tmp_path):
    """``make coverage``'s path-less legs collect this tree and deselect every
    item of it. An emitter that wrote exclusion records regardless would have
    every unit lane in the repo depositing conformance evidence.

    THE DOMAIN NARROWING IS INJECTED HERE TOO, and that is what gives this
    test teeth. Collection happens before mark filtering, so the excluded
    cells ARE captured even in a run that then executes nothing -- but the
    hermetic venue narrows nothing of its own, so without the injection there
    would be no exclusion to withhold and the test would pass whether the gate
    existed or not. MEASURED: dropping the gate leaves this green without the
    injection and red with it.
    """
    observations = tmp_path / "observations"
    (tmp_path / "injected_plugin.py").write_text(_INJECT_DOMAIN_NARROWING, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/conformance",
            "-m",
            "not conformance",
            "--no-cov",
            "-n0",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "injected_plugin",
        ],
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "OTTO_CONFORMANCE_OBSERVATIONS": str(observations),
            "OTTO_CONFORMANCE_CELLS": "all",
            "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]),
        },
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert "deselected" in result.stdout, f"the shape under test did not occur:\n{result.stdout}"
    assert read_records(observations) == []


# --------------------------------------------------------------------------
# THE POSITIVE CONTROLS -- §5's central requirement
# --------------------------------------------------------------------------
#
# "A `measured-ok` cell whose positive control is missing is a defect in the
# same class the matrix exists to expose." The schema already refuses a cell
# that OMITS the field and refuses a value that is not nodeid-shaped; what it
# structurally cannot do is follow the pointer. These do, and the last of the
# three checks is the one the item exists for: the named nodeid is resolved
# against a REAL collection, because a string that merely looks well formed is
# precisely the failure this artifact must not accept on trust.

_EXEC_CONTROL = (
    "tests/conformance/test_exec_contract.py"
    "::test_control_the_exit_code_channel_reports_more_than_one_code"
)


@pytest.fixture(scope="module")
def bed_nodeids() -> "set[str]":
    """Every nodeid the BED venue really collects -- the resolution target.

    BED and not hermetic, per the ruling of 2026-08-24: hermetic cells carry
    elements no lab declares (``axes_for('busybox-1.16.1')`` raises), so they
    populate no matrix cell and can never be a cell's evidence. Module-scoped
    because it is a subprocess collection; measured at ~1s wall for all 565
    items, and it opens nothing.
    """
    return collect_conformance_nodeids(bed=True)


def test_every_surface_has_exactly_one_positive_control():
    """Both directions, so a control deleted, renamed or misfiled fails HERE.

    A surface with no control could never carry a ``measured-ok`` cell at all
    (``positive_control_for`` raises), which is the right outcome but a
    confusing place to discover it; a surface with two would make "the
    control" ambiguous and let collation pick the weaker one.
    """
    by_surface: "dict[str, list[str]]" = {}
    for control in discover_controls():
        by_surface.setdefault(control.surface, []).append(control.nodeid)
    assert sorted(by_surface) == sorted(surface.id for surface in SURFACES), (
        f"controls name surfaces {sorted(by_surface)}, the matrix declares "
        f"{sorted(s.id for s in SURFACES)}"
    )
    assert {s: ids for s, ids in by_surface.items() if len(ids) != 1} == {}


def test_a_positive_control_is_never_counted_as_a_contract():
    """The exclusion that keeps a control out of the matrix's ROWS.

    Both take ``resolved_cell``, so without the marker every control would
    arrive as a new surface -- and the artifact would then grow six rows whose
    "contract" is a test about an instrument. Asserted as a disjointness AND
    as the unchanged contract set, because a discovery bug that dropped every
    control by dropping every test would satisfy the first alone.
    """
    contracts = set(discover_contracts())
    controls = {control.nodeid for control in discover_controls()}
    assert contracts.isdisjoint(controls), f"counted as contracts: {sorted(contracts & controls)}"
    assert contracts == {surface.contract for surface in SURFACES}


def test_no_positive_control_declares_a_surface_the_matrix_does_not_have():
    """A marker whose argument this cannot read is a loud failure, not a demotion.

    ``control_surface_of`` answers ``""`` for a marker it cannot resolve to a
    single string literal, which lands here rather than silently reclassifying
    the test as a contract.
    """
    known = {surface.id for surface in SURFACES}
    stray = [c.nodeid for c in discover_controls() if c.surface not in known]
    assert stray == [], f"controls declaring an unknown surface: {stray}"


def test_every_control_runs_on_exactly_the_cells_its_contract_runs_on(bed_nodeids):
    """§5's "a control passing on ``gnu`` says nothing about ``busybox-1.16.1``".

    Asserted against a REAL bed collection and not by reading the source: what
    decides a control's cells is ``tests/conformance/conftest.py``'s
    ``pytest_generate_tests``, through the module-level ``applicable_cell``
    the control inherits, and only collection knows what that produced. So a
    control moved to a module with a different domain -- or a domain narrowed
    for the contract and not for its control -- fails here.

    Compares the PARAMETRIZATIONS rather than the counts. A control that ran
    on the right NUMBER of the wrong cells satisfies a count and is exactly
    the thing this item is about.
    """
    for surface in SURFACES:
        control = positive_control_for(surface.id)
        contract_cells = {
            nodeid[len(surface.contract) :]
            for nodeid in bed_nodeids
            if nodeid.startswith(f"{surface.contract}[")
        }
        control_cells = {
            nodeid[len(control) :] for nodeid in bed_nodeids if nodeid.startswith(f"{control}[")
        }
        assert contract_cells, f"{surface.id}: the contract itself collected no cells"
        assert control_cells == contract_cells, (
            f"{surface.id}: its control runs on {sorted(control_cells - contract_cells)} "
            f"that the contract does not, and misses "
            f"{sorted(contract_cells - control_cells)} that it does"
        )


def test_the_committed_matrix_names_only_controls_that_resolve(committed, bed_nodeids):
    """The live guard over the artifact, at the cell AND at every route.

    ★ THIS DOCSTRING USED TO SAY IT WAS VACUOUS -- "every committed cell is
    ``untested``, so this searches an empty set". That was written when the
    artifact was empty and has been FALSE since the collate step first ran:
    the committed matrix holds 41 ``measured-ok`` cells, each citing a
    control, and every passing route now cites one of its own. The identical
    stale claim was found in a neighbouring guard while the per-cell breakdown
    was added, and the reason it matters both times is that a guard describing
    itself as weaker than it is invites being deleted.
    """
    assert positive_control_errors(committed, collected=bed_nodeids) == []


def _measured_ok_at(committed: dict, control: "str | None", **overrides) -> "tuple[dict, tuple]":
    """The complete ``measured-ok`` fixture with its control replaced, injected."""
    cell = {**COMPLETE_MEASURED_OK, **overrides}
    if control is None:
        cell.pop("positive_control", None)
    else:
        cell["positive_control"] = control
    return _with_cell(committed, cell, TRANSFER_X_ZEPHYR37)


def _control_errors(injected: "tuple[dict, tuple]", collected: "set[str] | None") -> "list[str]":
    """Errors raised AT the injected cell, and nowhere else.

    Scoped exactly as ``_rejects`` is, and for the reason Task 1 measured: an
    unscoped assertion goes red for a cell the injection was not aimed at, and
    a guard that can fail for a reason it is not about invites someone to
    "fix" it by weakening the thing it guards.
    """
    document, under = injected
    where = ".".join(str(part) for part in under)
    return [
        error for error in positive_control_errors(document, collected) if error.startswith(where)
    ]


def test_a_fully_evidenced_measured_ok_cell_passes_the_control_guard(committed, bed_nodeids):
    """THE CONTROL FOR EVERY REJECTION BELOW.

    A guard that rejected everything would satisfy each negative test in this
    section and nothing else here would notice -- which is the very defect the
    section is about, one level up.
    """
    assert _control_errors(_measured_ok_at(committed, None), bed_nodeids) == []
    assert (
        _control_errors(
            _measured_ok_at(committed, COMPLETE_MEASURED_OK["positive_control"]), bed_nodeids
        )
        == []
    )


def test_a_measured_ok_cell_naming_another_surfaces_control_is_rejected(committed, bed_nodeids):
    """Right shape, real test, really collected -- and still not evidence for THIS row.

    The framing control run on this cell's own element: everything a shape
    check or a collection lookup can see is correct, and it proves nothing
    about the transfer roundtrip.
    """
    other = positive_control_for("exec-framing")
    named = f"{other}[bed-zephyr[zephyr37_fat:telnet:console]]"
    assert named in bed_nodeids, "the fixture must name a control that really is collected"
    errors = _control_errors(_measured_ok_at(committed, named), bed_nodeids)
    assert errors, "a cell citing another surface's control was accepted"
    assert "is not this surface's control" in errors[0], errors


def test_a_measured_ok_cell_whose_control_ran_on_an_unobserved_element_is_rejected(
    committed, bed_nodeids
):
    """The right control, really collected, on an element this verdict does NOT rest on.

    ``zephyr37_lfs`` is in this fixture's ``observed_on`` and ``bb1161`` is not
    in the profile at all, so the second is the honest hostile case: evidence
    gathered on a BusyBox guest cited by a Zephyr cell. Nothing about the
    string says so.
    """
    named = f"{positive_control_for('transfer-roundtrip')}[bed-busybox[bb1161:telnet:shell]]"
    assert named in bed_nodeids, "the fixture must name a control that really is collected"
    errors = _control_errors(_measured_ok_at(committed, named), bed_nodeids)
    assert errors, "a cell citing a control run on another profile's element was accepted"
    assert "is not this surface's control" in errors[0], errors


def _route_at(committed: dict, control: "str | None", index: int = 0) -> "tuple[dict, tuple]":
    """The ``measured-ok`` fixture with ONE route's own citation replaced, injected.

    The cell-level ``positive_control`` is left alone, so what the assertions
    below prove is that the ROUTE's citation was judged -- not that the cell
    was rejected for something else it was carrying.
    """
    entries = copy.deepcopy(COMPLETE_MEASURED_OK["observed_cells"])
    if control is None:
        entries[index].pop("positive_control", None)
    else:
        entries[index]["positive_control"] = control
    return _with_cell(
        committed, COMPLETE_MEASURED_OK | {"observed_cells": entries}, TRANSFER_X_ZEPHYR37
    )


def test_a_routes_control_run_on_another_route_of_the_same_device_is_rejected(
    committed, bed_nodeids
):
    """★ PER CELL, NOT PER ELEMENT -- and TWO weaker rules would pass this guard's
    first draft.

    ★ I WROTE THIS ON ``zephyr37_fat`` FIRST, AND IT WAS A GUARD THAT COULD NOT
    FAIL. That element draws exactly ONE bed cell, so "the control ran on this
    cell" and "the control ran somewhere on this device" are the SAME
    STATEMENT there -- the check weakened from per-cell to per-element stayed
    GREEN under mutation, which is the one thing this whole item is about. The
    hostile condition has to be a device with more than one route, and
    ``bb1161`` has exactly two.

    So the injected citation is this surface's control, really collected, on a
    route of the very device the verdict rests on:
    :func:`acceptable_controls` accepts it, an element-scoped route rule would
    accept it, and it is still not evidence -- it proves the roundtrip
    comparison could go red over ``nc`` and it is cited under ``shell``, which
    is where the page's whole positive claim about this row lives. otto's
    ``nc`` gap is a per-cell defect; ``test1`` draws eight cells over four
    transfer backends. "Somewhere on this device" is the weakening
    ``observed_cells`` exists to retire, restored at the level below.
    """
    control = positive_control_for("transfer-roundtrip")
    neighbour = f"{control}[bed-busybox[bb1161:telnet:nc]]"
    assert neighbour in bed_nodeids, "the fixture must name a control that really is collected"
    assert neighbour in acceptable_controls(
        "transfer-roundtrip", list(COMPLETE_MEASURED_BROKEN["observed_on"])
    ), "this guard is pointless unless the CELL-level rule would have accepted it"
    entries = copy.deepcopy(COMPLETE_MEASURED_BROKEN["observed_cells"])
    survivor = next(entry for entry in entries if entry["outcome"] == "passed")
    assert survivor["cell_label"] != "bed-busybox[bb1161:telnet:nc]"
    assert survivor["element"] == "bb1161", (
        "the injected citation must come from ANOTHER CELL OF THE SAME DEVICE, or a "
        "rule scoped to the element would refuse it too and this guard proves nothing"
    )
    survivor["positive_control"] = neighbour
    injected = _with_cell(
        committed,
        COMPLETE_MEASURED_BROKEN | {"observed_cells": entries},
        TRANSFER_X_BUSYBOX1161,
    )
    errors = _control_errors(injected, bed_nodeids)
    assert errors, "a route citing the control from another route of the same device passed"
    assert "does not back THIS route" in errors[0], errors
    assert "bed-busybox[bb1161:telnet:shell]" in errors[0], errors


def test_a_routes_control_from_another_surface_is_rejected(committed, bed_nodeids):
    """The framing control, on exactly the right cell, backing the roundtrip's route.

    Everything a shape check can see is right and it says nothing about
    whether the roundtrip comparison could reject a corrupted byte there.
    """
    named = f"{positive_control_for('exec-framing')}[bed-zephyr[zephyr37_fat:telnet:console]]"
    assert named in bed_nodeids, "the fixture must name a control that really is collected"
    errors = _control_errors(_route_at(committed, named), bed_nodeids)
    assert errors, "a route citing another surface's control was accepted"
    assert "does not back THIS route" in errors[0], errors


def test_a_routes_control_that_collection_never_produces_is_rejected(committed, bed_nodeids):
    """Resolution against COLLECTION, at the route.

    The citation is derivable, correct and aimed at its own cell -- the only
    thing wrong with it is that nothing collects that id. INJECTED on the
    collection side, because the tree and its collection agree today, and both
    rows are asserted so this cannot pass by rejecting either way.
    """
    named = COMPLETE_MEASURED_OK["observed_cells"][0]["positive_control"]
    assert named in bed_nodeids, "the fixture's route control must really be collected"
    assert _control_errors(_route_at(committed, named), bed_nodeids) == []
    errors = _control_errors(_route_at(committed, named), bed_nodeids - {named})
    assert errors, "a route naming a nodeid nothing collects was accepted"
    assert "NO collected test has that id" in errors[0], errors


def test_a_control_the_tree_promises_but_collection_never_produces_is_rejected(
    committed, bed_nodeids
):
    """THE CHECK THIS ITEM EXISTS FOR: resolution against COLLECTION, not against a string.

    The injected nodeid passes every derivable test -- it is the right
    surface's control, parametrized on a cell of an element the verdict rests
    on, and built by the very function ``conftest.py`` passes as ``ids=``. The
    only thing wrong with it is that no collected test has that id, and the
    hostile condition is INJECTED on the collection side rather than inherited
    (the tree and its collection agree today, which is why the honest way to
    exercise the disagreement is to withhold the id).

    Both rows, so this cannot pass by rejecting either way.
    """
    named = COMPLETE_MEASURED_OK["positive_control"]
    assert named in bed_nodeids, "the fixture's control must really be collected"
    injected = _measured_ok_at(committed, named)
    assert _control_errors(injected, bed_nodeids) == []
    errors = _control_errors(injected, bed_nodeids - {named})
    assert errors, "a cell naming a nodeid nothing collects was accepted"
    assert "NO collected test has that id" in errors[0], errors


def test_an_unparametrized_control_is_not_accepted_as_evidence(committed, bed_nodeids):
    """A control that ran SOMEWHERE is not a control that ran on this cell.

    The unparametrized nodeid is a real test function and would resolve
    against a naive substring check; it names no cell, so it cannot be
    evidence about one.
    """
    errors = _control_errors(
        _measured_ok_at(committed, positive_control_for("transfer-roundtrip")), bed_nodeids
    )
    assert errors, "a cell citing an unparametrized control was accepted"
    assert "is not this surface's control" in errors[0], errors


def test_a_positive_control_leaves_a_control_record_and_never_an_observation(tmp_path):
    """A control's outcome is evidence about the INSTRUMENT, never about the host.

    One real session running a contract AND its control over every hermetic
    cell, so the two are compared under identical conditions. Three claims,
    and the third is the one Task 4 added:

    1. the contract's eight items each leave an OBSERVATION;
    2. the control's eight leave NONE -- which is what stops a control's green
       from ever becoming a cell's verdict, or from arriving at the collator as
       an observation whose ``surface`` is ``null``;
    3. the control's eight each leave a ``control`` record instead, naming the
       surface its MARKER declares. Without that record the collator could only
       CONSTRUCT a positive-control nodeid, which publishes "a test with this
       name is collected" -- wiring, not evidence. A control that ran and
       FAILED would then still be cited by a ``measured-ok`` cell.

    Scoped in both directions: a bug that stopped the emitter writing anything
    would satisfy claim 2 on its own, and claim 3 refuses it.
    """
    selected, records = _run_conformance(tmp_path, _EXEC_CONTRACT, also=(_EXEC_CONTROL,))
    assert any(nodeid.startswith(_EXEC_CONTROL) for nodeid in selected), (
        f"the inner run never selected the control: {selected}"
    )
    observations = [record for record in records if record["kind"] == "observation"]
    assert {record["nodeid"] for record in observations} == {
        nodeid for nodeid in selected if nodeid.startswith(_EXEC_CONTRACT)
    }
    assert [r for r in observations if r["contract"] == _EXEC_CONTROL] == [], (
        "a control's result must never arrive as an observation about the host"
    )
    controls = [record for record in records if record["kind"] == CONTROL]
    assert {record["nodeid"] for record in controls} == {
        nodeid for nodeid in selected if nodeid.startswith(_EXEC_CONTROL)
    }, "every control item must leave a control record naming the cell it ran on"
    assert {record["surface"] for record in controls} == {"exec-exit-code"}
    assert {record["outcome"] for record in controls} == {PASSED}
    assert all(record["surface"] is not None for record in records), (
        f"a record carries surface: null, which only a non-contract can produce: {records}"
    )


_FRAMING_CONTRACT = (
    "tests/conformance/test_exec_contract.py::test_exec_frames_output_without_prompt_noise"
)
_FRAMING_CONTROL = (
    "tests/conformance/test_exec_contract.py::test_control_the_framing_check_sees_planted_pollution"
)


def test_a_control_record_names_the_surface_its_own_marker_declares(tmp_path):
    """★ AND NOT THE ONE ITS MODULE OR ITS NAME SUGGESTS. MEASURED, after a green mutation.

    A collator writing ``measured-ok`` asks whether the instrument for THIS
    surface proved itself on this cell, so a control record that named the
    wrong surface would let the framing control vouch for the exit-code row.

    The cell chosen is the one an inference gets WRONG. Three of this tree's
    six controls live in ``test_exec_contract.py``, so any rule keyed on the
    MODULE (or on ``"exec" in nodeid``) answers one surface for all three -- and
    a guard that ran only the exit-code control would agree with that rule by
    coincidence and prove nothing. This runs the FRAMING one and requires
    ``exec-framing``. Recorded because the first version of this file used the
    exit-code control and stayed GREEN when the marker read was replaced with
    exactly that inference.
    """
    _, records = _run_conformance(tmp_path, _FRAMING_CONTRACT, also=(_FRAMING_CONTROL,))
    controls = [record for record in records if record["kind"] == CONTROL]
    assert controls, "the inner run left no control record"
    assert {record["surface"] for record in controls} == {"exec-framing"}, (
        f"a control record named {sorted({r['surface'] for r in controls})}, not the surface "
        f"its @pytest.mark.positive_control marker declares"
    )
    assert all(record["contract"] == _FRAMING_CONTROL for record in controls)


def test_the_observable_a_contract_notes_is_the_one_that_reaches_the_record(tmp_path):
    """★ THE REFINEMENT'S WHOLE PATH, end to end. MEASURED, after a green mutation.

    The marker's template is the floor; :func:`~tests.conformance._observable.note_observable`
    is what makes an observable genuinely per-cell, and it is the half §5 asks
    the field for -- shell-history is provable on bash and not at all on the
    BusyBox guests. Nothing proved that a noted observable ever REACHED a
    record: with the note discarded and the template used instead, every guard
    in this file and in ``tests/unit/test_conformance_bed.py`` stayed green,
    because the only guard that had ever seen a note called the contract
    directly and never went near the emitter.

    So this asserts BOTH halves against a real session: the record carries what
    the body noted, AND it is NOT the bare template. The second is what the
    mutation defeats; without it, a template that happened to contain the same
    words would satisfy the first.
    """
    _, records = _run_conformance(tmp_path, _FRAMING_CONTRACT)
    observations = [record for record in records if record["kind"] == "observation"]
    assert observations, "the inner run left no observation"
    template = next(
        entry.template for entry in discover_observables() if entry.contract == _FRAMING_CONTRACT
    )
    for record in observations:
        observable = record["observable"]
        assert "exact equality against the 2 lines the tester chose" in observable, (
            f"{record['cell_label']}: the record carries {observable!r}, which is not what "
            f"the contract's body noted -- the refinement never reached the emitter"
        )
        assert not template.startswith(observable[:80]), (
            f"{record['cell_label']}: the record carries the marker's TEMPLATE, so a "
            f"contract's per-cell narrowing is being discarded"
        )


def test_the_observable_marker_is_registered():
    """An unregistered marker is a WARNING, and this repo turns warnings into errors."""
    registered = [
        line
        for line in (PROJECT_ROOT / "pyproject.toml").read_text().splitlines()
        if line.strip().startswith(f'"{OBSERVABLE_MARK}(')
    ]
    assert len(registered) == 1, f"`{OBSERVABLE_MARK}` is registered {len(registered)} times"


def test_the_control_marker_is_registered(committed):
    """An unregistered marker is a WARNING, and this repo turns warnings into errors.

    Pinned rather than trusted to the run: ``filterwarnings = ["error"]``
    means a missing registration fails the conformance lane and not this one,
    which is a long way from the edit that caused it.
    """
    registered = [
        line
        for line in (PROJECT_ROOT / "pyproject.toml").read_text().splitlines()
        if line.strip().startswith(f'"{CONTROL_MARK}(')
    ]
    assert len(registered) == 1, f"`{CONTROL_MARK}` is registered {len(registered)} times"


# --------------------------------------------------------------------------
# THE CONTROLS' OWN CONTROL -- a host whose observable CANNOT MOVE
# --------------------------------------------------------------------------
#
# The guards above establish that each surface HAS a control, that it runs on
# the right cells, and that a cell may only cite it as evidence. None of them
# can see whether the control's BODY asserts anything, and that is the exact
# defect this whole item exists to prevent, one level up: a control made
# VACUOUS -- `assert True`, or an assertion already true of every host --
# would satisfy every one of them while proving nothing, and the matrix would
# then publish `measured-ok` cells backed by a nodeid that vouches for
# nothing.
#
# So each control is driven twice against a fake host, in the two directions
# that together pin it:
#
#   * against a host whose observable MOVES, it must PASS -- without this
#     half, a control mutated to `assert False` would satisfy the other one;
#   * against a host whose observable CANNOT MOVE, it must FAIL -- and this
#     is the half a vacuous control cannot survive.
#
# NOT `tests/unit/test_conformance_bed.py`'s `_ScriptedHost`, and the
# difference is the question rather than the machinery. That harness asks
# *does this CONTRACT catch a LYING product?* and drives the six contracts
# against nine lies. This one asks *does this CONTROL catch an IMMOVABLE
# observable?*, which needs a host that also puts and gets files, and needs
# each control's own particular kind of inertness. Two questions, two
# fixtures, each named for the one it answers.


def _printf(command: str) -> str:
    """What the POSIX vocabulary's two ``printf`` spellings print.

    A real (tiny) implementation rather than a lookup keyed by the exact
    command string: the stimuli are built from tokens in
    ``tests/conformance/_vocabulary.py``, and a table here would have to
    restate them and could then disagree. Both spellings this suite uses are
    ``%s\\n`` repeated, so the arguments joined by newlines is the answer.
    """
    parts = shlex.split(command)
    return "\n".join(parts[2:])


class _FakeTransfer:
    """Just enough of a transfer backend for ``_transfer_backend`` to read."""

    def __init__(self, supports_mode: bool) -> None:
        self.supports_mode = supports_mode


class _FakeHost:
    """A POSIX cell in memory, honest by default and IMMOVABLE on request.

    Answers only the commands the POSIX vocabulary names, plus the two the
    controls build (``stat -c %a`` and the removal). Anything else RAISES,
    for the reason ``_ScriptedHost`` gives: a permissive default would let a
    control that drifted onto a hard-coded spelling pass here and fail on the
    bed.

    Each *twist* freezes one observable, and each is named for the property it
    removes rather than for the control it defeats -- a twist is a claim about
    a HOST, and which control catches it is what the table below asserts.
    """

    def __init__(self, *twists: str, mode_support: bool = True) -> None:
        self._twists = frozenset(twists)
        self._files: "dict[str, bytes]" = {}
        self._modes: "dict[str, int]" = {}
        self._file_transfer = _FakeTransfer(mode_support)

    async def run(self, command, timeout=None):
        commands = [command] if isinstance(command, str) else list(command)
        return Results.collect([self._answer(one, timeout) for one in commands])

    def _answer(self, command: str, timeout) -> CommandResult:
        if "always-times-out" in self._twists:
            return CommandResult(
                Status.Error,
                value=f"Command timed out after {timeout}s; partial output discarded",
                command=command,
                retcode=-1,
                timed_out=True,
            )
        if command == POSIX.long_running_command and timeout is not None:
            return CommandResult(
                Status.Error,
                value=f"Command timed out after {timeout}s; partial output discarded",
                command=command,
                retcode=-1,
                timed_out=True,
            )
        if "one-exit-code" in self._twists:
            return CommandResult(Status.Failed, value="", command=command, retcode=7)
        if "every-command-fails" in self._twists:
            return CommandResult(Status.Failed, value="", command=command, retcode=3)
        retcode, value = self._honest(command)
        status = Status.Success if retcode == 0 else Status.Failed
        return CommandResult(status, value=value, command=command, retcode=retcode)

    def _honest(self, command: str) -> "tuple[int, str]":
        if command == POSIX.succeeding_command:
            return 0, ""
        if command == POSIX.failing_command:
            return POSIX.failing_code, ""
        if command == POSIX.sequence_failing_command:
            return POSIX.sequence_failing_code, ""
        if command == POSIX.sentinel_plant_command:
            # The immovable framing host is one whose output never carries a
            # sentinel -- exactly the host on which the framing contract's
            # `leak is None` cannot go red.
            return 0, (
                "clean" if "output-never-carries-a-sentinel" in self._twists else _printf(command)
            )
        if command.startswith("printf "):
            return 0, _printf(command)
        if command.startswith("stat -c %a "):
            path = command[len("stat -c %a ") :]
            if "the-landed-mode-never-moves" in self._twists:
                return 0, f"{_TRANSFER_MODE:o}"
            if path not in self._files:
                return 1, f"stat: cannot stat '{path}'"
            return 0, f"{self._modes.get(path, 0o644):o}"
        if command.startswith("rm "):
            path = command[len("rm ") :]
            if path not in self._files:
                return 1, f"rm: cannot remove '{path}': No such file or directory"
            del self._files[path]
            self._modes.pop(path, None)
            return 0, ""
        raise AssertionError(
            f"a control issued {command!r}, which the POSIX vocabulary does not name -- "
            f"a hard-coded spelling has crept into a control body"
        )

    async def put(self, src: Path, dest_dir: Path, mode=None):
        if mode is not None and not self._file_transfer.supports_mode:
            msg = (
                "this backend refuses every put"
                if "the-refusal-says-one-thing" in self._twists
                else (
                    f"host 'fake': _FakeTransfer has no permission model; cannot apply "
                    f"mode 0o{mode:o}."
                )
            )
            return Result(Status.Error, value={src: Result(Status.Error, msg=msg)}, msg=msg)
        landed = str(dest_dir / src.name)
        self._files[landed] = src.read_bytes()
        if mode is not None:
            self._modes[landed] = mode
        return Result(Status.Success, value={src: Result(Status.Success)})

    async def get(self, src: Path, dest_dir: Path):
        if "get-answers-the-contracts-payload" in self._twists:
            data = _PAYLOAD
        else:
            data = self._files.get(str(src))
        if data is None:
            return Result(Status.Error, msg=f"no such file: {src}")
        (dest_dir / Path(src).name).write_bytes(data)
        return Result(Status.Success, value={src: Result(Status.Success)})


def _fake_cell(host: _FakeHost) -> ResolvedCell:
    """A resolved cell whose host is *host* and whose vocabulary is POSIX."""

    @contextlib.asynccontextmanager
    async def opener():
        yield host

    return ResolvedCell(
        cell=Cell("fake", "term", "transfer"),
        kind="fake",
        open_host=opener,
        remote_scratch=lambda tmp: tmp,
        vocabulary=POSIX,
    )


#: The controls this harness drives, by surface. NAMED rather than walked, for
#: the reason ``test_conformance_bed.py``'s ``CONTRACTS`` is: a control that
#: stopped being driven here would be a silent hole, and a hole in the thing
#: that catches vacuous controls is the worst one in this file.
#: ``test_the_harness_drives_every_control_the_tree_declares`` closes it.
CONTROLS = {
    "exec-exit-code": _exec_contract.test_control_the_exit_code_channel_reports_more_than_one_code,
    "exec-framing": _exec_contract.test_control_the_framing_check_sees_planted_pollution,
    "exec-failure-in-sequence": (
        _exec_contract.test_control_a_succeeding_sequence_is_not_reported_as_failed
    ),
    "transfer-roundtrip": (
        _transfer_contract.test_control_the_roundtrip_comparison_rejects_a_corrupted_byte
    ),
    "transfer-mode": (
        _transfer_contract.test_control_the_landed_mode_follows_the_mode_that_was_asked_for
    ),
    "timeout": (
        _timeout_contract.test_control_a_command_inside_its_budget_is_not_reported_as_timed_out
    ),
}

#: ``(case name, surface, twists, mode support, must pass)``. Every surface
#: appears at least once in each direction; ``transfer-mode`` appears twice in
#: each, because its two arms are different observables -- a landed mode on a
#: backend that carries one, and a REFUSAL on a backend that does not.
#:
#: DATA AND NEVER A CALLABLE, the rule ``tests/conformance/_vocabulary.py``
#: states for its own table: a row that could build its own host could quietly
#: build a different one from the one it is named for, and the name is what a
#: reader trusts when a row goes red.
HOST_CASES = [
    ("exit-code moves", "exec-exit-code", (), True, True),
    ("exit-code frozen", "exec-exit-code", ("one-exit-code",), True, False),
    ("sentinel survives", "exec-framing", (), True, True),
    (
        "sentinel never arrives",
        "exec-framing",
        ("output-never-carries-a-sentinel",),
        True,
        False,
    ),
    ("aggregate discriminates", "exec-failure-in-sequence", (), True, True),
    (
        "aggregate always fails",
        "exec-failure-in-sequence",
        ("every-command-fails",),
        True,
        False,
    ),
    ("get reads the far side", "transfer-roundtrip", (), True, True),
    (
        "get answers a constant",
        "transfer-roundtrip",
        ("get-answers-the-contracts-payload",),
        True,
        False,
    ),
    ("mode follows the request", "transfer-mode", (), True, True),
    ("mode never moves", "transfer-mode", ("the-landed-mode-never-moves",), True, False),
    ("refusal reads the mode", "transfer-mode", (), False, True),
    ("refusal is a constant", "transfer-mode", ("the-refusal-says-one-thing",), False, False),
    ("budget is respected", "timeout", (), True, True),
    ("everything times out", "timeout", ("always-times-out",), True, False),
]


def test_the_harness_drives_every_control_the_tree_declares():
    """A control missing from :data:`CONTROLS` is a control nothing proves meaningful."""
    assert sorted(CONTROLS) == sorted(control.surface for control in discover_controls())
    named = {f"{fn.__module__.replace('.', '/')}.py::{fn.__name__}" for fn in CONTROLS.values()}
    assert named == {control.nodeid for control in discover_controls()}
    assert sorted({surface for _, surface, _, _, _ in HOST_CASES}) == sorted(CONTROLS)
    for surface in CONTROLS:
        directions = {passes for _, named, _, _, passes in HOST_CASES if named == surface}
        assert directions == {True, False}, (
            f"{surface} is driven only in the {directions} direction, so its case proves "
            f"either that the control can pass or that it can fail, never both"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "surface", "twists", "mode_support", "must_pass"),
    HOST_CASES,
    ids=[case for case, _, _, _, _ in HOST_CASES],
)
async def test_a_control_passes_when_its_observable_moves_and_fails_when_it_cannot(
    case, surface, twists, mode_support, must_pass, tmp_path
):
    """Each control, driven both ways. THE VACUOUS-CONTROL CATCHER.

    A control edited to ``assert True`` passes the immovable row, which is
    what turns that row red; a control edited to ``assert False`` fails the
    moving row. Neither mutation is caught by any guard above, because both
    leave a well-formed, correctly-parametrized, really-collected nodeid.
    """
    remote = tmp_path / "remote"
    remote.mkdir()
    control = CONTROLS[surface]
    resolved = _fake_cell(_FakeHost(*twists, mode_support=mode_support))
    kwargs = {"resolved_cell": resolved}
    if surface.startswith("transfer-"):
        kwargs |= {"remote_scratch": remote, "tmp_path": tmp_path, "worker_id": "master"}

    if must_pass:
        await control(**kwargs)
        return
    with pytest.raises(AssertionError):
        await control(**kwargs)


# --------------------------------------------------------------------------
# THE OBSERVABLE EACH CONTRACT DECLARES
# --------------------------------------------------------------------------
#
# §5 requires a `measured-ok` cell to name its `observable`, and the ruling of
# 2026-08-24 refuses to let it be DERIVED from the surface id: a field that
# cannot disagree with the cell's own key satisfies the schema while proving
# nothing. So each contract declares one, and these guards keep the
# declarations honest the way the control guards keep the controls honest.


def test_every_surface_declares_exactly_one_observable():
    """A surface with no declared observable can never be marked ``measured-ok``.

    Both directions, like the control guard beside it. A contract that lost its
    marker would quietly become a row no run can ever move off ``untested`` --
    a matrix that goes stale in one cell and looks like it was simply never
    drawn -- and an observable declared for a surface the tree no longer has is
    a declaration nothing reads.
    """
    declared = {entry.contract: entry.template for entry in discover_observables()}
    assert set(declared) == {surface.contract for surface in SURFACES}, (
        f"the contracts declaring an observable are not the matrix's surfaces: "
        f"{sorted(set(declared) ^ {surface.contract for surface in SURFACES})}"
    )
    unreadable = sorted(contract for contract, template in declared.items() if not template)
    assert unreadable == [], (
        f"{unreadable} carry an @pytest.mark.observable whose argument this cannot read "
        f"from source -- a computed marker puts the declaration where no guard can follow"
    )


def test_a_positive_control_declares_no_observable_of_its_own():
    """The declaration belongs to the CONTRACT; a control vouches for it.

    Without this, a control carrying the marker would arrive in
    :func:`discover_observables` as a seventh declaration and the both-ways
    check above would fail for a confusing reason. Stated as its own claim so
    the failure names the real mistake.
    """
    controls = {control.nodeid for control in discover_controls()}
    overlap = sorted(controls & {entry.contract for entry in discover_observables()})
    assert overlap == [], f"{overlap} are positive controls carrying a contract's marker"


@pytest.mark.parametrize("userland", ["gnu", "busybox-1.35.0", "zephyr-3.7"])
def test_every_declared_observable_renders_against_every_vocabulary(userland):
    """A template naming a field that does not exist must fail HERE, not in a run.

    An unrenderable template reaches a reporting hook, where it either raises
    inside `pytest_runtest_makereport` (an INTERNALERROR far from the edit) or,
    worse under a laxer renderer, reaches the artifact as a literal
    ``{words.typo}`` that the docs page then publishes.

    The timeout contract is excluded on Zephyr rather than expected to render:
    its ``applicable_cell`` keeps it off every Zephyr cell precisely because
    that userland has no command that outlives a budget, so its template
    interpolates a ``None`` there and no run can ever produce it.
    """
    words = vocabulary_for_userland(userland)
    resolved = _fabricated_cell("element", "telnet", "console", kind="bed-zephyr")
    resolved = ResolvedCell(
        cell=resolved.cell,
        kind=resolved.kind,
        open_host=resolved.open_host,
        remote_scratch=None,
        vocabulary=words,
    )
    for entry in discover_observables():
        if "timeout" in entry.contract and words.long_running_command is None:
            continue
        rendered = render_observable(entry.template, resolved)
        assert rendered.strip(), entry.contract
        assert "{" not in rendered, f"{entry.contract}: unrendered placeholder in {rendered!r}"


def test_no_declared_observable_is_a_restatement_of_its_surface(committed):
    """★ THE RULING'S CENTRAL POINT, made checkable.

    "Deriving it from the surface id would make the field a restatement of the
    cell's own key, which is worse than absent." So no rendered observable may
    BE its surface's id or title -- and each must name something the surface's
    name does not: a command, a result attribute, a transport.

    Weak on its own, and it is here because the strong form is elsewhere:
    ``test_the_framing_observable_differs_between_the_two_vocabularies`` in
    ``tests/unit/test_conformance_bed.py`` measures that one surface's
    observable really DIFFERS between two userlands, which no derivation from a
    surface id could do.
    """
    words = vocabulary_for_userland("gnu")
    base = _fabricated_cell("test1", "ssh", "scp", kind="bed-unix")
    resolved = ResolvedCell(
        cell=base.cell,
        kind=base.kind,
        open_host=base.open_host,
        remote_scratch=None,
        vocabulary=words,
    )
    by_contract = {surface.contract: surface for surface in SURFACES}
    for entry in discover_observables():
        surface = by_contract[entry.contract]
        rendered = render_observable(entry.template, resolved)
        assert rendered != surface.id
        assert rendered != surface.title
        assert len(rendered) > len(surface.title), (
            f"{surface.id}: the declared observable {rendered!r} is no more specific "
            f"than the row's own title, so it carries no information"
        )


def test_a_cells_observables_differ_only_by_the_transport_its_entry_already_names(committed):
    """★ WHY `CellOutcome` STORES NO PER-CELL `observable`. The decision, and its tripwire.

    Task 4b left the field open and Task 5 left it open again with a caller that would
    use one: the page can say WHICH transport broke but not WHAT was watched on each.
    MEASURED 2026-08-25 over the 575 records of a real bed lane, and that is what
    settles it: of the 51 cells a run drew observations for -- the other three are the
    `not-observable` timeout cells, which have none -- 12 have contributing observations
    whose observables differ at all, and in **every one of the 51** the ONLY thing that
    differs is the transport token, which `observed_cells` already carries as a
    structural `transfer` field. Storing the string per cell would repeat a
    ~110-character sentence 32 times on `gnu` x transfer-mode to say `scp` / `sftp` /
    `ftp` / `nc`, which is the 900-character field Task 4 measured and shrank,
    restored one level down.

    So the decision is: NOT STORED, and this is what makes it falsifiable rather than a
    preference. A template that started naming the DEVICE, or a vocabulary field that
    varied inside one profile, would make two drawn cells watch genuinely different
    things -- and then the cell-level join really would be hiding something and the
    field should be added.

    ITS BLIND SPOT, DECLARED: this renders the declared TEMPLATE, so it cannot see a
    `note_observable` refinement, which only a running contract produces. That layer
    was covered by the record measurement above rather than by this guard, and the
    normalisation here is the same one that measurement used.
    """
    varied = 0
    checked = 0
    for surface in SURFACES:
        template = observable_template_for(surface.contract)
        for profile in committed["profiles"]:
            entries = committed["cells"][surface.id][profile["id"]].get("observed_cells", [])
            if len(entries) < 2:
                continue
            words = vocabulary_for_userland(profile["id"])
            rendered = [
                (
                    entry,
                    render_observable(
                        template,
                        ResolvedCell(
                            cell=Cell(entry["element"], entry["term"], entry["transfer"]),
                            kind="bed-unix",
                            open_host=lambda: None,
                            remote_scratch=None,
                            vocabulary=words,
                        ),
                    ),
                )
                for entry in entries
            ]
            checked += 1
            if len({text for _, text in rendered}) > 1:
                varied += 1
            normalised = {
                text.replace(f"`{entry['transfer']}`", "<transport>").replace(
                    f"`{entry['term']}`", "<console>"
                )
                for entry, text in rendered
            }
            assert len(normalised) == 1, (
                f"{surface.id} x {profile['id']}: its drawn cells watch things that "
                f"differ by more than the transport their own entries already name "
                f"{sorted(normalised)} -- the cell-level observable is now hiding a "
                f"real difference, and `CellOutcome` needs the field this test declined"
            )
    assert checked, "no cell has two drawn cells, so the normalisation examined nothing"
    assert varied, (
        "no cell's observables vary at all today, so the normalisation above proves "
        "nothing: it would pass over identical strings"
    )


# --------------------------------------------------------------------------
# COLLATION -- the only writer of a `measured-*` verdict
# --------------------------------------------------------------------------
#
# `scripts/collate_support_matrix.py` folds a run's observation records into
# the committed artifact. Everything below drives its PURE half (`collate`),
# which reads no clock, no filesystem and no environment, so a synthetic record
# set produces an exactly assertable cell.
#
# ★ WHAT THESE GUARDS CANNOT SEE, stated because Task 3's deepest finding was
# that existence and wiring guards are blind to vacuity, and the same class of
# blindness applies here at a different level. These prove that the COLLATOR
# refuses to mint a verdict without its evidence. They cannot prove that a cell
# already in the committed JSON was minted by the collator rather than typed by
# hand: the records are run output and git-ignored, so a hand-edit that picks a
# real element, a real control nodeid on a real cell of that element, and an
# invented `observable` satisfies the schema, `positive_control_errors` and
# every check in this file. `test_the_committed_verdicts_are_reproducible_from_the_records`
# is the one guard that closes it, and it can only run where the records are --
# this dev VM after a bed lane. It is INERT in CI and says so out loud.


def _unchanged(committed: dict, result, at: "tuple[str, str]") -> bool:
    """Whether the cell at *at* came through a collation untouched.

    REFUSED IS NOT DOWNGRADED, and this is how the difference is asserted. The
    naive spelling -- compare against ``{"status": "untested"}`` -- was written
    first and is WRONG the moment the committed artifact holds real verdicts,
    which it does the moment `make conformance-bed` has ever run here. It would
    then fail for having a verdict rather than for having been rewritten, and a
    guard that fails for the wrong reason invites being "fixed" by weakening it.
    """
    surface, profile = at
    return result.matrix["cells"][surface][profile] == committed["cells"][surface][profile]


#: userland prefix -> the venue kind that stands a host of it up. The mapping
#: ``tests/conformance/_bed.py::_kind_for_userland`` makes, restated here from
#: the PROFILE rather than from the element's name -- an
#: ``element.startswith("zephyr")`` sniff is a guess about a naming convention,
#: and that module's own docstring rejects it.
_BED_KINDS = (("zephyr-", "bed-zephyr"), ("busybox-", "bed-busybox"), ("gnu", "bed-unix"))


def _bed_label(element: str, term: str = "telnet", transfer: str = "console") -> str:
    """A bed cell label, spelled the way ``cell_label`` spells it.

    ★ THIS USED TO ANSWER ``bed-unix`` FOR EVERY BUSYBOX GUEST, which is a
    label the bed venue never draws -- the five BusyBox cells are
    ``bed-busybox[...]``. MEASURED against ``bed_space()`` while adding the
    per-cell breakdown. It went unnoticed because nothing resolved a label
    until then: the only guard that resolves anything, the positive-control
    one, is driven from the Zephyr fixture. It mattered most in
    ``test_one_failing_cell_makes_the_whole_element_broken``, the guard for
    otto's ``nc`` gap and the exact case this breakdown exists for.
    """
    profile = profile_for(element)
    assert profile is not None, f"{element} is in no profile column, so this fixture is wrong"
    kind = next(kind for prefix, kind in _BED_KINDS if profile.startswith(prefix))
    return f"{kind}[{element}:{term}:{transfer}]"


_TRANSFER_CONTROL = (
    "tests/conformance/test_transfer_contract.py"
    "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
)
_OBSERVABLE = "the bytes get() reads back over `console` after put() of a known payload"


def _record(kind: str, surface: str, contract: str, element: str, label: str, **extra) -> dict:
    """One well-formed BED record, with only the fields the collator reads.

    ★ ``term`` AND ``transfer`` ARE DERIVED FROM THE LABEL, not fixed. They
    used to be the literals ``telnet`` / ``console`` whatever label the caller
    passed, so every fixture that varied the transport -- including
    ``test_one_failing_cell_makes_the_whole_element_broken``, the guard for
    otto's ``nc`` gap -- produced records whose axes contradicted their own
    cell id. Nothing read those two fields until the per-cell breakdown did,
    so nothing noticed. A real record reads all three off one ``ResolvedCell``
    (``tests/conformance/_observation.py::cell_facts``) and they cannot
    disagree there; this keeps the fixture in the same shape.
    """
    profile = profile_for(element)
    assert profile is not None, f"{element} is in no profile column, so this fixture is wrong"
    kind_prefix, axes = label.split("[", 1)
    label_element, term, transfer = axes.rstrip("]").split(":")
    assert label_element == element, f"{label} is not a cell of {element}"
    return {
        "format": 1,
        "kind": kind,
        "surface": surface,
        "contract": contract,
        "venue": "bed",
        "as_of": "2026-08-24",
        "element": element,
        "profile": profile,
        "term": term,
        "transfer": transfer,
        "cell_kind": kind_prefix,
        "cell_label": label,
    } | extra


def _observation(element, *, surface="transfer-roundtrip", label=None, outcome=PASSED, **extra):
    """A contract's result on one cell, evidential by default."""
    contract = next(s.contract for s in SURFACES if s.id == surface)
    label = label or _bed_label(element)
    return _record(
        "observation",
        surface,
        contract,
        element,
        label,
        nodeid=f"{contract}[{label}]",
        outcome=outcome,
        evidential=is_evidential(outcome),
        observable=extra.pop("observable", _OBSERVABLE),
        **extra,
    )


def _control(element, *, surface="transfer-roundtrip", label=None, outcome=PASSED):
    """The matching positive control's result on that same cell."""
    control = positive_control_for(surface)
    label = label or _bed_label(element)
    return _record(
        CONTROL,
        surface,
        control,
        element,
        label,
        nodeid=f"{control}[{label}]",
        outcome=outcome,
        evidential=is_evidential(outcome),
    )


def _exclusion(element, *, surface="transfer-roundtrip", label=None):
    """A drawn cell a contract declared outside its domain."""
    contract = next(s.contract for s in SURFACES if s.id == surface)
    label = label or _bed_label(element)
    module = contract.split("::", 1)[0]
    return _record(
        DOMAIN_EXCLUSION,
        surface,
        contract,
        element,
        label,
        probed=f"{module}::applicable_cell({label})",
        probe_result="False -- ResolvedCell.remote_scratch is None",
    )


#: The real mixed case, and the reason Chris ruled for per-element evidence:
#: `zephyr-3.7` x transfer is measurable on two of its four elements and not on
#: the other two, so the cell cannot hold one honest scalar verdict.
_ZEPHYR37_OBSERVED = ("zephyr37_fat", "zephyr37_lfs")
_ZEPHYR37_NOT_OBSERVABLE = ("zephyr37_nofs", "zephyr37_llext")


def _mixed_records() -> "list[dict]":
    """The record set a real bed lane leaves for ``zephyr-3.7`` x transfer."""
    records = []
    for element in _ZEPHYR37_OBSERVED:
        records += [_observation(element), _control(element)]
    records += [_exclusion(element) for element in _ZEPHYR37_NOT_OBSERVABLE]
    return records


def test_the_mixed_zephyr37_transfer_cell_collates_to_a_mixed_cell(committed):
    """★ THE CASE CHRIS'S RULING EXISTS FOR. If this is wrong, nothing else matters.

    `zephyr-3.7` x transfer must come out ``measured-ok`` on the two elements
    that have a filesystem AND record, per element, what was probed on the two
    that do not. A cell that published ``measured-ok`` while hiding the other
    two would mislead exactly the reader it exists to inform: someone pointing
    otto at a no-filesystem 3.7 board.

    Every field is asserted, not just the status. A collapse to a scalar
    verdict is the mutation this is written against, and a collator that
    dropped `not_observable` would still leave the status right.
    """
    result = collate(committed, _mixed_records())
    assert result.refused == []
    cell = result.matrix["cells"]["transfer-roundtrip"]["zephyr-3.7"]
    assert cell["status"] == "measured-ok"
    assert cell["observed_on"] == list(_ZEPHYR37_OBSERVED)
    assert [entry["element"] for entry in cell["not_observable"]] == list(_ZEPHYR37_NOT_OBSERVABLE)
    assert all(entry["probed"] and entry["probe_result"] for entry in cell["not_observable"])
    assert cell["observable"] == _OBSERVABLE
    assert cell["positive_control"] == f"{_TRANSFER_CONTROL}[{_bed_label('zephyr37_fat')}]"
    assert cell["venue"] == "bed"
    assert cell["as_of"] == "2026-08-24"
    # DERIVED, never stored: two of four elements are accounted for by a
    # verdict and two by a probe, so nothing is left unaccounted here.
    coverage = cell_coverage(result.matrix, "transfer-roundtrip", "zephyr-3.7")
    assert coverage.unaccounted == []


def test_a_mixed_cell_is_not_collapsed_to_a_scalar_verdict(committed):
    """The same records, minus the exclusions: the cell must NARROW, not lie.

    ★ MUTATION (d). A collator that dropped the per-element breakdown would
    produce an identical `status` and an identical `observed_on` here, and only
    the two lists tell the two runs apart -- so this asserts the DIFFERENCE
    between a cell that knows why two elements are missing and one that does
    not. The second is a legitimate state (nobody has drawn them), and it is
    `unaccounted`, which the renderer must show and the artifact must not store.
    """
    only_observed = [r for r in _mixed_records() if r["kind"] != DOMAIN_EXCLUSION]
    cell = collate(committed, only_observed).matrix["cells"]["transfer-roundtrip"]["zephyr-3.7"]
    assert cell["status"] == "measured-ok"
    assert cell["observed_on"] == list(_ZEPHYR37_OBSERVED)
    assert cell["not_observable"] == [], (
        "with no exclusion records, the two remaining elements are NOT-YET-DRAWN and the "
        "cell must not claim their environment cannot express the observable"
    )
    matrix = collate(committed, only_observed).matrix
    assert cell_coverage(matrix, "transfer-roundtrip", "zephyr-3.7").unaccounted == list(
        _ZEPHYR37_NOT_OBSERVABLE
    )


def test_collating_nothing_leaves_the_matrix_byte_identical(committed):
    """★ MUTATION (b), in its purest form: absence must change NOTHING.

    A collator that reset undrawn cells to ``untested`` would destroy the
    matrix on every sampled run -- the bed lane at its default budget draws a
    fraction of the space -- and the destruction would look like ordinary
    churn. Compared as SERIALISED JSON rather than as objects, because "byte
    identical" is the claim: a re-ordered key list is a diff Chris would have
    to review.
    """
    measured = collate(committed, _mixed_records()).matrix
    again = collate(measured, [])
    assert json.dumps(again.matrix, indent=2) == json.dumps(measured, indent=2)
    assert again.changed == {}
    assert again.refused == []


def test_a_run_that_drew_one_cell_leaves_every_other_verdict_untouched(committed):
    """★ MUTATION (b) with the hostile condition INJECTED rather than inherited.

    The previous guard passes trivially against a collator that writes nothing
    at all. This one starts from a matrix that already holds SIX measured
    cells, collates a run that drew exactly ONE of them, and requires the other
    five to survive byte for byte -- so a collator that rebuilt the grid from
    the records it happened to see reddens here and not there.
    """
    populated = collate(committed, _mixed_records()).matrix
    for surface in ("exec-exit-code", "exec-framing", "timeout"):
        for element in ("test1", "test2"):
            label = _bed_label(element, "ssh", "scp")
            populated = collate(
                populated,
                [
                    _observation(element, surface=surface, label=label),
                    _control(element, surface=surface, label=label),
                ],
            ).matrix
    before = {
        (surface, profile): json.dumps(cell, sort_keys=True)
        for surface, row in populated["cells"].items()
        for profile, cell in row.items()
        if cell["status"] != "untested"
    }
    assert len(before) >= 4, f"the fixture must populate several cells, not {len(before)}"

    redrawn = collate(populated, _mixed_records())
    after = {
        (surface, profile): json.dumps(cell, sort_keys=True)
        for surface, row in redrawn.matrix["cells"].items()
        for profile, cell in row.items()
        if cell["status"] != "untested"
    }
    assert after == before, "a run that drew ONE cell rewrote or dropped another"
    assert redrawn.changed == {}


def test_hermetic_records_are_discarded_loudly_and_populate_nothing(committed):
    """★ MUTATION (c) — the ruling of 2026-08-24, and its LOUDNESS half.

    Hermetic cells have no lab entry, so `axes_for('busybox-1.16.1')` raises and
    every hermetic record's `profile` is null: it can populate NO column. The
    collator drops them -- and SAYS how many and why, because a collator that
    silently discarded 48 records would look exactly like a broken one and the
    next person could not tell which.

    Both halves are asserted. A collator that dropped them silently satisfies
    "populates nothing"; one that reported them and folded them in anyway
    satisfies "says how many".
    """
    hermetic = [
        {**_observation("zephyr37_fat"), "venue": "hermetic", "profile": None, "element": "local"}
        for _ in range(3)
    ]
    result = collate(committed, [*hermetic, *_mixed_records()])
    assert result.matrix["cells"]["transfer-roundtrip"]["zephyr-3.7"]["status"] == "measured-ok"
    for surface, row in result.matrix["cells"].items():
        for profile, cell in row.items():
            assert "local" not in cell.get("observed_on", []), f"{surface} x {profile}"
    lines = "\n".join(report(result, records_dir=Path("nowhere"), writing=False))
    assert "DISCARDED 3 record(s)" in lines, lines
    assert "hermetic cells have no lab entry" in lines, lines
    assert "ci-hermetic" in lines, "the report must name the spec deviation it embodies"


def test_a_cell_whose_positive_control_did_not_pass_is_refused(committed):
    """★ MUTATION (a). A contract's pass is evidence only if the instrument could say no.

    The contract passes on both elements; the control FAILS on one. A collator
    that only CONSTRUCTED the positive-control nodeid would write ``measured-ok``
    here -- the nodeid is well formed, names the right surface and really
    collects -- and the cell would cite a test that had just proved it could not
    tell a wrong answer from a right one.

    Refused, not downgraded: the cell keeps whatever it had, and the collation
    reports non-`ok` so a person sees it.
    """
    records = [r for r in _mixed_records() if r["kind"] != CONTROL]
    records += [_control("zephyr37_fat"), _control("zephyr37_lfs", outcome=FAILED)]
    result = collate(committed, records)
    assert _unchanged(committed, result, ("transfer-roundtrip", "zephyr-3.7"))
    assert not result.ok
    assert any("no positive control PASSED" in why for why in result.refused), result.refused
    assert any("zephyr37_lfs" in why for why in result.refused), result.refused


def test_a_cell_with_no_positive_control_record_at_all_is_refused(committed):
    """The same refusal when the control never RAN, which is the commoner case.

    A run that selected only the contracts leaves no control record, and the
    honest answer is that nothing showed the observable could go red -- not
    that the control is assumed to have passed because it exists in the tree.
    """
    result = collate(committed, [r for r in _mixed_records() if r["kind"] != CONTROL])
    assert _unchanged(committed, result, ("transfer-roundtrip", "zephyr-3.7"))
    assert any("no positive control PASSED" in why for why in result.refused), result.refused


def test_a_broken_cells_passing_route_must_have_its_own_passing_control(committed):
    """★ THE DEFECT THIS TASK EXISTS FOR, as the collator's refusal.

    ``bb1161`` moves files over ``shell`` and not over ``nc``, so the cell is
    honestly ``measured-broken`` and the page says *"Only over ``shell``. You
    can put a file on the device and get the same bytes back over ``shell``"*.
    That is a ``measured-ok``-strength claim about one route. Here the
    ``shell`` CONTROL fails while the ``shell`` contract passes -- so nothing
    showed the roundtrip comparison could reject a wrong answer there, and the
    contract's pass beside it proves nothing.

    THE OLD COLLATOR WROTE THIS CELL. It reached the citation check only for a
    whole-cell ``positive_control``, found the ``nc`` route uncontrolled,
    omitted the field and published the row anyway -- with its positive
    sentence intact and no control named. Refused now, not downgraded: the
    cell keeps whatever it had and the collation reports non-``ok``.

    The ``nc`` half is left exactly as a real lane leaves it (contract and
    control both xfailed), so the refusal can only be about the ``shell``
    route.
    """
    records = []
    for transfer, obs, ctl in (("shell", PASSED, FAILED), ("nc", XFAILED, XFAILED)):
        label = _bed_label("bb1161", "telnet", transfer)
        records += [
            _observation("bb1161", label=label, outcome=obs),
            _control("bb1161", label=label, outcome=ctl),
        ]
    result = collate(committed, records)
    assert _unchanged(committed, result, TRANSFER_X_BUSYBOX1161)
    assert not result.ok
    assert any("no positive control PASSED" in why for why in result.refused), result.refused
    assert any("bed-busybox[bb1161:telnet:shell]" in why for why in result.refused), result.refused
    assert not any("bed-busybox[bb1161:telnet:nc]" in why for why in result.refused), (
        "the refusal named the FAILING route, which never claims anything positive -- "
        "requiring a control there would make an honest broken cell unwritable"
    )


def test_a_mixed_cell_cites_the_control_for_the_route_it_still_claims(committed):
    """★ THE OTHER HALF, and the one a refusal guard alone cannot prove.

    A collator that refused every mixed cell would satisfy the row above and
    publish nothing. This is the real bed shape -- contract and control both
    pass over ``shell``, both xfail over ``nc`` -- and the cell must come out
    ``measured-broken`` with the ``shell`` route naming its own control and
    the ``nc`` route naming none.

    The expected nodeid is written OUT IN FULL rather than rebuilt from
    ``positive_control_for`` and the label, for task 4b's reason: comparing
    the output against the helper that built its input is a guard that agrees
    with itself even when both are wrong.
    """
    records = []
    for transfer, outcome in (("shell", PASSED), ("nc", XFAILED)):
        label = _bed_label("bb1161", "telnet", transfer)
        records += [
            _observation("bb1161", label=label, outcome=outcome),
            _control("bb1161", label=label, outcome=outcome),
        ]
    matrix = collate(committed, records).matrix
    cell = matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    assert cell["status"] == "measured-broken"
    cited = {entry["transfer"]: entry.get("positive_control") for entry in cell["observed_cells"]}
    assert cited == {
        "shell": (
            "tests/conformance/test_transfer_contract.py"
            "::test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
            "[bed-busybox[bb1161:telnet:shell]]"
        ),
        "nc": None,
    }, cell["observed_cells"]
    # AND THE CELL STILL CANNOT MAKE THE WHOLE-CELL CLAIM. The cell-level field
    # means "every contributing route was controlled", which is false here; the
    # per-route field is what carries the half that IS backed.
    assert "positive_control" not in cell
    assert _at(positive_control_errors(matrix), TRANSFER_X_BUSYBOX1161) == []


def test_a_routes_citation_is_the_controls_nodeid_and_not_the_contracts(committed):
    """READ off the control record, never taken from the observation beside it.

    The two records share a cell and differ in exactly one field that matters
    here, and a collator reaching for ``record["nodeid"]`` on the wrong one
    would produce a citation that is well formed, really collected and names
    the test whose pass is the thing being vouched FOR.
    """
    label = _bed_label("bb1161", "telnet", "shell")
    records = [_observation("bb1161", label=label), _control("bb1161", label=label)]
    cell = collate(committed, records).matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    entry = cell["observed_cells"][0]
    assert (
        "test_control_the_roundtrip_comparison_rejects_a_corrupted_byte"
        in (entry["positive_control"])
    )
    assert entry["positive_control"] != cell["nodeid"]
    assert not entry["positive_control"].startswith(f"{cell['nodeid']}[")


def test_a_failing_route_whose_control_passed_still_cites_it(committed):
    """★ THE COLLATOR REPORTS THE RECORD, IT DOES NOT DECIDE WHAT THE PAGE NEEDS.

    I first wrote this guard the other way round -- a route the contract did
    not pass on makes no positive claim, so a citation there "would be evidence
    attached to nothing" -- and the implementation disagreed with me. The
    implementation is right. ``_observed_cells``' whole discipline is that
    every value is LIFTED FROM THE RECORD; withholding a control record that
    really passed, because of a judgement about what a renderer will say, is
    the collator editorialising over its own evidence.

    And the state it reports is worth reporting. A contract that failed beside
    a control that PASSED says the instrument could tell a wrong answer from a
    right one on that very route -- so the failure is the product's, not the
    check's blindness. That is a stronger broken claim than "everything here
    failed", and the schema permits the field on a failing entry for exactly
    this reason.

    Not the bed's shape today: a strict xfail xfails contract and control
    together, so all ten real ``nc`` routes have no passing control. INJECTED
    rather than inherited, which is what makes this about the rule instead of
    about today's lane.
    """
    records = []
    for transfer, obs in (("shell", PASSED), ("nc", XFAILED)):
        label = _bed_label("bb1161", "telnet", transfer)
        records += [
            _observation("bb1161", label=label, outcome=obs),
            _control("bb1161", label=label, outcome=PASSED),
        ]
    matrix = collate(committed, records).matrix
    cell = matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    cited = {entry["transfer"]: entry.get("positive_control") for entry in cell["observed_cells"]}
    assert cited["nc"] is not None, cited
    assert cited["nc"].endswith("[bed-busybox[bb1161:telnet:nc]]"), cited
    assert cited["shell"] is not None, cited
    assert cited["shell"].endswith("[bed-busybox[bb1161:telnet:shell]]"), cited
    assert _at(positive_control_errors(matrix), TRANSFER_X_BUSYBOX1161) == []
    # EVERY contributing route IS controlled here, so the whole-cell claim is
    # available even though the cell is broken -- the state the schema permits
    # and the collator had no way to reach before.
    assert "positive_control" in cell
    # AND THE PAGE MUST NOT DROP IT. The row claims nothing about `nc`, so a
    # renderer keyed on "routes the page promises" would silently lose a piece
    # of evidence the artifact holds -- which is this page's own subject.
    block = _evidence_block(
        _page(_with_cell(committed, cell, TRANSFER_X_BUSYBOX1161)[0]),
        "transfer-roundtrip",
        "busybox-1.16.1",
    )
    assert f"*Positive control over `nc`:* `{cited['nc']}`" in block, block


def test_a_control_from_another_surface_cannot_stand_in(committed):
    """A control is evidence about ITS OWN surface, and the collator checks which.

    The framing control passing on a cell says nothing about whether the
    roundtrip comparison could reject a corrupted byte there. Here the control
    record is FILED under the transfer surface -- so the surface filter alone
    lets it through -- and only the nodeid gives it away.
    """
    framing_control = positive_control_for("exec-framing")
    records = [r for r in _mixed_records() if r["kind"] != CONTROL]
    records += [
        {
            **_control(element),
            "nodeid": f"{framing_control}[{_bed_label(element)}]",
        }
        for element in _ZEPHYR37_OBSERVED
    ]
    result = collate(committed, records)
    assert _unchanged(committed, result, ("transfer-roundtrip", "zephyr-3.7"))
    assert any("is not this surface's control" in why for why in result.refused), result.refused


def test_a_control_must_have_passed_on_that_very_cell_not_merely_that_element(committed):
    """★ THE PAIRING IS PER CELL. "Somewhere on this element" is not evidence either.

    ``test1`` has eight cells, one per ``(term, transfer)`` pair, and they run
    different transports behind different backends -- otto's ``nc`` gap on
    BusyBox is exactly a per-CELL defect. So a control that proved the
    observable can move over ``ssh:scp`` says nothing about ``ssh:nc``, and a
    collator pairing per element would cite the first as evidence for the
    second.

    The hostile condition is INJECTED: the contract passes on BOTH cells and
    the control only on one, so a per-element pairing writes ``measured-ok``
    here and a per-cell pairing refuses. Its companion below requires the
    fully-controlled version to be ACCEPTED, without which this row would be
    satisfied by a collator that refuses everything.
    """
    records = []
    for transfer in ("scp", "nc"):
        label = _bed_label("test1", "ssh", transfer)
        records.append(_observation("test1", label=label))
    records.append(_control("test1", label=_bed_label("test1", "ssh", "scp")))

    result = collate(committed, records)
    assert _unchanged(committed, result, ("transfer-roundtrip", "gnu"))
    assert any("ssh:nc" in why for why in result.refused), result.refused

    records.append(_control("test1", label=_bed_label("test1", "ssh", "nc")))
    accepted = collate(committed, records)
    assert accepted.refused == []
    assert accepted.matrix["cells"]["transfer-roundtrip"]["gnu"]["status"] == "measured-ok"


def test_control_records_alone_cannot_move_a_verdict(committed):
    """A control's outcome is about the INSTRUMENT, never about the host.

    A run that somehow produced only controls for a cell has measured nothing
    about that cell, and the collator says so rather than reading a green
    instrument as a green host.
    """
    controls = [_control(element) for element in _ZEPHYR37_OBSERVED]
    result = collate(committed, controls)
    assert _unchanged(committed, result, ("transfer-roundtrip", "zephyr-3.7"))
    assert any("only positive-control records" in why for why in result.refused), result.refused


def test_a_cell_with_no_declared_observable_is_refused(committed):
    """A surface whose contract declares no observable can never be ``measured-ok``.

    The ruling of 2026-08-24: deriving the observable from the surface id would
    make the field a restatement of the cell's own key, which is worse than
    absent -- §5 asks for it precisely because a surface's observable differs by
    environment. So a record carrying ``observable: null`` (the emitter's honest
    answer for an undeclared contract) refuses the whole cell rather than
    letting it publish a verdict that cannot say what was watched.
    """
    records = [
        {**record, "observable": None} if record["kind"] == "observation" else record
        for record in _mixed_records()
    ]
    result = collate(committed, records)
    assert _unchanged(committed, result, ("transfer-roundtrip", "zephyr-3.7"))
    assert any("no observable was declared" in why for why in result.refused), result.refused


def test_two_cells_of_one_element_watching_different_things_both_reach_the_verdict(committed):
    """An element has SEVERAL cells, and they can watch genuinely different things.

    ``test1`` alone has eight, one per ``(term, transfer)`` pair, and
    ``put(mode=...)`` is read back with ``stat -c %a`` where the backend carries
    a permission model and is a pre-flight REFUSAL where it does not. Taking
    whichever record came first would publish one cell's observable as the whole
    profile's, so both are carried and attributed.
    """
    records = []
    for element, transfer, observable in (
        ("test1", "scp", "the landed mode"),
        ("test1", "sftp", "the landed mode"),
        ("test2", "nc", "the refusal"),
    ):
        label = _bed_label(element, "ssh", transfer)
        records += [
            _observation(element, surface="transfer-mode", label=label, observable=observable),
            _control(element, surface="transfer-mode", label=label),
        ]
    cell = collate(committed, records).matrix["cells"]["transfer-mode"]["gnu"]
    assert cell["status"] == "measured-ok"
    assert cell["observed_on"] == ["test1", "test2"]
    assert "the landed mode (on test1)" in cell["observable"], cell["observable"]
    assert "the refusal (on test2)" in cell["observable"], cell["observable"]


def test_one_observable_seen_everywhere_carries_no_pointless_attribution(committed):
    """An attribution that names every observed element adds nothing, so it is dropped.

    MEASURED on the first real bed collation, which is the only reason this
    rule exists: ``gnu`` x transfer-mode watches four observables, one per
    transfer backend, and each was seen on all four elements -- so attributing
    every one of them produced a 900-character field listing 32 cell labels
    that said nothing the observables did not already say. The
    ``(on ...)`` clause is for the case a reader CANNOT reconstruct.
    """
    records = []
    for element in ("test1", "test2"):
        for transfer in ("scp", "nc"):
            label = _bed_label(element, "ssh", transfer)
            observable = f"the mode read back over `{transfer}`"
            records += [
                _observation(element, surface="transfer-mode", label=label, observable=observable),
                _control(element, surface="transfer-mode", label=label),
            ]
    cell = collate(committed, records).matrix["cells"]["transfer-mode"]["gnu"]
    assert "the mode read back over `scp`" in cell["observable"]
    assert "the mode read back over `nc`" in cell["observable"]
    assert "(on " not in cell["observable"], (
        f"both observables were seen on every observed element, so neither needs an "
        f"attribution: {cell['observable']!r}"
    )


def test_one_failing_cell_makes_the_whole_element_broken(committed):
    """otto's registered ``nc-transfer`` gap, in the artifact's own terms.

    ``bb1161`` transfers fine over ``shell`` and fails over ``nc``. An element
    is ``ok`` only if EVERY evidential record for it passed, so this cell is
    ``measured-broken`` -- a collator that took the passing half would publish
    ``measured-ok`` for a profile whose transfer is measurably broken, which is
    the gap otto has registered and open.
    """
    records = []
    for transfer, outcome in (("shell", PASSED), ("nc", XFAILED)):
        label = _bed_label("bb1161", "telnet", transfer)
        records += [
            _observation("bb1161", label=label, outcome=outcome),
            _control("bb1161", label=label, outcome=outcome),
        ]
    cell = collate(committed, records).matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    assert cell["status"] == "measured-broken"
    assert cell["observed_on"] == ["bb1161"]
    assert "telnet:nc" in cell["failure_summary"], cell["failure_summary"]
    assert "positive_control" not in cell, (
        "the control xfailed on the nc cell too, so this cell may not cite one"
    )


def test_the_collate_step_really_does_cut_at_the_cap_the_page_names(committed):
    """The other half of the page's cut marker, and it was unguarded.

    `scripts/render_support_matrix.py` prints "cut short by the collate step at N
    characters" from a LENGTH COMPARISON against `FAILURE_SUMMARY_LIMIT`. That
    sentence is only true if the collate step is what did the cutting -- a collator
    that stopped truncating would publish a longer summary and the page would announce
    a cut that never happened, with no guard the wiser: today's real summaries are
    780 characters, comfortably under the cap, so nothing inherits the condition.

    So it is INJECTED: a record whose reason is far longer than the cap, and the cell
    the collator writes must be exactly the cap, not the whole text.
    """
    label = _bed_label("bb1161", "telnet", "nc")
    long_reason = "call: expected failure -- " + "a very long reason. " * 300
    records = [
        _observation("bb1161", label=label, outcome=XFAILED, failure_summary=long_reason),
        _control("bb1161", label=label, outcome=XFAILED),
    ]
    cell = collate(committed, records).matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    assert cell["status"] == "measured-broken"
    assert len(cell["failure_summary"]) == FAILURE_SUMMARY_LIMIT, (
        f"the collate step wrote {len(cell['failure_summary'])} characters while the "
        f"page tells a reader it cuts at {FAILURE_SUMMARY_LIMIT}"
    )
    assert "cut short by the collate step" in _evidence_block(
        _page(collate(committed, records).matrix), "transfer-roundtrip", "busybox-1.16.1"
    )


def test_the_broken_busybox_cell_says_which_transport_broke_and_which_did_not(committed):
    """★ THE RULING OF 2026-08-24, and the row it was measured on.

    ``busybox-1.16.1`` x transfer-roundtrip is honestly ``measured-broken``,
    and a reader scanning that alone concludes otto cannot move files to a
    BusyBox 1.16.1 device. It can: over ``shell`` the roundtrip passes, and
    only ``nc`` fails, against a gap otto registered on 2026-08-13. Before
    this field the split lived in the cell only as prose -- a pipe-joined
    ``observable`` and an English ``failure_summary`` -- and a renderer that
    recovers structure by parsing English is one that will silently stop.

    Asserted as the PAIR, not as "an entry exists": the claim is that the two
    transports carry DIFFERENT outcomes, which is what a collator inferring an
    outcome from the cell's own verdict cannot produce.
    """
    records = []
    for transfer, outcome in (("shell", PASSED), ("nc", XFAILED)):
        label = _bed_label("bb1161", "telnet", transfer)
        records += [
            _observation("bb1161", label=label, outcome=outcome),
            _control("bb1161", label=label, outcome=outcome),
        ]
    matrix = collate(committed, records).matrix
    cell = matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    assert cell["status"] == "measured-broken"
    assert {entry["transfer"]: entry["outcome"] for entry in cell["observed_cells"]} == {
        "shell": PASSED,
        "nc": XFAILED,
    }, cell["observed_cells"]
    # LITERAL LABELS, not `_bed_label(...)` again. Comparing the output against
    # the same helper that built the input is a guard that cannot fail: it
    # would agree with itself even while both spelled a cell the bed never
    # draws -- which is precisely what that helper used to do for every BusyBox
    # guest. `cell_outcome_errors` resolves them against the real bed space.
    assert [entry["cell_label"] for entry in cell["observed_cells"]] == [
        "bed-busybox[bb1161:telnet:nc]",
        "bed-busybox[bb1161:telnet:shell]",
    ]
    assert _at(cell_outcome_errors(matrix), TRANSFER_X_BUSYBOX1161) == []


def test_the_breakdown_names_only_the_cells_a_run_actually_drew(committed):
    """★ READ, NEVER RECONSTRUCTED. The mutation this is written against.

    ``bb1161`` has exactly two bed cells. A collator that built the breakdown
    from the bed SPACE -- element in ``observed_on``, therefore every label
    that element draws -- would satisfy the schema, the accounting and the
    per-cell cross-references, while publishing a result for a transport no
    run touched. That is task 4's constructed-``positive_control`` finding in
    a new field, and the only thing that separates the two collators is a run
    that drew ONE of the two cells.
    """
    label = "bed-busybox[bb1161:telnet:shell]"
    records = [_observation("bb1161", label=label), _control("bb1161", label=label)]
    cell = collate(committed, records).matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    assert cell["status"] == "measured-ok"
    assert [entry["cell_label"] for entry in cell["observed_cells"]] == [label], (
        "the nc cell was never drawn, so nothing may be published about it -- "
        "not a pass, and not a placeholder"
    )


def test_the_breakdown_carries_each_cells_own_axes_and_not_the_first_cells(committed):
    """``test1`` alone draws eight cells over four transfer backends.

    A breakdown that collapsed them -- or that took the first cell's transport
    for the element -- would restore exactly the uniform reading the field
    exists to retire, this time inside one device rather than across a
    profile.
    """
    records = []
    for transfer in ("scp", "sftp", "nc"):
        label = _bed_label("test1", "ssh", transfer)
        records += [
            _observation("test1", surface="transfer-mode", label=label),
            _control("test1", surface="transfer-mode", label=label),
        ]
    cell = collate(committed, records).matrix["cells"]["transfer-mode"]["gnu"]
    assert [entry["transfer"] for entry in cell["observed_cells"]] == ["nc", "scp", "sftp"]
    assert {entry["term"] for entry in cell["observed_cells"]} == {"ssh"}
    assert {entry["element"] for entry in cell["observed_cells"]} == {"test1"}


def test_a_not_observable_cell_states_an_empty_breakdown_rather_than_omitting_it(committed):
    """An absent key and an empty list are different statements.

    The whole-profile exclusion below has no observations at all, so the honest
    breakdown is empty -- and the schema requires the key so that saying "no
    cell produced a result here" is not spelled the same way as silence.
    """
    records = [_exclusion(element, surface="timeout") for element in ("zephyr37_fat",)]
    cell = collate(committed, records).matrix["cells"]["timeout"]["zephyr-3.7"]
    assert cell["status"] == "not-observable"
    assert cell["observed_cells"] == []


def test_a_whole_profile_outside_a_contracts_domain_becomes_not_observable(committed):
    """All three Zephyr profiles are outside the TIMEOUT contract's domain.

    ``observed_on`` is empty and ``not_observable`` names every element with
    what was probed -- the one state the schema permits an empty ``observed_on``
    for, and the state §5 refuses to let collapse into ``untested``.
    """
    records = [
        _exclusion(element, surface="timeout")
        for element in ("zephyr37_fat", "zephyr37_lfs", "zephyr37_nofs", "zephyr37_llext")
    ]
    cell = collate(committed, records).matrix["cells"]["timeout"]["zephyr-3.7"]
    assert cell["status"] == "not-observable"
    assert cell["observed_on"] == []
    assert len(cell["not_observable"]) == 4
    assert cell["probed"]
    assert cell["probe_result"]


def test_a_skipped_observation_is_not_evidence(committed):
    """A skip inside a drawn cell reports success for a contract nobody ran.

    That is this suite's own rule, and the collator carries it: a non-evidential
    outcome is discarded with a reason, and a cell left with no evidence is left
    alone rather than downgraded.
    """
    records = [
        {**record, "outcome": SKIPPED, "evidential": False}
        if record["kind"] == "observation"
        else record
        for record in _mixed_records()
    ]
    result = collate(committed, records)
    assert result.matrix["cells"]["transfer-roundtrip"]["zephyr-3.7"]["status"] == "not-observable"
    assert any("statement about the RUN" in bucket.reason for bucket in result.discarded)


def test_a_collated_artifact_passes_every_offline_guard(validator, committed):
    """The end-to-end claim: what collation writes survives the guards, together.

    Not a restatement of the per-field tests above. Those inject one defect at a
    time into a HAND-WRITTEN cell; this asserts that a cell the collator really
    produced satisfies the schema, the element accounting AND the positive-control
    resolution at once -- including the resolution against a real collection,
    which is what a plausible-looking nodeid fails.
    """
    matrix = collate(committed, _mixed_records()).matrix
    assert _messages(validator, matrix) == []
    assert element_accounting_errors(matrix) == []
    assert cell_outcome_errors(matrix) == []
    assert positive_control_errors(matrix, collect_conformance_nodeids(bed=True)) == []


# --------------------------------------------------------------------------
# ...and collation is the ONLY writer of a verdict
# --------------------------------------------------------------------------


def test_the_collator_never_commits():
    """CI never commits the matrix, and neither does this script: a person does.

    Spec §5 puts a human in front of every verdict. The strongest form of that
    is a script with no way to commit at all, so this refuses the whole
    vocabulary rather than one spelling -- ``git``, ``subprocess`` and
    ``os.system`` are each absent, and adding any of them fails here rather than
    in review.
    """
    source = COLLATOR_PATH.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "*"))
    )
    for forbidden in ("subprocess", "os.system", "os.exec", '"git"', "'git'"):
        assert forbidden not in body, (
            f"{COLLATOR_PATH.name} reaches for {forbidden!r}; the collate step writes a "
            f"file and stops -- Chris commits every update and CI never commits"
        )


def test_the_collate_step_writes_nothing_without_being_asked(tmp_path, committed):
    """The default is a REPORT. A run meant only to look cannot move a verdict."""
    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(committed, indent=2) + "\n", encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    for index, record in enumerate(_mixed_records()):
        (records / f"{index}.json").write_text(json.dumps(record), encoding="utf-8")

    before = matrix.read_text(encoding="utf-8")
    assert collate_main(["--records", str(records), "--matrix", str(matrix)]) == 0
    assert matrix.read_text(encoding="utf-8") == before, "a report-only run WROTE"

    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 0
    written = json.loads(matrix.read_text(encoding="utf-8"))
    assert written["cells"]["transfer-roundtrip"]["zephyr-3.7"]["status"] == "measured-ok"


def test_the_collate_step_refuses_to_write_over_a_self_contradicting_artifact(tmp_path, committed):
    """★ THE WIRING GUARD for the per-CELL check `main` runs before writing.

    It is a cross-reference the schema cannot make, so if `main` stopped
    calling it the collator would happily write a file whose own per-transport
    evidence contradicts the elements beside it, and a reader would meet the
    contradiction before any guard did.

    THE HOSTILE CONDITION IS INJECTED, not inherited, and it is injected
    NARROWLY. Today's collator cannot produce a contradiction, so one is
    hand-written into a cell the collation does not touch (there are no
    `timeout` records here, and rule 2 leaves an undrawn cell byte-identical).
    The first draft of this poison also put a Zephyr element in the `gnu`
    column, which `element_accounting_errors` catches -- so the guard would
    have stayed GREEN with the per-cell check removed from `main`, passing on
    evidence it is not about. This poison violates ONLY the per-cell check:
    real gnu element, real control on a real cell of it, and a breakdown
    naming a DIFFERENT device.

    The run must exit 2 and write NOTHING -- not the good cells either,
    because a partial write would put half a refused artifact on disk.
    """
    poisoned = copy.deepcopy(committed)
    poisoned["cells"]["timeout"]["gnu"] = {
        "status": "measured-ok",
        "nodeid": next(s.contract for s in SURFACES if s.id == "timeout"),
        "venue": "bed",
        "as_of": "2026-08-25",
        "observable": "how long a command that outlives its budget takes to come back",
        "positive_control": f"{positive_control_for('timeout')}[bed-unix[test1:ssh:scp]]",
        "observed_on": ["test1"],
        "observed_cells": [
            {
                "cell_label": "bed-unix[test2:ssh:scp]",
                "element": "test2",
                "term": "ssh",
                "transfer": "scp",
                "outcome": "passed",
            }
        ],
        "not_observable": [],
    }
    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(poisoned, indent=2) + "\n", encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    for index, record in enumerate(_mixed_records()):
        (records / f"{index}.json").write_text(json.dumps(record), encoding="utf-8")

    before = matrix.read_text(encoding="utf-8")
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 2
    assert matrix.read_text(encoding="utf-8") == before, "a REFUSED collation wrote anyway"


def test_the_collate_step_refuses_an_artifact_that_double_counts_a_device(tmp_path, committed):
    """★ THE WIRING GUARD for the per-ELEMENT check `main` runs before writing.

    MEASURED as M19 during task 4b and reported GREEN: dropping
    `element_accounting_errors` from `scripts/collate_support_matrix.py`'s `main` left
    the whole suite passing. The function was exercised directly by several guards and
    ran in production, and NOTHING asserted that `main` still called it -- a guard that
    cannot fail, in its purest form. This is that measurement closed.

    THE HOSTILE CONDITION IS INJECTED, and it is injected so that it violates ONLY the
    per-element check -- the sibling guard above records what happens when a poison is
    caught by the wrong checker, and this one is narrowed the same way:

    * `observed_on` and `observed_cells` name the SAME real gnu device on a real drawn
      cell, so `cell_outcome_errors` is satisfied;
    * `positive_control` is this surface's real control parametrized on a real cell of
      that same device, so `positive_control_errors` is satisfied;
    * the device is ALSO filed under `not_observable`, which only
      `element_accounting_errors` can see -- it is a cross-reference between a cell and
      the `profiles` entry it sits under, and no schema can follow that pointer.

    Why double-counting is the right poison rather than a foreign element: it is the one
    that makes a RENDERED SENTENCE false. `docs/architecture/support-matrix.md` derives
    "measured on 2 of 4 devices, 2 could not be measured" from exactly these two lists,
    and a device in both is counted twice -- the page would then account for more
    devices than the profile has.

    `timeout` x `gnu` again, for the sibling's reason: `_mixed_records()` holds no
    timeout record, and rule 2 leaves an undrawn cell byte-identical, so the poison
    survives collation to be met by the check.
    """
    poisoned = copy.deepcopy(committed)
    poisoned["cells"]["timeout"]["gnu"] = {
        "status": "measured-ok",
        "nodeid": next(s.contract for s in SURFACES if s.id == "timeout"),
        "venue": "bed",
        "as_of": "2026-08-25",
        "observable": "how long a command that outlives its budget takes to come back",
        "positive_control": f"{positive_control_for('timeout')}[bed-unix[test1:ssh:scp]]",
        "observed_on": ["test1"],
        "observed_cells": [
            {
                "cell_label": "bed-unix[test1:ssh:scp]",
                "element": "test1",
                "term": "ssh",
                "transfer": "scp",
                "outcome": "passed",
            }
        ],
        "not_observable": [
            {
                "element": "test1",
                "probed": "tests/conformance/test_timeout_contract.py::applicable_cell"
                "(bed-unix[test1:ssh:scp])",
                "probe_result": "False -- invented, and contradicting observed_on above",
            }
        ],
    }
    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(poisoned, indent=2) + "\n", encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    for index, record in enumerate(_mixed_records()):
        (records / f"{index}.json").write_text(json.dumps(record), encoding="utf-8")

    # The poison is invisible to the other two checkers -- so if `main` stops calling
    # this one, nothing else catches it and the refusal below never happens.
    assert cell_outcome_errors(poisoned) == []
    assert positive_control_errors(poisoned) == []
    assert element_accounting_errors(poisoned) != []

    before = matrix.read_text(encoding="utf-8")
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 2
    assert matrix.read_text(encoding="utf-8") == before, "a REFUSED collation wrote anyway"


def test_the_collate_step_refuses_an_artifact_whose_route_cites_the_wrong_cell(tmp_path, committed):
    """★ THE WIRING GUARD for the POSITIVE-CONTROL check `main` runs before writing.

    MEASURED GREEN and closed here. Task 4b found ``element_accounting_errors``
    unwired in ``main`` with nothing to notice (M19) and task 5 closed it; task
    4b's own ``cell_outcome_errors`` got a wiring guard when it was added. THE
    THIRD CHECKER NEVER DID. Dropping ``positive_control_errors`` from
    ``main`` left all 189 guards passing -- it is task 4's line, it has run in
    production since, and nothing asserted that ``main`` still called it. The
    per-route check added on 2026-08-25 rides the same call, so its production
    wiring was unguarded from the moment it existed.

    THE POISON VIOLATES ONLY THIS CHECKER, narrowed the way both siblings
    record: a real gnu device, a real drawn cell of it in ``observed_cells``,
    no double-counting -- and the ROUTE cites this surface's real control
    parametrized on ANOTHER CELL OF THAT SAME DEVICE. ``test1`` draws eight,
    so the citation is well formed, really collected, and about a different
    transport than the route it backs. That is invisible to the other two.
    """
    control = positive_control_for("timeout")
    poisoned = copy.deepcopy(committed)
    poisoned["cells"]["timeout"]["gnu"] = {
        "status": "measured-ok",
        "nodeid": next(s.contract for s in SURFACES if s.id == "timeout"),
        "venue": "bed",
        "as_of": "2026-08-25",
        "observable": "how long a command that outlives its budget takes to come back",
        "positive_control": f"{control}[bed-unix[test1:ssh:scp]]",
        "observed_on": ["test1"],
        "observed_cells": [
            {
                "cell_label": "bed-unix[test1:ssh:scp]",
                "element": "test1",
                "term": "ssh",
                "transfer": "scp",
                "outcome": "passed",
                # THE ONE THING WRONG: `sftp`'s control, cited under `scp`.
                "positive_control": f"{control}[bed-unix[test1:ssh:sftp]]",
            }
        ],
        "not_observable": [],
    }
    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(poisoned, indent=2) + "\n", encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    for index, record in enumerate(_mixed_records()):
        (records / f"{index}.json").write_text(json.dumps(record), encoding="utf-8")

    # Invisible to the other two, so only the check this guard names can refuse it.
    assert cell_outcome_errors(poisoned) == []
    assert element_accounting_errors(poisoned) == []
    assert positive_control_errors(poisoned) != []

    before = matrix.read_text(encoding="utf-8")
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 2
    assert matrix.read_text(encoding="utf-8") == before, "a REFUSED collation wrote anyway"


def test_the_same_records_over_a_clean_artifact_do_write(tmp_path, committed):
    """The control for the refusal above: the records themselves are fine.

    Without it, a `main` that exited 2 on every run would satisfy the guard it
    controls, and the refusal would be proving nothing about the contradiction
    it names.
    """
    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(committed, indent=2) + "\n", encoding="utf-8")
    records = tmp_path / "records"
    records.mkdir()
    for index, record in enumerate(_mixed_records()):
        (records / f"{index}.json").write_text(json.dumps(record), encoding="utf-8")
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 0


def test_no_ci_workflow_runs_the_collate_step():
    """The collate step hangs off `make conformance-bed`, which no workflow invokes.

    CI has no lab, so every record it could produce is hermetic and discarded --
    running the step there would print a 48-record discard and change nothing,
    while implying to a reader that CI collates. Checked over every workflow
    rather than the one nightly job, so a future workflow cannot pick it up
    quietly.
    """
    workflows = sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found, so this guard would pass vacuously"
    for path in workflows:
        # COMMENTS ARE STRIPPED, and that is not a loophole: nightly.yml
        # explains at length why the bed lane is not and cannot be run in CI,
        # naming `make conformance-bed` to do it. A guard that could not tell a
        # prose mention from an invocation would force that explanation out of
        # the file it belongs in.
        commands = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        for forbidden in (
            "collate_support_matrix",
            "support-matrix",
            "conformance-bed",
            "release-matrix",
        ):
            offending = [line.strip() for line in commands if forbidden in line]
            assert offending == [], f"{path.name} runs the collate step: {offending}"


def test_the_downgrade_gate_reads_the_artifact_and_never_writes_it():
    """What stops the gate's allow-listing above from being a hole.

    It is exempted from the `measured-*` literal scan because classifying a
    transition means naming the states -- so the exemption has to be paid for
    behaviourally. Both halves are checked: the source reaches for no way to
    write, and a real run over the committed artifact leaves it byte-identical.
    A gate that could write would be a second minter with an exemption already
    granted, which is the worst of both.
    """
    source = DOWNGRADE_GATE_PATH.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "*"))
    )
    for forbidden in ("write_text", "write_bytes", "json.dump", "open(", "shutil"):
        assert forbidden not in body, (
            f"{DOWNGRADE_GATE_PATH.name} reaches for {forbidden!r}; the gate classifies "
            f"and prints -- only the collate step may write a verdict"
        )

    before = MATRIX_PATH.read_bytes()
    assert gate_main(["--baseline", str(MATRIX_PATH), "--candidate", str(MATRIX_PATH)]) == 0
    assert MATRIX_PATH.read_bytes() == before, "running the gate rewrote the committed matrix"


def test_the_release_refreshes_the_matrix_before_it_builds_the_docs():
    """Ordering is load-bearing, not taste.

    `SPHINX_SRCS` lists `schemas/support_matrix.json`, so `make docs` renders the
    support-matrix page FROM that file. A release that re-measured AFTER building
    its docs would publish a page disagreeing with the artifact it commits in the
    same release, and nothing else would notice: both halves would be internally
    consistent and the page would simply be one release stale.
    """
    body = (
        (PROJECT_ROOT / "Makefile")
        .read_text(encoding="utf-8")
        .split("\nrelease:", 1)[1]
        .split("\n\n", 1)[0]
    )
    stages = re.findall(r"\$\(MAKE\) ([a-z-]+)", body)
    assert "release-matrix" in stages, (
        f"the release no longer refreshes the support matrix; stages: {stages}"
    )
    assert stages.index("release-matrix") < stages.index("docs"), (
        f"the release must refresh the matrix BEFORE building the docs that render it; "
        f"stages: {stages}"
    )


def test_only_the_collator_ever_writes_a_measured_verdict():
    """No second writer of ``measured-*``, anywhere the artifact can be reached.

    Half of "collation is the only writer", and the half that IS checkable
    offline: ``tests/_fixtures/support_matrix.py``'s ``rewrite_matrix_axes``
    adds and removes CELLS as the tree changes and copies existing verdicts
    across, but it cannot mint one -- its only literal is ``untested``. A second
    minter added anywhere under ``src/``, ``scripts/`` or ``tests/_fixtures/``
    reddens here.

    ★ WHAT THIS DOES NOT CATCH, and the file's banner says it louder: a
    hand-edit of the JSON itself. This is a guard on the CODE.

    ★ THE RENDERER IS ALLOW-LISTED BY NAME, like the gap registry beside it, because
    the scan is by LITERAL and a page that renders four states has to name all four.
    Allow-listing by name rather than narrowing the scan keeps the collision visible;
    what stops the exemption from being a hole is the guard directly below, which
    proves behaviourally that rendering leaves the artifact byte-identical.

    ★ THE DOWNGRADE GATE IS ALLOW-LISTED FOR THE SAME REASON. It decides which
    transitions the release may auto-commit, so it must name the states it
    classifies. Its exemption is backed the same way, by the guard below it:
    the gate reads two files and prints, and running it leaves the artifact
    byte-identical.
    """
    roots = (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts", PROJECT_ROOT / "tests" / "_fixtures")
    exempt = (COLLATOR_PATH, GAP_REGISTRY_PATH, RENDERER_PATH, DOWNGRADE_GATE_PATH)
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in roots
        for path in root.rglob("*.py")
        if path not in exempt
        and any(literal in path.read_text(encoding="utf-8") for literal in _VERDICT_LITERALS)
    ]
    assert offenders == [], (
        f"{offenders} construct a `measured-*` verdict; only "
        f"{COLLATOR_PATH.relative_to(PROJECT_ROOT)} may"
    )


def test_the_gap_registry_uses_the_same_two_status_words_for_a_different_artifact():
    """★ A COLLISION WORTH KNOWING ABOUT, pinned rather than worked around.

    ``otto.host.userland`` already spells ``measured-broken`` and ``untested``
    -- they are the GAP REGISTRY's two statuses, rendered into
    ``docs/architecture/subsystems/busybox-support.md``. That is a different
    artifact with a different schema, and it is the reason the guard above
    allow-lists one file rather than narrowing its scope: an allow-list keeps
    the collision visible, and a narrowed scope would hide it.

    The two vocabularies AGREE in spirit -- "ran it, watched it fail, recorded
    what it said" is what a ``measured-broken`` matrix cell means too, and
    "untested, NOT unsupported" is the stance both take. What they do NOT share
    is the other two states: the matrix has ``measured-ok`` and
    ``not-observable`` and the registry has neither. A reader meeting both pages
    must be told which is which, which is why the rendered matrix page owes a
    division-of-labour note.
    """
    from otto.host import userland

    assert userland.MEASURED_BROKEN == "measured-broken"
    assert userland.UNTESTED == "untested"
    assert not hasattr(userland, "MEASURED_OK"), (
        "the gap registry has grown a third status; the matrix page's "
        "division-of-labour note needs revisiting"
    )


def test_no_committed_verdict_contradicts_this_machine_s_records():
    """★ THE ONLY GUARD THAT CAN CATCH A FABRICATED HAND-EDIT -- and it is CONDITIONAL.

    A `measured-ok` typed by hand -- a real element, a real control nodeid on a
    real cell of that element, an invented `observable` and today's date --
    satisfies the schema, `element_accounting_errors`, `positive_control_errors`
    and every other check in this file. The only thing that can tell it from a
    collated one is the EVIDENCE, so this re-collates this machine's records and
    requires the committed cell to be exactly what they produce.

    **DECLARED VACUITY.** The records are run output under a git-ignored
    `reports/` directory, so on a fresh checkout and in CI there are none and
    this asserts nothing. Committing them is the only way to make it
    unconditional, and a few hundred JSON files regenerated by every bed lane
    do not belong in git. It is NOT written as a skip -- a skip reports success
    for a check nobody ran, which is this suite's own rule -- so it makes the
    weaker claim it can always make and this docstring says which claim that is.

    Scoped to cells the present records actually SPEAK about. The directory is
    not cleared between runs but `make clean` empties it, so a verdict can
    legitimately outlive its evidence; only a cell the records DISAGREE with is
    a failure.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    records = [record for record in read_records(observations_dir()) if record.get("venue") == BED]
    reproduced = collate(matrix, records).matrix["cells"]
    spoken_for = {(record["surface"], record["profile"]) for record in records}
    contradicted = [
        f"{surface} x {profile}"
        for surface, profile in sorted(spoken_for)
        if surface in matrix["cells"]
        and profile in matrix["cells"][surface]
        and reproduced[surface][profile] != matrix["cells"][surface][profile]
    ]
    assert contradicted == [], (
        f"these committed verdicts are NOT what collating this machine's records "
        f"produces: {contradicted}. A hand-edited verdict is what this catches"
    )


# --------------------------------------------------------------------------
# The rendered page: a LOOKUP REFERENCE, and the five ways it could mislead
# --------------------------------------------------------------------------
#
# `docs/architecture/support-matrix.md` is generated at Sphinx `builder-inited`
# from the committed artifact. Chris asked for it as something to LOOK THINGS UP
# IN, so the failure modes these guards are about are all failures of READING,
# not of bookkeeping: a reader who takes a row at face value and points otto at
# the device it describes must not have been misled by it.
#
# Every guard below either INJECTS its hostile condition into a copy of the real
# artifact, or asserts a property over every cell that HAS it and refuses to run
# vacuously when no such cell exists. The distinction matters here more than
# usual: today's artifact happens to contain a transport split and a mixed
# profile, and a guard that merely inherited them would go quietly vacuous the
# next time the bed comes back all green.

_RENDERED_ON = datetime.date(2026, 9, 30)
"""A fixed render date, so a page compared against another page differs only
where the ARTIFACT differs. The real renderer stamps today's UTC date."""


def _page(matrix: dict) -> str:
    """Render *matrix* at a fixed date."""
    return render(matrix, rendered_on=_RENDERED_ON)


def _profile_block(page: str, profile_id: str) -> str:
    """Everything under one profile's ``###`` heading."""
    heading = f"\n### {profile_id}\n"
    assert page.count(heading) == 1, f"no single section for {profile_id!r}"
    rest = page[page.index(heading) + 1 :]
    following = rest.find("\n### ")
    return rest if following < 0 else rest[:following]


def _row(page: str, surface_id: str, profile_id: str) -> str:
    """The one table row a reader scanning *profile_id* meets for *surface_id*."""
    title = next(surface.title for surface in SURFACES if surface.id == surface_id)
    rows = [
        line
        for line in _profile_block(page, profile_id).splitlines()
        if line.startswith(f"| {title} |")
    ]
    assert len(rows) == 1, f"{len(rows)} rows for {surface_id} x {profile_id}"
    return rows[0]


def _grid_token(page: str, surface_id: str, profile_id: str) -> str:
    """The one at-a-glance word a hurried reader meets for *surface_id* x *profile_id*.

    Read out of the RENDERED page rather than by calling :func:`grid_token`, so the
    assertion is about what reaches a reader and not about what a function returned.
    """
    grid = page[page.index("## At a glance") : page.index("## The profiles")]
    rows = [
        line
        for line in grid.splitlines()
        if line.startswith("| {ref}`") and f"`{profile_id} <" in line.split("|")[1]
    ]
    assert len(rows) == 1, f"{len(rows)} grid rows for {profile_id!r}"
    column = 2 + [surface.id for surface in SURFACES].index(surface_id)
    return rows[0].split("|")[column].strip()


def _evidence_block(page: str, surface_id: str, profile_id: str) -> str:
    """The provenance bullets under a profile's table for one surface."""
    title = next(surface.title for surface in SURFACES if surface.id == surface_id)
    block = _profile_block(page, profile_id)
    start = block.index(f"- **{title}** -- ")
    rest = block[start + 1 :]
    following = rest.find("\n- **")
    return rest if following < 0 else rest[:following]


def _cells(matrix: dict):
    """Every ``(surface_id, profile_id, cell, coverage)`` in the artifact."""
    for surface in SURFACES:
        for profile in matrix["profiles"]:
            yield (
                surface.id,
                profile["id"],
                matrix["cells"][surface.id][profile["id"]],
                cell_coverage(matrix, surface.id, profile["id"]),
            )


def _distinguishing(group: "list[dict]", other: "list[dict]") -> "set[str]":
    """Axis values that belong to *group* and to no cell in *other*."""
    values: "set[str]" = set()
    for axis in ("element", "term", "transfer"):
        values |= {entry[axis] for entry in group} - {entry[axis] for entry in other}
    return values


def test_the_renderer_agrees_with_the_committed_artifact():
    """The control for every refusal below: today's tree and artifact DO agree.

    Without it, an :func:`axes_mismatch` that reported a problem unconditionally
    would satisfy all four refusal guards and prove nothing about the
    disagreements they name.
    """
    assert axes_mismatch(json.loads(MATRIX_PATH.read_text())) == []


def test_a_cell_whose_drawn_cells_disagreed_never_renders_as_one_answer():
    """★ THE TRANSPORT SPLIT, which a scalar status cannot say and this page must.

    `busybox-1.16.1` x `transfer-roundtrip` is `measured-broken` -- and the roundtrip
    WORKS over `shell`; only `nc` fails, against a gap otto already has registered.
    Rendered as a bare red row it tells a reader transfer is broken on a BusyBox 1.16.1
    device, which is false, and it is false in the expensive direction: they would go
    and build something else.

    Asserted as a PROPERTY over every cell whose drawn cells disagreed, and the
    property is that the row names something UNIQUE to each outcome group -- so a
    renderer that printed only the winners, or only the losers, or a single word for
    both, fails. `assert mixed` refuses to let it pass vacuously if the bed ever comes
    back uniform.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    page = _page(matrix)
    mixed = [
        (surface_id, profile_id, cell)
        for surface_id, profile_id, cell, _ in _cells(matrix)
        if len({entry["outcome"] for entry in cell.get("observed_cells", [])}) > 1
    ]
    assert mixed, "no cell in the artifact has disagreeing drawn cells; this guard is vacuous"
    grid = {
        line.split("|")[1].strip(): line
        for line in page[page.index("## At a glance") : page.index("## The profiles")].splitlines()
        if line.startswith("| {ref}`")
    }
    for surface_id, profile_id, cell in mixed:
        row = _row(page, surface_id, profile_id)
        # THE GRID IS READ FIRST, so it is held to the same rule. A grid saying
        # "broken" over a section saying "works over `shell`" misleads exactly the
        # reader who was in a hurry.
        grid_row = next(line for key, line in grid.items() if f"`{profile_id} <" in key)
        column = 2 + [surface.id for surface in SURFACES].index(surface_id)
        token = grid_row.split("|")[column].strip()
        passing = [e for e in cell["observed_cells"] if e["outcome"] == "passed"]
        others = [e for e in cell["observed_cells"] if e["outcome"] != "passed"]
        if passing and others:
            assert token not in ("broken", "works"), (
                f"{surface_id} x {profile_id}: the grid says {token!r} for a cell whose "
                f"drawn cells disagreed -- one word for two answers"
            )
        groups: "dict[str, list[dict]]" = {}
        for entry in cell["observed_cells"]:
            groups.setdefault(entry["outcome"], []).append(entry)
        for outcome, group in groups.items():
            others = [e for o, g in groups.items() if o != outcome for e in g]
            unique = _distinguishing(group, others)
            assert unique, f"{surface_id} x {profile_id}: {outcome} has no distinguishing axis"
            assert any(f"`{value}`" in row for value in unique), (
                f"{surface_id} x {profile_id}: the row does not name where {outcome!r} "
                f"was seen (any of {sorted(unique)}) -- a reader takes the status as "
                f"covering every route. Row was: {row}"
            )


def test_a_split_injected_where_there_was_none_is_still_named(committed):
    """The guard above reads real splits; this one MAKES one where the tree has none.

    `exec-exit-code` x `busybox-1.16.1` is green on both of its drawn cells today, so
    turning one red is a hostile condition that is INJECTED rather than inherited --
    which is what keeps the property from quietly going vacuous the day the bed comes
    back uniform, on a surface that has never had a split.
    """
    injected = copy.deepcopy(committed)
    cell = injected["cells"]["exec-exit-code"]["busybox-1.16.1"]
    assert {entry["outcome"] for entry in cell["observed_cells"]} == {"passed"}
    cell["status"] = "measured-broken"
    cell["failure_summary"] = "bed-busybox[bb1161:telnet:nc]: injected, for this guard"
    del cell["positive_control"]
    broken = next(entry for entry in cell["observed_cells"] if entry["transfer"] == "nc")
    broken["outcome"] = "failed"

    row = _row(_page(injected), "exec-exit-code", "busybox-1.16.1")
    assert "`shell`" in row, f"the injected split does not name what still works: {row}"
    assert "`nc`" in row, f"the injected split does not name what broke: {row}"
    assert "nothing predicted" in row, f"an injected surprise reads as routine: {row}"
    assert "**Only over `shell`.**" in row


def test_the_page_cites_the_control_behind_a_route_it_claims_positively(committed):
    """★ THE DEFECT, AS THE PAGE READS IT. Ten rows made a promise and named nothing.

    Every mixed row says *"Only over ``shell``. You can put a file on the
    device and get the same bytes back over ``shell``"* -- a claim of the same
    strength as a ``measured-ok`` verdict, about one route. MEASURED
    2026-08-25, all ten of them cited NO control, because the cell-level field
    needs every contributing route controlled and a mixed cell's failing route
    never is: the ``shell`` control had run and PASSED, its record was
    collected, and it was thrown away at citation time.

    Asserted as a property over every mixed cell in the artifact, and
    ``assert mixed`` refuses to let it pass vacuously if the bed ever comes
    back uniform. Its injecting twin below makes the same claim on a surface
    that has never had a split.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    page = _page(matrix)
    mixed = [
        (surface_id, profile_id, cell)
        for surface_id, profile_id, cell, _ in _cells(matrix)
        if len({entry["outcome"] for entry in cell.get("observed_cells", [])}) > 1
    ]
    assert mixed, "no cell in the artifact has disagreeing drawn cells; this guard is vacuous"
    for surface_id, profile_id, cell in mixed:
        block = _evidence_block(page, surface_id, profile_id)
        for entry in cell["observed_cells"]:
            if entry["outcome"] != "passed":
                continue
            named = entry.get("positive_control")
            assert named, (
                f"{surface_id} x {profile_id}: the row claims {entry['transfer']!r} works "
                f"and the artifact backs it with nothing"
            )
            assert f"`{named}`" in block, (
                f"{surface_id} x {profile_id}: the page claims {entry['transfer']!r} works "
                f"and never cites the control that proved the check could fail there. "
                f"Evidence was: {block}"
            )
            assert f"`{entry['transfer']}`" in block


def test_a_positive_route_claim_the_page_cannot_cite_is_not_made(committed):
    """★ THE OTHER DIRECTION, INJECTED: take the citation away and the promise must go.

    The artifact cannot produce this state -- the schema requires a passing
    route to name its control, and the collate step refuses a cell that cannot
    -- so the hostile condition is MADE rather than inherited. It is still a
    live path: the renderer runs from ``docs/conf.py`` over whatever
    ``schemas/support_matrix.json`` holds and validates nothing, so a
    hand-edit reaches this branch at ``make docs`` time.

    Both rows. Without the first, a renderer that never made the positive
    claim at all would satisfy the second.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    row = _row(_page(matrix), "transfer-roundtrip", "busybox-1.16.1")
    assert "**Only over `shell`.** You can put a file" in row, row

    injected = copy.deepcopy(matrix)
    cell = injected["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    stripped = next(e for e in cell["observed_cells"] if e["outcome"] == "passed")
    assert stripped.pop("positive_control", None), "nothing was taken away; the guard is vacuous"
    page = _page(injected)
    row = _row(page, "transfer-roundtrip", "busybox-1.16.1")
    assert "You can put a file" not in row, (
        f"the page promises a route it cannot cite -- the `measured-ok` requirement "
        f"evaded by making the same claim one level down. Row was: {row}"
    )
    assert "nothing proved it could fail there" in row, row
    assert "`shell`" in row, row
    assert "`nc`" in row, row
    block = _evidence_block(page, "transfer-roundtrip", "busybox-1.16.1")
    assert "*Positive control:* **none** over `shell`" in block, (
        f"the evidence hides why the sentence above it changed: {block}"
    )


def test_a_compact_citation_says_how_many_other_routes_carry_one(committed):
    """★ THE COMPACT LINE MAKES A CLAIM ABOUT ROUTES IT DOES NOT NAME, so it is checked.

    A ``measured-ok`` cell prints ONE nodeid, and the collate step guarantees
    the rest: the cell-level field cannot be written unless every contributing
    route had a control of its own pass on it. The clause beside it -- *"and
    each of the other 31 drawn cells carries a control of its own"* -- is how
    a reader learns that a single citation is not standing in for 32
    unchecked routes. It is a positive claim about routes, so it may not be
    decorative.

    FOUND AS A GREEN MUTATION: deleting the clause left all 189 guards
    passing. The count varies across the artifact (1, 3 and 31 today), so a
    constant cannot satisfy this and neither can silence.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    page = _page(matrix)
    checked = 0
    for surface_id, profile_id, cell, _ in _cells(matrix):
        entries = cell.get("observed_cells", [])
        if cell["status"] != "measured-ok" or len(entries) < 2:
            continue
        assert all(entry.get("positive_control") for entry in entries), (
            f"{surface_id} x {profile_id}: a `measured-ok` cell has an uncited route"
        )
        block = _evidence_block(page, surface_id, profile_id)
        others = len(entries) - 1
        expected = (
            "and the other drawn cell carries a control of its own."
            if others == 1
            else f"and each of the other {others} drawn cells carries a control of its own,"
        )
        assert expected in block, (
            f"{surface_id} x {profile_id}: one nodeid stands for {len(entries)} routes and "
            f"the page does not say so. Expected {expected!r} in: {block}"
        )
        checked += 1
    assert checked > 1, "this guard saw fewer than two multi-route cells; it proves little"
    assert len({len(cell.get("observed_cells", [])) for _, _, cell, _ in _cells(matrix)}) > 2, (
        "every cell has the same number of routes, so a hardcoded count would pass"
    )


def test_a_cell_whose_every_route_failed_still_cites_a_control_that_worked(committed):
    """The strongest broken claim there is, and the renderer must not swallow it.

    Every route's CONTRACT failed while every route's CONTROL passed: the
    instrument could tell a wrong answer from a right one everywhere, and the
    product failed anyway. The collate step can write the cell-level citation
    there -- every contributing route is controlled -- and the page prints it,
    because "the check worked and it still said no" is a materially different
    reading from "we could not tell".

    A state today's bed does not produce (a strict xfail xfails the control
    beside the contract), so it is INJECTED. Its renderer branch was dead
    under mutation until this guard existed.
    """
    records = []
    for transfer in ("shell", "nc"):
        label = _bed_label("bb1161", "telnet", transfer)
        records += [
            _observation("bb1161", label=label, outcome=FAILED),
            _control("bb1161", label=label, outcome=PASSED),
        ]
    matrix = collate(committed, records).matrix
    cell = matrix["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    assert cell["status"] == "measured-broken"
    assert not any(entry["outcome"] == "passed" for entry in cell["observed_cells"])
    assert "positive_control" in cell, "every route was controlled, so the cell may cite one"

    block = _evidence_block(_page(matrix), "transfer-roundtrip", "busybox-1.16.1")
    assert f"*Positive control:* `{cell['positive_control']}`" in block, block
    assert _row(_page(matrix), "transfer-roundtrip", "busybox-1.16.1").count("**No.**") == 1

    # ★ AND THE SAME CELL WITH ITS ROUTE CITATIONS REMOVED. MEASURED: the
    # assertions above reach the COMPACT branch, because every route here is
    # cited, so deleting the renderer's cell-level fallback for a cell with no
    # passing route left them green. That fallback cannot be produced by the
    # collate step -- it writes the cell-level field only when every route is
    # cited -- but the SCHEMA permits the shape, so a hand-edit reaches it at
    # `make docs` time, and dropping the branch would silently lose the only
    # citation such a cell has.
    stripped = copy.deepcopy(matrix)
    victim = stripped["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    for entry in victim["observed_cells"]:
        assert entry.pop("positive_control", None), "the fixture had no route citation to remove"
    block = _evidence_block(_page(stripped), "transfer-roundtrip", "busybox-1.16.1")
    assert f"*Positive control:* `{victim['positive_control']}`" in block, block


def test_the_evidence_and_the_verdict_never_disagree_about_a_missing_control(committed):
    """★ THE TWO HALVES OF THE PAGE READ THE SAME FIELD AND MUST NOT SAY DIFFERENT THINGS.

    ★ FOUND AS A GREEN MUTATION IN MY OWN NEW GUARDS. A renderer that marked
    EVERY passing route as uncited ("*Positive control:* **none** over
    ``shell``") stayed green across all fourteen: the verdict sentence is
    derived in :func:`meaning` and the marker in ``_control_lines``, and
    nothing required them to agree. The page could publish *"You can put a
    file on the device and get the same bytes back over ``shell``"* directly
    above *"positive control: none over ``shell``"* -- a contradiction in one
    section, from the artifact this item exists to keep honest.

    So the assertion is EQUALITY, over the real artifact and over an injected
    copy that has the state the real one cannot: a marker with no downgrade
    fails, and a downgrade with no marker fails.
    """
    stripped = copy.deepcopy(committed)
    victim = stripped["cells"]["transfer-roundtrip"]["busybox-1.16.1"]
    entry = next(e for e in victim["observed_cells"] if e["outcome"] == "passed")
    assert entry.pop("positive_control", None), "nothing was taken away; the guard is vacuous"
    seen = {True: 0, False: 0}
    for matrix in (committed, stripped):
        page = _page(matrix)
        for surface_id, profile_id, _cell, _ in _cells(matrix):
            block = _evidence_block(page, surface_id, profile_id)
            row = _row(page, surface_id, profile_id)
            marked = "*Positive control:* **none**" in block
            downgraded = "nothing proved it could fail there" in row
            assert marked == downgraded, (
                f"{surface_id} x {profile_id}: the evidence says the route is "
                f"{'un' if marked else ''}cited and the verdict above it says the "
                f"opposite.\nRow: {row}\nEvidence: {block}"
            )
            seen[marked] += 1
    assert seen[True] == 1, f"the injection produced {seen[True]} markers, not exactly one"
    assert seen[False] > 1, "no cell reached this guard with a control it could cite"


def test_a_cited_split_injected_where_there_was_none_is_still_cited(committed):
    """The injecting twin of the property guard, on a surface that has never split.

    ``exec-exit-code`` x ``busybox-1.16.1`` passes on both of its drawn cells
    today, so a renderer could satisfy the property guard above by citing only
    the two transfer surfaces it happens to have been written against. Turning
    one route red here makes a mixed cell where the tree has none, and the
    surviving route's own control must still reach the page.
    """
    injected = copy.deepcopy(committed)
    cell = injected["cells"]["exec-exit-code"]["busybox-1.16.1"]
    assert {entry["outcome"] for entry in cell["observed_cells"]} == {"passed"}
    cell["status"] = "measured-broken"
    cell["failure_summary"] = "bed-busybox[bb1161:telnet:nc]: injected, for this guard"
    del cell["positive_control"]
    survivor = next(entry for entry in cell["observed_cells"] if entry["transfer"] == "shell")
    next(entry for entry in cell["observed_cells"] if entry["transfer"] == "nc")["outcome"] = (
        "failed"
    )

    page = _page(injected)
    row = _row(page, "exec-exit-code", "busybox-1.16.1")
    assert "**Only over `shell`.** You can run a command" in row, row
    block = _evidence_block(page, "exec-exit-code", "busybox-1.16.1")
    assert f"*Positive control over `shell`:* `{survivor['positive_control']}`" in block, block


def test_a_partial_verdict_injected_where_there_was_none_still_names_its_remainder(committed):
    """The same, for the per-device axis: `gnu` rests on all four devices today.

    Taking two of them away must change what the row says about the other two, and the
    row must name them -- a reader with a `test3`-shaped device is exactly who a
    whole-profile reading would mislead.
    """
    injected = copy.deepcopy(committed)
    cell = injected["cells"]["exec-exit-code"]["gnu"]
    assert cell["observed_on"] == ["test1", "test2", "test3", "test4"]
    cell["observed_on"] = ["test1", "test2"]
    cell["observed_cells"] = [
        entry for entry in cell["observed_cells"] if entry["element"] in ("test1", "test2")
    ]
    cell["not_observable"] = [
        {
            "element": "test3",
            "probed": "tests/conformance/test_exec_contract.py::applicable_cell"
            "(bed-unix[test3:ssh:scp])",
            "probe_result": "False -- injected, for this guard",
        }
    ]

    row = _row(_page(injected), "exec-exit-code", "gnu")
    assert "2 of 4 devices" in row, f"a partial verdict still reads as whole-profile: {row}"
    assert "`test3` could not be measured" in row, f"the probed device is unnamed: {row}"
    assert "`test4` has never been drawn" in row, f"the undrawn device is unnamed: {row}"
    assert "all 4 devices" not in row


def test_a_verdict_resting_on_some_devices_never_reads_as_all_of_them():
    """★ THE MIXED PROFILE. `zephyr-3.7` x transfer is `measured-ok` on 2 of 4 devices.

    A reader who takes that as uniform and deploys to a no-filesystem 3.7 board has
    been misled by the artifact built to inform them -- which is why Chris ruled for
    per-element evidence rather than a scalar verdict. The row must therefore carry the
    count AND name every device the verdict does not rest on.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    page = _page(matrix)
    partial = [
        (surface_id, profile_id, cell, coverage)
        for surface_id, profile_id, cell, coverage in _cells(matrix)
        if cell["status"].startswith("measured-")
        and len(coverage.observed_on) != len(coverage.elements)
    ]
    assert partial, "no partially covered cell in the artifact; this guard is vacuous"
    for surface_id, profile_id, _cell, coverage in partial:
        row = _row(page, surface_id, profile_id)
        where = f"{surface_id} x {profile_id}"
        assert f"{len(coverage.observed_on)} of {len(coverage.elements)} devices" in row, (
            f"{where}: the verdict rests on {len(coverage.observed_on)} of "
            f"{len(coverage.elements)} devices and the row does not say so: {row}"
        )
        assert "all " not in row.split("|")[2], f"{where}: reads as whole-profile: {row}"
        for element in coverage.not_observable + coverage.unaccounted:
            assert f"`{element}`" in row, f"{where}: the row never names {element!r}: {row}"


def test_not_observable_never_renders_like_untested(committed):
    """★ COLLAPSING THESE TWO IS THE ERROR §5 IS EXPLICIT ABOUT, in both directions.

    `not-observable` is a MEASUREMENT: something was probed and answered. `untested` is
    the absence of one, and it must never read as a refusal. The hostile condition is
    INJECTED -- the committed artifact has no `untested` cell at all today, so a guard
    that only read it would be asserting about a state the page never renders.

    Four properties, because "the two strings differ" is satisfied by a renderer that
    prints two different words and means the same thing by both.
    """
    matrix = copy.deepcopy(committed)
    matrix["cells"]["timeout"]["gnu"] = {"status": "untested"}
    page = _page(matrix)
    untested = _row(page, "timeout", "gnu")
    unobservable = _row(page, "timeout", "zephyr-3.7")
    assert committed["cells"]["timeout"]["zephyr-3.7"]["status"] == "not-observable"

    assert "not unsupported" in untested, f"untested must say what it is NOT: {untested}"
    assert "`untested`" in untested
    assert "not the same as broken" in unobservable
    assert "not unsupported" not in unobservable, (
        f"the not-observable row is wearing untested's sentence: {unobservable}"
    )
    assert committed["cells"]["timeout"]["zephyr-3.7"]["as_of"] in unobservable, (
        "a not-observable cell was MEASURED, so its row carries the date it was measured"
    )
    assert not any(char.isdigit() for char in untested.split("|")[2]), (
        f"the untested row carries a date, as though something had been run: {untested}"
    )
    assert "probed" in _evidence_block(page, "timeout", "zephyr-3.7")
    assert "probed" not in _evidence_block(page, "timeout", "gnu")


def test_a_device_no_run_has_drawn_is_not_rendered_as_one_that_cannot_be_measured(committed):
    """★ "NOT YET DRAWN" IS A THIRD THING, and it is DERIVED rather than stored.

    `unaccounted` = profile elements - `observed_on` - `not_observable`. Today every
    cell accounts for its whole profile, because the last bed lane ran at `CELLS=all`
    -- so the hostile condition is INJECTED by deleting one `not_observable` entry,
    which is exactly the shape a `CONFORMANCE_CELLS=8` run leaves behind.

    "Measured on 2 of 4, one has no filesystem, one has never been drawn" and "2 of 4,
    two have no filesystem" are different claims about somebody's hardware. The page
    must not merge them, and this asserts the sentences are BOTH present and DIFFERENT
    -- a renderer that folded the remainder into one bucket satisfies neither half.
    """
    both = copy.deepcopy(committed)
    cell = both["cells"]["transfer-roundtrip"]["zephyr-3.7"]
    assert [entry["element"] for entry in cell["not_observable"]] == [
        "zephyr37_nofs",
        "zephyr37_llext",
    ]
    cell["not_observable"] = [cell["not_observable"][0]]
    page = _page(both)
    row = _row(page, "transfer-roundtrip", "zephyr-3.7")

    assert "`zephyr37_nofs` could not be measured" in row, (
        f"the probed device lost its measurement sentence: {row}"
    )
    assert "never been drawn" in row, (
        f"`zephyr37_llext` is in no bucket now, and the row does not say so: {row}"
    )
    assert "`zephyr37_llext` has never been drawn" in row
    assert "`zephyr37_llext` could not be measured" not in row, (
        f"a device nobody looked at is being reported as one that cannot be measured: {row}"
    )
    assert "*Never drawn:* `zephyr37_llext`" in _evidence_block(
        page, "transfer-roundtrip", "zephyr-3.7"
    )

    # ...and the unmutated cell must say the opposite, or the two sentences are
    # interchangeable and the guard above proves nothing about the distinction.
    # ...and the same on a `not-observable` cell, where the two buckets are easiest to
    # confuse: `not_observable` need not exhaust the profile there either, so a device
    # nobody drew must still be reported as undrawn rather than absorbed into the
    # measurement the cell is named for.
    unobservable = copy.deepcopy(committed)
    probed = unobservable["cells"]["timeout"]["zephyr-3.7"]["not_observable"]
    assert len(probed) == 4
    unobservable["cells"]["timeout"]["zephyr-3.7"]["not_observable"] = probed[:3]
    undrawn = probed[3]["element"]
    unobservable_row = _row(_page(unobservable), "timeout", "zephyr-3.7")
    assert f"`{undrawn}` has never been drawn" in unobservable_row, (
        f"a not-observable cell absorbed an undrawn device into its probe: {unobservable_row}"
    )
    assert "never been drawn" not in _row(_page(committed), "timeout", "zephyr-3.7")

    untouched = _row(_page(committed), "transfer-roundtrip", "zephyr-3.7")
    assert "never been drawn" not in untouched, (
        "the untouched cell accounts for its whole profile, so nothing there is undrawn"
    )
    assert "`zephyr37_nofs` and `zephyr37_llext` could not be measured" in untouched, (
        "with both devices probed, both belong in the SAME sentence -- and it is the "
        "measurement sentence, not the never-drawn one"
    )


def test_an_unexpected_failure_is_not_rendered_as_a_registered_gap(committed):
    """A strict `xfail` and a surprise are different news, and the page must not merge them.

    Every `measured-broken` cell today failed the way the suite PREDICTED -- otto
    already carries the defect. A cell that failed when nothing predicted it is a fresh
    defect, and rendering it with the reassuring "otto already knows about this"
    sentence would be the worst error this page could make. INJECTED, because the
    artifact contains no such cell.
    """
    surprise = copy.deepcopy(committed)
    entries = surprise["cells"]["transfer-roundtrip"]["busybox-1.16.1"]["observed_cells"]
    xfailed = next(entry for entry in entries if entry["outcome"] == "xfailed")
    xfailed["outcome"] = "failed"

    predicted_row = _row(_page(committed), "transfer-roundtrip", "busybox-1.16.1")
    surprise_row = _row(_page(surprise), "transfer-roundtrip", "busybox-1.16.1")

    assert "registered gap" in predicted_row
    assert "subsystems/busybox-support" in predicted_row, (
        "a predicted failure is otto's ALREADY-REGISTERED gap; the row must send the "
        f"reader to the page that owns it: {predicted_row}"
    )
    assert "nothing predicted" in surprise_row, f"a surprise reads as routine: {surprise_row}"
    assert "registered gap ({doc}" not in surprise_row
    assert "already carries this as a registered gap" not in surprise_row


def test_evidence_the_collate_step_cut_short_is_marked_as_cut(committed):
    """★ FOUND BY LOOKING AT THE RENDERED PAGE, and invisible to every guard before it.

    The collate step caps `failure_summary` at `FAILURE_SUMMARY_LIMIT`, and a reader
    meeting a reason that trails off mid-sentence cannot tell truncated evidence from
    careless writing -- the difference decides whether they go looking for the rest.

    ★ IT INJECTS ITS OWN HOSTILE CONDITION. Task 5 wrote this guard against the ten
    cells that WERE at the cap and asserted the list was non-empty, which made the whole
    check inherited: raising the cap in Task 6 so the real reasons fit whole left it
    with nothing to look at, and it failed on its own vacuity assertion rather than on
    anything about the page. A cap that no longer bites is the good outcome, so the cut
    is now made here.

    The marker is a LENGTH COMPARISON against the collate step's own constant, never a
    reading of the text -- the summary itself is reproduced verbatim, because it is
    evidence. Both directions are asserted: a shortened summary must NOT be marked, or
    the marker would be decoration rather than a fact about this cell.
    """
    broken = [
        (surface_id, profile_id)
        for surface_id, profile_id, cell, _ in _cells(committed)
        if "failure_summary" in cell
    ]
    assert broken, "no cell carries a failure summary, so there is nothing to mark"
    surface_id, profile_id = broken[0]

    at_the_cap = copy.deepcopy(committed)
    cell = at_the_cap["cells"][surface_id][profile_id]
    cell["failure_summary"] = (cell["failure_summary"] + " ")[:1] * FAILURE_SUMMARY_LIMIT
    block = _evidence_block(_page(at_the_cap), surface_id, profile_id)
    assert "cut short by the collate step" in block, (
        f"{surface_id} x {profile_id}: a reason that stops mid-sentence at the cap and "
        f"the page does not say it was cut"
    )
    assert str(FAILURE_SUMMARY_LIMIT) in block, "the page does not say where it was cut"

    intact = copy.deepcopy(committed)
    intact["cells"][surface_id][profile_id]["failure_summary"] = "it fell over. That is all."
    assert "cut short" not in _evidence_block(_page(intact), surface_id, profile_id), (
        "a summary that ENDED is being reported as one that was CUT"
    )


def test_a_record_can_never_be_clipped_below_the_cap_the_page_reports(committed):
    """★ TWO CAPS, AND THE PAGE CAN ONLY SEE ONE. MEASURED 2026-08-25.

    A run's own record caps its summary at `RECORD_SUMMARY_LIMIT` before the collate
    step ever joins one; the page marks a cut by comparing the JOINED length against
    `FAILURE_SUMMARY_LIMIT`. So a cut made at the record, below the join's cap, would
    reach a reader with nothing saying it had been cut -- and that is exactly what was
    happening: every record summary in the artifact was EXACTLY 500 characters, the
    record cap, while the page named the collate step's 500 as the reason.

    The ordering is what closes it. A joined segment carries `"<cell label>: "` on top
    of the record's text, so a record clipped at the larger cap necessarily overflows
    the smaller one and the page announces it. Raising either constant without the
    other reopens the hole silently, which is why this is a guard and not a comment.
    """
    assert RECORD_SUMMARY_LIMIT > FAILURE_SUMMARY_LIMIT, (
        f"a record clipped at {RECORD_SUMMARY_LIMIT} can join to under "
        f"{FAILURE_SUMMARY_LIMIT} characters and reach the page unmarked as cut"
    )
    cut = failure_summary(
        {"call": _report("call", "skipped", wasxfail="a reason without an end. " * 400)}
    )
    assert cut is not None, "no summary at all came back from a fired xfail"
    assert len(cut) == RECORD_SUMMARY_LIMIT, (
        "the record cap above is not applied, so the ordering it states pins nothing"
    )
    longest = max(
        (
            len(cell["failure_summary"])
            for _, _, cell, _ in _cells(committed)
            if "failure_summary" in cell
        ),
        default=0,
    )
    assert longest < FAILURE_SUMMARY_LIMIT, (
        f"the published evidence is truncated again ({longest} characters): either "
        f"raise both caps, keeping the ordering above, or accept the page's cut marker"
    )


def test_every_measured_broken_cell_points_at_the_registry_that_owns_it():
    """The ten broken cells are otto's `nc-transfer` gap, measured 2026-08-13, not news.

    Republishing a known-broken surface is right -- a matrix that omitted one would lie
    by omission -- but a row that did not name it as a REGISTERED gap would read as
    though this page had discovered something otto has known for weeks.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    page = _page(matrix)
    broken = [
        (surface_id, profile_id, cell)
        for surface_id, profile_id, cell, _ in _cells(matrix)
        if cell["status"] == "measured-broken"
    ]
    assert broken, "no measured-broken cell in the artifact; this guard is vacuous"
    for surface_id, profile_id, cell in broken:
        if any(entry["outcome"] == "failed" for entry in cell["observed_cells"]):
            continue
        row = _row(page, surface_id, profile_id)
        where = f"{surface_id} x {profile_id}"
        assert "registered gap" in row, f"{where} reads as this page's own discovery: {row}"
        assert "subsystems/busybox-support" in row, (
            f"{where} never sends the reader to the page that owns the gap: {row}"
        )
    assert "{data}`~otto.host.userland.GAPS`" in page, "the page never names the registry"
    assert "Division of labour with {doc}`subsystems/busybox-support`" in page, (
        "the two pages share `measured-broken` and `untested` with `otto.host.userland`'s "
        "gap registry; without a division-of-labour note they drift into restating each other"
    )


def test_the_page_dates_everything_in_utc_and_says_so_before_the_first_verdict():
    """★ STALENESS IS THIS PAGE'S MOST LOAD-BEARING FEATURE (ruling, 2026-08-24).

    Nothing refreshes the matrix: CI has no lab, so every row's currency depends on
    somebody running `make conformance-bed` by hand. A confidently rendered
    three-month-old verdict is worse than a blank, so the dates and the reason they can
    rot come BEFORE the grid rather than in a footnote after it.

    And they are UTC. The artifact committed on the evening of 2026-08-24 local time is
    dated 2026-08-25; a page that did not say so tells a reader a run happened tomorrow.
    """
    matrix = json.loads(MATRIX_PATH.read_text())
    page = _page(matrix)
    first_verdict = page.index("## At a glance")
    preamble = page[:first_verdict]

    assert "UTC" in preamble, "the page reaches its first verdict without saying dates are UTC"
    assert "conformance-bed" in preamble, "nothing tells a reader what would refresh a row"
    assert "no lab" in preamble, "nothing says CI cannot re-measure a row"
    dates = sorted({cell["as_of"] for _, _, cell, _ in _cells(matrix) if "as_of" in cell})
    assert dates, "no cell carries a date; this guard is vacuous"
    for date in (dates[0], dates[-1]):
        assert date in preamble, f"{date} is a measurement date the preamble never shows"
    assert _RENDERED_ON.isoformat() in preamble, (
        "the page does not say when it was rendered, so a reader cannot age a row"
    )


def test_the_page_states_the_spec_requirement_it_does_not_meet():
    """§5's CI path is deliberately unimplemented, and a reader must not have to guess.

    "The collate step also accepts observation artifacts downloaded from the nightly
    `conformance-hermetic` job, recording `venue: ci-hermetic`" is a spec requirement
    this item consciously does not meet: a hermetic cell has no profile to land in. A
    reader comparing the spec to the artifact must be able to tell a decision from an
    oversight, so the page says which it is and why.
    """
    page = _page(json.loads(MATRIX_PATH.read_text()))
    assert "ci-hermetic" in page
    assert "decision rather than an oversight" in page
    assert "no code path" in page or "produced by no" in page


def test_the_page_admits_the_limits_of_its_own_guarantee():
    """A reference that overstates its own strength is the thing it was built to prevent.

    A `measured-ok` cell names a control proving its observable can go red -- and that
    guarantee is only as strong as the vacuity catcher, which is itself a construction
    that could be wrong. "Nothing survived being made vacuous" is a BUILT result, not a
    found one, and the reproducibility check that would catch a hand-edited verdict is
    live on one dev machine and INERT in CI. All three belong on the page.
    """
    page = _page(json.loads(MATRIX_PATH.read_text()))
    assert "built" in page.lower(), "the page presents a built result as a found one"
    assert "vacuous" in page, "the page never names what the controls are protected from"
    assert "inert" in page.lower(), "the page never admits the fabrication check is conditional"
    assert "positive control" in page.lower()


def test_the_page_says_its_hand_written_reasons_are_checked_and_how_far(committed):
    """The narrowing reasons are the only hand-written claim on the page; it must say so.

    A reader meeting "could not be measured: otto reports nowhere to put a file on
    them" cannot tell a machine-derived statement from a sentence somebody typed two
    releases ago. The build now checks that sentence against the contract's own domain
    rule -- and that is worth stating, together with what it does NOT check, because a
    reference that overstates its own strength is the thing this one exists to prevent.
    """
    page = _page(committed)
    section = page[page.index("## How this page is produced") :]
    assert "written by hand is checked too" in section, (
        "the page does not say that its one hand-written claim is checked at all"
    )
    assert "domain rule" in section, "it does not say what the reason is checked against"
    assert "not** checked is the wording" in section, "it claims a check on prose it does not make"


def test_the_renderer_refuses_a_surface_the_tree_no_longer_declares(committed):
    """★ SPEC §5's FAIL-ON-UNDECLARED, in both directions, as a build FAILURE.

    Injected into the artifact rather than into the tree, because the tree's answer is
    what the renderer is checking AGAINST -- a mutation of the tree would move the
    reference, not the thing referred to.
    """
    extra = copy.deepcopy(committed)
    extra["surfaces"].append(
        {"id": "ghost", "title": "a surface nobody declares", "contract": "tests/x.py::test_x"}
    )
    extra["cells"]["ghost"] = {
        profile["id"]: {"status": "untested"} for profile in extra["profiles"]
    }
    assert any("ghost" in problem for problem in axes_mismatch(extra))
    with pytest.raises(ValueError, match="ghost"):
        _page(extra)

    dropped = copy.deepcopy(committed)
    gone = dropped["surfaces"].pop()["id"]
    del dropped["cells"][gone]
    assert any(gone in problem for problem in axes_mismatch(dropped))
    with pytest.raises(ValueError, match=gone):
        _page(dropped)

    renamed = copy.deepcopy(committed)
    renamed["surfaces"][0]["title"] = "a title the tree does not use"
    assert any("title" in problem for problem in axes_mismatch(renamed))
    with pytest.raises(ValueError, match="title"):
        _page(renamed)


def test_the_renderer_refuses_a_profile_the_tree_no_longer_declares(committed):
    """The other axis, and the devices inside it.

    A profile whose device list has drifted is the subtler half: the column still
    exists, the page still renders, and every count on it is about a lab that changed.
    """
    extra = copy.deepcopy(committed)
    extra["profiles"].append({"id": "ghost-userland", "elements": ["nothing"]})
    for row in extra["cells"].values():
        row["ghost-userland"] = {"status": "untested"}
    assert any("ghost-userland" in problem for problem in axes_mismatch(extra))
    with pytest.raises(ValueError, match="ghost-userland"):
        _page(extra)

    dropped = copy.deepcopy(committed)
    gone = dropped["profiles"].pop()["id"]
    for row in dropped["cells"].values():
        del row[gone]
    assert any(gone in problem for problem in axes_mismatch(dropped))

    drifted = copy.deepcopy(committed)
    drifted["profiles"][0]["elements"] = ["a-device-the-lab-does-not-have"]
    assert any("devices are" in problem for problem in axes_mismatch(drifted))
    with pytest.raises(ValueError, match="devices are"):
        _page(drifted)


def test_the_renderer_refuses_a_surface_or_profile_it_has_no_words_for(committed, monkeypatch):
    """A blank column is the same defect one level down.

    `VOICE` and `FAMILY_BLURB` are the only prose in this pipeline, and a surface or a
    userland family with no entry would render as a heading with nothing under it --
    which reads to a stranger as "otto has nothing to say about this", not as "somebody
    forgot to write a sentence". So it fails the docs build instead.
    """
    without_voice = dict(VOICE)
    del without_voice["timeout"]
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", without_voice)
    assert any("VOICE" in problem for problem in axes_mismatch(committed))

    monkeypatch.setattr("scripts.render_support_matrix.VOICE", VOICE)
    without_family = {key: value for key, value in FAMILY_BLURB.items() if key != "zephyr-"}
    monkeypatch.setattr("scripts.render_support_matrix.FAMILY_BLURB", without_family)
    problems = axes_mismatch(committed)
    assert any("FAMILY_BLURB" in problem and "zephyr" in problem for problem in problems)


def test_rendering_never_writes_to_the_artifact(tmp_path, committed):
    """The renderer READS a verdict; only the collate step may write one.

    This is what stops the by-name exemption in
    `test_only_the_collator_ever_writes_a_measured_verdict` from being a hole: that one
    is a scan for literals, and a renderer has to name all four states to render them.
    This one is behavioural -- run the real `main` and require the artifact it read to
    come back byte-identical.
    """
    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(committed, indent=2) + "\n", encoding="utf-8")
    before = matrix.read_bytes()
    page = tmp_path / "support-matrix.md"
    assert render_main(["--matrix", str(matrix), "--page", str(page)]) == 0
    assert matrix.read_bytes() == before, "the RENDERER wrote to the artifact"
    assert page.read_text(encoding="utf-8").startswith("<!-- GENERATED FILE")


def test_the_renderer_recovers_no_field_by_parsing_prose():
    """The per-transport evidence is STRUCTURAL, and this keeps it that way.

    Task 4b moved the transport split out of a pipe-joined `observable` and an English
    `failure_summary` into `observed_cells`' own `element`/`term`/`transfer` keys,
    precisely so a renderer would not have to do string surgery on an id whose spelling
    belongs to `tests/conformance/_sample.py::cell_label`. A renderer that recovered
    structure by parsing English would keep parsing successfully, against the wrong
    fields, the day that spelling changed.

    THE BAN IS ON RECOVERING A FIELD. `code_spans` splits a `VOICE` clause on backticks,
    which is this file's own hand-written prose rather than anything the artifact
    stores, and it does so to CHECK that clause against the rule it describes -- see
    `narrowing_mismatch`. Every assertion below is unchanged by it: `re` stays banned
    outright, because the day one appears here it will be pointed at a cell.
    """
    source = RENDERER_PATH.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "*"))
    )
    for forbidden in ('.split("["', ".split('['", '.split(" | "', "re.compile", "import re"):
        assert forbidden not in body, (
            f"{RENDERER_PATH.name} reaches for {forbidden!r}; every field it renders is "
            f"read from a key, not recovered from a sentence"
        )


def test_the_docs_build_renders_the_page_and_fails_when_the_renderer_does():
    """The page reaches a reader only because `docs/conf.py` runs the renderer.

    Hooked on `builder-inited` beside `_generate_docs_media`, for EVERY builder rather
    than html-only -- the toctree names the page, so the doctest builder `make docs`
    also runs has to find it on disk too. A non-zero exit RAISES, which is what makes
    §5's fail-on-undeclared a build failure and not a warning.
    """
    conf = (PROJECT_ROOT / "docs" / "conf.py").read_text(encoding="utf-8")
    assert 'app.connect("builder-inited", _generate_support_matrix)' in conf
    hook = conf[conf.index("def _generate_support_matrix") :]
    hook = hook[: hook.index("\ndef ")]
    assert '"-m", "scripts.render_support_matrix"' in hook
    assert "raise RuntimeError" in hook, "a failed render would only warn"
    assert "app.builder.name" not in hook, "the page is a source file; every builder needs it"


def test_the_page_is_reachable_from_the_architecture_index():
    """A page no index names is built, warned about under `-W`, and reachable by URL only.

    In the OVERVIEW toctree beside `testing` and `quality-gates`, and deliberately NOT
    under `subsystems/`: the spec says "alongside the busybox-support page", but this
    matrix is cross-cutting -- six surfaces spanning hosts, execution and transfer over
    nine profiles -- and filing a cross-cutting reference under one area makes it less
    findable than it deserves.
    """
    index = (PROJECT_ROOT / "docs" / "architecture" / "index.rst").read_text(encoding="utf-8")
    entries = [line.strip() for line in index.splitlines()]
    assert "support-matrix" in entries, "no toctree entry, so the page is an orphan"
    overview = entries[entries.index("overview") : entries.index("overview") + 6]
    assert "support-matrix" in overview, (
        "the page belongs in the Overview toctree beside testing and quality-gates, "
        "not among the per-area subsystem pages"
    )
    assert "subsystems/support-matrix" not in entries


def test_the_rendered_page_is_build_output_and_not_a_second_committed_copy():
    """Two copies of 54 verdicts, and the one that goes stale is the one nothing runs."""
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "docs/architecture/support-matrix.md" in [line.strip() for line in ignored]
    assert PAGE_PATH.relative_to(PROJECT_ROOT).as_posix() == "docs/architecture/support-matrix.md"


# --------------------------------------------------------------------------
# The narrowing prose, PINNED TO THE RULE IT DESCRIBES
# --------------------------------------------------------------------------
#
# A `not-observable` cell renders a hand-written clause from `VOICE` saying WHY a
# contract put those devices outside its own domain. Task 5 shipped those six
# clauses and recorded that nothing tied them to anything: if a contract's DOMAIN
# RULE changed, the page would keep publishing the old reason, and a reader could
# not tell -- the verdict, the device list and the probe beside it would all still
# be right.
#
# WHAT IS PINNED IS THE MECHANISM, NOT THE PROSE, and `tests/unit/test_docs_gap_sync.py`
# is the precedent rather than the model to copy: that file says in its own docstring
# that pinning paragraphs verbatim "would buy a copying ritual rather than a check --
# it would redden on a typo fix and stay green on a lie", and pins STRUCTURE instead.
# The equivalent here is that a clause may name only mechanisms its contract's
# `applicable_cell` mentions, and must name at least one identifier that predicate's
# BODY reads. Wording stays review's job.


def test_the_narrowing_prose_is_pinned_to_the_rule_it_describes(committed):
    """The control for every refusal below: today's clauses and rules DO agree.

    Without it a `narrowing_mismatch` that reported a problem unconditionally would
    satisfy all five refusals and prove nothing -- the same shape
    `test_the_renderer_agrees_with_the_committed_artifact` holds for `axes_mismatch`.
    """
    assert narrowing_mismatch(committed) == []


def test_the_narrowing_pin_examines_a_real_mechanism_on_every_narrowing_surface():
    """★ THE VACUITY CONTROL, because a pin over empty sets passes on any prose.

    Both checks in `narrowing_mismatch` are satisfied by a clause that names NOTHING
    if the surface has no domain rule, so a tree where nothing narrows would report
    green while pinning nothing at all. This asserts the inputs are real: the surfaces
    whose contract declares an `applicable_cell` each contribute at least one code span
    AND at least one identifier that predicate's body reads.
    """
    with_rules = {}
    for surface in SURFACES:
        rule = _domain_rule(PROJECT_ROOT / surface.contract.split("::")[0])
        if rule is not None:
            with_rules[surface.id] = rule
    assert set(with_rules) == {"transfer-roundtrip", "transfer-mode", "timeout"}, (
        f"the set of narrowing surfaces moved: {sorted(with_rules)}. That is not a "
        f"failure by itself, but every clause below has to be re-read against its new "
        f"rule rather than inherited"
    )
    for surface_id, rule in sorted(with_rules.items()):
        voice = VOICE[surface_id]
        spans = code_spans(voice.narrowed)
        assert spans, f"{surface_id}: its narrowing clause names no mechanism to pin"
        named = code_spans(f"{voice.narrowed} {voice.narrowed_detail}")
        assert named & rule.reads, (
            f"{surface_id}: nothing its prose names is read by the predicate's body"
        )


def test_a_clause_naming_a_mechanism_its_rule_does_not_is_refused(committed, monkeypatch):
    """★ THE DRIFT ITSELF: the page gives a reason the rule no longer gives.

    Injected on the PROSE side and not on the rule's, because a mutation of the
    contract would move the reference rather than the thing referred to -- the same
    reason `test_the_renderer_refuses_a_surface_the_tree_no_longer_declares` injects
    into the artifact.
    """
    drifted = dict(VOICE)
    drifted["transfer-mode"] = dataclasses.replace(
        VOICE["transfer-mode"],
        narrowed="otto's backend answers `supports_mode` False, so there is no mode to read",
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
    problems = narrowing_mismatch(committed)
    assert any("transfer-mode" in problem and "supports_mode" in problem for problem in problems), (
        f"a clause naming a mechanism the rule never mentions was accepted: {problems}"
    )
    assert not any("timeout" in problem for problem in problems), (
        "the refusal is not scoped to the clause that drifted"
    )


def test_a_clause_that_names_no_mechanism_at_all_is_refused(committed, monkeypatch):
    """The vacuous clause, which check 1 alone accepts and this item exists to catch.

    "Every code span must appear in the rule" is trivially true of a clause with no
    code spans, so a reason rewritten into pure English would satisfy it while tying
    the page to nothing.
    """
    wordy = dict(VOICE)
    wordy["timeout"] = dataclasses.replace(
        VOICE["timeout"],
        narrowed="nothing on them can be made to take long enough to matter",
        narrowed_detail="",
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", wordy)
    problems = narrowing_mismatch(committed)
    assert any("timeout" in problem and "identifiers" in problem for problem in problems), (
        f"a clause tied to no identifier at all was accepted: {problems}"
    )


def test_a_clause_naming_only_the_predicates_own_argument_is_refused(committed, monkeypatch):
    """★ CAUGHT AS A GREEN MUTATION IN THIS TASK'S OWN GUARD, and repaired rather than shipped.

    `_domain_rule` subtracts the predicate's parameter names from what it reports the
    rule as READING. Removing that subtraction (mutation N8) left the whole suite
    green, because no clause happens to name `resolved` -- the narrowing was defensive
    and nothing could see it. So a clause that names ONLY the argument must be refused
    here: `resolved` really is in the predicate's source, so check 1 accepts it, and
    only the subtraction makes check 2 reject it.
    """
    argument_only = dict(VOICE)
    argument_only["timeout"] = dataclasses.replace(
        VOICE["timeout"],
        narrowed="the `resolved` cell offers nothing that can outlive a budget",
        narrowed_detail="",
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", argument_only)
    problems = narrowing_mismatch(committed)
    assert any("timeout" in problem and "identifiers" in problem for problem in problems), (
        f"a clause tied only to the predicate's own parameter was accepted: {problems}"
    )


def test_a_surface_whose_contract_narrows_nothing_may_not_give_a_reason(committed, monkeypatch):
    """MEASURED: `test_exec_contract.py` declares NO `applicable_cell` -- exec narrows nothing.

    So the three exec clauses can never be rendered, and a surface-specific reason
    there would describe an exclusion no run can produce. Task 5 shipped the generic
    sentence for all three; this is what keeps it generic.
    """
    invented = dict(VOICE)
    invented["exec-framing"] = dataclasses.replace(
        VOICE["exec-framing"],
        narrowed="their `remote_scratch` is `None`, so there is no output to frame",
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", invented)
    problems = narrowing_mismatch(committed)
    assert any(
        "exec-framing" in problem and "narrows nothing" in problem for problem in problems
    ), f"a reason was accepted from a contract with no domain rule: {problems}"


def test_a_not_observable_entry_under_a_contract_with_no_domain_rule_is_refused(committed):
    """The other direction, and it is a claim about the ARTIFACT rather than the prose.

    A `not_observable` entry says a contract excluded a device. `test_exec_contract.py`
    declares no `applicable_cell`, so nothing there can exclude anything -- an exec cell
    carrying one is a verdict no run could have produced, and the page would render a
    reason to go with it.
    """
    poisoned = copy.deepcopy(committed)
    cell = poisoned["cells"]["exec-exit-code"]["gnu"]
    cell["not_observable"] = [
        {
            "element": cell["observed_on"][0],
            "probed": "tests/conformance/test_exec_contract.py::applicable_cell(x)",
            "probe_result": "False",
            "venue": "bed",
            "as_of": cell["as_of"],
        }
    ]
    problems = narrowing_mismatch(poisoned)
    assert any("exec-exit-code" in problem and "gnu" in problem for problem in problems), (
        f"an exclusion under a contract that cannot exclude was accepted: {problems}"
    )
    assert not any(
        "exec-exit-code" in problem and "gnu" in problem
        for problem in narrowing_mismatch(committed)
    ), "the poison leaked into the real artifact"


def test_the_renderer_refuses_to_write_when_the_narrowing_came_loose(
    tmp_path, committed, monkeypatch
):
    """`main` must CALL the check, and Task 4b measured what an unguarded call is worth.

    Dropping `element_accounting_errors` from the collate step's `main` left the whole
    suite green (mutation M19), because the function was exercised directly everywhere
    and nothing asserted it was still wired. So this poisons ONLY the narrowing --
    `axes_mismatch` is asserted empty first -- and requires the real `main` to exit
    non-zero and write no page.
    """
    drifted = dict(VOICE)
    drifted["transfer-roundtrip"] = dataclasses.replace(
        VOICE["transfer-roundtrip"],
        narrowed="their `supports_mode` answer is False, so there is no roundtrip",
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
    assert axes_mismatch(committed) == [], "the poison must violate ONLY the narrowing check"
    assert narrowing_mismatch(committed) != []

    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(committed, indent=2) + "\n", encoding="utf-8")
    page = tmp_path / "support-matrix.md"
    assert render_main(["--matrix", str(matrix), "--page", str(page)]) == 1
    assert not page.exists(), "the page was written from prose that no longer matches its rule"


# --------------------------------------------------------------------------
# The promise follows the CELL'S OWN observable, never a per-surface constant
# --------------------------------------------------------------------------


def test_the_zephyr_mode_rows_do_not_promise_the_mode_they_refuse(committed):
    """★ THE DEFECT AS IT SHIPPED, pinned on the artifact that produced it.

    All three Zephyr `transfer-mode` cells are `measured-ok`, and every one stores an
    `observable` reading *"the pre-flight refusal `ConsoleFileTransfer` returns for a
    non-None mode -- aggregate and per file -- because it declares no permission
    model"*. MEASURED 2026-08-25, the page published *"**Yes.** You can put a file and
    have the permission mode you asked for land on it"* for all three: the answer
    column promising the exact opposite of what the cell one column over had stored,
    corrected only by an evidence bullet further down that a reader in a hurry never
    reaches.

    `Voice.capability` was a per-surface CONSTANT and `meaning()` composed the sentence
    from it, so **nothing in the 192 guards could relate the two**. Its injecting twin
    below keeps this alive if the bed ever stops drawing a Zephyr cell.
    """
    page = _page(committed)
    promise = "have the permission mode you asked for land on it"
    refusal = next(branch for branch in VOICE["transfer-mode"].branches if not branch.capability)
    zephyr = [profile["id"] for profile in committed["profiles"] if profile["id"].startswith("z")]
    assert zephyr, "no Zephyr profile in the artifact; this guard is vacuous"
    for profile_id in zephyr:
        cell = committed["cells"]["transfer-mode"][profile_id]
        assert "permission model" in cell["observable"], (
            f"transfer-mode x {profile_id} no longer stores the refusal observable; "
            f"this guard is describing a cell that has changed underneath it"
        )
        row = _row(page, "transfer-mode", profile_id)
        assert promise not in row, f"the row still promises a mode otto refuses: {row}"
        # ★ THE HEADLINE AND THE CLAUSE ARE READ OUT OF THE DECLARATION, not copied.
        # MEASURED as a GREEN mutation in this task's own new code: deleting
        # `promise.headline` from the sentence -- the bolded first thing a reader sees,
        # and the word that answers their question -- left every guard passing.
        assert refusal.headline, "the arm declares no headline; the assertion below is vacuous"
        assert refusal.instead, "the arm declares no clause; the assertion below is vacuous"
        assert f"| **{refusal.headline}** " in row, (
            f"the row loses the answer a reader came for: {row}"
        )
        assert refusal.instead[1:] in row, f"the row does not say what was watched instead: {row}"
        assert _grid_token(page, "transfer-mode", profile_id).startswith("refused"), (
            f"the grid publishes {_grid_token(page, 'transfer-mode', profile_id)!r} under "
            f"a column headed 'file mode' for a device on which otto refuses one"
        )


def test_a_cell_whose_contract_watched_a_refusal_never_renders_as_a_capability(committed):
    """The guard above reads the cells that HAVE a refusal; this one MAKES one.

    `transfer-mode` x `gnu` watches the mode `stat -c %a` reads back and legitimately
    promises it, so replacing its observable with the refusal the SAME contract writes
    on its other arm is a hostile condition INJECTED rather than inherited -- and the
    promise must vanish from the row and from the grid together. The `observable`
    itself must still be reproduced verbatim in the evidence, because it is evidence.
    """
    promise = VOICE["transfer-mode"].capability
    assert promise in _row(_page(committed), "transfer-mode", "gnu"), (
        "the control: the mode-reading arm really does promise the capability today"
    )
    refusal = committed["cells"]["transfer-mode"]["zephyr-2.7"]["observable"]

    injected = copy.deepcopy(committed)
    injected["cells"]["transfer-mode"]["gnu"]["observable"] = refusal
    page = _page(injected)
    row = _row(page, "transfer-mode", "gnu")
    arm = next(branch for branch in VOICE["transfer-mode"].branches if not branch.capability)
    assert arm.headline, "the arm declares no headline; the assertions below are vacuous"
    assert arm.instead, "the arm declares no clause; the assertions below are vacuous"
    assert promise not in row, f"a refusal was published as the capability: {row}"
    assert "You can" not in row, f"a refusal was published as some other promise: {row}"
    assert f"| **{arm.headline}** " in row, f"the row loses its answer: {row}"
    assert arm.instead[1:] in row, f"the row loses what was watched instead: {row}"
    assert _grid_token(page, "transfer-mode", "gnu") == "refused", (
        f"the grid still says {_grid_token(page, 'transfer-mode', 'gnu')!r}"
    )
    assert refusal in _evidence_block(page, "transfer-mode", "gnu"), (
        "the observable is evidence and must still be reproduced verbatim"
    )


def test_a_cell_whose_observable_matches_no_declared_arm_promises_nothing(committed):
    """FAIL CLOSED. An observable this page cannot place must not fall back to the promise.

    That fallback is the defect itself: a sentence published about a cell nobody had
    checked. `promise_mismatch` reports the same cell so `make docs` fails on it, and
    this asserts what the page says if one reaches it anyway -- which it can, because
    the renderer runs over whatever the artifact holds and validates nothing.

    ★★★ BOTH SIDES OF `!= 1`, because the rule is "zero arms, OR TWO" and the two
    halves are not the same code. `promise_mismatch` has a guard for each
    (`...matches_no_arm_fails_the_build` / `...matches_two_arms_fails_the_build`);
    `promise_of` had one only for zero. MEASURED 2026-08-25: relaxing `len(matched)
    != 1` to `len(matched) < 1` left all 215 guards passing, and a cell matching BOTH
    arms was then published under `matched[0]` -- the CAPABILITY arm, so a cell the
    page has just admitted it cannot place promises the thing. Markers that stop
    discriminating is precisely the drift the arm design is exposed to, so the state
    it lands in is the one that must not promise.
    """
    unplaceable = {
        "none": "something nobody described",
        "both": " ".join(branch.marker for branch in VOICE["transfer-mode"].branches),
    }
    for shape, observable in unplaceable.items():
        matched = [b for b in VOICE["transfer-mode"].branches if b.marker in observable]
        assert len(matched) == (0 if shape == "none" else 2), (
            f"the {shape!r} observable matches {len(matched)} arms; this case is vacuous"
        )
        injected = copy.deepcopy(committed)
        injected["cells"]["transfer-mode"]["gnu"]["observable"] = observable
        page = _page(injected)
        row = _row(page, "transfer-mode", "gnu")
        assert VOICE["transfer-mode"].capability not in row, (
            f"an observable matching {shape} of the arms fell back to the promise: {row}"
        )
        assert "You can" not in row, f"promised something anyway ({shape}): {row}"
        # ★ THE EXPECTATION IS READ FROM THE THING UNDER TEST, so its emptiness is
        # asserted separately. MEASURED as a GREEN mutation: blanking `_UNINTERPRETED`'s
        # headline satisfied `f"| **{_UNINTERPRETED.headline}** " in row` on both sides
        # at once, and the page published a bold nothing. VOICE's own arms are covered
        # by `promise_mismatch`'s completeness check; this constant is covered by
        # nothing else.
        blank = "the fallback says nothing, so a cell this page cannot place publishes a blank row"
        assert _UNINTERPRETED.headline, blank
        assert _UNINTERPRETED.instead, blank
        assert _UNINTERPRETED.grid_word, blank
        assert _grid_token(page, "transfer-mode", "gnu") == _UNINTERPRETED.grid_word
        # Read out of the declaration, for the reason the refusal arm's twin records: a
        # replacement sentence that can be deleted without a guard noticing is not a
        # replacement, and MEASURED this happened twice in this task's own new code.
        assert f"| **{_UNINTERPRETED.headline}** " in row, f"the row says nothing at all: {row}"
        assert _UNINTERPRETED.instead[1:] in row, f"the row does not say why: {row}"


def test_a_measured_cell_that_declares_no_observable_promises_nothing(committed):
    """★★★ THE FAIL-OPEN ARM, AND IT RESURRECTED THE ORIGINAL BLOCKING DEFECT.

    `promise_of`'s no-observable arm was recorded as "unreachable for a measured cell,
    so harmless": the schema requires `observable` on both `measured-*` states, so a
    collated artifact cannot hold one. But the renderer runs from `docs/conf.py` over
    whatever `schemas/support_matrix.json` holds and VALIDATES NOTHING, so the field is
    exactly one hand-edit away from absent -- and MEASURED 2026-08-25 on the untouched
    tree, deleting it from `transfer-mode` x `zephyr-2.7` published *"**Yes.** You can
    put a file and have the permission mode you asked for land on it"* about the device
    whose contract watched otto REFUSE one, with a `works` in the grid. That is blocker
    1 verbatim, restored by removing a key, and `promise_mismatch` answered `[]`.

    "Unreachable, therefore harmless" was the wrong reading twice over: unreachable
    from the COLLATOR is not unreachable from the RENDERER, and an arm reached only by
    a hand-edit is reached exactly when someone is editing by hand.
    """
    injected = copy.deepcopy(committed)
    cell = injected["cells"]["transfer-mode"]["zephyr-2.7"]
    assert cell["status"] == "measured-ok", (
        f"zephyr-2.7 is {cell['status']!r}; this guard needs a MEASURED cell to strip"
    )
    assert cell.pop("observable", None), "the cell declares no observable; this guard is vacuous"
    page = _page(injected)
    row = _row(page, "transfer-mode", "zephyr-2.7")
    assert VOICE["transfer-mode"].capability not in row, (
        f"a cell that says nothing about what it watched published the promise: {row}"
    )
    assert "You can" not in row, f"it promised something anyway: {row}"
    assert f"| **{_UNINTERPRETED.headline}** " in row, f"the row says nothing at all: {row}"
    assert _grid_token(page, "transfer-mode", "zephyr-2.7") == _UNINTERPRETED.grid_word


def test_a_measured_cell_that_declares_no_observable_fails_the_build(committed):
    """The other half, and the half that stops `make docs` rather than degrading a row.

    `promise_mismatch`'s per-cell check walked PAST a cell with no observable, which is
    what made the missing field the one way past both defences at once. MEASURED as the
    green pair above: the row promised, and the checker reported nothing.
    """
    injected = copy.deepcopy(committed)
    assert injected["cells"]["transfer-mode"]["zephyr-2.7"].pop("observable", None), "vacuous"
    problems = promise_mismatch(injected)
    assert any(
        "declares no observable" in problem and "zephyr-2.7" in problem for problem in problems
    ), problems


def test_a_cell_nothing_has_watched_yet_still_describes_the_promise(committed):
    """THE ACCEPT-CONTROL for both guards above: the fail-closed discriminates by STATE.

    Without it, a `promise_of` that answered `_UNINTERPRETED` for every missing
    observable would satisfy both, and every `untested` row on a branched surface would
    lose the sentence that is its whole point -- *"no run has yet asked whether you can
    ..."*, which is true of the surface whichever arm a future run would take. The
    branched surface has no such cell in today's artifact, so this makes one.
    """
    assert VOICE["transfer-mode"].branches, "transfer-mode is unbranched; this guard is vacuous"
    for status in sorted(_UNWATCHED_STATES):
        injected = copy.deepcopy(committed)
        injected["cells"]["transfer-mode"]["zephyr-2.7"] = {"status": status}
        assert promise_of("transfer-mode", injected["cells"]["transfer-mode"]["zephyr-2.7"]) == (
            Promise(capability=VOICE["transfer-mode"].capability)
        ), f"a {status!r} cell lost the promise it is waiting on"
        assert promise_mismatch(injected) == [], (
            f"a {status!r} cell declares no observable BY DESIGN and must not fail the build"
        )


def test_a_surface_with_one_observable_still_promises_it(committed):
    """The accept-control for all of the above: five of the six surfaces are unbranched.

    Without it, a `promise_of` that answered "no promise" for everything would satisfy
    every refusal above and publish a page that promises nothing at all.
    """
    page = _page(committed)
    unbranched = [surface.id for surface in SURFACES if not VOICE[surface.id].branches]
    assert len(unbranched) == 5, f"the tree's branching surfaces have changed: {unbranched}"
    for surface_id in unbranched:
        cell = committed["cells"][surface_id]["gnu"]
        assert promise_of(surface_id, cell).capability == VOICE[surface_id].capability
    row = _row(page, "exec-exit-code", "gnu")
    assert "**Yes.** You can run a command and trust the exit code" in row, row


# --------------------------------------------------------------------------
# ... and the arms are pinned to the contracts that write them
# --------------------------------------------------------------------------


def test_the_promise_arms_agree_with_the_contracts_today(committed):
    """The control for every refusal below: today's VOICE and artifact DO agree."""
    assert promise_mismatch(committed) == []


def test_an_arm_the_contract_no_longer_writes_is_refused(committed, monkeypatch):
    """The marker is the ONE question this file asks of an observable, so it is pinned.

    Reword the contract's refusal arm without rewording this file and every Zephyr cell
    silently returns to the promise otto refuses -- the exact defect, restored by a
    typo. It must fail the build instead.

    ★ POKED ON EVERY ARM, NOT JUST THE REFUSAL. MEASURED 2026-08-25: narrowing the
    check to `if not branch.capability and branch.marker not in source:` left all 212
    guards passing, because this guard drifted only `branches[1]`. The CAPABILITY arm's
    marker is pinned for the same reason and the drift is symmetric -- a reworded
    `put(mode=` clause sends every `gnu` cell to whichever arm still matches, which is
    the refusal, and the page tells four working devices they are refused. Check 4
    catches it once the bed regenerates the artifact; the pin is what catches it at the
    edit. So the loop below poisons each arm in turn and requires a report for each.
    """
    voice = VOICE["transfer-mode"]
    assert len(voice.branches) == 2, "the arms have changed; the loop below is describing two"
    rewordings = ("no longer written by the contract", "because it has no permissions")
    for index, replacement in enumerate(rewordings):
        drifted = dict(VOICE)
        branches = list(voice.branches)
        branches[index] = dataclasses.replace(branches[index], marker=replacement)
        drifted["transfer-mode"] = dataclasses.replace(voice, branches=tuple(branches))
        monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
        assert axes_mismatch(committed) == [], "the poison must violate ONLY the promise check"
        assert narrowing_mismatch(committed) == [], "the poison must violate ONLY the promise check"
        problems = promise_mismatch(committed)
        pinned = [p for p in problems if "no longer writes" in p and replacement in p]
        assert pinned, (
            f"arm {index} was reworded to {replacement!r} and the pin said nothing: {problems}"
        )


def test_an_arm_that_declares_both_a_capability_and_a_refusal_is_refused(committed, monkeypatch):
    """A cell licenses one or the other, and an arm claiming both means neither is checked."""
    voice = VOICE["transfer-mode"]
    drifted = dict(VOICE)
    drifted["transfer-mode"] = dataclasses.replace(
        voice,
        branches=(
            dataclasses.replace(voice.branches[0], grid_word="works", instead="and also refuses"),
            voice.branches[1],
        ),
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
    problems = promise_mismatch(committed)
    assert any("capability AND a refusal" in problem for problem in problems), problems


def test_a_refusal_arm_missing_its_grid_word_is_refused(committed, monkeypatch):
    """Half a refusal arm renders an empty bold lead or a blank grid cell.

    That is this item's signature defect one level down -- a missing entry that looks
    like a passing one -- so the incompleteness is reported rather than rendered.
    """
    voice = VOICE["transfer-mode"]
    drifted = dict(VOICE)
    drifted["transfer-mode"] = dataclasses.replace(
        voice,
        branches=(voice.branches[0], dataclasses.replace(voice.branches[1], grid_word="")),
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
    problems = promise_mismatch(committed)
    assert any("incomplete" in problem for problem in problems), problems


def test_an_arms_prose_naming_a_mechanism_the_contract_does_not_is_refused(committed, monkeypatch):
    """`narrowing_mismatch`'s rule, applied to the other piece of hand-written prose.

    The page may not tell a reader about a mechanism the contract does not mention.
    """
    voice = VOICE["transfer-mode"]
    drifted = dict(VOICE)
    drifted["transfer-mode"] = dataclasses.replace(
        voice,
        branches=(
            voice.branches[0],
            dataclasses.replace(
                voice.branches[1],
                instead="its backend answers `supports_permissions` False, so `put` refuses",
            ),
        ),
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
    problems = promise_mismatch(committed)
    assert any("supports_permissions" in problem for problem in problems), problems


def test_a_cell_whose_observable_matches_no_arm_fails_the_build(committed):
    """A contract that grew an arm nobody described must stop the docs build.

    Not be published under whichever promise happened to match first, and not be
    published under none of them silently either: the page degrading and the build
    failing are two different obligations and this item has been bitten by having only
    the first.
    """
    injected = copy.deepcopy(committed)
    injected["cells"]["transfer-mode"]["gnu"]["observable"] = "an arm nobody described"
    problems = promise_mismatch(injected)
    assert any("matches 0 of the 2 observables" in problem for problem in problems), problems


def test_a_cell_whose_observable_matches_two_arms_fails_the_build(committed):
    """Markers that do not discriminate are as bad as a marker that matches nothing."""
    injected = copy.deepcopy(committed)
    both = " ".join(branch.marker for branch in VOICE["transfer-mode"].branches)
    injected["cells"]["transfer-mode"]["gnu"]["observable"] = both
    problems = promise_mismatch(injected)
    assert any("matches 2 of the 2 observables" in problem for problem in problems), problems


def test_the_renderer_refuses_to_write_when_a_promise_came_loose(tmp_path, committed, monkeypatch):
    """`main` must CALL the check, and this item has measured three times what that is worth.

    `element_accounting_errors` (Task 4b), `positive_control_errors` (Task 6b) and
    `cell_outcome_errors` all ran in production with nothing asserting they were still
    wired. So this poisons ONLY the promise check -- `axes_mismatch` and
    `narrowing_mismatch` are asserted empty first -- and requires the real `main` to
    exit non-zero and write no page.
    """
    voice = VOICE["transfer-mode"]
    drifted = dict(VOICE)
    drifted["transfer-mode"] = dataclasses.replace(
        voice,
        branches=(
            voice.branches[0],
            dataclasses.replace(voice.branches[1], marker="an arm this contract never writes"),
        ),
    )
    monkeypatch.setattr("scripts.render_support_matrix.VOICE", drifted)
    assert axes_mismatch(committed) == [], "the poison must violate ONLY the promise check"
    assert narrowing_mismatch(committed) == [], "the poison must violate ONLY the promise check"
    assert promise_mismatch(committed) != []

    matrix = tmp_path / "support_matrix.json"
    matrix.write_text(json.dumps(committed, indent=2) + "\n", encoding="utf-8")
    page = tmp_path / "support-matrix.md"
    assert render_main(["--matrix", str(matrix), "--page", str(page)]) == 1
    assert not page.exists(), "the page was written from a promise nothing backs"


# --------------------------------------------------------------------------
# The grid's OTHER axis: how much of the profile a word stands for
# --------------------------------------------------------------------------


def test_a_grid_word_never_covers_a_profile_the_verdict_does_not(committed):
    """★ THE COVERAGE AXIS OF THE GRID, which nothing held.

    `test_a_cell_whose_drawn_cells_disagreed_never_renders_as_one_answer` holds the
    grid token honest on the TRANSPORT axis. Nothing held it on this one: MEASURED
    2026-08-25, replacing `grid_token`'s `partial` with `""` left **all 192 guards
    passing** while the grid published a bare `works` for `zephyr-3.7` x `transfer`
    (which rests on 2 of that profile's 4 devices) and for `zephyr-4.4` x `transfer`
    (1 of 2) -- against a legend on this very page defining `works` as *"every drawn
    cell passed, **on every device in the profile**"*.

    INJECTED on cells that are whole today, and on all three shapes the suffix can
    ride: a green cell, a red one, and one whose routes disagreed. Today's artifact
    happening to contain a partial cell is what let the hole stay open, so this guard
    makes its own.

    ★★★ AND ON BOTH REASONS A CELL CAN BE PARTIAL, which is the THIRD hole and the same
    mistake in its other direction. `partial` fires on `not_observable OR unaccounted`,
    and the injections here produce only the second -- while EVERY partial cell on the
    committed page is the first, with `unaccounted` empty on all four. MEASURED
    2026-08-25: narrowing the predicate to `if not coverage.unaccounted` left all 215
    guards passing AND degraded the shipped page, `zephyr-3.7` x `transfer-mode` falling
    from `refused (2 of 4)` to a bare `refused`. A guard that injects the shape the page
    does not use and skips the one it does is a guard aimed one cell over, so the
    remainder is now an axis too, and the committed tokens are pinned outright below.

    ★★★ AND ON ALL THREE PROMISE ARMS, WHICH IS THE SECOND HOLE AND IT WAS INSIDE THE
    FIX FOR THE FIRST. `grid_token` composes `{word}{partial}` at FIVE returns, and
    they do not share a code path: the `capability` returns are reached only after the
    `not promise.capability` return above them. The first version of this guard injected
    on `exec-exit-code`, an UNBRANCHED surface that can only take a capability arm, so
    the refusal return was covered by nothing here -- MEASURED 2026-08-25, replacing
    just `f"{promise.grid_word}{partial}"` with `promise.grid_word` left **all 212
    guards passing**, and the committed page would then publish a bare `refused` for
    `zephyr-3.7` (2 of 4 devices) beside a `works (2 of 4)` in the next column: a
    profile-wide refusal claimed from two devices out of four. A green subset of the
    very mutation this guard was written for. So the arm is now an axis of the loop,
    driven by injecting the observable that selects it.
    """
    whole = cell_coverage(committed, "exec-exit-code", "gnu")
    assert whole.observed_on == whole.elements, (
        "exec-exit-code x gnu is no longer whole; this guard has nothing to make partial"
    )
    assert _grid_token(_page(committed), "exec-exit-code", "gnu") == "works", (
        "the control: a whole cell publishes the bare word"
    )
    mode = cell_coverage(committed, "transfer-mode", "gnu")
    assert mode.observed_on == mode.elements, (
        "transfer-mode x gnu is no longer whole; the refusal arm has nothing to make partial"
    )

    def _partial(
        surface_id: str,
        shape: str,
        observable: "str | None" = None,
        remainder: str = "unaccounted",
    ) -> dict:
        injected = copy.deepcopy(committed)
        cell = injected["cells"][surface_id]["gnu"]
        if observable is not None:
            cell["observable"] = observable
        dropped = cell["observed_on"][-1]
        cell["observed_on"] = cell["observed_on"][:-1]
        cell["observed_cells"] = [e for e in cell["observed_cells"] if e["element"] != dropped]
        if remainder == "not-observable":
            # The dropped device stops being "nobody has looked" and becomes "there is
            # nothing here to look at" -- the SAME arithmetic, the other disjunct, and
            # the only one the committed page actually exercises.
            module = next(s.contract for s in SURFACES if s.id == surface_id).split("::")[0]
            cell["not_observable"].append(
                {
                    "element": dropped,
                    "probed": f"{module}::applicable_cell(injected, for this guard)",
                    "probe_result": "False -- injected, for this guard",
                }
            )
        if shape != "measured-ok":
            cell["status"] = "measured-broken"
            cell["failure_summary"] = "injected, for this guard"
            cell.pop("positive_control", None)
            failing = cell["observed_cells"] if shape == "broken" else cell["observed_cells"][:1]
            for entry in failing:
                entry["outcome"] = "xfailed"
                entry.pop("positive_control", None)
        return injected

    remainders = ("unaccounted", "not-observable")
    for shape in ("measured-ok", "broken", "mixed"):
        for remainder in remainders:
            injected = _partial("exec-exit-code", shape, remainder=remainder)
            token = _grid_token(_page(injected), "exec-exit-code", "gnu")
            assert token.endswith(" (3 of 4)"), (
                f"the grid publishes {token!r} for a {shape} verdict resting on 3 of the "
                f"profile's 4 devices, the fourth being {remainder!r} -- one word "
                f"standing for a whole profile it does not cover"
            )

    # ★ THE ARM AXIS. `transfer-mode` x `gnu` really is measured against the mode it
    # reads back, so BOTH no-capability arms are hostile conditions INJECTED here and
    # not inherited from an artifact that happens to hold one. The expected word is
    # read from the declaration rather than typed, for the reason the refusal guards
    # upstream record: a word this guard spells itself is a word a rename can silently
    # part from. `_page` renders whatever it is handed, so the uninterpreted arm --
    # which `promise_mismatch` refuses at build time -- is still reachable to assert on.
    refusal = next(branch for branch in VOICE["transfer-mode"].branches if not branch.capability)
    refusing = committed["cells"]["transfer-mode"]["zephyr-2.7"]["observable"]
    assert refusal.marker in refusing, (
        "the injected observable no longer selects the refusal arm; this guard is vacuous"
    )
    arms = {
        refusal.grid_word: refusing,
        _UNINTERPRETED.grid_word: "something nobody described",
    }
    assert len(arms) == 2, "the two no-capability arms print the same word; this guard is vacuous"
    for word, observable in arms.items():
        assert word, "the arm declares no grid word; the assertions below are vacuous"
        for shape in ("measured-ok", "broken", "mixed"):
            for remainder in remainders:
                injected = _partial("transfer-mode", shape, observable, remainder)
                token = _grid_token(_page(injected), "transfer-mode", "gnu")
                assert token == f"{word} (3 of 4)", (
                    f"the grid publishes {token!r} for a {shape} cell whose contract "
                    f"watched {word!r} on 3 of the profile's 4 devices, the fourth being "
                    f"{remainder!r} -- one word standing for a whole profile it does not "
                    f"cover, and it reads WORSE than the `works` case: a refusal claimed "
                    f"profile-wide from a subset tells a reader their device is refused "
                    f"when nothing measured it"
                )

    # ★★★ AND THE FIFTH RETURN, WHICH THE THREE SHAPES ABOVE CANNOT REACH. The
    # per-transport word -- `` `shell` only `` -- is chosen only when `split.varying`
    # holds exactly ONE axis, and every cell wide enough to be made partial by dropping
    # a device varies on the element axis too, so `_partial` above always lands on
    # `partly broken`. MEASURED 2026-08-25: deleting `{partial}` from THAT return alone
    # left all 215 guards passing, including the four cases above.
    #
    # It is the shape every BusyBox row is ONE lab change away from -- today those
    # profiles hold a single device, so the suffix is empty and the hole cannot be seen
    # on the committed page. The day the bed gains a second guest at that version, a
    # bare `` `shell` only `` would tell a reader `shell` carries their file on a
    # device no run has drawn at all. So the guard builds that cell rather than waiting
    # for the lab to: `gnu` x `transfer-roundtrip` is narrowed to ONE device and ONE
    # terminal, which leaves `transfer` the only axis that varies and puts the verdict
    # on 1 of the profile's 4 devices at the same time.
    narrowed = copy.deepcopy(committed)
    cell = narrowed["cells"]["transfer-roundtrip"]["gnu"]
    kept = [e for e in cell["observed_cells"] if e["element"] == "test1" and e["term"] == "ssh"]
    assert len({e["transfer"] for e in kept}) > 1, (
        "test1 over ssh no longer spans two transports; there is no per-transport word to make"
    )
    survivor = min(e["transfer"] for e in kept)
    cell["observed_on"] = ["test1"]
    cell["observed_cells"] = kept
    cell["status"] = "measured-broken"
    cell["failure_summary"] = "injected, for this guard"
    cell.pop("positive_control", None)
    for entry in kept:
        if entry["transfer"] != survivor:
            entry["outcome"] = "xfailed"
            entry.pop("positive_control", None)
    token = _grid_token(_page(narrowed), "transfer-roundtrip", "gnu")
    assert token == f"`{survivor}` only (1 of 4)", (
        f"the grid publishes {token!r} for a per-transport verdict resting on 1 of the "
        f"profile's 4 devices -- the transport word is the most specific thing in the "
        f"grid, and a reader who trusts it reads it as true of their whole profile"
    )

    # ★ AND THE SHIPPED CELLS, PINNED RATHER THAN DESCRIBED. Everything above is
    # injection, which is what keeps the guard alive when the lab changes; this is what
    # keeps it honest about the page that goes out today. Every partial cell the
    # artifact holds is `not_observable`-shaped, and its suffix is derived arithmetic
    # over fields, so pinning the rendered string is a check on the whole chain and not
    # a restatement of it. A cell that stops being partial is a CHANGED grid, so it
    # belongs in a diff rather than in an `if`.
    page = _page(committed)
    for surface_id, profile_id, count in (
        ("transfer-roundtrip", "zephyr-3.7", "(2 of 4)"),
        ("transfer-roundtrip", "zephyr-4.4", "(1 of 2)"),
        ("transfer-mode", "zephyr-3.7", "(2 of 4)"),
        ("transfer-mode", "zephyr-4.4", "(1 of 2)"),
    ):
        shipped = cell_coverage(committed, surface_id, profile_id)
        drifted = (
            f"{surface_id} x {profile_id} is no longer the not-observable-shaped partial "
            f"cell this pin was written for: not_observable={shipped.not_observable}, "
            f"unaccounted={shipped.unaccounted}"
        )
        assert shipped.not_observable, drifted
        assert not shipped.unaccounted, drifted
        token = _grid_token(page, surface_id, profile_id)
        assert token.endswith(f" {count}"), (
            f"the committed page publishes {token!r} for {surface_id} x {profile_id}, a "
            f"verdict that rests on {len(shipped.observed_on)} of the profile's "
            f"{len(shipped.elements)} devices because the rest cannot express the "
            f"observable -- this is the shipped grid, not an injected one"
        )


def test_the_grid_legend_explains_every_word_the_grid_can_print(committed):
    """A token a reader meets with no entry in the key is a word they have to guess at.

    Both new words -- `refused` and the `(k of n)` suffix, which stopped being a
    `works`-only thing when a refusal could carry it -- arrived with this task, and an
    unexplained token in the one table people read first is a worse defect than the
    sentence it replaced.
    """
    page = _page(committed)
    legend = page[page.index("\n`works`\n") : page.index("## The profiles")]
    keys = {
        "works": "`works`",
        "broken": "`broken`",
        "refused": "`refused`",
        "not interpreted": "`not interpreted`",
        "not observable": "`not observable`",
        "untested": "`untested`",
    }
    seen = set()
    for surface in SURFACES:
        for profile in committed["profiles"]:
            token = _grid_token(page, surface.id, profile["id"])
            word = token.split(" (")[0]
            key = keys.get(word, "`` `x` only ``" if word.endswith(" only") else None)
            assert key is not None, (
                f"the grid prints {word!r}, which this guard has no legend key for -- "
                f"a new token needs an entry in the page's key and a line here"
            )
            assert f"\n{key}\n:" in legend, (
                f"the grid prints {word!r} and the legend never says what it means"
            )
            seen.add(key)
            if token != word:
                assert "\n`(k of n)`\n:" in legend, (
                    f"the grid prints the suffix in {token!r} and the legend does not explain it"
                )
    assert len(seen) > 1, "the grid prints one word only; this guard is vacuous"


# --------------------------------------------------------------------------
# Counting, and the grammar of counting
# --------------------------------------------------------------------------


def test_the_registered_gap_tally_counts_only_the_failures_the_suite_predicted(committed):
    """★ THE ROW IS GUARDED; THE SECTION'S COUNT WAS NOT.

    MEASURED 2026-08-25: replacing `_registered_gaps_section`'s `predicted` with
    `list(broken)` left all 192 guards passing, and the page would then say *"10 of them
    failed in a way the suite predicted"* with a surprise failure among them -- the one
    sentence on the page that tells a reader none of this is news. Cardinality
    blindness: the count can only diverge in a state no guard injected, so this injects
    one.
    """
    broken = [
        (surface_id, profile_id)
        for surface_id, profile_id, cell, _ in _cells(committed)
        if cell["status"] == "measured-broken"
    ]
    assert broken, "no measured-broken cell in the artifact; this guard is vacuous"

    def _tally(matrix: dict) -> str:
        page = _page(matrix)
        return next(
            line for line in page.splitlines() if "of these cells read `measured-broken`" in line
        )

    assert _tally(committed) == (
        f"{len(broken)} of these cells read `measured-broken`, and **{len(broken)} of them"
    ), "the control: every broken cell in the artifact today failed as the suite predicted"

    injected = copy.deepcopy(committed)
    surprise = injected["cells"][broken[0][0]][broken[0][1]]
    entry = next(e for e in surprise["observed_cells"] if e["outcome"] == "xfailed")
    entry["outcome"] = "failed"
    assert _tally(injected) == (
        f"{len(broken)} of these cells read `measured-broken`, and **{len(broken) - 1} of them"
    ), "a failure nothing predicted was counted as one the suite did"


def test_no_row_counts_a_single_drawn_cell_in_the_plural(committed):
    """ "every one of the 1 drawn cells passed" -- visible on every single-route row.

    `zephyr-2.7` and `zephyr-4.4` draw one cell per device, so this was not a corner:
    it was a third of the page. Task 6b fixed exactly this class one clause over, in
    the control citation, and left these two. Asserted BOTH ways -- the singular is
    present where it belongs, and the plural-of-one appears nowhere -- because a
    renderer that dropped the count entirely would satisfy only the second.
    """
    page = _page(committed)
    for wrong in (" 1 drawn cells", "all 1 ", "the drawn cells `bed"):
        assert wrong not in page, f"the page counts one thing in the plural: {wrong!r}"

    single = [
        (surface_id, profile_id)
        for surface_id, profile_id, cell, _ in _cells(committed)
        if len(cell.get("observed_cells", [])) == 1
    ]
    assert single, "no single-route cell in the artifact; this guard is vacuous"
    surface_id, profile_id = single[0]
    assert "its only drawn cell" in _row(page, surface_id, profile_id)
    assert "*Drawn cells:* the only one" in _evidence_block(page, surface_id, profile_id)


def test_a_multi_route_cell_reduced_to_one_route_is_still_counted_in_the_singular(committed):
    """The guard above inherits its single-route cells from the bed; this one makes one."""
    injected = copy.deepcopy(committed)
    cell = injected["cells"]["exec-exit-code"]["busybox-1.16.1"]
    assert len(cell["observed_cells"]) > 1, "nothing to reduce; this guard is vacuous"
    cell["observed_cells"] = cell["observed_cells"][:1]
    cell["positive_control"] = cell["observed_cells"][0]["positive_control"]

    page = _page(injected)
    assert " 1 drawn cells" not in page
    assert "its only drawn cell passed" in _row(page, "exec-exit-code", "busybox-1.16.1")
    assert "*Drawn cells:* the only one passed." in _evidence_block(
        page, "exec-exit-code", "busybox-1.16.1"
    )


def test_a_measured_ok_cell_with_no_control_promises_nothing_either(committed):
    """★ THE NINTH LEVEL OF THIS ITEM'S OWN DEFECT, found by mutating my own new fix.

    Task 6b closed R3 -- the page promising a route directly above *"positive control:
    **none**"* for it -- on a MIXED cell, and left the identical hole on the status that
    makes the STRONGER claim. MEASURED 2026-08-25 by stripping the citations from
    `transfer-roundtrip` x `gnu` and rendering: the row said *"**Yes.** You can put a
    file on the device and get the same bytes back -- watched on all 4 devices"* while
    its own evidence four lines down said *"*Positive control:* **none** ... so the pass
    there is an observation and not a guarantee"*. A contradiction in one section, from
    the page this item exists to keep honest.

    `test_the_evidence_and_the_verdict_never_disagree_about_a_missing_control` asserts
    exactly that equality -- and could not see this, because its injection is on a
    `measured-broken` cell and the collate step refuses to WRITE an uncited
    `measured-ok` one. A guard that inherits which status its hostile condition lands
    on is blind to the other.

    ALSO the only thing on this page that reaches `_where`'s by-label fallback with ONE
    label, so it is what keeps *"on the drawn cell"* from silently going back to the
    plural.
    """
    injected = copy.deepcopy(committed)
    cell = injected["cells"]["transfer-roundtrip"]["zephyr-2.7"]
    assert cell["status"] == "measured-ok", "not a measured-ok cell any more"
    assert len(cell["observed_cells"]) == 1, "not a single-route cell any more"
    label = cell["observed_cells"][0]["cell_label"]
    assert cell["observed_cells"][0].pop("positive_control", None), "nothing was taken away"
    cell.pop("positive_control", None)

    page = _page(injected)
    row = _row(page, "transfer-roundtrip", "zephyr-2.7")
    block = _evidence_block(page, "transfer-roundtrip", "zephyr-2.7")
    assert VOICE["transfer-roundtrip"].capability not in row, (
        f"an uncited `measured-ok` cell still makes the promise: {row}"
    )
    assert "nothing proved it could fail there" in row, f"the row was not downgraded: {row}"
    assert "*Positive control:* **none**" in block, block
    assert f"on the drawn cell `{label}`" in row, f"one drawn cell, named in the plural: {row}"
    assert "on the drawn cells `" not in page


def test_a_refusal_cell_that_broke_still_reports_where_it_broke(committed):
    """The refusal row's OTHER two outcome shapes, neither of which the bed has drawn.

    A cell can watch a refusal and still have that refusal fail somewhere -- and
    `meaning`'s no-promise return has one arm per shape. MEASURED as a GREEN mutation:
    collapsing the two into a constant *"it was measured"* left every guard passing,
    because the only refusal cells on today's page are whole. The collate step cannot
    write these, and this page renders whatever the artifact holds, which is the same
    argument Task 6b made for its own degraded branch.

    `busybox-1.16.1` x `transfer-mode` is the base because its drawn cells differ along
    ONE axis, so the row names transports rather than falling back to a list of labels.
    """
    refusal = committed["cells"]["transfer-mode"]["zephyr-2.7"]["observable"]

    mixed = copy.deepcopy(committed)
    cell = mixed["cells"]["transfer-mode"]["busybox-1.16.1"]
    assert {e["outcome"] for e in cell["observed_cells"]} == {"passed", "xfailed"}, (
        "the base cell is no longer mixed; this guard is vacuous"
    )
    cell["observable"] = refusal
    row = _row(_page(mixed), "transfer-mode", "busybox-1.16.1")
    assert "it held over `shell`" in row, f"the row does not say where the refusal held: {row}"
    assert "failed over `nc`" in row, f"the row does not say where it broke: {row}"
    assert "the suite predicted" in row, f"the row does not say whose news it is: {row}"

    dead = copy.deepcopy(mixed)
    cell = dead["cells"]["transfer-mode"]["busybox-1.16.1"]
    for entry in cell["observed_cells"]:
        entry["outcome"] = "xfailed"
        entry.pop("positive_control", None)
    row = _row(_page(dead), "transfer-mode", "busybox-1.16.1")
    assert "every drawn cell failed" in row, f"a wholly broken refusal reads as intact: {row}"
    assert "the suite predicted" in row, row


def test_a_narrowing_clause_is_written_for_one_device_or_for_four(committed):
    """A clause appended to a list of unknown length may not commit to a number.

    `_remainder_sentences` joins the devices that could not be measured and then appends
    `VOICE`'s hand-written clause, and that list holds ONE element on `zephyr-4.4` and
    two on `zephyr-3.7`. MEASURED 2026-08-25 by reading the rendered page: *"`zephyr44_llext`
    could not be measured: otto reports nowhere to put a file on **them** -- **their**
    filesystem backend answers `supports_transfer` False"*, about a single device. The
    same clause is also spliced into the `not-observable` sentence, so one plural
    pronoun was wrong in two places.

    The rule, rather than the wording: the clause is number-agnostic. Guarded here
    rather than in `narrowing_mismatch` because it is about English and not about the
    domain rule, and because `tests/unit/test_docs_gap_sync.py`'s precedent is that
    prose gets a RULE and not a copy.
    """
    plural = (" them", " they ", " their ", "these devices", "those devices")
    for surface in SURFACES:
        clause = VOICE[surface.id].narrowed
        for word in plural:
            assert word not in clause, (
                f"surface {surface.id!r}: its narrowing clause says {word!r}, and the "
                f"list it is appended to can hold one device -- {clause!r}"
            )
    page = _page(committed)
    single = [
        (surface_id, profile_id)
        for surface_id, profile_id, _cell, coverage in _cells(committed)
        if len(coverage.not_observable) == 1
    ]
    assert single, "no cell excludes exactly one device; this guard is vacuous"
    read = 0
    for surface_id, profile_id in single:
        row = _row(page, surface_id, profile_id)
        # A `not-observable` row splices the clause into a DIFFERENT sentence -- that
        # half of the remainder IS the cell there, so it is never repeated as an
        # exclusion -- and its lead ("has nothing on ... to be watched against") is
        # itself about the excluded devices. MEASURED as a GREEN mutation: anchoring on
        # the colon read past the very phrase that was wrong. So the segment starts at
        # whichever lead comes first and runs to the never-drawn sentence, which is the
        # only other prose in the row and is about a different set of devices.
        leads = [lead for lead in ("could not be measured:", "The promise --") if lead in row]
        assert leads, f"{surface_id} x {profile_id} excludes a device and never says so: {row}"
        segment = row[min(row.index(lead) for lead in leads) :]
        segment = segment.split(" never been drawn")[0]
        read += 1
        for word in plural:
            assert word not in segment, (
                f"{surface_id} x {profile_id} excludes one device and the row says "
                f"{word!r}: {segment}"
            )
    assert read, "no clause was read; this guard is vacuous"
