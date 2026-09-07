"""Conformance helpers verified against otto's built-in backends + an error sample."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

from otto.labs import (
    HostSummary,
    JsonFileLabRepository,
    LabNotFoundError,
    SupportsHostSummaries,
)
from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
    Reservation,
    ReservationBackendBase,
    SupportsResourceHolders,
)
from otto.reservations.check import ReservationBackendError
from otto.testing import (
    assert_lab_repository_conforms,
    assert_reservation_backend_conforms,
)
from otto.testing.conformance import _CLOCK_SKEW
from tests._fixtures.labdata import write_lab_json


def _hosts_file(path: Path) -> None:
    write_lab_json(
        path / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "a",
                "creds": [{"login": "u", "password": "p"}],
                "resources": ["a"],
                "labs": ["alpha"],
            },
            {
                "ip": "10.0.0.2",
                "element": "b",
                "creds": [{"login": "u", "password": "p"}],
                "resources": ["b"],
                "labs": ["beta"],
            },
        ],
    )


def _reservations_file(path: Path) -> Path:
    f = path / "reservations.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["lab-a", "shared"]},
                    {"user": "bob", "resources": ["lab-b", "shared"]},
                ],
            }
        )
    )
    return f


class TestLabRepositoryConformance:
    def test_json_builtin_conforms(self, tmp_path):
        _hosts_file(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        # Must not raise.
        assert_lab_repository_conforms(repo, expected_labs=["alpha", "beta"])

    def test_non_conforming_repo_raises_with_aggregate(self):
        class Broken:
            def load_lab(self, name, preferences=None, inventory=None):
                return "not a lab"  # wrong type

            def list_labs(self):
                return "not a list"  # wrong type

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(Broken())
        assert "LabRepository" in str(exc.value)

    def test_load_lab_without_an_inventory_keyword_is_a_violation(self):
        """Spec §6: every backend takes ``inventory=``, checked on the SIGNATURE.

        A backend whose own data holds no referenced entry satisfies every
        BEHAVIOURAL rule here while silently dropping the argument — until
        someone points it at an inventory. So the rule reads the signature,
        and this backend is otherwise perfectly conforming: the aggregate
        must name the missing keyword and nothing else.
        """
        from otto.config.lab import Lab

        class NoInventoryKeyword:
            def load_lab(self, name, preferences=None):
                if name != "mylab":
                    raise LabNotFoundError(name)
                return Lab(name=name)

            def list_labs(self):
                return ["mylab"]

        with pytest.raises(AssertionError, match=r"must accept an inventory= keyword"):
            assert_lab_repository_conforms(NoInventoryKeyword())

    def test_host_summaries_without_an_inventory_keyword_is_a_violation(self):
        """The optional capability owes the same keyword, and the asserter must
        call it the way production does.

        ``otto.labs.list_host_summaries`` calls ``list_host_summaries(inventory=…)``
        by keyword; a backend spelling the method with no parameters passes the
        behavioural rules (this one summarizes nothing and builds nothing, so
        every set agrees) and TypeErrors in the shell. The asserter itself used
        to call it with no arguments, which is why this backend used to pass.
        """
        from otto.config.lab import Lab

        class NoKeywordOnSummaries:
            def load_lab(self, name, preferences=None, inventory=None):
                if name != "mylab":
                    raise LabNotFoundError(name)
                return Lab(name=name)

            def list_labs(self):
                return ["mylab"]

            def list_host_summaries(self):
                return []

        with pytest.raises(
            AssertionError, match=r"list_host_summaries must accept an inventory= keyword"
        ):
            assert_lab_repository_conforms(NoKeywordOnSummaries())

    def test_a_conforming_host_summaries_backend_is_called_by_keyword(self):
        """The asserter's own call shape: keyword ``inventory=``, never positional
        — pinned so the check cannot drift back to the shape it certified wrongly."""
        from otto.config.lab import Lab

        calls: list = []

        class Recording:
            def load_lab(self, name, preferences=None, inventory=None):
                if name != "mylab":
                    raise LabNotFoundError(name)
                return Lab(name=name)

            def list_labs(self):
                return ["mylab"]

            # Keyword-only AND required: a positional call from the asserter
            # TypeErrors here, and so does a no-argument one (the shape this
            # asserter used to have) — a green run proves the keyword shape.
            def list_host_summaries(self, *, inventory):
                calls.append(inventory)
                return []

        assert_lab_repository_conforms(Recording())
        assert calls == [None]

    def test_missing_list_labs_raises_assertion_not_attribute_error(self):
        """A repo with no list_labs at all must aggregate an AssertionError,
        not propagate an AttributeError before raise_if_failures()."""

        class NoListLabs:
            def load_lab(self, name, preferences=None, inventory=None):
                raise KeyError(name)

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(NoListLabs())
        assert "LabRepository" in str(exc.value)

    def test_load_lab_raises_on_idempotency_recall_records_not_crashes(self):
        """A backend whose load_lab raises on the second call (idempotency re-call)
        must produce an aggregated AssertionError, not propagate the raw exception."""
        from otto.config.lab import Lab

        class RaisesOnSecondCall:
            def __init__(self):
                self._call_count = 0

            def load_lab(self, name, preferences=None, inventory=None):
                self._call_count += 1
                if self._call_count == 1:
                    return Lab(name=name)
                raise RuntimeError("second load_lab call exploded")

            def list_labs(self):
                return ["mylab"]

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(RaisesOnSecondCall())
        assert "LabRepository" in str(exc.value)


class TestReservationBackendConformance:
    def test_null_builtin_conforms(self, caplog):
        # The null backend holds nothing, so the overlap rule has no row a
        # narrow window could drop; the helper must SAY it skipped rather than
        # count a vacuous pass.
        with caplog.at_level(logging.WARNING, logger="otto.testing.conformance"):
            assert_reservation_backend_conforms(NullReservationBackend(username="alice"))
        assert "skipping the overlap rule" in caplog.text

    def test_json_builtin_conforms_with_round_trip(self, tmp_path):
        f = _reservations_file(tmp_path)
        backend = JsonReservationBackend(path=f, username="alice")
        assert_reservation_backend_conforms(
            backend, known_user="alice", known_resources=["lab-a", "shared"]
        )

    def test_a_backend_with_no_identity_skips_the_reservations_rules(self, tmp_path, caplog):
        """``config.completion_cache`` builds exactly this: no username, only list_usernames.

        ``reservations`` raises ``ReservationBackendError`` by design there. The
        helper must record it as a skipped rule, never let it escape.
        """
        backend = JsonReservationBackend(path=_reservations_file(tmp_path))
        with caplog.at_level(logging.WARNING, logger="otto.testing.conformance"):
            assert_reservation_backend_conforms(backend)
        assert "skipping the `reservations` rules" in caplog.text

    def test_a_backend_that_raises_despite_having_an_identity_is_a_failure(self):
        """The no-identity escape hatch must not swallow a real backend outage."""

        class Raises:
            username = "alice"

            @property
            def reservations(self):
                raise ReservationBackendError("scheduler unreachable")

            def fetch_reservations(self, username, start=None, end=None):
                return [Reservation(user=username, resource="rack3")]

            def backend_name(self):
                return "raises"

        with pytest.raises(AssertionError, match="must not raise for a backend whose username is"):
            assert_reservation_backend_conforms(Raises())

    def test_missing_reservations_member_is_reported(self):
        """A structural backend that never provides ``reservations`` fails."""

        class NoMember:
            def fetch_reservations(self, username, start=None, end=None):
                return [Reservation(user=username, resource="rack3")]

            def backend_name(self):
                return "no-member"

        with pytest.raises(AssertionError, match="must expose a `reservations` member"):
            assert_reservation_backend_conforms(NoMember())

    def test_every_violation_is_reported_in_one_aggregate_error(self):
        """The ExpectCollector contract: three defects, one raise, all three named.

        Anchored on all three phrases rather than on the substring
        ``"ReservationBackend"`` — which every message in this module contains,
        so a fixture defect anywhere would have satisfied it. The claim here is
        aggregation: a backend author sees every problem at once.
        """

        class Broken:
            def fetch_reservations(self, username, start=None, end=None):
                return {"not": "a list"}  # wrong type

            def backend_name(self):
                return ""  # empty — invalid

        with pytest.raises(AssertionError) as exc:
            assert_reservation_backend_conforms(Broken())
        msg = str(exc.value)
        for phrase in (
            "backend_name() must return a non-empty str",
            "must expose a `reservations` member",
            "must return a list of Reservation",
        ):
            assert phrase in msg, f"{phrase!r} missing from the aggregate:\n{msg}"

    def test_a_non_list_return_aggregates_rather_than_crashing_the_helper(self):
        """A wrong-typed return must land as an AssertionError, not a TypeError.

        The row and overlap rules iterate what the backend hands back; a
        non-list has to be reported and stepped over, or the helper dies inside
        the very rule that exists to name the defect.
        """

        class NoneReturner:
            username = "alice"
            reservations = None  # non-list — must be reported, not iterated

            def fetch_reservations(self, username, start=None, end=None):
                return None

            def backend_name(self):
                return "none-returner"

        with pytest.raises(AssertionError, match="must return a list of Reservation"):
            assert_reservation_backend_conforms(
                NoneReturner(), known_user="alice", known_resources=["lab-x"]
            )


def _base(**over):
    """A minimal conforming backend; *over* replaces one method to violate one rule."""
    ns = {
        "fetch_reservations": lambda self, username, start=None, end=None: [
            Reservation(user=username, resource="rack3", end=None)
        ],
        "backend_name": lambda self: "double",
        # Answers for the ONE resource the double's fetch reports. A holders()
        # that claimed every resource asked about would make each double below
        # violate the holder rules as well as its own, and a test that goes red
        # for a rule it does not name proves nothing about the rule it does.
        "holders": lambda self, resource: (
            [Reservation(user="alice", resource=resource)] if resource == "rack3" else []
        ),
    }
    ns.update(over)
    return type("Double", (ReservationBackendBase,), ns)(username="alice")


class TestOneHostileDoublePerRule:
    """One violating double per rule — a conforming backend proves nothing.

    What each test guarantees is one *anchored* expectation, not one tripped
    rule: the helper reports every violation it finds, and a double built to
    break one rule usually trips two to six of them in passing (an empty
    ``resource`` also breaks the holder agreement for ``''``; an empty ``user``
    also breaks "rows belong to the queried user"). Each ``match=`` is
    therefore anchored on the distinctive phrase of the rule its test names, so
    deleting that rule alone reddens that test alone — which is what the
    mutation runs in the task report demonstrate. ``ExpectCollector`` dumps
    caller locals into its failure text, so a loose ``match=`` would pass off
    an unrelated rule's message.
    """

    def test_the_base_double_conforms(self):
        """The control: every hostile double below differs from this by ONE method."""
        assert_reservation_backend_conforms(_base(), known_user="alice", known_resources=["rack3"])

    def test_rejects_a_backend_missing_fetch_reservations(self):
        """Two rules at once, anchored separately — see the callable test below."""

        class NoFetch:
            reservations: ClassVar[list] = []

            def backend_name(self):
                return "no-fetch"

        with pytest.raises(
            AssertionError, match="must satisfy the runtime_checkable ReservationBackend protocol"
        ):
            assert_reservation_backend_conforms(NoFetch())

    def test_rejects_a_backend_whose_fetch_reservations_is_not_callable(self):
        class NotCallable:
            reservations: ClassVar[list] = []
            fetch_reservations = "not a method"

            def backend_name(self):
                return "not-callable"

        with pytest.raises(AssertionError, match="fetch_reservations must be callable"):
            assert_reservation_backend_conforms(NotCallable())

    def test_rejects_a_backend_missing_backend_name(self):
        class NoName:
            username = "alice"
            reservations: ClassVar[list] = []

            def fetch_reservations(self, username, start=None, end=None):
                return []

        with pytest.raises(AssertionError, match="backend_name must be callable"):
            assert_reservation_backend_conforms(NoName())

    def test_rejects_an_unstable_backend_name(self):
        """A per-call identifier breaks log-history searches and diagnostics."""
        with pytest.raises(AssertionError, match="backend_name\\(\\) must be stable across calls"):
            assert_reservation_backend_conforms(_base(backend_name=lambda self: f"sched-{uuid4()}"))

    def test_rejects_non_reservation_rows(self):
        with pytest.raises(AssertionError, match="entries must be Reservation"):
            assert_reservation_backend_conforms(
                _base(fetch_reservations=lambda self, username, start=None, end=None: ["rack3"])
            )

    def test_rejects_naive_datetimes(self):
        with pytest.raises(AssertionError, match="start/end must be timezone-aware"):
            assert_reservation_backend_conforms(
                _base(
                    fetch_reservations=lambda self, username, start=None, end=None: [
                        Reservation(
                            user=username,
                            resource="rack3",
                            end=datetime(2026, 9, 7, 15, 0),  # noqa: DTZ001 — naive on purpose
                        )
                    ]
                )
            )

    def test_rejects_start_after_end(self):
        # Relative to the real clock, not a literal: a fixed past instant makes
        # this row lapsed as well as inverted, so the double would trip the
        # lapsed-row rule too and break this file's one-hostile-double-per-rule
        # protocol. It would still pass on the substring match=, for the wrong
        # reason.
        now = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        with pytest.raises(AssertionError, match="start <= end required"):
            assert_reservation_backend_conforms(
                _base(
                    fetch_reservations=lambda self, username, start=None, end=None: [
                        Reservation(
                            user=username,
                            resource="rack3",
                            start=now,
                            end=now - timedelta(hours=1),
                        )
                    ]
                )
            )

    def test_rejects_rows_belonging_to_another_user(self):
        with pytest.raises(AssertionError, match="must return only rows for 'alice'"):
            assert_reservation_backend_conforms(
                _base(
                    fetch_reservations=lambda self, username, start=None, end=None: [
                        Reservation(user="somebody-else", resource="rack3")
                    ]
                )
            )

    def test_rejects_empty_resource_string(self):
        with pytest.raises(AssertionError, match="resource must be a non-empty str"):
            assert_reservation_backend_conforms(
                _base(
                    fetch_reservations=lambda self, username, start=None, end=None: [
                        Reservation(user=username, resource="")
                    ]
                )
            )

    def test_rejects_empty_user_string(self):
        with pytest.raises(AssertionError, match="user must be a non-empty str"):
            assert_reservation_backend_conforms(
                _base(
                    fetch_reservations=lambda self, username, start=None, end=None: [
                        Reservation(user="", resource="rack3")
                    ]
                )
            )

    def test_rejects_a_backend_that_drops_a_straddling_booking(self):
        """THE fail-open rule: containment semantics must be caught here."""

        def containment(self, username, start=None, end=None):
            now = datetime.now(tz=timezone.utc)
            row = Reservation(
                user=username,
                resource="rack3",
                start=now - timedelta(days=1),
                end=now + timedelta(days=1),
            )
            if start is None or end is None:
                return [row]
            # Wrong: only returns bookings fully inside the window.
            return [row] if start <= row.start and row.end <= end else []

        with pytest.raises(AssertionError, match="must match bookings overlapping"):
            assert_reservation_backend_conforms(_base(fetch_reservations=containment))

    def test_rejects_a_backend_that_returns_a_lapsed_booking(self):
        """The OTHER fail-open direction: over-returning an already-ended row.

        A backend reading a query's ``start=None`` as "-infinity" rather than
        "this instant" hands back bookings that have already ended.  The
        overlap rule computes ``wide - narrow`` and so cannot see it, and
        otto's check gate never re-filters by ``end`` — it would admit a user whose
        booking is over.
        """

        def over_returns(self, username, start=None, end=None):
            now = datetime.now(tz=timezone.utc)
            return [
                Reservation(
                    user=username,
                    resource="rack3",
                    start=now - timedelta(days=2),
                    end=now - timedelta(days=1),
                )
            ]

        with pytest.raises(AssertionError, match="must return only bookings still active"):
            assert_reservation_backend_conforms(_base(fetch_reservations=over_returns))

    def test_a_booking_that_ended_moments_ago_is_forgiven_as_clock_skew(self):
        """The backend's clock is not this helper's; a microsecond race is not a defect.

        Bracketed by the same one second the overlap rule brackets ``now`` with
        — a row still legitimately returned for that window must not be failed
        as lapsed by this one.
        """

        def just_expired(self, username, start=None, end=None):
            now = datetime.now(tz=timezone.utc)
            return [
                Reservation(
                    user=username,
                    resource="rack3",
                    start=now - timedelta(days=1),
                    end=now - timedelta(milliseconds=10),
                )
            ]

        assert_reservation_backend_conforms(_base(fetch_reservations=just_expired))

    def test_the_skew_tolerance_is_not_eaten_by_the_round_trip(self):
        """The clock must be read BEFORE the fetch, not when the answer lands.

        A slow scheduler is not a broken one. If the helper read ``now`` after
        the call returned, transport latency would be spent out of
        ``_CLOCK_SKEW`` rather than covered by it: an 800 ms backend holding a
        booking that ends shortly after it answers reports an ``end`` more than
        a second before the helper's clock, and a CORRECT backend fails
        intermittently — the worst failure mode a conformance helper can have.

        Pinning the ordering needs real elapsed time, because the ordering IS
        elapsed time: nothing else distinguishes the two readings. So this
        double sleeps longer than ``_CLOCK_SKEW`` inside the unbounded call and
        expires its row at the instant it was entered — legitimately active
        when asked, lapsed by the time a post-fetch clock read would happen.
        Under the old ordering the cutoff lands ~0.1 s AFTER that row's ``end``
        and the helper fails it; under the correct ordering the cutoff precedes
        the call entirely, so no amount of delay can trip it. The asymmetry is
        deliberate — extra slowness only strengthens the red, and cannot
        weaken the green.
        """
        delay = _CLOCK_SKEW.total_seconds() + 0.1

        def slow(self, username, start=None, end=None):
            entered = datetime.now(tz=timezone.utc)
            if start is None and end is None:
                # Only the unbounded call pays the latency; the overlap rule's
                # narrow re-query would otherwise double this test's cost.
                time.sleep(delay)
            return [Reservation(user=username, resource="rack3", end=entered)]

        assert_reservation_backend_conforms(_base(fetch_reservations=slow))

    def test_an_open_ended_booking_is_never_lapsed(self):
        """``end=None`` is open-ended, not a missing value to treat as expired."""
        assert_reservation_backend_conforms(
            _base(
                fetch_reservations=lambda self, username, start=None, end=None: [
                    Reservation(user=username, resource="rack3", end=None)
                ]
            )
        )

    def test_rejects_holders_that_omit_a_known_holder(self):
        with pytest.raises(AssertionError, match="who holds it per fetch_reservations"):
            assert_reservation_backend_conforms(
                _base(holders=lambda self, resource: []),
                known_user="alice",
                known_resources=["rack3"],
            )

    def test_rejects_a_fetch_that_drops_a_resource_the_caller_knows_is_held(self):
        """The caller's ground truth — the one claim the backend cannot self-certify."""
        with pytest.raises(AssertionError, match="must include known-held resource 'rack9'"):
            assert_reservation_backend_conforms(
                _base(), known_user="alice", known_resources=["rack9"]
            )

    def test_rejects_holders_naming_someone_fetch_disagrees_with(self):
        """The other direction: a holder row nobody's own query corroborates."""
        with pytest.raises(AssertionError, match="does not return 'rack3'"):
            assert_reservation_backend_conforms(
                _base(
                    holders=lambda self, resource: [
                        Reservation(user="alice", resource=resource),
                        Reservation(user="ghost", resource=resource),
                    ],
                    fetch_reservations=lambda self, username, start=None, end=None: (
                        [Reservation(user=username, resource="rack3")]
                        if username == "alice"
                        else []
                    ),
                )
            )

    def test_rejects_a_non_list_from_holders(self):
        with pytest.raises(AssertionError, match=r"holders\('rack3'\) must return a list"):
            assert_reservation_backend_conforms(_base(holders=lambda self, resource: None))

    def test_backend_without_holders_conforms(self):
        """A per-user-only backend is skipped for the holder rules, not failed."""

        class PerUserOnly(ReservationBackendBase):
            def fetch_reservations(self, username, start=None, end=None):
                return [Reservation(user=username, resource="rack3")]

            def backend_name(self):
                return "per-user-only"

        backend = PerUserOnly(username="alice")
        assert not isinstance(backend, SupportsResourceHolders)
        assert_reservation_backend_conforms(backend)  # must not raise

    def test_a_preseeded_reservations_cache_is_accepted(self):
        """Pre-seeding is documented as available, so the helper must not fail it.

        An earlier draft asserted ``"reservations" not in vars(backend)`` to
        prove the base did not fetch during ``__init__``. The helper only ever
        sees an already-constructed instance, where a deliberate pre-seed is
        indistinguishable from an eager base — so that rule is otto's own,
        tested against ``ReservationBackendBase`` in tests/unit/reservations.
        """

        class Eager(ReservationBackendBase):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.reservations = self.fetch_reservations(self.username)

            def fetch_reservations(self, username, start=None, end=None):
                return [Reservation(user=username, resource="rack3")]

            def backend_name(self):
                return "eager"

        assert_reservation_backend_conforms(Eager(username="alice"))


class TestReservationErrorContract:
    """The error-contract rule (§4.3) is exercised by a purpose-built failing
    sample, not the generic helper (which cannot force a healthy backend to fail).
    """

    def test_failure_modes_raise_reservation_backend_error(self):
        class FailingBackend(ReservationBackendBase):
            def fetch_reservations(self, username, start=None, end=None):
                raise ReservationBackendError("scheduler unreachable")

            def holders(self, resource):
                raise ReservationBackendError("scheduler unreachable")

            def backend_name(self):
                return "failing"

        backend = FailingBackend(username="alice")
        with pytest.raises(ReservationBackendError):
            backend.fetch_reservations("anyone")
        with pytest.raises(ReservationBackendError):
            backend.holders("anything")
        with pytest.raises(ReservationBackendError):
            _ = backend.reservations


def _per_user_only():
    """A conforming reservation backend that deliberately omits ``holders``."""

    class PerUserOnly(ReservationBackendBase):
        def fetch_reservations(self, username, start=None, end=None):
            return [Reservation(user=username, resource="rack3")]

        def backend_name(self):
            return "per-user-only"

    return PerUserOnly(username="alice")


def _lab_repo(*, summaries=True, ghost_id=False):
    """A conforming repository, optionally without ``list_host_summaries``.

    *ghost_id* makes the capability itself violate the "every id you offer must
    be one ``load_lab`` produces" rule, so the "present but not expected"
    direction is proved by a double that FAILS rather than one that passes.
    """
    from otto.config.lab import Lab

    class Repo:
        def load_lab(self, name, preferences=None, inventory=None):
            if name != "mylab":
                raise LabNotFoundError(name)
            return Lab(name=name)

        def list_labs(self):
            return ["mylab"]

    if summaries:

        def list_host_summaries(self, *, inventory=None):
            return [HostSummary(id="ghost")] if ghost_id else []

        Repo.list_host_summaries = list_host_summaries

    return Repo()


class TestExpectedCapabilities:
    """``expect_holders`` / ``expect_host_summaries``: assert a capability SET.

    Absence of either optional capability is legal (reservation spec §7 rule 6
    for ``holders``; the same shape for ``SupportsHostSummaries``), so the
    helpers skip those rules by default. That default is a trap for an author
    whose backend IS meant to have the capability: implement it, ship, refactor
    it away, and conformance stays green while users lose the surface it feeds.
    The kwargs let such an author opt into strictness. Both directions are
    proved with a VIOLATING double, and each ``match=`` is anchored on the
    kwarg's own phrase — ``ExpectCollector`` dumps caller locals into its
    failure text, so a loose match would pass for the wrong reason.
    """

    def test_holders_present_and_expected_passes(self):
        assert_reservation_backend_conforms(
            _base(), known_user="alice", known_resources=["rack3"], expect_holders=True
        )

    def test_holders_absent_and_expected_is_a_conformance_failure(self):
        backend = _per_user_only()
        assert not isinstance(backend, SupportsResourceHolders)
        with pytest.raises(
            AssertionError,
            match=r"expect_holders=True, but the backend does not implement holders\(\)",
        ):
            assert_reservation_backend_conforms(backend, expect_holders=True)

    def test_holders_absent_and_not_expected_still_skips(self):
        """The default must be byte-for-byte today's behaviour."""
        assert_reservation_backend_conforms(_per_user_only())  # must not raise

    def test_holders_present_and_not_expected_still_runs_the_rules(self):
        """The kwarg gates only the ABSENCE branch; a present-but-broken
        ``holders`` fails under the default exactly as it always did."""
        with pytest.raises(AssertionError, match=r"holders\('rack3'\) must return a list"):
            assert_reservation_backend_conforms(_base(holders=lambda self, resource: None))

    def test_host_summaries_present_and_expected_passes(self):
        assert_lab_repository_conforms(
            _lab_repo(), expected_labs=["mylab"], expect_host_summaries=True
        )

    def test_host_summaries_absent_and_expected_is_a_conformance_failure(self):
        repo = _lab_repo(summaries=False)
        assert not isinstance(repo, SupportsHostSummaries)
        with pytest.raises(
            AssertionError,
            match=(
                r"expect_host_summaries=True, but the repository does not implement "
                r"list_host_summaries\(\)"
            ),
        ):
            assert_lab_repository_conforms(repo, expect_host_summaries=True)

    def test_host_summaries_absent_and_not_expected_still_skips(self):
        """The default must be byte-for-byte today's behaviour."""
        assert_lab_repository_conforms(_lab_repo(summaries=False))  # must not raise

    def test_host_summaries_present_and_not_expected_still_runs_the_rules(self):
        with pytest.raises(AssertionError, match=r"but no load_lab\(\) produces them"):
            assert_lab_repository_conforms(_lab_repo(ghost_id=True))
