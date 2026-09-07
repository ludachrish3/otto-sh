"""Reusable conformance suites for otto's pluggable backend interfaces.

Four helpers — one per interface — assert that a backend satisfies otto's
contract. Each runs every rule as a non-fatal ``expect()`` on a single
:class:`~otto.suite.expect.ExpectCollector`, then raises once with *all*
violations, so a backend author sees every problem at once instead of fixing
them one failed assertion at a time.

Structural/type rules always run. Behavioral round-trip rules run only when the
caller supplies known ground truth (so a SUT author can leverage their own
fixtures).

Usage::

    from otto.testing import (
        assert_creds_store_conforms,
        assert_inventory_conforms,
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
    )


    def test_my_backend_conforms():
        assert_reservation_backend_conforms(MyBackend(), known_user="alice", known_resources=["r1"])
"""

import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config.lab import Lab
from ..creds import CredsStore
from ..host.remote_host import RemoteHost
from ..inventory import Inventory, InventoryKeyError
from ..labs import HostSummary, LabNotFoundError, LabRepository, SupportsHostSummaries
from ..models.host import CredSpec
from ..models.inventory import (
    FILLABLE_INVENTORY_FIELDS,
    INVENTORY_KEY_FIELDS,
    SUPPLIES_EXEMPT_FIELDS,
    InventoryRecord,
)
from ..reservations import (
    Reservation,
    ReservationBackend,
    ReservationBackendError,
    SupportsResourceHolders,
    SupportsUsernameCompletion,
)
from ..suite.expect import ExpectCollector

# Sentinels for "this name definitely does not exist" probes.
_NO_SUCH_LAB = "__otto_conformance_no_such_lab__"
_PROBE_USER = "__otto_conformance_probe_user__"
_PROBE_RESOURCE = "__otto_conformance_probe_resource__"
_PROBE_CREDS_KEY = "__otto_conformance_no_such_key__"

# Distinguishes "the member is absent" from "the member is None", which a
# structural backend could legitimately (if wrongly) return.
_MISSING = object()

_LOG = logging.getLogger(__name__)
"""Where a rule the backend's own data cannot exercise is announced as skipped."""

_CLOCK_SKEW = timedelta(seconds=1)
"""Slack allowed between the backend's clock and this helper's.

The two do not share an instant — a remote scheduler answers from its own
clock, and the round trip costs time on top.  Every time-sensitive rule here
brackets ``now`` by this one amount rather than inventing its own tolerance:
the overlap rule asks for ``[now - _CLOCK_SKEW, now + _CLOCK_SKEW]``, and the
lapsed-row rule forgives a booking that ended within ``_CLOCK_SKEW`` of now.
The two are the same number on purpose — a row still returned for that window
is exactly a row the lapsed rule must not fail.
"""


def assert_lab_repository_conforms(
    repo: LabRepository,
    *,
    expected_labs: list[str] | None = None,
) -> None:
    """Assert *repo* satisfies the :class:`~otto.labs.protocol.LabRepository` contract.

    Runs structural rules unconditionally; for every listed lab, asserts it
    loads to a valid :class:`~otto.config.lab.Lab`; asserts an unknown
    name raises :class:`~otto.labs.LabNotFoundError`. When *expected_labs*
    is given, also asserts each appears in ``list_labs()`` and loads. Raises a
    single :class:`AssertionError` aggregating every violated rule.

    ``load_lab`` must also accept an ``inventory=`` keyword (spec 2026-08-28
    host-inventory §6): checked on the SIGNATURE, because a backend with no
    referenced entry in its data would pass every behavioural rule while
    silently dropping the argument the moment one appears.

    Parameters
    ----------
    repo : LabRepository
        The backend instance under test.
    expected_labs : list[str] | None
        Optional lab names the caller knows the backend should provide.
    """
    c = ExpectCollector()

    c.expect(
        isinstance(repo, LabRepository),
        "LabRepository: must satisfy the runtime_checkable LabRepository protocol",
    )
    c.expect(
        callable(getattr(repo, "load_lab", None)),
        "LabRepository: load_lab must be callable",
    )
    c.expect(
        callable(getattr(repo, "list_labs", None)),
        "LabRepository: list_labs must be callable",
    )
    if callable(getattr(repo, "load_lab", None)):
        params = inspect.signature(repo.load_lab).parameters
        c.expect(
            "inventory" in params,
            "LabRepository: load_lab must accept an inventory= keyword "
            "(spec 2026-08-28 host-inventory §6)",
        )

    names = repo.list_labs() if callable(getattr(repo, "list_labs", None)) else []
    names_ok = isinstance(names, list)
    c.expect(
        names_ok,
        f"LabRepository: list_labs() must return a list, got {type(names).__name__}",
    )
    if names_ok:
        for n in names:
            c.expect(
                isinstance(n, str),
                f"LabRepository: list_labs() entries must be str, got {type(n).__name__} ({n!r})",
            )

        for n in names:
            if not isinstance(n, str):
                continue
            try:
                lab = repo.load_lab(n)
            except Exception as e:  # noqa: BLE001 — conformance check, must catch any impl exception to report violation
                c.expect(False, f"LabRepository: load_lab({n!r}) raised {type(e).__name__}: {e}")
                continue
            is_lab = isinstance(lab, Lab)
            c.expect(
                is_lab,
                f"LabRepository: load_lab({n!r}) must return a Lab, got {type(lab).__name__}",
            )
            if is_lab:
                for host_id, host in lab.hosts.items():
                    c.expect(
                        isinstance(host, RemoteHost),
                        f"LabRepository: lab {n!r} host {host_id!r} must be a "
                        f"RemoteHost, got {type(host).__name__}",
                    )
                    c.expect(
                        host_id == getattr(host, "id", None),
                        f"LabRepository: lab {n!r} host key {host_id!r} must equal "
                        f"host.id {getattr(host, 'id', None)!r}",
                    )
                try:
                    lab2 = repo.load_lab(n)
                except Exception as e:  # noqa: BLE001 — conformance check, must catch any impl exception to report violation
                    c.expect(
                        False,
                        f"LabRepository: load_lab({n!r}) idempotency re-call raised "
                        f"{type(e).__name__}: {e}",
                    )
                else:
                    c.expect(
                        sorted(lab.hosts) == sorted(lab2.hosts) and lab.resources == lab2.resources,
                        f"LabRepository: load_lab({n!r}) must be idempotent "
                        f"(two calls must yield equivalent labs)",
                    )

    # Unknown lab must raise LabNotFoundError (not return None / bare KeyError).
    try:
        repo.load_lab(_NO_SUCH_LAB)
        c.expect(
            False,
            f"LabRepository: load_lab({_NO_SUCH_LAB!r}) must raise LabNotFoundError "
            f"for an unknown lab, but it returned normally",
        )
    except LabNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 — conformance check, distinguishes unexpected exception from LabNotFoundError
        c.expect(
            False,
            f"LabRepository: an unknown lab must raise LabNotFoundError, got "
            f"{type(e).__name__}: {e}",
        )

    if expected_labs is not None:
        listed = set(names) if names_ok else set()
        for n in expected_labs:
            c.expect(n in listed, f"LabRepository: expected lab {n!r} to appear in list_labs()")
            try:
                repo.load_lab(n)
            except Exception as e:  # noqa: BLE001 — conformance check, must catch any impl exception to report violation
                c.expect(
                    False, f"LabRepository: expected lab {n!r} to load, got {type(e).__name__}: {e}"
                )

    # Optional capability: only checked when the backend advertises it (the
    # same shape as SupportsUsernameCompletion for reservation backends).
    if isinstance(repo, SupportsHostSummaries) and names_ok:
        _expect_host_summaries_conform(c, repo, names)

    c.raise_if_failures()


def _expect_host_summaries_conform(
    c: ExpectCollector,
    repo: "SupportsHostSummaries",
    names: list[str],
) -> None:
    """Check the optional ``SupportsHostSummaries`` capability's contract.

    Three rules, in ascending order of what they cost a user to get wrong:

    - Every summarized id must be one ``load_lab`` actually produces. A fast
      path deriving ids by a different route than host construction offers
      completions that do not dispatch — worse than offering none.
    - Every constructed host must be summarized. The reverse direction, and
      the reason it matters is that nothing else notices: the completer just
      quietly stops offering that host.
    - Every FIELD must agree with the constructed host. A summary is not an
      id lookup — ``labs`` drives ``--lab``-scoped completion, ``docker_capable``
      gates ``otto docker --on``, and ``ip`` drives tunnel narrowing. A backend
      that fills in only ``id`` passed every earlier version of this check
      while silently breaking the other surfaces.

    All three are scoped to labs that actually LOADED. A lab whose load
    raises (a host naming an ``os_profile`` this process never registered,
    say) is reported by the caller's own rules; letting its hosts count here
    made the first rule report every one of them as undispatchable.
    """
    # The call shape production uses (``otto.labs.list_host_summaries`` passes
    # ``inventory=`` by keyword, spec 2026-08-28 host-inventory §6), checked on
    # the SIGNATURE first for the same reason ``load_lab``'s is: a backend whose
    # data holds no referenced entry passes every behavioural rule below with
    # a no-argument method — and then TypeErrors in the shell the first time
    # otto calls it for real. This asserter once called it with no arguments
    # itself, certifying exactly that backend.
    params = inspect.signature(repo.list_host_summaries).parameters
    c.expect(
        "inventory" in params,
        "SupportsHostSummaries: list_host_summaries must accept an inventory= keyword "
        "(spec 2026-08-28 host-inventory §6; production calls it that way)",
    )
    if "inventory" not in params:
        return
    try:
        summaries = repo.list_host_summaries(inventory=None)
    except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
        c.expect(
            False, f"SupportsHostSummaries: list_host_summaries() raised {type(e).__name__}: {e}"
        )
        return

    if not isinstance(summaries, list):
        c.expect(
            False,
            f"SupportsHostSummaries: must return a list, got {type(summaries).__name__}",
        )
        return

    seen: set[str] = set()
    for s in summaries:
        if not isinstance(s, HostSummary):
            c.expect(
                False,
                f"SupportsHostSummaries: entries must be HostSummary, got {type(s).__name__}",
            )
            continue
        c.expect(
            bool(s.id) and isinstance(s.id, str),
            f"SupportsHostSummaries: every summary needs a non-empty str id, got {s.id!r}",
        )
        c.expect(
            s.id not in seen,
            f"SupportsHostSummaries: duplicate id {s.id!r} — hosts in several labs "
            f"must merge into one summary with both labs",
        )
        seen.add(s.id)

    constructed: dict[str, Any] = {}
    labs_of: dict[str, set[str]] = {}
    loaded: set[str] = set()
    for n in names:
        if not isinstance(n, str):
            continue
        try:
            hosts = repo.load_lab(n).hosts  # ty: ignore[unresolved-attribute]
        except Exception:  # noqa: BLE001, S112 — load failures are reported by the caller's own rules
            continue
        loaded.add(n)
        for host_id, host in hosts.items():
            constructed[host_id] = host
            labs_of.setdefault(host_id, set()).add(n)

    # Only summaries belonging to a lab that loaded are comparable; a summary
    # for a lab that raised tells us nothing about the backend's id derivation.
    comparable = {
        s.id
        for s in summaries
        if isinstance(s, HostSummary) and (not s.labs or set(s.labs) & loaded)
    }
    undispatchable = sorted(comparable - set(constructed))
    c.expect(
        not undispatchable,
        f"SupportsHostSummaries: ids {undispatchable} are offered by list_host_summaries() "
        f"but no load_lab() produces them — completion would offer ids that cannot dispatch",
    )

    # No built-in exemption: ``local`` is injected by ``config.lab.load_lab``,
    # never by a backend, so it is not in `constructed` to begin with. Excusing
    # it would only ever excuse a backend that defines its OWN ``local`` —
    # which otto explicitly allows, and which ``otto.labs.host_summaries``
    # deliberately refuses to filter, so the two paths would disagree on
    # exactly the id that comment says must not be dropped.
    unsummarized = sorted(set(constructed) - seen)
    c.expect(
        not unsummarized,
        f"SupportsHostSummaries: load_lab() produces {unsummarized} but "
        f"list_host_summaries() omits them — completion would silently stop offering them",
    )

    for summary in summaries:
        if not isinstance(summary, HostSummary):
            continue
        host = constructed.get(summary.id)
        if host is None:
            continue
        for field, summarized, built in (
            ("ip", summary.ip, getattr(host, "ip", "") or ""),
            (
                "docker_capable",
                summary.docker_capable,
                bool(getattr(host, "docker_capable", False)),
            ),
            # A constructed host ALWAYS carries a selector (the factory defaults
            # it to "unix"), so a summary leaving it None is a backend that did
            # not record it, not a host that has none.
            ("os_type", summary.os_type, getattr(host, "os_type", None)),
        ):
            c.expect(
                summarized == built,
                f"SupportsHostSummaries: {summary.id!r}.{field} is {summarized!r} in the "
                f"summary but {built!r} on the constructed host"
                + (
                    " — `otto host <id> <TAB>` would offer every class's verbs "
                    "instead of this host's"
                    if field == "os_type"
                    else ""
                ),
            )
        produced_in = labs_of.get(summary.id, set())
        claimed = set(summary.labs)
        c.expect(
            claimed >= produced_in,
            f"SupportsHostSummaries: {summary.id!r}.labs is {sorted(claimed)} but "
            f"load_lab() produced it for {sorted(produced_in)} — "
            f"--lab-scoped completion would drop it",
        )
        # And the other direction, which is the load-bearing rule again at a
        # different granularity: completion buckets by `labs`, so claiming a
        # lab that does not contain the host offers an id `-l <that lab>`
        # cannot dispatch. Restricted to labs that loaded, for the same reason
        # everything else here is.
        overclaimed = sorted((claimed & loaded) - produced_in)
        c.expect(
            not overclaimed,
            f"SupportsHostSummaries: {summary.id!r}.labs claims {overclaimed} but "
            f"load_lab() does not produce it there — `otto host -l <lab> <TAB>` would "
            f"offer an id that cannot dispatch",
        )


def _expect_reservation_rows(c: ExpectCollector, label: str, rows: object) -> list[Reservation]:
    """Apply the per-row ``Reservation`` rules to *rows*.

    Parameters
    ----------
    c : ExpectCollector
        Collector recording every violated rule.
    label : str
        Names the call the rows came from, e.g. ``"reservations"``, so a
        failure says which query produced the bad row.
    rows : object
        Whatever the backend returned.  A non-list is reported, not iterated.

    Returns
    -------
    list[Reservation]
        The entries that really are :class:`~otto.reservations.protocol.Reservation`
        objects, so later rules can reason about resources without tripping
        over a malformed row the caller has already been told about.
    """
    if not isinstance(rows, list):
        c.expect(
            False,
            f"ReservationBackend: {label} must return a list of Reservation, got "
            f"{type(rows).__name__}",
        )
        return []
    good: list[Reservation] = []
    for r in rows:
        if not isinstance(r, Reservation):
            c.expect(
                False,
                f"ReservationBackend: {label} entries must be Reservation, got {type(r).__name__}",
            )
            continue
        c.expect(
            isinstance(r.user, str) and r.user != "",
            f"ReservationBackend: {label} user must be a non-empty str, got {r.user!r}",
        )
        c.expect(
            isinstance(r.resource, str) and r.resource != "",
            f"ReservationBackend: {label} resource must be a non-empty str, got {r.resource!r}",
        )
        aware = all(b is None or b.tzinfo is not None for b in (r.start, r.end))
        c.expect(
            aware,
            f"ReservationBackend: {label} start/end must be timezone-aware when set "
            f"({r.resource!r}: start={r.start!r}, end={r.end!r})",
        )
        if aware and r.start is not None and r.end is not None:
            c.expect(
                r.start <= r.end,
                f"ReservationBackend: {label} start <= end required "
                f"({r.resource!r}: {r.start} > {r.end})",
            )
        good.append(r)
    return good


def _expect_rows_belong_to(
    c: ExpectCollector, label: str, rows: list[Reservation], user: str
) -> None:
    """Expect every row in *rows* to name *user* as its holder.

    Parameters
    ----------
    c : ExpectCollector
        Collector recording every violated rule.
    label : str
        Names the call the rows came from.
    rows : list[Reservation]
        Well-formed rows, as returned by :func:`_expect_reservation_rows`.
    user : str
        The username the query named.
    """
    for r in rows:
        c.expect(
            r.user == user,
            f"ReservationBackend: {label} must return only rows for {user!r}, "
            f"got a row held by {r.user!r} ({r.resource!r})",
        )


def _expect_overlap_semantics(
    c: ExpectCollector,
    backend: ReservationBackend,
    probe_user: str,
    unbounded: list[Reservation],
) -> None:
    """Expect the window predicate to be overlap rather than containment.

    The helper cannot create a straddling booking — otto never writes
    reservations — so the rule is checked differentially against the backend's
    own data: a booking active right now overlaps *any* window containing now,
    so every resource the unbounded call reports must come back for a window
    bracketing this instant.  Containment semantics drop the straddling rows
    here; overlap semantics do not.  This is the fail-open rule — a backend
    that reads the window as "contained in" tells otto a held rack is free.

    Parameters
    ----------
    c : ExpectCollector
        Collector recording every violated rule.
    backend : ReservationBackend
        The backend instance under test.
    probe_user : str
        The username both calls query for.
    unbounded : list[Reservation]
        Well-formed rows from the unbounded ``fetch_reservations(probe_user)``.
    """
    wide = {r.resource for r in unbounded}
    if not wide:
        _LOG.warning(
            "conformance: skipping the overlap rule — fetch_reservations(%r) returned no "
            "rows, so there is nothing a narrow window could drop. Pass known_user= for a "
            "user who holds something to exercise it.",
            probe_user,
        )
        return
    now = datetime.now(tz=timezone.utc)
    narrow = backend.fetch_reservations(probe_user, start=now - _CLOCK_SKEW, end=now + _CLOCK_SKEW)
    narrow_rows = _expect_reservation_rows(c, "fetch_reservations(window)", narrow)
    dropped = sorted(wide - {r.resource for r in narrow_rows})
    c.expect(
        not dropped,
        f"ReservationBackend: fetch_reservations() must match bookings overlapping "
        f"[start, end], not bookings contained in it — {dropped!r} came back unbounded "
        f"but not for a window bracketing now, so a booking overlapping that window was "
        f"dropped; that reading is what makes otto's gate fail open",
    )


def _expect_no_lapsed_rows(c: ExpectCollector, label: str, rows: list[Reservation]) -> None:
    """Expect the default query to return no booking that has already ended.

    The other direction of the window predicate, and the one nothing else
    catches.  :func:`_expect_overlap_semantics` computes ``wide - narrow``, so
    it only sees a backend that *drops* rows; a backend that reads a query's
    ``start=None`` as "-infinity" instead of "this instant" hands back every
    booking that ever existed, lapsed ones included, and passes it.  That is
    the dangerous direction: otto's *check* gate never re-filters by ``end`` —
    it reads ``.end`` only for the expiry warning and the "held by … until"
    text — so a lapsed row admits a user whose booking is over.  It fails OPEN.

    The published predicate (``docs/library/reservation-backends.md``, "The
    query window") is ``row.end is None or row.end > start``, with ``start``
    substituted as ``now`` for the unbounded call.  So for the default query
    every returned row must satisfy ``row.end is None or row.end > now``, and
    this rule is that clause read back.  ``end is None`` is open-ended and
    always passes; it is never a sentinel far-future date.  The ``>`` here is
    deliberately not the ``<=`` in
    :meth:`otto.reservations.protocol.Reservation.is_active`, whose docstring
    names the one row the two disagree about and why.

    Applied to a **freshly issued** ``fetch_reservations`` only, never to the
    cached ``reservations`` member: that cache is deliberately populated once
    per run (and may be pre-seeded), so a row lapsing while the process lives
    is correct behaviour rather than an over-return.

    The rule is **vacuous against a fixture holding no lapsed booking** — every
    row open-ended, say, as both of otto's documentation samples are.  That is
    inherent: conformance runs against the author's own data and this helper
    cannot fabricate a row the backend never returned.  A green run here is not
    proof the backend filters; it is proof it did not over-return *this* data.

    Parameters
    ----------
    c : ExpectCollector
        Collector recording every violated rule.
    label : str
        Names the call the rows came from.
    rows : list[Reservation]
        Well-formed rows from the unbounded ``fetch_reservations(user)``.
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now - _CLOCK_SKEW
    for r in rows:
        # A naive ``end`` is uncomparable to an aware ``now``; the row rules
        # already report it, and raising TypeError here would lose every other
        # violation the collector holds.
        if r.end is None or r.end.tzinfo is None or r.end > cutoff:
            continue
        c.expect(
            False,
            f"ReservationBackend: {label} must return only bookings still active — "
            f"{r.resource!r} ended at {r.end.isoformat()}, before now ({now.isoformat()}), "
            f"so a lapsed booking came back; an unbounded call means 'active at this "
            f"instant', not 'every booking that ever existed', and otto's gate does not "
            f"re-filter by end, so that reading admits a user whose booking is over",
        )


def _expect_holder_agreement(
    c: ExpectCollector,
    backend: ReservationBackend,
    holders: "Callable[[str], object]",
    resources: list[str],
    owned: dict[str, set[str]],
) -> None:
    """Expect ``holders()`` to agree with ``fetch_reservations()`` in both directions.

    Parameters
    ----------
    c : ExpectCollector
        Collector recording every violated rule.
    backend : ReservationBackend
        The backend instance under test, for the corroborating forward query.
    holders : Callable[[str], object]
        The backend's bound ``holders`` method — passed rather than reached for
        through *backend*, whose Protocol type knows nothing of the optional
        capability.
    resources : list[str]
        The resources to probe.
    owned : dict[str, set[str]]
        Resources each probed user holds per ``fetch_reservations``.  Every one
        must show up as a holder row, and every holder row must show up here.
    """
    for resource in resources:
        rows = _expect_reservation_rows(c, f"holders({resource!r})", holders(resource))
        named = {r.user for r in rows}
        for user, held in owned.items():
            if resource in held:
                c.expect(
                    user in named,
                    f"SupportsResourceHolders: holders({resource!r}) must include a row for "
                    f"{user!r}, who holds it per fetch_reservations; got {sorted(named)!r}",
                )
        for r in rows:
            back = _expect_reservation_rows(
                c, f"fetch_reservations({r.user!r})", backend.fetch_reservations(r.user)
            )
            c.expect(
                resource in {b.resource for b in back},
                f"SupportsResourceHolders: holders({resource!r}) names {r.user!r} as a holder, "
                f"but fetch_reservations({r.user!r}) does not return {resource!r}",
            )


def assert_reservation_backend_conforms(
    backend: ReservationBackend,
    *,
    known_user: str | None = None,
    known_resources: list[str] | None = None,
) -> None:
    """Assert *backend* satisfies the ReservationBackend contract.

    The required contract is two methods (``fetch_reservations`` and
    ``backend_name``) plus a ``reservations`` list member, which
    :class:`~otto.reservations.ReservationBackendBase` provides.  Structural,
    row, lapsed-row and overlap rules always run.  When *known_user* and *known_resources*
    (resources that user is known to hold) are both given, round-trip
    consistency rules run too.  The optional
    :class:`~otto.reservations.SupportsUsernameCompletion` and
    :class:`~otto.reservations.SupportsResourceHolders` capabilities are
    checked only when the backend implements them — a backend whose scheduler
    answers only per-user queries omits ``holders`` and is *skipped* for the
    holder rules, not failed.  Raises a single :class:`AssertionError`
    aggregating every violated rule.

    There is deliberately **no** construction-time rule here.  The helper
    receives an already-constructed instance, where a deliberately pre-seeded
    cache (which the base class documents as available) is indistinguishable
    from an eager base; the laziness of
    :attr:`~otto.reservations.ReservationBackendBase.reservations` is otto's
    own invariant, covered by otto's unit tests for the base class.

    Rules the backend's own data cannot exercise — the overlap rule against a
    user who holds nothing, the row rules against a backend built with no
    identity — are logged as skipped on the ``otto.testing.conformance``
    logger rather than passing silently.  The lapsed-row rule is the one that
    cannot announce itself: it is vacuous against a fixture that holds no
    already-ended booking, and no query this helper can issue distinguishes
    that from a backend that filters correctly.

    Parameters
    ----------
    backend : ReservationBackend
        The backend instance under test.
    known_user : str | None
        A username known to hold ``known_resources`` (enables round-trip rules).
    known_resources : list[str] | None
        Resources ``known_user`` is known to currently hold.
    """
    c = ExpectCollector()

    c.expect(
        isinstance(backend, ReservationBackend),
        "ReservationBackend: must satisfy the runtime_checkable ReservationBackend protocol",
    )
    fetch_ok = callable(getattr(backend, "fetch_reservations", None))
    c.expect(fetch_ok, "ReservationBackend: fetch_reservations must be callable")
    name_ok = callable(getattr(backend, "backend_name", None))
    c.expect(name_ok, "ReservationBackend: backend_name must be callable")

    name = backend.backend_name() if name_ok else ""
    c.expect(
        isinstance(name, str) and name != "",
        f"ReservationBackend: backend_name() must return a non-empty str, got {name!r}",
    )
    if name_ok:
        c.expect(
            name == backend.backend_name(),
            "ReservationBackend: backend_name() must be stable across calls",
        )

    # The `reservations` member is absent from the Protocol on purpose —
    # isinstance() against a runtime_checkable Protocol EVALUATES a non-method
    # member, which would turn every type check into a live scheduler query —
    # so this helper is where its presence and shape are enforced.
    identity = getattr(backend, "username", None)
    cached: object = _MISSING
    try:
        cached = getattr(backend, "reservations", _MISSING)
    except ReservationBackendError as exc:
        # Documented for a backend built with no identity purely to call
        # list_usernames. It must never escape as anything but a recorded rule.
        c.expect(
            identity is None,
            f"ReservationBackend: reservations must not raise for a backend whose "
            f"username is {identity!r}, but it raised: {exc}",
        )
        if identity is None:
            _LOG.warning(
                "conformance: skipping the `reservations` rules — the backend was built "
                "without a username, so the member raises by design."
            )
    else:
        if cached is _MISSING:
            c.expect(
                False,
                "ReservationBackend: must expose a `reservations` member holding the "
                "invoking user's active rows; inherit ReservationBackendBase, which "
                "provides it, or supply it yourself",
            )
        else:
            cached_rows = _expect_reservation_rows(c, "reservations", cached)
            if isinstance(identity, str) and identity != "":
                _expect_rows_belong_to(c, "reservations", cached_rows, identity)

    if not fetch_ok:
        c.raise_if_failures()
        return

    probe_user = known_user if known_user is not None else identity or _PROBE_USER
    label = f"fetch_reservations({probe_user!r})"
    fetched = _expect_reservation_rows(c, label, backend.fetch_reservations(probe_user))
    _expect_rows_belong_to(c, label, fetched, probe_user)
    _expect_no_lapsed_rows(c, label, fetched)
    _expect_overlap_semantics(c, backend, probe_user, fetched)

    held = {r.resource for r in fetched}
    if known_user is not None and known_resources is not None:
        # The caller's ground truth, which is the only thing here the backend
        # cannot define into agreement with itself. It also carries the holder
        # ground truth: once these resources are in the forward query's answer,
        # `_expect_holder_agreement` requires holders() to name known_user for
        # each of them.
        for resource in known_resources:
            c.expect(
                resource in held,
                f"ReservationBackend: {label} must include known-held resource "
                f"{resource!r}, got {sorted(held)!r}",
            )

    if isinstance(backend, SupportsResourceHolders):
        probes = sorted(held | set(known_resources or []))
        _expect_holder_agreement(
            c, backend, backend.holders, probes or [_PROBE_RESOURCE], {probe_user: held}
        )

    if isinstance(backend, SupportsUsernameCompletion):
        usernames = backend.list_usernames()
        u_ok = isinstance(usernames, list)
        c.expect(
            u_ok,
            f"SupportsUsernameCompletion: list_usernames() must return a list, got "
            f"{type(usernames).__name__}",
        )
        if u_ok:
            for u in usernames:
                c.expect(
                    isinstance(u, str),
                    f"SupportsUsernameCompletion: list_usernames() entries must be "
                    f"str, got {type(u).__name__}",
                )

    c.raise_if_failures()


def assert_inventory_conforms(
    inventory: Inventory,
    *,
    expected_keys: list[str] | None = None,
    repository: "LabRepository | None" = None,
    lab: str | None = None,
) -> None:
    """Assert *inventory* satisfies the :class:`~otto.inventory.protocol.Inventory` contract.

    (spec §14.) Structural rules always run: protocol satisfied, ``label`` a
    string, ``supplies`` a subset of the record fields containing ``"ip"``,
    ``list_keys()`` a list of strings each of which resolves, ``lookup``
    idempotent (an equal record on a second call) and never returning a field
    outside ``supplies`` (keys and ``extra`` excepted), an unknown key raising
    :class:`~otto.inventory.errors.InventoryKeyError`, ``fingerprint()`` ``str | None``.
    With *expected_keys*, each must resolve AND appear in ``list_keys()``. With
    *repository* AND *lab*, the positive control: *lab* must FAIL to load
    without the inventory and LOAD with it, and at least one host must carry
    ``inventory_ref.referenced`` — a backend that ignores ``inventory=`` fails
    here.
    """
    c = ExpectCollector()
    c.expect(
        isinstance(inventory, Inventory),
        "Inventory: must satisfy the runtime_checkable Inventory protocol",
    )
    label = getattr(inventory, "label", None)
    c.expect(
        isinstance(label, str) and bool(label),
        f"Inventory: label must be a non-empty str, got {label!r}",
    )
    supplies = getattr(inventory, "supplies", None)
    supplies_ok = isinstance(supplies, frozenset)
    c.expect(supplies_ok, f"Inventory: supplies must be a frozenset, got {type(supplies).__name__}")
    allowed = FILLABLE_INVENTORY_FIELDS | INVENTORY_KEY_FIELDS
    if supplies_ok:
        c.expect("ip" in supplies, "Inventory: supplies must contain 'ip'")
        c.expect(
            supplies <= allowed,
            f"Inventory: supplies names non-record fields: {sorted(supplies - allowed)}",
        )
    try:
        keys = inventory.list_keys() if callable(getattr(inventory, "list_keys", None)) else None
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, f"Inventory: list_keys() raised {type(e).__name__}: {e}")
        keys = None
    keys_ok = isinstance(keys, list) and all(isinstance(k, str) for k in keys)
    c.expect(keys_ok, f"Inventory: list_keys() must return list[str], got {keys!r}")
    if expected_keys is not None:
        listed = set(keys) if keys_ok else set()
        for key in expected_keys:
            c.expect(
                key in listed,
                f"Inventory: expected key {key!r} to appear in list_keys()",
            )
    probe = _PROBE_CREDS_KEY
    try:
        inventory.lookup(probe)
        c.expect(
            False,
            "Inventory: lookup(unknown key) must raise InventoryKeyError, returned a record",
        )
    except InventoryKeyError:
        pass
    except Exception as e:  # noqa: BLE001 — conformance check, report the wrong type
        c.expect(
            False,
            f"Inventory: lookup(unknown key) must raise InventoryKeyError, "
            f"raised {type(e).__name__}",
        )
    for key in [*(keys if keys_ok else []), *(expected_keys or [])]:
        try:
            first = inventory.lookup(key)
            second = inventory.lookup(key)
        except Exception as e:  # noqa: BLE001 — conformance check
            c.expect(
                False, f"Inventory: expected key {key!r} did not resolve: {type(e).__name__}: {e}"
            )
            continue
        c.expect(
            isinstance(first, InventoryRecord),
            f"Inventory: lookup({key!r}) must return an InventoryRecord",
        )
        c.expect(
            first == second,
            f"Inventory: lookup({key!r}) must be idempotent (equal record on a second call)",
        )
        if isinstance(first, InventoryRecord) and supplies_ok:
            stated = set(first.model_fields_set) - SUPPLIES_EXEMPT_FIELDS
            leaked = sorted(stated - supplies)
            c.expect(
                not leaked,
                f"Inventory: lookup({key!r}) returned fields outside supplies: {leaked}",
            )
    fp = inventory.fingerprint() if callable(getattr(inventory, "fingerprint", None)) else 0
    c.expect(
        fp is None or isinstance(fp, str),
        f"Inventory: fingerprint() must be str | None, got {type(fp).__name__}",
    )
    if repository is not None and lab is not None:
        try:
            repository.load_lab(lab)
            c.expect(
                False,
                f"Inventory: lab {lab!r} loaded WITHOUT the inventory — its entries do not "
                "reference it, or the backend ignores inventory=",
            )
        except Exception:  # noqa: BLE001, S110 — the failure is the expected outcome
            pass
        try:
            loaded = repository.load_lab(lab, inventory=inventory)
            referenced = [
                h
                for h in loaded.hosts.values()
                if getattr(h, "inventory_ref", None) is not None and h.inventory_ref.referenced
            ]
            c.expect(
                bool(referenced),
                f"Inventory: lab {lab!r} loaded with the inventory but no host carries a "
                "referenced inventory_ref",
            )
        except Exception as e:  # noqa: BLE001 — conformance check
            c.expect(
                False,
                f"Inventory: lab {lab!r} failed to load WITH the inventory: "
                f"{type(e).__name__}: {e}",
            )
    c.raise_if_failures()


def assert_creds_store_conforms(
    store: CredsStore,
    *,
    known_key: str | None = None,
) -> None:
    """Assert *store* satisfies the :class:`~otto.creds.protocol.CredsStore` contract.

    (spec 2026-09-06 creds-store §5.3.) Structural rules always run: protocol
    satisfied, ``label`` a non-empty str, ``lookup`` of an unknown key ``[]``
    (never ``None``, never a raise), ``list_keys()`` a sorted ``list[str]`` or
    ``None``, ``fingerprint()`` ``str | None``. Every listed key must resolve
    to a non-empty ``list[CredSpec]`` with unique logins, idempotently. With
    *known_key*, that key must resolve non-empty and — when the store
    enumerates — appear in ``list_keys()``.
    """
    c = ExpectCollector()
    c.expect(
        isinstance(store, CredsStore),
        "CredsStore: must satisfy the runtime_checkable CredsStore protocol",
    )
    label = getattr(store, "label", None)
    c.expect(
        isinstance(label, str) and bool(label),
        f"CredsStore: label must be a non-empty str, got {label!r}",
    )
    try:
        fp = store.fingerprint() if callable(getattr(store, "fingerprint", None)) else None
        c.expect(
            fp is None or isinstance(fp, str),
            f"CredsStore: fingerprint() must be str or None, got {fp!r}",
        )
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, f"CredsStore: fingerprint() raised {type(e).__name__}: {e}")
    try:
        probe = store.lookup(_PROBE_CREDS_KEY)
        c.expect(probe == [], f"CredsStore: lookup(unknown key) must return [], got {probe!r}")
    except Exception as e:  # noqa: BLE001 — conformance check, report the wrong behaviour
        c.expect(
            False, f"CredsStore: lookup(unknown key) must return [], raised {type(e).__name__}"
        )
    keys: list[str] | None = None
    try:
        keys = store.list_keys() if callable(getattr(store, "list_keys", None)) else None
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, f"CredsStore: list_keys() raised {type(e).__name__}: {e}")
    if keys is not None:
        keys_ok = isinstance(keys, list) and all(isinstance(k, str) for k in keys)
        c.expect(keys_ok, f"CredsStore: list_keys() must return list[str] or None, got {keys!r}")
        if keys_ok:
            c.expect(keys == sorted(keys), "CredsStore: list_keys() must be sorted")
            if known_key is not None:
                c.expect(
                    known_key in keys,
                    f"CredsStore: known_key {known_key!r} must appear in list_keys()",
                )
    to_resolve = [
        *(keys if isinstance(keys, list) else []),
        *([known_key] if known_key is not None else []),
    ]
    for key in dict.fromkeys(to_resolve):
        try:
            first = store.lookup(key)
            second = store.lookup(key)
        except Exception as e:  # noqa: BLE001 — conformance check
            c.expect(False, f"CredsStore: lookup({key!r}) raised {type(e).__name__}: {e}")
            continue
        entries_ok = isinstance(first, list) and all(isinstance(e, CredSpec) for e in first)
        c.expect(entries_ok, f"CredsStore: lookup({key!r}) must return list[CredSpec]")
        if not entries_ok:
            continue
        if key == known_key:
            c.expect(bool(first), f"CredsStore: known_key {known_key!r} resolved to no entries")
        elif not first:
            c.expect(False, f"CredsStore: list_keys() names {key!r} but lookup({key!r}) is empty")
        c.expect(first == second, f"CredsStore: lookup({key!r}) must be idempotent")
        logins = [e.login for e in first]
        for login in sorted({x for x in logins if logins.count(x) > 1}):
            c.expect(False, f"CredsStore: lookup({key!r}) repeats login {login!r}")
    c.raise_if_failures()
