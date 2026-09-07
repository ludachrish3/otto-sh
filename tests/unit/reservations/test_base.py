"""Unit tests for ReservationBackendBase — the official base class for backends.

The Protocol in ``otto.reservations.protocol`` stays the contract otto is
written against; the base class is the recommended way to satisfy it. These
tests pin what inheriting buys an implementer: a missing method fails at
instantiation naming that method, a complete subclass satisfies the runtime
Protocol, the constructor keeps what the factory passes, the ``reservations``
cache is lazy and fetched exactly once, and the optional capabilities stay
structural.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.reservations import (
    Reservation,
    ReservationBackend,
    ReservationBackendBase,
    ReservationBackendError,
    SupportsResourceHolders,
    SupportsUsernameCompletion,
)
from otto.testing import assert_reservation_backend_conforms

AWARE = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


class _Complete(ReservationBackendBase):
    """A conforming double: it honours the window predicate rather than ignoring it.

    Its rows end an hour from now, computed at call time. A fixed instant was
    the earlier spelling and it was a time bomb — the conformance helper's
    lapsed-row rule (an unbounded call means "active at this instant") would
    have started failing this double the moment that instant went past.
    """

    def __init__(self, *, url=None, repo_dir=None, username=None, holdings=None):
        super().__init__(url=url, repo_dir=repo_dir, username=username)
        self._holdings = holdings or {}

    def fetch_reservations(self, username, start=None, end=None):
        now = datetime.now(tz=timezone.utc)
        window_start = start if start is not None else now
        window_end = end if end is not None else now
        expires = now + timedelta(hours=1)
        if not (expires > window_start and window_start <= window_end):
            return []
        return [
            Reservation(user=username, resource=r, end=expires)
            for r in self._holdings.get(username, ())
        ]

    def backend_name(self) -> str:
        return "complete"


class RecordingBackend(ReservationBackendBase):
    """Counts fetches so laziness and caching are observable."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls: list[str] = []
        self.late_attribute = "set after super().__init__"

    def fetch_reservations(self, username, start=None, end=None):
        self.calls.append(username)
        # Touching an attribute assigned AFTER super().__init__ proves the
        # base never fetched during construction.
        assert self.late_attribute == "set after super().__init__"
        return [Reservation(user=username, resource="rack3", end=AWARE)]

    def backend_name(self):
        return "recording"


class TestAbstractContract:
    def test_missing_method_fails_at_instantiation_naming_it(self):
        class _Incomplete(ReservationBackendBase):
            def backend_name(self):
                return "incomplete"

        with pytest.raises(TypeError, match="fetch_reservations"):
            _Incomplete()

    def test_complete_subclass_satisfies_the_protocol(self):
        assert isinstance(_Complete(), ReservationBackend)

    def test_complete_subclass_without_holders_passes_conformance(self):
        """Satisfying the Protocol is structural; conformance is the behavioural claim.

        The public helper is what a third-party backend runs against, so the
        base class implementers are pointed at has to pass it.

        ``_Complete`` omits ``holders``, so this covers the structural, row,
        overlap and caller-ground-truth paths and *skips* the
        ``SupportsResourceHolders`` path — deliberately: a conforming backend
        whose scheduler answers only per-user queries is a shape worth keeping
        under test. The capability path is covered by
        ``tests/unit/testing/test_conformance.py``'s
        ``test_json_builtin_conforms_with_round_trip`` and
        ``test_the_base_double_conforms``.
        """
        backend = _Complete(username="alice", holdings={"alice": ["rack1"]})
        assert_reservation_backend_conforms(backend, known_user="alice", known_resources=["rack1"])


class TestConstructor:
    def test_keeps_what_the_factory_passes(self, tmp_path):
        backend = _Complete(url="https://sched.example", repo_dir=tmp_path, username="alice")
        assert backend.url == "https://sched.example"
        assert backend.repo_dir == tmp_path
        assert backend.username == "alice"

    def test_defaults_are_none(self):
        backend = _Complete()
        assert backend.url is None
        assert backend.repo_dir is None
        assert backend.username is None

    def test_repo_dir_is_kept_as_a_path(self, tmp_path):
        backend = _Complete(repo_dir=str(tmp_path))
        assert isinstance(backend.repo_dir, Path)


class TestReservationsProperty:
    def test_construction_performs_no_query(self):
        backend = RecordingBackend(username="alice")
        assert backend.calls == []

    def test_reservations_fetches_for_the_constructed_user(self):
        backend = RecordingBackend(username="alice")
        assert [r.resource for r in backend.reservations] == ["rack3"]
        assert backend.calls == ["alice"]

    def test_reservations_is_cached_after_first_access(self):
        backend = RecordingBackend(username="alice")
        first, second = backend.reservations, backend.reservations
        assert first is second
        assert backend.calls == ["alice"]

    def test_username_is_stored_like_repo_dir(self):
        backend = RecordingBackend(username="alice")
        assert backend.username == "alice"

    def test_without_a_username_it_raises_rather_than_guessing(self):
        backend = RecordingBackend()
        with pytest.raises(ReservationBackendError, match="no username was resolved"):
            _ = backend.reservations
        assert backend.calls == []


class TestOptionalCapabilitiesStayStructural:
    def test_base_alone_claims_no_capability(self):
        backend = _Complete()
        assert not isinstance(backend, SupportsUsernameCompletion)
        assert not isinstance(backend, SupportsResourceHolders)

    def test_base_does_not_provide_holders(self):
        """holders is a capability, not an inherited default (spec §4.5)."""
        assert not isinstance(RecordingBackend(username="alice"), SupportsResourceHolders)

    def test_adding_the_method_is_the_signal(self):
        class _WithUsers(_Complete):
            def list_usernames(self) -> list[str]:
                return sorted(self._holdings)

        assert isinstance(_WithUsers(), SupportsUsernameCompletion)
        assert not isinstance(_WithUsers(), SupportsResourceHolders)

    def test_adding_holders_is_the_signal(self):
        class _WithHolders(_Complete):
            def holders(self, resource: str) -> list[Reservation]:
                return [
                    Reservation(user=u, resource=resource)
                    for u, rs in self._holdings.items()
                    if resource in rs
                ]

        assert isinstance(_WithHolders(), SupportsResourceHolders)


class TestShippedBackendsInherit:
    """The built-ins and the docs' worked example use the base they recommend."""

    def test_null_backend(self):
        from otto.reservations import NullReservationBackend

        assert isinstance(NullReservationBackend(), ReservationBackendBase)

    def test_json_backend(self, tmp_path):
        from otto.reservations import JsonReservationBackend

        assert isinstance(JsonReservationBackend(path=tmp_path / "r.json"), ReservationBackendBase)

    def test_example_backend(self):
        from otto.examples.reservations import ExampleReservationBackend

        assert isinstance(ExampleReservationBackend(), ReservationBackendBase)
