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

from ..configmodule.lab import Lab
from ..host.remote_host import RemoteHost
from ..reservations import ReservationBackend, SupportsUsernameCompletion
from ..storage import LabNotFoundError, LabRepository
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
    """Assert *repo* satisfies the :class:`~otto.storage.protocol.LabRepository` contract.

    Runs structural rules unconditionally; for every listed lab, asserts it
    loads to a valid :class:`~otto.configmodule.lab.Lab`; asserts an unknown
    name raises :class:`~otto.storage.LabNotFoundError`. When *expected_labs*
    is given, also asserts each appears in ``list_labs()`` and loads. Raises a
    single :class:`AssertionError` aggregating every violated rule.

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

    c.raise_if_failures()


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
    :class:`~otto.reservations.SupportsUsernameCompletion` capability is checked
    only when the backend implements it. Raises a single :class:`AssertionError`
    aggregating every violated rule.

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
