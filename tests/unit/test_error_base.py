"""The OttoError invariant: every public exception otto raises is an OttoError.

Converts the churn-and-design review's "every subsystem names its outcome
convention" policy into a gate: each public exception class keeps its original
stdlib root (so existing ``except ValueError`` / ``except RuntimeError``
handlers stay correct) while also subclassing :class:`otto.errors.OttoError`,
so one ``except OttoError`` clause catches anything otto raised.
"""

import builtins

import pytest

from otto.bootstrap import BootstrapError, DependencyError
from otto.cli.invoke import LabContextError
from otto.coverage.capture.gitio import GitUnavailableError
from otto.coverage.errors import (
    CoverageConfigError,
    CoverageDataMismatchError,
    CoverageToolVersionError,
    NoCoverageDataError,
)
from otto.coverage.overrides import OverrideConfigError
from otto.coverage.tickets import TicketConfigError
from otto.errors import OttoError
from otto.host.app_shell import AppShellActiveError, AppShellTimeoutError, ParseMismatch
from otto.host.login_proxy import LoginProxyError
from otto.labs.errors import LabNotFoundError, LabRepositoryError
from otto.lifecycle import SyncPhaseInterrupt
from otto.monitor.archive_edit import ArchiveLockedError
from otto.monitor.db import UnsupportedDBError
from otto.monitor.event_ops import EventValidationError
from otto.reservations.check import MissingReservationError, ReservationBackendError
from otto.suite.run import NoTestsMatchedError
from otto.suite.selection import UnknownSelectionError
from otto.tunnel.records import TunnelScanFailedError

CASES: list[tuple[type[BaseException], type[BaseException]]] = [
    (BootstrapError, Exception),
    (DependencyError, Exception),
    (LabContextError, Exception),
    (GitUnavailableError, RuntimeError),
    (CoverageToolVersionError, RuntimeError),
    (CoverageConfigError, ValueError),
    (NoCoverageDataError, ValueError),
    (CoverageDataMismatchError, RuntimeError),
    (OverrideConfigError, ValueError),
    (TicketConfigError, ValueError),
    (ParseMismatch, ValueError),
    (AppShellActiveError, RuntimeError),
    (AppShellTimeoutError, TimeoutError),
    (LoginProxyError, ConnectionError),
    (LabRepositoryError, Exception),
    (LabNotFoundError, Exception),
    (ArchiveLockedError, RuntimeError),
    (UnsupportedDBError, RuntimeError),
    (EventValidationError, ValueError),
    (ReservationBackendError, Exception),
    (MissingReservationError, Exception),
    (NoTestsMatchedError, ValueError),
    (UnknownSelectionError, ValueError),
    (TunnelScanFailedError, RuntimeError),
]


@pytest.mark.parametrize(("cls", "stdlib_root"), CASES, ids=[cls.__name__ for cls, _ in CASES])
def test_public_exception_is_ottoerror_and_keeps_stdlib_root(cls, stdlib_root):
    """Re-parenting added OttoError without disturbing the original stdlib root."""
    assert issubclass(cls, OttoError)
    assert issubclass(cls, stdlib_root)


def test_sync_phase_interrupt_stays_outside_ottoerror():
    """SyncPhaseInterrupt must never join the OttoError (Exception) hierarchy.

    It is raised by :func:`otto.lifecycle.sync_phase`'s signal handler and must
    never be swallowed by ``except Exception`` — an Exception-rooted OttoError
    base would break the ``128 + signum`` exit contract. It is the documented
    exception to the OttoError rule.
    """
    assert not issubclass(SyncPhaseInterrupt, OttoError)
    assert issubclass(SyncPhaseInterrupt, KeyboardInterrupt)


# The enumerated CASES above pin the KNOWN classes; this sweep is the
# completeness half of the gate — a future public exception added without
# OttoError fails here, not in review prose (the repo's own history says
# ungated conventions decay).

_STDLIB_EXCEPTION_SEEDS = frozenset(
    name
    for name, obj in vars(builtins).items()
    if isinstance(obj, type) and issubclass(obj, BaseException)
)

_SWEEP_EXCLUSIONS = frozenset(
    {
        # KeyboardInterrupt by signal contract — see test above.
        "SyncPhaseInterrupt",
    }
)


def _base_names(node) -> set[str]:
    import ast

    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def test_every_public_exception_in_src_is_ottoerror_rooted():
    """AST sweep: no public Exception-family class under src/otto escapes OttoError.

    Statically walks every module, fixpoints the exception family from the
    stdlib seeds (so grandchildren like ``DependencyError(BootstrapError)``
    count), then requires each PUBLIC member to reach ``OttoError`` through
    its base chain or appear in the explicit exclusion list. Private
    (``_``-prefixed) classes are internal control-flow signals and exempt.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).parents[2] / "src" / "otto"
    assert src.is_dir(), src

    bases_by_class: dict[str, set[str]] = {}
    file_by_class: dict[str, str] = {}
    for py in sorted(src.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.bases:
                # First definition wins on a (non-exception) name collision;
                # exception class names are unique across src/otto today.
                bases_by_class.setdefault(node.name, _base_names(node))
                file_by_class.setdefault(node.name, str(py.relative_to(src)))

    # Fixpoint 1: which classes are exception-family?
    family: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, bases in bases_by_class.items():
            if name not in family and bases & (_STDLIB_EXCEPTION_SEEDS | family):
                family.add(name)
                changed = True

    # Fixpoint 2: which of those reach OttoError?
    rooted: set[str] = {"OttoError"}
    changed = True
    while changed:
        changed = False
        for name in family:
            if name not in rooted and bases_by_class[name] & rooted:
                rooted.add(name)
                changed = True

    offenders = sorted(
        f"{name} ({file_by_class[name]})"
        for name in family
        if not name.startswith("_")
        and name not in rooted
        and name not in _SWEEP_EXCLUSIONS
        and name != "OttoError"
    )
    assert not offenders, (
        "public exception classes without an OttoError root "
        f"(re-parent them or add a justified exclusion): {offenders}"
    )
    # Positive control: the sweep actually sees the family (a broken walk
    # would pass vacuously with an empty offender list).
    assert {"BootstrapError", "DependencyError", "LoginProxyError"} <= family
    assert "SyncPhaseInterrupt" in family
    assert "SyncPhaseInterrupt" not in rooted
