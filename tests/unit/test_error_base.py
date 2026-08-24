"""The OttoError invariant: every public exception otto DEFINES is an OttoError.

Converts the churn-and-design review's "every subsystem names its outcome
convention" policy into a gate: each public exception class keeps its original
stdlib root (so existing ``except ValueError`` / ``except RuntimeError``
handlers stay correct) while also subclassing :class:`otto.errors.OttoError`,
so one ``except OttoError`` clause catches all of them at once.

DEFINES, not raises. otto also raises plain stdlib exceptions at several
hundred sites — rejecting an argument with a bare ``ValueError`` is ordinary
and is not going to change — so ``except OttoError`` means "one of otto's
named failures", never "anything otto raised". The gate is scoped to the
claim that is actually true; the docstrings that claimed the wider one have
been corrected.

Four properties, and the last two exist because the first two together let a
new error through. The sweep proves a class reaches ``OttoError``; ``CASES``
proves a class keeps its stdlib root. But nothing made a NEW class appear in
``CASES``, and a ``CASES`` row declaring bare ``Exception`` asserts nothing —
every exception is an ``Exception``. So ``class FooError(OttoError)`` passed
both while being uncatchable by any ``except ValueError`` in a caller's code,
and ``(FooError, Exception)`` "fixed" it without changing anything. Eight
classes genuinely have no stdlib root; they are now listed by name, which is
what lets the assertion fail for the ninth.

This file owns the RAISES half of the convention. The RETURNS half — public
API returns a Result-family value, never a bare ``Status`` — is gated by
``.ast-grep/rules/no-bare-status-return.yml`` (run by ``make lint-arch``).
Both are documented in ``docs/architecture/utilities/results.md``.
"""

import builtins

import pytest

from otto.bootstrap import BootstrapError, DependencyError, ProjectScopeError
from otto.cli.invoke import LabContextError
from otto.config.scope import EmptySelectionError
from otto.coverage.capture.gitio import (
    GitCommandFailedError,
    GitMissingError,
    GitUnavailableError,
    NotAGitRepoError,
)
from otto.coverage.errors import (
    CoverageConfigError,
    CoverageDataMismatchError,
    CoverageToolVersionError,
    NoCoverageDataError,
)
from otto.coverage.overrides import OverrideConfigError
from otto.coverage.tickets import TicketConfigError
from otto.errors import EnsureStateError, OttoError
from otto.host.app_shell import AppShellActiveError, AppShellTimeoutError, ParseMismatch
from otto.host.errors import (
    HostCommandError,
    HostUnreachableError,
    UnsupportedOnUserlandError,
)
from otto.host.login_proxy import LoginProxyError
from otto.host.transport import HopTransportTornDownError
from otto.labs.errors import LabNotFoundError, LabRepositoryError
from otto.lifecycle import SyncPhaseInterrupt
from otto.link.manage import (
    LinkCommandFailedError,
    LinkHostUnreachableError,
    LinkNotMeasuredError,
)
from otto.monitor.archive_edit import ArchiveLockedError
from otto.monitor.db import UnsupportedDBError
from otto.monitor.event_ops import EventValidationError
from otto.project.orchestrator import InactiveRequiredDependencyError
from otto.reservations.check import MissingReservationError, ReservationBackendError
from otto.result import CommandNotRunError
from otto.suite._retry import RetryAttemptTimeoutError
from otto.suite.run import NoTestsMatchedError
from otto.suite.selection import UnknownSelectionError
from otto.tunnel.discovery import TunnelNotMeasuredError
from otto.tunnel.records import TunnelScanFailedError
from otto.tunnel.socat import NoFreePortError
from otto.utils import WaitTimeoutError
from tests._fixtures.paths import PROJECT_ROOT

CASES: list[tuple[type[BaseException], type[BaseException]]] = [
    (BootstrapError, Exception),
    (DependencyError, Exception),
    (ProjectScopeError, Exception),
    (InactiveRequiredDependencyError, Exception),
    (EmptySelectionError, ValueError),
    (LabContextError, Exception),
    (GitUnavailableError, RuntimeError),
    (GitMissingError, RuntimeError),
    (NotAGitRepoError, RuntimeError),
    (GitCommandFailedError, RuntimeError),
    (EnsureStateError, RuntimeError),
    (CoverageToolVersionError, RuntimeError),
    (CoverageConfigError, ValueError),
    (NoCoverageDataError, ValueError),
    (CoverageDataMismatchError, RuntimeError),
    (OverrideConfigError, ValueError),
    (TicketConfigError, ValueError),
    (ParseMismatch, ValueError),
    (HostUnreachableError, RuntimeError),
    (HostCommandError, RuntimeError),
    (UnsupportedOnUserlandError, RuntimeError),
    (HopTransportTornDownError, RuntimeError),
    (AppShellActiveError, RuntimeError),
    (AppShellTimeoutError, TimeoutError),
    (WaitTimeoutError, TimeoutError),
    (LoginProxyError, ConnectionError),
    (LabRepositoryError, Exception),
    (LabNotFoundError, Exception),
    (ArchiveLockedError, RuntimeError),
    (UnsupportedDBError, RuntimeError),
    (EventValidationError, ValueError),
    (ReservationBackendError, Exception),
    (MissingReservationError, Exception),
    (CommandNotRunError, RuntimeError),
    (NoTestsMatchedError, ValueError),
    (RetryAttemptTimeoutError, TimeoutError),
    (UnknownSelectionError, ValueError),
    (TunnelScanFailedError, RuntimeError),
    (TunnelNotMeasuredError, RuntimeError),
    (NoFreePortError, RuntimeError),
    (LinkHostUnreachableError, RuntimeError),
    (LinkCommandFailedError, RuntimeError),
    (LinkNotMeasuredError, RuntimeError),
]


DELIBERATELY_ROOTLESS: frozenset[type[BaseException]] = frozenset(
    {
        # Nothing generic to keep: these never existed as a stdlib type a
        # caller could already be catching, so `Exception` in CASES is their
        # honest answer rather than an unfilled slot. Listing them is what
        # makes the `is not Exception` assertion below able to fail: every
        # exception IS an Exception, so an undeclared `(Foo, Exception)` row
        # asserts nothing at all.
        BootstrapError,
        DependencyError,
        ProjectScopeError,
        # A contradictory ACTIVATION configuration: the labs drop a provider
        # while a kept repo requires it. Sits beside ProjectScopeError above
        # for the same reason it has no stdlib root — the project layer's
        # "these declarations cannot work together" failures are otto's own
        # concept, never a ValueError a caller was already catching.
        InactiveRequiredDependencyError,
        LabContextError,
        LabRepositoryError,
        LabNotFoundError,
        ReservationBackendError,
        MissingReservationError,
    }
)


@pytest.mark.parametrize(("cls", "stdlib_root"), CASES, ids=[cls.__name__ for cls, _ in CASES])
def test_public_exception_is_ottoerror_and_keeps_stdlib_root(cls, stdlib_root):
    """Re-parenting added OttoError without disturbing the original stdlib root."""
    assert issubclass(cls, OttoError)
    assert issubclass(cls, stdlib_root)
    assert stdlib_root is not Exception or cls in DELIBERATELY_ROOTLESS, (
        f"{cls.__name__} declares a stdlib_root of bare Exception, which every "
        "exception satisfies — give it the root it actually keeps, or add it to "
        "DELIBERATELY_ROOTLESS to say it has none on purpose"
    )


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


def _import_aliases(tree) -> dict[str, str]:
    """``{local name: original name}`` for this module's ``from X import Y as Z``.

    Load-bearing: bases are resolved by NAME, so without this
    ``from ..errors import OttoError as _Base`` then ``class E(_Base)`` is
    invisible to BOTH sweeps below — the class is public and OttoError-rooted
    at runtime, but ``_Base`` is neither a stdlib seed nor a known family
    member, so it never joins the family and never has to declare anything.
    """
    import ast

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def _base_names(node, aliases: dict[str, str]) -> set[str]:
    import ast

    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(aliases.get(base.id, base.id))
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _sweep_src() -> tuple[set[str], set[str], dict[str, str]]:
    """``(family, rooted, file_by_class)`` for every class under ``src/otto``.

    Statically walks every module and fixpoints the exception family from the
    stdlib seeds (so grandchildren like ``DependencyError(BootstrapError)``
    count), then fixpoints which of those reach ``OttoError``.
    """
    import ast

    src = PROJECT_ROOT / "src" / "otto"
    assert src.is_dir(), src

    bases_by_class: dict[str, set[str]] = {}
    file_by_class: dict[str, str] = {}
    collisions: list[str] = []
    for py in sorted(src.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.bases:
                rel = str(py.relative_to(src))
                if node.name in bases_by_class:
                    collisions.append(f"{node.name} ({file_by_class[node.name]} and {rel})")
                bases_by_class.setdefault(node.name, _base_names(node, aliases))
                file_by_class.setdefault(node.name, rel)
    # Resolution is by bare name across the whole tree, so the FIRST definition
    # of a duplicated name wins and the second becomes invisible — a non-
    # exception `Product` would hide an exception `Product` from every gate
    # here. There are no duplicates today; this keeps that a fact rather than
    # a comment, since the failure mode is silence.
    assert not collisions, f"class names must be unique across src/otto: {sorted(collisions)}"

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

    return family, rooted, file_by_class


def test_every_public_exception_in_src_is_ottoerror_rooted():
    """AST sweep: no public Exception-family class under src/otto escapes OttoError.

    Requires each PUBLIC member of the family to reach ``OttoError`` through
    its base chain or appear in the explicit exclusion list. Private
    (``_``-prefixed) classes are internal control-flow signals and exempt.
    """
    family, rooted, file_by_class = _sweep_src()
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


def test_every_ottoerror_subclass_declares_its_stdlib_root():
    """The completeness half: CASES must name every public OttoError subclass.

    Without this, `class FooError(OttoError)` passes the sweep above (it does
    reach OttoError) while carrying no stdlib root at all — so the "existing
    ``except ValueError`` handlers keep working" half of the convention, the
    half that is invisible from the class statement, is silently not true for
    it. Proven: adding such a class to ``errors.py`` left this whole file
    green before this test existed.

    The reverse direction is the anti-vacuity control rather than a drift
    check: a rename or deletion cannot reach it (CASES holds imported class
    OBJECTS, so the module fails at collection first), but a sweep that stops
    finding classes — a broken walk, a moved `src` — empties `public` and
    fails here instead of passing with nothing to check.
    """
    _, rooted, file_by_class = _sweep_src()
    public = {n for n in rooted if not n.startswith("_") and n != "OttoError"}
    declared = {cls.__name__ for cls, _ in CASES}

    undeclared = sorted(f"{n} ({file_by_class[n]})" for n in public - declared)
    assert not undeclared, (
        "OttoError subclasses missing from CASES — add each with the stdlib "
        f"root it keeps (Exception if it deliberately has none): {undeclared}"
    )
    assert not sorted(declared - public), (
        f"CASES names classes the sweep cannot find in src/otto: {sorted(declared - public)}"
    )


def test_the_taxonomy_counts_in_errors_py_match_the_measured_split():
    """``otto.errors``' docstring publishes counts; nothing checked them.

    They render to users via ``docs/api/errors.rst``, and the docstring itself
    says "Re-measure by walking the AST of ``src/otto``; do not adjust these by
    hand" — but no gate enforced it, so adding one named class in 2026-08-11
    silently made four published figures wrong. ``CASES`` is already proven
    complete by the sweep above, which makes it a sound measuring stick.

    Deliberately narrow: this pins the NAMED-class split (the part that moves
    whenever anyone adds an exception), not the 301/254 raise-site figures,
    which are a different measurement over builtin raises and do not change
    when a named class is added.
    """
    import re

    from otto import errors

    doc = errors.__doc__ or ""
    named = len(CASES)
    covered = sum(1 for _, root in CASES if root in (ValueError, RuntimeError))
    rootless = len(DELIBERATELY_ROOTLESS)
    os_rooted = sum(1 for _, root in CASES if isinstance(root, type) and issubclass(root, OSError))

    assert covered + rootless + os_rooted == named, (
        "the three buckets no longer partition CASES, so the docstring's split "
        "cannot be expressed — reclassify before updating prose"
    )

    split = re.search(r"(\d+) \+ (\d+) \+ (\d+) = (\d+)", doc)
    assert split, "errors.py no longer states its split as 'a + b + c = total'"
    assert tuple(int(g) for g in split.groups()) == (covered, rootless, os_rooted, named), (
        f"errors.py says {split.group(0)}; measured {covered} + {rootless} + {os_rooted} = {named}"
    )
    assert f"otto's {named} NAMED failures" in doc, (
        f"errors.py's 'NAMED failures' count is not {named}"
    )
    assert f"{covered} of the {named} named classes" in doc, (
        f"errors.py's coverage sentence is not '{covered} of the {named} named classes'"
    )
