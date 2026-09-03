"""Reusable conformance suites for otto's pluggable backend interfaces.

Two helpers — one per interface — assert that a backend satisfies otto's
contract. Each runs every rule as a non-fatal ``expect()`` on a single
:class:`~otto.suite.expect.ExpectCollector`, then raises once with *all*
violations, so a backend author sees every problem at once instead of fixing
them one failed assertion at a time.

Structural/type rules always run. Behavioral round-trip rules run only when the
caller supplies known ground truth (so a SUT author can leverage their own
fixtures).

Usage::

    from otto.testing import (
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
    )


    def test_my_backend_conforms():
        assert_reservation_backend_conforms(MyBackend(), known_user="alice", known_resources=["r1"])
"""

import inspect
from datetime import datetime, timezone
from typing import Any

from ..config.lab import Lab
from ..host.remote_host import RemoteHost
from ..inventory import Inventory, InventoryKeyError
from ..labs import HostSummary, LabNotFoundError, LabRepository, SupportsHostSummaries
from ..models.inventory import (
    FILLABLE_INVENTORY_FIELDS,
    INVENTORY_KEY_FIELDS,
    SUPPLIES_EXEMPT_FIELDS,
    InventoryRecord,
)
from ..reservations import (
    ReservationBackend,
    ReservationWindow,
    SupportsReservationWindows,
    SupportsUsernameCompletion,
)
from ..suite.expect import ExpectCollector

# Sentinels for "this name definitely does not exist" probes.
_NO_SUCH_LAB = "__otto_conformance_no_such_lab__"
_PROBE_USER = "__otto_conformance_probe_user__"
_PROBE_RESOURCE = "__otto_conformance_probe_resource__"


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
      id lookup — ``labs`` drives ``--lab``-scoped completion, ``element``
      and ``element_id`` drive the positional handles (``dut1``),
      ``docker_capable`` gates ``otto docker --on``, and ``ip`` drives tunnel
      narrowing. A backend that fills in only ``id`` passed every earlier
      version of this check while silently breaking four surfaces.

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
            ("element", summary.element, getattr(host, "element", "") or ""),
            # Compared by (type, value): `7 == 7.0` and `False == 0` in
            # Python, and a float element_id is precisely the divergence
            # `host_identity` exists to prevent (`dut3.0` vs `dut3`).
            (
                "element_id",
                (type(summary.element_id), summary.element_id),
                (type(getattr(host, "element_id", None)), getattr(host, "element_id", None)),
            ),
            (
                "docker_capable",
                summary.docker_capable,
                bool(getattr(host, "docker_capable", False)),
            ),
        ):
            c.expect(
                summarized == built,
                f"SupportsHostSummaries: {summary.id!r}.{field} is {summarized!r} in the "
                f"summary but {built!r} on the constructed host",
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


def assert_reservation_backend_conforms(
    backend: ReservationBackend,
    *,
    known_user: str | None = None,
    known_resources: list[str] | None = None,
) -> None:
    """Assert *backend* satisfies the ReservationBackend contract.

    Structural/type rules always run. When *known_user* and *known_resources*
    (resources that user is known to hold) are both given, round-trip
    consistency rules run too. The optional
    :class:`~otto.reservations.SupportsUsernameCompletion` and
    :class:`~otto.reservations.SupportsReservationWindows` capabilities are
    checked only when the backend implements them. Raises a single
    :class:`AssertionError` aggregating every violated rule.

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
    c.expect(
        callable(getattr(backend, "get_reserved_resources", None)),
        "ReservationBackend: get_reserved_resources must be callable",
    )
    c.expect(
        callable(getattr(backend, "who_reserved", None)),
        "ReservationBackend: who_reserved must be callable",
    )
    c.expect(
        callable(getattr(backend, "backend_name", None)),
        "ReservationBackend: backend_name must be callable",
    )

    _backend_name_callable = callable(getattr(backend, "backend_name", None))
    name = backend.backend_name() if _backend_name_callable else ""
    c.expect(
        isinstance(name, str) and name != "",
        f"ReservationBackend: backend_name() must return a non-empty str, got {name!r}",
    )
    if _backend_name_callable:
        c.expect(
            name == backend.backend_name(),
            "ReservationBackend: backend_name() must be stable across calls",
        )

    probe_user = known_user if known_user is not None else _PROBE_USER
    reserved = (
        backend.get_reserved_resources(probe_user)
        if callable(getattr(backend, "get_reserved_resources", None))
        else set()
    )
    reserved_ok = isinstance(reserved, set)
    c.expect(
        reserved_ok,
        f"ReservationBackend: get_reserved_resources() must return a set, got "
        f"{type(reserved).__name__}",
    )
    if reserved_ok:
        for r in reserved:
            c.expect(
                isinstance(r, str),
                f"ReservationBackend: get_reserved_resources() entries must be str, "
                f"got {type(r).__name__}",
            )

    probe_resource = known_resources[0] if known_resources else _PROBE_RESOURCE
    holders = (
        backend.who_reserved(probe_resource)
        if callable(getattr(backend, "who_reserved", None))
        else []
    )
    holders_ok = isinstance(holders, list)
    c.expect(
        holders_ok,
        f"ReservationBackend: who_reserved() must return a list (empty = no holders, "
        f"never None), got {type(holders).__name__}",
    )
    if holders_ok:
        for u in holders:
            c.expect(
                isinstance(u, str),
                f"ReservationBackend: who_reserved() entries must be str, got {type(u).__name__}",
            )

    if known_user is not None and known_resources is not None:
        held = (
            backend.get_reserved_resources(known_user)
            if callable(getattr(backend, "get_reserved_resources", None))
            else set()
        )
        for r in known_resources:
            r_holders = (
                backend.who_reserved(r) if callable(getattr(backend, "who_reserved", None)) else []
            )
            c.expect(
                isinstance(r_holders, list) and known_user in r_holders,
                f"ReservationBackend: who_reserved({r!r}) must include known holder "
                f"{known_user!r}, got {r_holders!r}",
            )
            c.expect(
                isinstance(held, set) and r in held,
                f"ReservationBackend: get_reserved_resources({known_user!r}) must "
                f"include {r!r}, got {held!r}",
            )
            if isinstance(r_holders, list):
                for u in r_holders:
                    u_held = (
                        backend.get_reserved_resources(u)
                        if callable(getattr(backend, "get_reserved_resources", None))
                        else set()
                    )
                    c.expect(
                        isinstance(u_held, set) and r in u_held,
                        f"ReservationBackend: round-trip — {u!r} holds {r!r} per "
                        f"who_reserved, but {r!r} not in get_reserved_resources({u!r})",
                    )

    if isinstance(backend, SupportsReservationWindows):
        windows = backend.get_reservation_windows(probe_user)
        windows_ok = isinstance(windows, list)
        c.expect(
            windows_ok,
            f"SupportsReservationWindows: get_reservation_windows() must return a list, "
            f"got {type(windows).__name__}",
        )
        if windows_ok:
            for w in windows:
                is_window = isinstance(w, ReservationWindow)
                c.expect(
                    is_window,
                    f"SupportsReservationWindows: entries must be ReservationWindow, "
                    f"got {type(w).__name__}",
                )
                if not is_window:
                    continue
                c.expect(
                    isinstance(w.resource, str) and w.resource != "",
                    f"SupportsReservationWindows: resource must be a non-empty str, "
                    f"got {w.resource!r}",
                )
                c.expect(
                    w.start.tzinfo is not None and w.end.tzinfo is not None,
                    f"SupportsReservationWindows: start/end must be timezone-aware "
                    f"({w.resource!r})",
                )
                if w.start.tzinfo is not None and w.end.tzinfo is not None:
                    c.expect(
                        w.start <= w.end,
                        f"SupportsReservationWindows: start <= end required "
                        f"({w.resource!r}: {w.start} > {w.end})",
                    )
        # Only meaningful once every window is well-formed: a naive or
        # non-ReservationWindow entry makes the comparison below either raise
        # or report a difference the rules above already named.
        if windows_ok and known_user is not None:
            user_windows = backend.get_reservation_windows(known_user)
            if isinstance(user_windows, list) and all(
                isinstance(w, ReservationWindow)
                and w.start.tzinfo is not None
                and w.end.tzinfo is not None
                for w in user_windows
            ):
                now = datetime.now(tz=timezone.utc)
                active = {w.resource for w in user_windows if w.start <= now <= w.end}
                flat = backend.get_reserved_resources(known_user)
                c.expect(
                    isinstance(flat, set) and active == flat,
                    f"SupportsReservationWindows: resources with a window covering now "
                    f"({sorted(active)!r}) must equal get_reserved_resources() "
                    f"({sorted(flat) if isinstance(flat, set) else flat!r})",
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
    probe = "__otto_conformance_no_such_key__"
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
