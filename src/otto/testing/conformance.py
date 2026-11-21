"""Reusable conformance suites for otto's pluggable backend interfaces.

Four of :mod:`otto.testing`'s seven helpers — one per interface — assert that
a backend satisfies otto's contract; the three host-side ones (two more
conformance suites and the registrability check
:func:`~otto.testing.assert_host_registrable`) live in
:mod:`otto.testing.conformance_host` and are re-exported alongside these.
Each runs every rule as a non-fatal ``expect()`` on a single
:class:`~otto.suite.expect.ExpectCollector`, then raises once with *all*
violations, so a backend author sees every problem at once instead of fixing
them one failed assertion at a time.

Structural/type rules always run. Behavioral round-trip rules run only when the
caller supplies known ground truth (so a project author can leverage their own
fixtures).

Usage, importing the six conformance suites (the registrability check is
imported the same way)::

    from otto.testing import (
        assert_creds_store_conforms,
        assert_host_conforms,
        assert_inventory_conforms,
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
        assert_transfer_backend_conforms,
    )


    def test_my_backend_conforms():
        assert_reservation_backend_conforms(MyBackend(), known_user="alice", known_resources=["r1"])
"""

import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..creds.protocol import CredsStore
from ..host.remote_host import RemoteHost
from ..inventory.discovery import Discovery, SupportsDiscovery, discover_inventory
from ..inventory.errors import InventoryKeyError
from ..inventory.protocol import Inventory
from ..inventory.snapshot import stated_fields
from ..inventory.view import InventoryView
from ..labs.composition import compose_links, contribution_part
from ..labs.errors import LabNotFoundError, LabUnavailableError
from ..labs.loader import Lab
from ..labs.protocol import (
    HostSummary,
    LabContribution,
    LabRepository,
    LabScope,
    LabStatus,
    SupportsHostSummaries,
    SupportsHostSummariesInView,
    SupportsInventoryView,
    SupportsLabContribution,
    SupportsLabScope,
    SupportsLabStatus,
    logins_of_creds,
)
from ..labs.summaries import host_summaries_in_view
from ..labs.views import (
    lab_scope_of,
    lab_statuses,
    load_contribution_through,
    load_lab_through,
)
from ..models.base import CredSpec
from ..models.inventory import (
    FILLABLE_INVENTORY_FIELDS,
    INVENTORY_KEY_FIELDS,
    SUPPLIES_EXEMPT_FIELDS,
    InventoryInstance,
    InventoryRecord,
)
from ..reservations.check import ReservationBackendError
from ..reservations.protocol import (
    Reservation,
    ReservationBackend,
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
clock.  Every time-sensitive rule here brackets ``now`` by this one amount
rather than inventing its own tolerance: the overlap rule asks for
``[now - _CLOCK_SKEW, now + _CLOCK_SKEW]``, and the lapsed-row rule forgives a
booking that ended within ``_CLOCK_SKEW`` of the instant it asked about.  The
two are the same number on purpose — a row still returned for that window is
exactly a row the lapsed rule must not fail.

It covers skew **only**, never the round trip, and that is why the lapsed-row
rule is handed the instant read *before* the fetch rather than reading the
clock when the answer lands.  Reading it afterwards would spend the tolerance
on latency: an 800 ms scheduler holding a booking that ends 300 ms after it
answers reports an ``end`` 1.1 s before the helper's clock, and a correct
backend fails intermittently.  A tolerance consumed by transport is not a
tolerance.
"""


def assert_lab_repository_conforms(
    repository: LabRepository,
    *,
    expected_labs: list[str] | None = None,
    expect_host_summaries: bool = False,
    inventory: Inventory | None = None,
    view: InventoryView | None = None,
    expected_ips: dict[str, str] | None = None,
) -> None:
    """Assert *repository* satisfies the :class:`~otto.labs.protocol.LabRepository` contract.

    Runs structural rules unconditionally; for every listed lab, asserts it
    loads to a valid :class:`~otto.labs.loader.Lab`; asserts an unknown
    name raises :class:`~otto.labs.LabNotFoundError`. When *expected_labs*
    is given, also asserts each appears in ``list_labs()`` and loads. Raises a
    single :class:`AssertionError` aggregating every violated rule.

    ``load_lab`` must also accept an ``inventory=`` keyword (spec 2026-08-28
    host-inventory §6): checked on the SIGNATURE, because a backend with no
    referenced entry in its data would pass every behavioural rule while
    silently dropping the argument the moment one appears.

    The probes run in one of two modes. By default (*view* absent) every load
    is ``load_lab(name, inventory=inventory)``, the legacy call, and the
    capabilities are checked under the view that argument stands for,
    :meth:`InventoryView.of_default(inventory=inventory)
    <otto.inventory.InventoryView.of_default>`, which reaches ``default``
    only. With a named *view*, every load goes through
    :func:`~otto.labs.load_lab_through` and every summary through
    :func:`~otto.labs.host_summaries_in_view`, the adapters production uses,
    so a qualified ``{"name": ..., "key": ...}`` reference reaches its named
    inventory. A lab the source reports unavailable is never loaded, and an
    unknown name may raise :class:`~otto.labs.LabUnavailableError` instead of
    :class:`~otto.labs.LabNotFoundError` when a failed source's scope
    contains it.

    Each optional capability is checked on its own, only when the repository
    implements it:

    - :class:`~otto.labs.protocol.SupportsLabStatus`: the statuses name
      exactly the labs ``list_labs()`` returns, and every unavailable status
      states a reason.
    - :class:`~otto.labs.protocol.SupportsLabScope`: ``lab_scope()`` is
      ``None`` or a :class:`~otto.labs.LabScope`, and every lab the source
      reports is inside it.
    - :class:`~otto.labs.protocol.SupportsInventoryView`, by default: each
      lab loads to the same hosts at the same addresses through the view as
      through ``load_lab(inventory=inventory)``. With a named *view* the two
      are never compared (the legacy call rightly refuses a qualified
      reference to a named inventory); *expected_ips* is what tells a backend
      that ignores the view apart.
    - :class:`~otto.labs.protocol.SupportsLabContribution`: each lab's
      contribution, through the probes' view, holds the same hosts at the
      same addresses as the lab's load, and its links, composed over it
      alone, are the links the lab loads with through that view.
    - :class:`~otto.labs.protocol.SupportsHostSummaries` and
      :class:`~otto.labs.protocol.SupportsHostSummariesInView`: the summaries
      agree with the loads, id for id and field for field, under the same
      binding.

    Parameters
    ----------
    repository : LabRepository
        The backend instance under test.
    expected_labs : list[str] | None
        Optional lab names the caller knows the backend should provide.
    expect_host_summaries : bool
        When ``True``, the optional
        :class:`~otto.labs.protocol.SupportsHostSummaries` capability (or its
        in-view form) is required rather than merely honoured: a repository
        that implements neither FAILS instead of being skipped for those
        rules. Defaults to ``False``, which keeps the skip-on-absent
        behaviour the contract mandates — absence of the capability is legal.
        Pass ``True`` from your own suite when your repository is meant to
        have it, so a later refactor that drops the method reddens conformance
        instead of silently costing your users the fast completion path.
    inventory : Inventory | None
        The ``default`` inventory the probes bind, by default: pass the one
        your test data's bare ``"inventory": "<key>"`` references resolve
        through. Ignored when *view* is given. Defaults to ``None``, no
        inventory, as before.
    view : InventoryView | None
        A view holding named inventories. When given, every probe resolves
        through it (named mode). Defaults to ``None``.
    expected_ips : dict[str, str] | None
        Host id to the address each host must have when its lab loads, and in
        the summaries when the repository offers them. Under a named *view*
        this is the rule that fails a backend ignoring the view, so pass it
        for a host whose reference names an inventory other than ``default``.
    """
    c = ExpectCollector()
    named = view is not None
    probe_view = view if view is not None else InventoryView.of_default(inventory=inventory)

    def load(name: str) -> Lab:
        """Load *name* the way this mode's probes do."""
        if named:
            return load_lab_through(repository=repository, name=name, view=probe_view)
        return _legacy_load(repository, name, inventory=inventory)

    c.expect(
        isinstance(repository, LabRepository),
        msg="LabRepository: must satisfy the runtime_checkable LabRepository protocol",
    )
    c.expect(
        callable(getattr(repository, "load_lab", None)),
        msg="LabRepository: load_lab must be callable",
    )
    c.expect(
        callable(getattr(repository, "list_labs", None)),
        msg="LabRepository: list_labs must be callable",
    )
    if callable(getattr(repository, "load_lab", None)):
        params = inspect.signature(repository.load_lab).parameters
        c.expect(
            "inventory" in params,
            msg="LabRepository: load_lab must accept an inventory= keyword "
            "(spec 2026-08-28 host-inventory §6)",
        )

    names = repository.list_labs() if callable(getattr(repository, "list_labs", None)) else []
    names_ok = isinstance(names, list)
    c.expect(
        names_ok,
        msg=f"LabRepository: list_labs() must return a list, got {type(names).__name__}",
    )
    statuses = _expect_lab_statuses_conform(
        c, repository, names=names if names_ok else None, view=probe_view
    )
    unavailable = {s.name for s in statuses if not s.available}
    available: list[str] = []
    built: dict[str, Lab] = {}
    if names_ok:
        for n in names:
            c.expect(
                isinstance(n, str),
                msg=(
                    f"LabRepository: list_labs() entries must be str, "
                    f"got {type(n).__name__} ({n!r})"
                ),
            )
        available = [n for n in names if isinstance(n, str) and n not in unavailable]
        built = _expect_labs_load(c, names=available, load=load)

    _expect_unknown_lab_refused(c, load=load)

    if expected_labs is not None:
        listed = set(names) if names_ok else set()
        for n in expected_labs:
            c.expect(n in listed, msg=f"LabRepository: expected lab {n!r} to appear in list_labs()")
            if n in unavailable:
                continue
            try:
                load(n)
            except Exception as e:  # noqa: BLE001 — conformance check, must catch any impl exception to report violation
                c.expect(
                    False,
                    msg=f"LabRepository: expected lab {n!r} to load, got {type(e).__name__}: {e}",
                )

    if isinstance(repository, SupportsLabScope):
        _expect_lab_scope_conforms(c, repository, statuses=statuses)
    if isinstance(repository, SupportsInventoryView) and not named:
        _expect_view_load_matches_legacy(c, repository, built=built, view=probe_view)
    if isinstance(repository, SupportsLabContribution):
        _expect_contributions_match_loads(c, repository, built=built, view=probe_view)
    summarized = _expect_summaries_capabilities(
        c,
        repository,
        names=available if names_ok else None,
        expect_host_summaries=expect_host_summaries,
        inventory=inventory,
        view=view,
        probe_view=probe_view,
        load=load,
    )

    if expected_ips is not None:
        _expect_addresses(
            c,
            expected_ips=expected_ips,
            built=built,
            summaries=(
                (lambda: host_summaries_in_view(repository=repository, view=probe_view))
                if summarized
                else None
            ),
            where="under the supplied view" if named else "under inventory=",
        )
    elif named:
        _LOG.info(
            "assert_lab_repository_conforms: no expected_ips, so nothing tells a backend "
            "that ignores the view apart; pass expected_ips for a qualified reference"
        )

    c.raise_if_failures()


def _expect_labs_load(
    c: ExpectCollector, *, names: list[str], load: "Callable[[str], Lab]"
) -> dict[str, Lab]:
    """Check each lab of *names* loads to a valid, repeatable :class:`~otto.labs.loader.Lab`.

    Returns the labs that loaded, by name.
    """
    built: dict[str, Lab] = {}
    for n in names:
        try:
            lab = load(n)
        except Exception as e:  # noqa: BLE001 — conformance check, must catch any impl exception to report violation
            c.expect(False, msg=f"LabRepository: load_lab({n!r}) raised {type(e).__name__}: {e}")
            continue
        is_lab = isinstance(lab, Lab)
        c.expect(
            is_lab,
            msg=f"LabRepository: load_lab({n!r}) must return a Lab, got {type(lab).__name__}",
        )
        if not is_lab:
            continue
        built[n] = lab
        for host_id, host in lab.hosts.items():
            c.expect(
                isinstance(host, RemoteHost),
                msg=f"LabRepository: lab {n!r} host {host_id!r} must be a "
                f"RemoteHost, got {type(host).__name__}",
            )
            c.expect(
                host_id == getattr(host, "id", None),
                msg=f"LabRepository: lab {n!r} host key {host_id!r} must equal "
                f"host.id {getattr(host, 'id', None)!r}",
            )
        try:
            lab2 = load(n)
        except Exception as e:  # noqa: BLE001 — conformance check, must catch any impl exception to report violation
            c.expect(
                False,
                msg=f"LabRepository: load_lab({n!r}) idempotency re-call raised "
                f"{type(e).__name__}: {e}",
            )
        else:
            c.expect(
                sorted(lab.hosts) == sorted(lab2.hosts) and lab.resources == lab2.resources,
                msg=f"LabRepository: load_lab({n!r}) must be idempotent "
                f"(two calls must yield equivalent labs)",
            )
    return built


def _expect_unknown_lab_refused(c: ExpectCollector, *, load: "Callable[[str], Lab]") -> None:
    """Check an unknown name raises :class:`~otto.labs.LabNotFoundError`.

    Not ``None`` and not a bare ``KeyError``. A
    :class:`~otto.labs.LabUnavailableError` reporting a failed source
    (``declared=False``) is accepted on the error's own word: only the
    repository knows which source failed and what its scope holds, so this
    helper does not re-decide it. A declared one fails: a declaration cannot
    mark a name nobody declares.
    """
    try:
        load(_NO_SUCH_LAB)
        c.expect(
            False,
            msg=f"LabRepository: load_lab({_NO_SUCH_LAB!r}) must raise LabNotFoundError "
            f"for an unknown lab, but it returned normally",
        )
    except LabNotFoundError:
        pass
    except LabUnavailableError as e:
        c.expect(
            not e.declared,
            msg=f"LabRepository: an unknown lab must raise LabNotFoundError, got a declared "
            f"LabUnavailableError: {e}",
        )
    except Exception as e:  # noqa: BLE001 — conformance check, distinguishes unexpected exception from LabNotFoundError
        c.expect(
            False,
            msg=f"LabRepository: an unknown lab must raise LabNotFoundError, got "
            f"{type(e).__name__}: {e}",
        )


def _expect_summaries_capabilities(
    c: ExpectCollector,
    repository: LabRepository,
    *,
    names: list[str] | None,
    expect_host_summaries: bool,
    inventory: Inventory | None,
    view: InventoryView | None,
    probe_view: InventoryView,
    load: "Callable[[str], Lab]",
) -> bool:
    """Check whichever host-summaries capabilities *repository* implements.

    By default (*view* ``None``) the legacy ``list_host_summaries`` is checked
    against the legacy loads, and the in-view form against loads through
    *probe_view*. Under a named *view* the summaries are read the way
    production reads them, through :func:`~otto.labs.host_summaries_in_view`.
    *names* are the labs to load, or ``None`` when ``list_labs()`` failed.
    Returns whether the summaries can be read at all.
    """
    # Optional capability: only checked when the backend advertises it (the
    # same shape as SupportsUsernameCompletion for reservation backends).
    # `expect_host_summaries=True` turns that skip into a failure, because
    # absence is legal for the contract but not necessarily for THIS caller.
    legacy = isinstance(repository, SupportsHostSummaries)
    in_view = isinstance(repository, SupportsHostSummariesInView)
    if expect_host_summaries and not (legacy or in_view):
        c.expect(
            False,
            msg="SupportsHostSummaries: expect_host_summaries=True, but the repository "
            "implements neither list_host_summaries() nor list_host_summaries_in_view() — "
            "completion would fall back to loading every lab instead of the fast path this "
            "caller asserts it has",
        )
    # A legacy method without the inventory= keyword fails on its signature
    # alone: calling it any other way than production does certifies nothing.
    signed = not legacy or _expect_summaries_signature(c, repository)
    if not (legacy or in_view) or not signed:
        return False
    if names is None:
        return True
    if legacy and view is None:
        _expect_host_summaries_conform(
            c,
            rule="SupportsHostSummaries",
            method="list_host_summaries()",
            summarize=lambda: repository.list_host_summaries(inventory=inventory),
            load=load,
            names=names,
        )
    if in_view or view is not None:
        _expect_host_summaries_conform(
            c,
            rule="SupportsHostSummariesInView" if in_view else "SupportsHostSummaries",
            method="list_host_summaries_in_view()" if in_view else "list_host_summaries()",
            summarize=lambda: host_summaries_in_view(repository=repository, view=probe_view),
            load=lambda name: load_lab_through(repository=repository, name=name, view=probe_view),
            names=names,
        )
    return True


def _legacy_load(repository: LabRepository, name: str, *, inventory: Inventory | None) -> Lab:
    """Call the legacy ``load_lab``: with *inventory* when one is bound, else exactly as before."""
    if inventory is None:
        return repository.load_lab(name)
    return repository.load_lab(name, inventory=inventory)


def _expect_lab_statuses_conform(
    c: ExpectCollector,
    repository: LabRepository,
    *,
    names: list[str] | None,
    view: InventoryView,
) -> list[LabStatus]:
    """Return the statuses :func:`~otto.labs.lab_statuses` reports; check the capability's rules.

    The rules run only for a repository implementing
    :class:`~otto.labs.protocol.SupportsLabStatus`: the statuses name exactly
    the labs ``list_labs()`` returns (*names*), and every unavailable status
    states a reason. Any other repository reports every listed lab
    available, so nothing here can fail for it.
    """
    advertised = isinstance(repository, SupportsLabStatus)
    try:
        statuses = lab_statuses(repository=repository, view=view)
    except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
        c.expect(
            not advertised,
            msg=f"SupportsLabStatus: list_lab_status() raised {type(e).__name__}: {e}",
        )
        return []
    if not isinstance(statuses, list):
        c.expect(
            False,
            msg=f"SupportsLabStatus: list_lab_status() must return a list, "
            f"got {type(statuses).__name__}",
        )
        return []
    valid = [s for s in statuses if isinstance(s, LabStatus)]
    for s in statuses:
        c.expect(
            isinstance(s, LabStatus),
            msg=f"SupportsLabStatus: entries must be LabStatus, got {type(s).__name__}",
        )
    if not advertised:
        return valid
    if names is not None:
        reported = sorted(s.name for s in valid)
        c.expect(
            reported == sorted(n for n in names if isinstance(n, str)),
            msg=f"SupportsLabStatus: list_lab_status() reports {reported} but list_labs() "
            f"returns {sorted(names)} — the two must name the same labs",
        )
    for s in valid:
        if s.unavailable is None:
            continue
        reason = getattr(s.unavailable, "reason", None)
        c.expect(
            isinstance(reason, str) and bool(reason.strip()),
            msg=f"SupportsLabStatus: lab {s.name!r} is unavailable but states no reason "
            f"({s.unavailable!r}) — `otto --list-labs` would show nothing to act on",
        )
    return valid


def _expect_lab_scope_conforms(
    c: ExpectCollector, repository: LabRepository, *, statuses: list[LabStatus]
) -> None:
    """Check the :class:`~otto.labs.protocol.SupportsLabScope` capability's rules.

    ``lab_scope()`` is ``None`` or a :class:`~otto.labs.LabScope`, and every lab
    the repository reports lies inside it: a scoped source's outage fails only
    the labs in its scope, so a lab outside it would be spared wrongly.
    """
    try:
        scope = lab_scope_of(repository=repository)
    except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
        c.expect(False, msg=f"SupportsLabScope: lab_scope() raised {type(e).__name__}: {e}")
        return
    if scope is None:
        return
    if not isinstance(scope, LabScope):
        c.expect(
            False,
            msg=f"SupportsLabScope: lab_scope() must return None or a LabScope, "
            f"got {type(scope).__name__}",
        )
        return
    for s in statuses:
        c.expect(
            scope.contains(name=s.name),
            msg=f"SupportsLabScope: the repository reports lab {s.name!r}, which is outside "
            f"its lab_scope() {scope!r} — its outage would wrongly spare that lab",
        )


def _addresses(lab: Lab) -> dict[str, str]:
    """Return each host of *lab* by id, with its address."""
    return {host_id: getattr(host, "ip", "") or "" for host_id, host in lab.hosts.items()}


def _expect_view_load_matches_legacy(
    c: ExpectCollector,
    repository: LabRepository,
    *,
    built: dict[str, Lab],
    view: InventoryView,
) -> None:
    """Check :class:`~otto.labs.protocol.SupportsInventoryView` under the default-only *view*.

    *view* reaches ``default`` alone, so loading through it must give the same
    hosts at the same addresses as the legacy ``load_lab(inventory=inventory)``
    did for every lab in *built* (the labs the default-mode probes loaded,
    which is that legacy call).
    """
    for n, legacy in built.items():
        try:
            in_view = load_lab_through(repository=repository, name=n, view=view)
        except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
            c.expect(
                False,
                msg=f"SupportsInventoryView: load_lab_in_view({n!r}) under the default-only "
                f"view raised {type(e).__name__}: {e}",
            )
            continue
        c.expect(
            _addresses(in_view) == _addresses(legacy),
            msg=f"SupportsInventoryView: load_lab_in_view({n!r}) under the default-only view "
            f"must give the same hosts as load_lab({n!r}, inventory=...): "
            f"{_addresses(in_view)} vs {_addresses(legacy)}",
        )


def _expect_contributions_match_loads(
    c: ExpectCollector,
    repository: LabRepository,
    *,
    built: dict[str, Lab],
    view: InventoryView,
) -> None:
    """Check :class:`~otto.labs.protocol.SupportsLabContribution` against the probes' loads.

    Each lab in *built* (the labs the probes loaded) must contribute, through
    *view*, the same hosts at the same addresses, and its links, composed
    over that contribution alone (:func:`~otto.labs.composition.compose_links`),
    must be the links the lab loads with through *view*
    (:func:`~otto.labs.load_lab_through`): the composite merges the
    contribution, so a contribution that differs from the load is a lab that
    loads one way directly and another way through ``[[lab.sources]]``.
    """
    for n, loaded in built.items():
        try:
            contribution = load_contribution_through(repository=repository, name=n, view=view)
        except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
            c.expect(
                False,
                msg=f"SupportsLabContribution: load_contribution({n!r}) raised "
                f"{type(e).__name__}: {e}",
            )
            continue
        lab = getattr(contribution, "lab", None)
        got = _addresses(lab) if isinstance(lab, Lab) else None
        c.expect(
            got == _addresses(loaded),
            msg=f"SupportsLabContribution: load_contribution({n!r}) must contribute the hosts "
            f"the lab loads with: {got} vs {_addresses(loaded)}",
        )
        if isinstance(lab, Lab) and contribution is not None:
            _expect_composed_links_match_load(
                c, repository, lab=lab, contribution=contribution, view=view
            )


def _expect_composed_links_match_load(
    c: ExpectCollector,
    repository: LabRepository,
    *,
    lab: Lab,
    contribution: LabContribution,
    view: InventoryView,
) -> None:
    """Check *contribution*'s links, composed over its *lab* alone, are the links it loads with."""
    name = lab.name
    try:
        part = contribution_part(label=type(repository).__name__, contribution=contribution)
        composed = compose_links(lab=lab, contributions=[part])
        in_view = load_lab_through(repository=repository, name=name, view=view).links
    except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
        c.expect(
            False,
            msg=f"SupportsLabContribution: composing load_contribution({name!r})'s links "
            f"against the load raised {type(e).__name__}: {e}",
        )
        return
    c.expect(
        composed == in_view,
        msg=f"SupportsLabContribution: load_contribution({name!r})'s links, composed alone, "
        f"must be the links load_lab_in_view({name!r}) gives: "
        f"{[link.id for link in composed]} vs {[link.id for link in in_view]}",
    )


def _expect_addresses(
    c: ExpectCollector,
    *,
    expected_ips: dict[str, str],
    built: dict[str, Lab],
    summaries: "Callable[[], list[HostSummary]] | None",
    where: str,
) -> None:
    """Check every host of *expected_ips* has its address in the loads and the summaries.

    *built* holds the labs the probes loaded. *summaries*, when the repository
    offers them, reads them under the same binding.
    """
    loaded: dict[str, str] = {}
    for lab in built.values():
        loaded.update(_addresses(lab))
    for host_id, ip in expected_ips.items():
        got = loaded.get(host_id)
        c.expect(
            got == ip,
            msg=f"InventoryView: {where}, host {host_id!r} must load at {ip!r}, but the "
            f"lab load gives {got!r} — the backend does not resolve its references "
            "through the binding it was handed",
        )
    if summaries is None:
        return
    try:
        offered = {s.id: s.ip for s in summaries() if isinstance(s, HostSummary)}
    except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
        c.expect(False, msg=f"InventoryView: {where}, the summaries raised {type(e).__name__}: {e}")
        return
    for host_id, ip in expected_ips.items():
        got = offered.get(host_id)
        c.expect(
            got == ip,
            msg=f"InventoryView: {where}, host {host_id!r} must be summarized at {ip!r}, but "
            f"the summaries give {got!r}",
        )


def _expect_summaries_signature(c: ExpectCollector, repository: "SupportsHostSummaries") -> bool:
    """Check ``list_host_summaries`` takes the ``inventory=`` keyword; return whether it does."""
    # The call shape production uses (``otto.labs.list_host_summaries`` passes
    # ``inventory=`` by keyword, spec 2026-08-28 host-inventory §6), checked on
    # the SIGNATURE first for the same reason ``load_lab``'s is: a backend whose
    # data holds no referenced entry passes every behavioural rule below with
    # a no-argument method — and then TypeErrors in the shell the first time
    # otto calls it for real. This asserter once called it with no arguments
    # itself, certifying exactly that backend.
    params = inspect.signature(repository.list_host_summaries).parameters
    c.expect(
        "inventory" in params,
        msg="SupportsHostSummaries: list_host_summaries must accept an inventory= keyword "
        "(spec 2026-08-28 host-inventory §6; production calls it that way)",
    )
    return "inventory" in params


def _expect_host_summaries_conform(
    c: ExpectCollector,
    *,
    rule: str,
    method: str,
    summarize: "Callable[[], list[HostSummary]]",
    load: "Callable[[str], Lab]",
    names: list[str],
) -> None:
    """Check a host-summaries capability's contract: *summarize* agrees with *load*.

    *rule* names the capability in every message and *method* the call that
    produced the summaries. *summarize* and *load* bind the same inventory, so
    the two describe the same hosts. *names* are the labs to load.

    Three rules, in ascending order of what they cost a user to get wrong:

    - Every summarized id must be one ``load_lab`` actually produces. A fast
      path deriving ids by a different route than host construction offers
      completions that do not dispatch — worse than offering none.
    - Every constructed host must be summarized. The reverse direction, and
      the reason it matters is that nothing else notices: the completer just
      quietly stops offering that host.
    - Every FIELD must agree with the constructed host. A summary is not an
      id lookup — ``labs`` drives ``--lab``-scoped completion, ``docker_capable``
      gates ``otto docker --parent``, and ``ip`` drives tunnel narrowing. A backend
      that fills in only ``id`` passed every earlier version of this check
      while silently breaking the other surfaces.

    All three are scoped to labs that actually LOADED. A lab whose load
    raises (a host naming an ``os_profile`` this process never registered,
    say) is reported by the caller's own rules; letting its hosts count here
    made the first rule report every one of them as undispatchable.
    """
    try:
        summaries = summarize()
    except Exception as e:  # noqa: BLE001 — conformance check, any failure is a violation
        c.expect(False, msg=f"{rule}: {method} raised {type(e).__name__}: {e}")
        return

    if not isinstance(summaries, list):
        c.expect(
            False,
            msg=f"{rule}: must return a list, got {type(summaries).__name__}",
        )
        return

    seen: set[str] = set()
    for s in summaries:
        if not isinstance(s, HostSummary):
            c.expect(
                False,
                msg=f"{rule}: entries must be HostSummary, got {type(s).__name__}",
            )
            continue
        c.expect(
            bool(s.id) and isinstance(s.id, str),
            msg=f"{rule}: every summary needs a non-empty str id, got {s.id!r}",
        )
        c.expect(
            s.id not in seen,
            msg=f"{rule}: duplicate id {s.id!r} — hosts in several labs "
            f"must merge into one summary with both labs",
        )
        seen.add(s.id)

    constructed: dict[str, Any] = {}
    labs_of: dict[str, set[str]] = {}
    loaded: set[str] = set()
    for n in names:
        try:
            hosts = load(n).hosts
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
        msg=f"{rule}: ids {undispatchable} are offered by {method} "
        f"but no load_lab() produces them — completion would offer ids that cannot dispatch",
    )

    # No built-in exemption: ``local`` is injected by ``labs.loader.load_lab``,
    # never by a backend, so it is not in `constructed` to begin with. Excusing
    # it would only ever excuse a backend that defines its OWN ``local`` —
    # which otto explicitly allows, and which ``otto.labs.host_summaries``
    # deliberately refuses to filter, so the two paths would disagree on
    # exactly the id that comment says must not be dropped.
    unsummarized = sorted(set(constructed) - seen)
    c.expect(
        not unsummarized,
        msg=f"{rule}: load_lab() produces {unsummarized} but "
        f"{method} omits them — completion would silently stop offering them",
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
            (
                "docker_priority",
                summary.docker_priority,
                int(getattr(host, "docker_priority", 0)),
            ),
            # A constructed host ALWAYS carries a selector (the factory defaults
            # it to "unix"), so a summary leaving it None is a backend that did
            # not record it, not a host that has none.
            ("os_type", summary.os_type, getattr(host, "os_type", None)),
            (
                "logins",
                list(summary.logins),
                logins_of_creds(creds=getattr(host, "creds", None) or []),
            ),
        ):
            hint = {
                "os_type": (
                    " — `otto host <id> <TAB>` would offer every class's verbs "
                    "instead of this host's"
                ),
                "logins": " — `--user <TAB>` would offer the wrong logins",
            }.get(field, "")
            c.expect(
                summarized == built,
                msg=f"{rule}: {summary.id!r}.{field} is {summarized!r} in the "
                f"summary but {built!r} on the constructed host" + hint,
            )
        produced_in = labs_of.get(summary.id, set())
        claimed = set(summary.labs)
        c.expect(
            claimed >= produced_in,
            msg=f"{rule}: {summary.id!r}.labs is {sorted(claimed)} but "
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
            msg=f"{rule}: {summary.id!r}.labs claims {overclaimed} but "
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
            msg=f"ReservationBackend: {label} must return a list of Reservation, got "
            f"{type(rows).__name__}",
        )
        return []
    good: list[Reservation] = []
    for r in rows:
        if not isinstance(r, Reservation):
            c.expect(
                False,
                msg=(
                    f"ReservationBackend: {label} entries must be Reservation, "
                    f"got {type(r).__name__}"
                ),
            )
            continue
        c.expect(
            isinstance(r.user, str) and r.user != "",
            msg=f"ReservationBackend: {label} user must be a non-empty str, got {r.user!r}",
        )
        c.expect(
            isinstance(r.resource, str) and r.resource != "",
            msg=f"ReservationBackend: {label} resource must be a non-empty str, got {r.resource!r}",
        )
        aware = all(b is None or b.tzinfo is not None for b in (r.start, r.end))
        c.expect(
            aware,
            msg=f"ReservationBackend: {label} start/end must be timezone-aware when set "
            f"({r.resource!r}: start={r.start!r}, end={r.end!r})",
        )
        if aware and r.start is not None and r.end is not None:
            c.expect(
                r.start <= r.end,
                msg=f"ReservationBackend: {label} start <= end required "
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
            msg=f"ReservationBackend: {label} must return only rows for {user!r}, "
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
        msg=f"ReservationBackend: fetch_reservations() must match bookings overlapping "
        f"[start, end], not bookings contained in it — {dropped!r} came back unbounded "
        f"but not for a window bracketing now, so a booking overlapping that window was "
        f"dropped; that reading is what makes otto's gate fail open",
    )


def _expect_no_lapsed_rows(
    c: ExpectCollector, label: str, rows: list[Reservation], asked_at: datetime
) -> None:
    """Expect the default query to return no booking that has already ended.

    The other direction of the window predicate, and the one nothing else
    catches.  :func:`_expect_overlap_semantics` computes ``wide - narrow``, so
    it only sees a backend that *drops* rows; a backend that reads a query's
    ``start=None`` as "-infinity" instead of "this instant" hands back every
    booking that ever existed, lapsed ones included, and passes it.  That is
    the dangerous direction: otto's *check* gate never re-filters by ``end`` —
    it reads ``.end`` only for the expiry warning and the "held by … until"
    text — so a lapsed row admits a user whose booking is over.  It fails OPEN.

    The published predicate (:doc:`/extend/reservation-backends`, "The
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
    asked_at : datetime
        The instant the caller read the clock **before** issuing the fetch.
        Not re-read here, and that is the whole point: taking it afterwards
        would let the round trip eat into ``_CLOCK_SKEW`` instead of being
        covered by it, so a slow scheduler holding a booking that lapses
        mid-call would fail a backend doing nothing wrong.
    """
    cutoff = asked_at - _CLOCK_SKEW
    for r in rows:
        # A naive ``end`` is uncomparable to an aware ``now``; the row rules
        # already report it, and raising TypeError here would lose every other
        # violation the collector holds.
        if r.end is None or r.end.tzinfo is None or r.end > cutoff:
            continue
        c.expect(
            False,
            msg=f"ReservationBackend: {label} must return only bookings still active — "
            f"{r.resource!r} ended at {r.end.isoformat()}, before the instant asked about "
            f"({asked_at.isoformat()}), so a lapsed booking came back; an unbounded call "
            f"means 'active at this instant', not 'every booking that ever existed', and "
            f"otto's check gate does not re-filter by end, so that reading admits a user "
            f"whose booking is over",
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
                    msg=f"SupportsResourceHolders: holders({resource!r}) must include a row for "
                    f"{user!r}, who holds it per fetch_reservations; got {sorted(named)!r}",
                )
        for r in rows:
            back = _expect_reservation_rows(
                c, f"fetch_reservations({r.user!r})", backend.fetch_reservations(r.user)
            )
            c.expect(
                resource in {b.resource for b in back},
                msg=f"SupportsResourceHolders: holders({resource!r}) names {r.user!r} as a holder, "
                f"but fetch_reservations({r.user!r}) does not return {resource!r}",
            )


def assert_reservation_backend_conforms(
    backend: ReservationBackend,
    *,
    known_user: str | None = None,
    known_resources: list[str] | None = None,
    expect_holders: bool = False,
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
    holder rules, not failed — unless the caller passes *expect_holders*, which
    turns that skip into a failure.  Raises a single :class:`AssertionError`
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
    expect_holders : bool
        When ``True``, the optional
        :class:`~otto.reservations.SupportsResourceHolders` capability is
        required rather than merely honoured: a backend that does not implement
        ``holders`` FAILS instead of being skipped for those rules. Defaults to
        ``False``, which keeps the skip-on-absent behaviour the contract
        mandates — a per-user-only scheduler legitimately has no ``holders``.
        Pass ``True`` from your own suite when your backend is meant to answer
        the inverted query, so a later refactor that drops the method reddens
        conformance instead of silently degrading every refusal message to
        ``held by: unknown``.
    """
    c = ExpectCollector()

    c.expect(
        isinstance(backend, ReservationBackend),
        msg="ReservationBackend: must satisfy the runtime_checkable ReservationBackend protocol",
    )
    fetch_ok = callable(getattr(backend, "fetch_reservations", None))
    c.expect(fetch_ok, msg="ReservationBackend: fetch_reservations must be callable")
    name_ok = callable(getattr(backend, "backend_name", None))
    c.expect(name_ok, msg="ReservationBackend: backend_name must be callable")

    name = backend.backend_name() if name_ok else ""
    c.expect(
        isinstance(name, str) and name != "",
        msg=f"ReservationBackend: backend_name() must return a non-empty str, got {name!r}",
    )
    if name_ok:
        c.expect(
            name == backend.backend_name(),
            msg="ReservationBackend: backend_name() must be stable across calls",
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
            msg=f"ReservationBackend: reservations must not raise for a backend whose "
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
                msg="ReservationBackend: must expose a `reservations` member holding the "
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
    # Read BEFORE the call, not after: this is the instant the query means by
    # "now", and _expect_no_lapsed_rows compares against it so that the round
    # trip cannot eat the skew tolerance.
    asked_at = datetime.now(tz=timezone.utc)
    fetched = _expect_reservation_rows(c, label, backend.fetch_reservations(probe_user))
    _expect_rows_belong_to(c, label, fetched, probe_user)
    _expect_no_lapsed_rows(c, label, fetched, asked_at)
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
                msg=f"ReservationBackend: {label} must include known-held resource "
                f"{resource!r}, got {sorted(held)!r}",
            )

    # Absence of the capability is legal (spec §7 rule 6), so it skips by
    # default; `expect_holders=True` is the caller asserting a capability SET
    # rather than relying on that skip.
    if expect_holders and not isinstance(backend, SupportsResourceHolders):
        c.expect(
            False,
            msg="SupportsResourceHolders: expect_holders=True, but the backend does not implement "
            "holders() — otto would print 'held by: unknown' in every refusal naming a "
            "resource this backend guards",
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
            msg=f"SupportsUsernameCompletion: list_usernames() must return a list, got "
            f"{type(usernames).__name__}",
        )
        if u_ok:
            for u in usernames:
                c.expect(
                    isinstance(u, str),
                    msg=f"SupportsUsernameCompletion: list_usernames() entries must be "
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
    ``list_keys()`` a sorted list of strings each of which resolves, ``lookup``
    idempotent (an equal record on a second call) and never returning a field
    outside ``supplies`` (keys and ``extra`` excepted), an unknown key raising
    :class:`~otto.inventory.errors.InventoryKeyError`, ``fingerprint()``
    ``str | None`` and never raising. The protocol also requires
    ``fingerprint()`` to answer from local state and never fetch or probe the
    network; that rule is the backend author's, and this helper does not check it.
    With *expected_keys*, each must resolve AND appear in ``list_keys()``. With
    *repository* AND *lab*, the positive control: *lab* must FAIL to load
    without the inventory and LOAD with it, and at least one host must carry
    ``inventory_ref.referenced`` — a backend that ignores ``inventory=`` fails
    here.

    An inventory implementing the optional
    :class:`~otto.inventory.SupportsDiscovery` capability is also checked
    against itself: discovery's record keys equal ``list_keys()``, each of its
    records equals ``lookup`` of that key, and its instances are ``None`` or a
    dict of :class:`~otto.models.inventory.InventoryInstance` by name.
    """
    c = ExpectCollector()
    c.expect(
        isinstance(inventory, Inventory),
        msg="Inventory: must satisfy the runtime_checkable Inventory protocol",
    )
    label = getattr(inventory, "label", None)
    c.expect(
        isinstance(label, str) and bool(label),
        msg=f"Inventory: label must be a non-empty str, got {label!r}",
    )
    supplies = getattr(inventory, "supplies", None)
    supplies_ok = isinstance(supplies, frozenset)
    c.expect(
        supplies_ok, msg=f"Inventory: supplies must be a frozenset, got {type(supplies).__name__}"
    )
    allowed = FILLABLE_INVENTORY_FIELDS | INVENTORY_KEY_FIELDS
    if supplies_ok:
        c.expect("ip" in supplies, msg="Inventory: supplies must contain 'ip'")
        c.expect(
            supplies <= allowed,
            msg=f"Inventory: supplies names non-record fields: {sorted(supplies - allowed)}",
        )
    try:
        keys = inventory.list_keys() if callable(getattr(inventory, "list_keys", None)) else None
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, msg=f"Inventory: list_keys() raised {type(e).__name__}: {e}")
        keys = None
    keys_ok = isinstance(keys, list) and all(isinstance(k, str) for k in keys)
    c.expect(keys_ok, msg=f"Inventory: list_keys() must return list[str], got {keys!r}")
    if keys_ok:
        c.expect(keys == sorted(keys), msg="Inventory: list_keys() must be sorted")
    if expected_keys is not None:
        listed = set(keys) if keys_ok else set()
        for key in expected_keys:
            c.expect(
                key in listed,
                msg=f"Inventory: expected key {key!r} to appear in list_keys()",
            )
    probe = _PROBE_CREDS_KEY
    try:
        inventory.lookup(probe)
        c.expect(
            False,
            msg="Inventory: lookup(unknown key) must raise InventoryKeyError, returned a record",
        )
    except InventoryKeyError:
        pass
    except Exception as e:  # noqa: BLE001 — conformance check, report the wrong type
        c.expect(
            False,
            msg=f"Inventory: lookup(unknown key) must raise InventoryKeyError, "
            f"raised {type(e).__name__}",
        )
    for key in [*(keys if keys_ok else []), *(expected_keys or [])]:
        try:
            first = inventory.lookup(key)
            second = inventory.lookup(key)
        except Exception as e:  # noqa: BLE001 — conformance check
            c.expect(
                False,
                msg=f"Inventory: expected key {key!r} did not resolve: {type(e).__name__}: {e}",
            )
            continue
        c.expect(
            isinstance(first, InventoryRecord),
            msg=f"Inventory: lookup({key!r}) must return an InventoryRecord",
        )
        c.expect(
            first == second,
            msg=f"Inventory: lookup({key!r}) must be idempotent (equal record on a second call)",
        )
        if isinstance(first, InventoryRecord) and supplies_ok:
            stated = set(first.model_fields_set) - SUPPLIES_EXEMPT_FIELDS
            leaked = sorted(stated - supplies)
            c.expect(
                not leaked,
                msg=f"Inventory: lookup({key!r}) returned fields outside supplies: {leaked}",
            )
    try:
        fp = inventory.fingerprint() if callable(getattr(inventory, "fingerprint", None)) else 0
        c.expect(
            fp is None or isinstance(fp, str),
            msg=f"Inventory: fingerprint() must be str | None, got {type(fp).__name__}",
        )
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, msg=f"Inventory: fingerprint() raised {type(e).__name__}: {e}")
    if isinstance(inventory, SupportsDiscovery):
        _expect_discovery_conforms(c, inventory, keys=keys if keys_ok else None)
    if repository is not None and lab is not None:
        try:
            repository.load_lab(lab)
            c.expect(
                False,
                msg=f"Inventory: lab {lab!r} loaded WITHOUT the inventory — its entries do not "
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
                msg=f"Inventory: lab {lab!r} loaded with the inventory but no host carries a "
                "referenced inventory_ref",
            )
        except Exception as e:  # noqa: BLE001 — conformance check
            c.expect(
                False,
                msg=f"Inventory: lab {lab!r} failed to load WITH the inventory: "
                f"{type(e).__name__}: {e}",
            )
    c.raise_if_failures()


def _expect_discovery_conforms(
    c: ExpectCollector, inventory: Inventory, *, keys: list[str] | None
) -> None:
    """Check the optional :class:`~otto.inventory.SupportsDiscovery` capability's rules.

    Discovery (read through :func:`~otto.inventory.discover_inventory`, as
    every reader does) is the same inventory read whole: its record keys are
    exactly ``list_keys()`` (*keys*), each record equals ``lookup`` of its
    key and states the same fields
    (:func:`~otto.inventory.snapshot.stated_fields`), and its instances are
    ``None`` (unsupported) or a dict of
    :class:`~otto.models.inventory.InventoryInstance` by name.
    """
    try:
        found = discover_inventory(inventory=inventory)
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(
            False, msg=f"SupportsDiscovery: inventory_discovery() raised {type(e).__name__}: {e}"
        )
        return
    if not isinstance(found, Discovery):
        c.expect(
            False,
            msg=f"SupportsDiscovery: inventory_discovery() must return a Discovery, "
            f"got {type(found).__name__}",
        )
        return
    if keys is not None:
        c.expect(
            sorted(found.records) == keys,
            msg=f"SupportsDiscovery: inventory_discovery() record keys {sorted(found.records)} "
            f"must equal list_keys() {keys}",
        )
    for key, record in found.records.items():
        try:
            looked_up = inventory.lookup(key)
        except Exception as e:  # noqa: BLE001 — conformance check
            c.expect(
                False,
                msg=f"SupportsDiscovery: inventory_discovery() holds record {key!r}, but "
                f"lookup({key!r}) raised {type(e).__name__}: {e}",
            )
            continue
        c.expect(
            record == looked_up,
            msg=f"SupportsDiscovery: inventory_discovery() record {key!r} must equal "
            f"lookup({key!r}): {record!r} vs {looked_up!r}",
        )
        # Equal values can still differ in what they state (a field given at
        # its default), which otto's join, export and diff all read.
        stated, stated_by_lookup = stated_fields(record), stated_fields(looked_up)
        c.expect(
            record != looked_up or stated == stated_by_lookup,
            msg=f"SupportsDiscovery: inventory_discovery() record {key!r} states "
            f"{sorted(stated)} but lookup({key!r}) states {sorted(stated_by_lookup)}",
        )
    instances = found.instances
    c.expect(
        instances is None
        or (
            isinstance(instances, dict)
            and all(
                isinstance(name, str) and isinstance(instance, InventoryInstance)
                for name, instance in instances.items()
            )
        ),
        msg=f"SupportsDiscovery: instances must be None or a dict of InventoryInstance by "
        f"name, got {instances!r}",
    )


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
    to a non-empty ``list[CredSpec]`` that repeats no identity (login plus
    ``protocols`` scope, :func:`~otto.models.base.cred_identity`),
    idempotently. With *known_key*, that key must resolve non-empty and — when
    the store enumerates — appear in ``list_keys()``.
    """
    c = ExpectCollector()
    c.expect(
        isinstance(store, CredsStore),
        msg="CredsStore: must satisfy the runtime_checkable CredsStore protocol",
    )
    label = getattr(store, "label", None)
    c.expect(
        isinstance(label, str) and bool(label),
        msg=f"CredsStore: label must be a non-empty str, got {label!r}",
    )
    try:
        fp = store.fingerprint() if callable(getattr(store, "fingerprint", None)) else None
        c.expect(
            fp is None or isinstance(fp, str),
            msg=f"CredsStore: fingerprint() must be str or None, got {fp!r}",
        )
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, msg=f"CredsStore: fingerprint() raised {type(e).__name__}: {e}")
    try:
        probe = store.lookup(_PROBE_CREDS_KEY)
        c.expect(probe == [], msg=f"CredsStore: lookup(unknown key) must return [], got {probe!r}")
    except Exception as e:  # noqa: BLE001 — conformance check, report the wrong behaviour
        c.expect(
            False, msg=f"CredsStore: lookup(unknown key) must return [], raised {type(e).__name__}"
        )
    keys: list[str] | None = None
    try:
        keys = store.list_keys() if callable(getattr(store, "list_keys", None)) else None
    except Exception as e:  # noqa: BLE001 — conformance check
        c.expect(False, msg=f"CredsStore: list_keys() raised {type(e).__name__}: {e}")
    if keys is not None:
        keys_ok = isinstance(keys, list) and all(isinstance(k, str) for k in keys)
        c.expect(
            keys_ok, msg=f"CredsStore: list_keys() must return list[str] or None, got {keys!r}"
        )
        if keys_ok:
            c.expect(keys == sorted(keys), msg="CredsStore: list_keys() must be sorted")
            if known_key is not None:
                c.expect(
                    known_key in keys,
                    msg=f"CredsStore: known_key {known_key!r} must appear in list_keys()",
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
            c.expect(False, msg=f"CredsStore: lookup({key!r}) raised {type(e).__name__}: {e}")
            continue
        entries_ok = isinstance(first, list) and all(isinstance(e, CredSpec) for e in first)
        c.expect(entries_ok, msg=f"CredsStore: lookup({key!r}) must return list[CredSpec]")
        if not entries_ok:
            continue
        if key == known_key:
            c.expect(bool(first), msg=f"CredsStore: known_key {known_key!r} resolved to no entries")
        elif not first:
            c.expect(
                False, msg=f"CredsStore: list_keys() names {key!r} but lookup({key!r}) is empty"
            )
        c.expect(first == second, msg=f"CredsStore: lookup({key!r}) must be idempotent")
        identities = [e.identity for e in first]
        for identity in sorted({x for x in identities if identities.count(x) > 1}):
            c.expect(False, msg=f"CredsStore: lookup({key!r}) repeats identity {identity!r}")
    c.raise_if_failures()
