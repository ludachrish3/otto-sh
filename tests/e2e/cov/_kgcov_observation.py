"""What a `make kgcov` run LEAVES BEHIND: one JSON record per (test, compiler).

The kgcov compatibility matrix (``schemas/kgcov_matrix.json``) may gain a
``measured-*`` verdict only from these records, so this module's whole job
is to be un-fabricatable, for the reasons ``tests/conformance/_observation.py``
gives at length and this module borrows rather than restates: the outcome
is read from pytest's phase reports at the TEARDOWN report, never from a
test body (a record written inside a test reports success for a test that
then fails); a skip is its own outcome, which no collation reads as
evidence; the record's provenance — compiler version and kernel release —
is taken from the :class:`~tests.e2e.cov._repo5_build.Toolchain` the
fixture already measured, through ``config.stash``, never probed again.

The records go to :func:`observations_dir`, ``reports/kgcov-observations/``
under ``PROJECT_ROOT`` (git-ignored; ``make clean`` removes ``reports/``).
The first write of a session clears the directory, so a fold is never
contaminated by an earlier run's leftovers; records of one session share a
``run_id``, which is how the collate step requires a column's control and
its contracts to come from the same run. BOTH of those are properties of
ONE PROCESS: a ``-n`` run hands each xdist worker its own copy of this
module, so an N-worker run would clear the others' records out from under
them and mint N different run_ids for what is supposed to be one session.
The hook refuses to write under a worker rather than publish a record that
looks like it came from a run that never happened; ``make kgcov`` runs
``-n0`` for exactly this reason.
"""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from tests._fixtures.kgcov_matrix import BED, BUILD, CROSS_PROFILE, surface_for
from tests._fixtures.paths import PROJECT_ROOT
from tests.conformance._observation import failure_summary, outcome_of, today
from tests.e2e.cov._repo5_build import TOOLCHAINS_KEY

FORMAT = 1
OBSERVATION = "observation"
DEFAULT_OBSERVATIONS_DIR = PROJECT_ROOT / "reports" / "kgcov-observations"

BED_PARAM = "built_with"
BUILD_FIXTURE = "cross_build"

_PHASE_REPORTS = pytest.StashKey["dict[str, object]"]()
_RUN = pytest.StashKey["dict[str, object]"]()


def observations_dir() -> Path:
    """Where this run writes its records: always the one committed default.

    No env override (unlike ``tests/conformance/_observation.py``'s): a
    kgcov run has no xdir-shaped invocation to redirect from, and giving the
    collator a fixed path is what lets it be run with no arguments.
    """
    return DEFAULT_OBSERVATIONS_DIR


def contract_of(nodeid: str) -> str:
    """*nodeid* without its parametrization — the matrix row's own id."""
    return nodeid.split("[", 1)[0]


def _run(config) -> dict:
    """This session's mutable slot: a ``run_id`` minted once and which
    directories have already had their stale records cleared.

    Stashed on ``config`` rather than module state so that two ``_Config``
    instances in a test (or, in a real run, two processes each with their
    own ``pytest.Config``) never share a run identity by accident — the
    thing :func:`run_id` promises callers.
    """
    slot = config.stash.get(_RUN, None)
    if slot is None:
        slot = {"id": uuid.uuid4().hex, "cleared": set()}
        config.stash[_RUN] = slot
    return slot


def run_id(config) -> str:
    """One id per pytest session, minted on first use."""
    return _run(config)["id"]


def profile_of_item(item) -> "tuple[str, str] | None":
    """``(profile, venue)`` for *item*, or None when it measures no column.

    A bed item carries the compiler in its callspec (the ``built_with``
    parameter, whose value is the NAME); a build item takes ``cross_build``.

    Placement by fixture/param alone is not the whole answer: a nodeid the
    surface table (:data:`~tests._fixtures.kgcov_matrix.SURFACES`) does not
    name writes nothing here, silently -- that nodeid is not a matrix row,
    and ``tests/unit/test_kgcov_matrix.py``'s axis guard is what reddens a
    contract the tree declares but the table forgets, loudly, at the point
    where a human edited the wrong file. But a nodeid the table DOES name,
    placed in a venue the table disagrees with, is not a missing row -- it
    is this function's own placement logic wrong, or a surface hand-edited
    out of step with its fixture, and that is raised rather than swallowed:
    a hook that silently mis-filed a record would let a bed result count
    toward a build column (or the reverse) with nothing in the artifact to
    say so.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is not None and BED_PARAM in callspec.params:
        placed = str(callspec.params[BED_PARAM]), BED
    elif BUILD_FIXTURE in getattr(item, "fixturenames", ()):
        placed = CROSS_PROFILE, BUILD
    else:
        return None
    surface = surface_for(contract_of(item.nodeid))
    if surface is None:
        return None
    if surface.venue != placed[1]:
        raise RuntimeError(
            f"{item.nodeid}: placed in venue {placed[1]!r} by its fixture/param, "
            f"but the surface table declares {surface.venue!r} for this contract"
        )
    return placed


def observation_record(
    *, item, profile: str, venue: str, outcome: str, summary: "str | None"
) -> dict:
    """The JSON-able document for one placed item's outcome.

    Provenance is read off ``item.config.stash[TOOLCHAINS_KEY]`` -- the
    toolchain the fixture already measured for *profile* -- and NEVER
    re-probed here: probing again would let this record disagree with the
    one the fixture actually built with, and would give the hook a reason
    to run at all outside a test session. Absent (``tc is None``) is a real
    outcome, not a bug -- see the null-provenance test -- and is carried
    through as ``None`` rather than guessed.
    """
    tc = (item.config.stash.get(TOOLCHAINS_KEY, None) or {}).get(profile)
    record = {
        "kind": OBSERVATION,
        "format": FORMAT,
        "nodeid": contract_of(item.nodeid),
        "item": item.nodeid,
        "profile": profile,
        "venue": venue,
        "outcome": outcome,
        "compiler_version": tc.compiler_version if tc is not None else None,
        "kernel_release": tc.kernel_release if tc is not None else None,
        "as_of": today(),
        "run_id": run_id(item.config),
    }
    if summary is not None:
        record["failure_summary"] = summary
    return record


def record_filename(record: dict) -> str:
    """Stable per (nodeid, profile), so a rerun REPLACES rather than accumulates."""
    digest = hashlib.sha256(f"{record['nodeid']}\x1f{record['profile']}".encode()).hexdigest()[:16]
    return f"{record['kind']}-{record['profile']}-{digest}.json"


def write_record(directory: Path, record: dict) -> Path:
    """Write *record* to its stable path under *directory*, creating it first.

    ``record_filename`` keys the path on ``(nodeid, profile)``, so this
    OVERWRITES a prior record for the same cell rather than accumulating a
    second file for it -- a rerun of the same item under the same compiler
    replaces its own evidence rather than leaving stale evidence beside it.
    A different nodeid or a different profile gets its own path, and both
    survive side by side.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / record_filename(record)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def read_records(directory: Path) -> "list[dict]":
    """Every record under *directory*, sorted by filename for a stable fold.

    An absent directory answers ``[]`` rather than raising: the collate
    step's own job is to tell an empty run from a missing one, not this
    reader's.
    """
    if not directory.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def _clear_once(config, directory: Path) -> None:
    """Empty *directory* the FIRST time this session writes to it, never again.

    Refuses outright under an xdist worker (``config.workerinput`` is only
    ever set on a worker's own ``Config``, never on the controller's): each
    worker imports this module fresh, so an N-worker run would mint N
    run_ids and each worker's first write would delete every other worker's
    records out from under it -- the exact contamination this function
    exists to prevent, turned into contamination it would be the CAUSE of.
    Loud rather than quiet, like every other hook fault here: a run that
    silently dropped this guard would still look "measured", one compiler
    short of the truth.
    """
    if hasattr(config, "workerinput"):
        raise RuntimeError(
            "kgcov observations are written by a -n0 run only: each xdist worker "
            "would clear the others' records and mint its own run_id; make kgcov "
            "runs -n0"
        )
    cleared = _run(config)["cleared"]
    if directory in cleared:
        return
    if directory.is_dir():
        for stale in directory.glob("*.json"):
            stale.unlink()
    cleared.add(directory)


def record_phase(item, report, directory: Path) -> "Path | None":
    """Accumulate *report*; on TEARDOWN write the item's record. None otherwise.

    Placement is checked FIRST, before this item's phase reports are ever
    stashed: an item that ``profile_of_item`` answers None for measures no
    matrix column, so nothing about it should accumulate here at all -- and
    checking first is also what lets a misplaced item's :class:`RuntimeError`
    surface at ``setup``, rather than waiting for ``teardown`` to notice.
    """
    placed = profile_of_item(item)
    if placed is None:
        return None
    profile, venue = placed
    reports = item.stash.get(_PHASE_REPORTS, None)
    if reports is None:
        reports = {}
        item.stash[_PHASE_REPORTS] = reports
    reports[report.when] = report
    if report.when != "teardown":
        return None
    _clear_once(item.config, directory)
    return write_record(
        directory,
        observation_record(
            item=item,
            profile=profile,
            venue=venue,
            outcome=outcome_of(reports),
            summary=failure_summary(reports),
        ),
    )
