"""Conformance helpers verified against otto's built-in backends + an error sample."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.labs import JsonFileLabRepository
from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
    ReservationWindow,
)
from otto.reservations.check import ReservationBackendError
from otto.testing import (
    assert_lab_repository_conforms,
    assert_reservation_backend_conforms,
)
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
            def load_lab(self, name, preferences=None):
                return "not a lab"  # wrong type

            def list_labs(self):
                return "not a list"  # wrong type

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(Broken())
        assert "LabRepository" in str(exc.value)

    def test_missing_list_labs_raises_assertion_not_attribute_error(self):
        """A repo with no list_labs at all must aggregate an AssertionError,
        not propagate an AttributeError before raise_if_failures()."""

        class NoListLabs:
            def load_lab(self, name, preferences=None):
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

            def load_lab(self, name, preferences=None):
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
    def test_null_builtin_conforms(self):
        assert_reservation_backend_conforms(NullReservationBackend())

    def test_json_builtin_conforms_with_round_trip(self, tmp_path):
        f = _reservations_file(tmp_path)
        backend = JsonReservationBackend(path=f)
        assert_reservation_backend_conforms(
            backend, known_user="alice", known_resources=["lab-a", "shared"]
        )

    def test_non_conforming_backend_raises(self):
        class Broken:
            def get_reserved_resources(self, username):
                return ["not", "a", "set"]  # wrong type

            def who_reserved(self, resource):
                return None  # wrong type — must be list

            def backend_name(self):
                return ""  # empty — invalid

        with pytest.raises(AssertionError) as exc:
            assert_reservation_backend_conforms(Broken())
        assert "ReservationBackend" in str(exc.value)

    def test_get_reserved_resources_returns_none_raises_assertion_not_type_error(self):
        """A backend whose get_reserved_resources returns None (not a set) must
        aggregate an AssertionError via raise_if_failures(), not crash the helper
        with a TypeError when the round-trip path does `r in None`."""

        class NoneReturner:
            def get_reserved_resources(self, username):
                return None  # non-set — triggers the round-trip guard

            def who_reserved(self, resource):
                # Returns a list with a holder so the round-trip path is entered.
                return ["alice"]

            def backend_name(self):
                return "none-returner"

        with pytest.raises(AssertionError) as exc:
            assert_reservation_backend_conforms(
                NoneReturner(),
                known_user="alice",
                known_resources=["lab-x"],
            )
        assert "ReservationBackend" in str(exc.value)


def _now() -> datetime:
    """The clock the window helpers read, AT CALL TIME.

    Issue #265: this used to be a module-level ``_NOW`` captured at import,
    and ``_window()``'s default span was ``_NOW +/- 1h`` — valid only while
    the test body ran within an hour of collection. The validator under test
    compares against a fresh ``datetime.now()``, so any session longer than
    that (unit-repeat during the 2026-08-25 outage: 64 min) reddened
    ``test_conforming_backend_passes`` on its later repeat. A seam rather
    than an inline ``datetime.now()`` so the guard below can move the clock.
    """
    return datetime.now(tz=timezone.utc)


class _WindowedBackend:
    """Minimal conforming backend that implements ``SupportsReservationWindows``.

    ``get_reserved_resources`` derives the flat view from the windows (so the
    two agree by construction) unless *flat* overrides it. The derivation skips
    malformed entries deliberately: a test that violates one window rule must
    surface as a *validator* failure, not as an AttributeError/TypeError raised
    inside the fake before the validator ever runs.
    """

    def __init__(self, windows, flat=None):
        self._windows = windows
        self._flat = flat

    def backend_name(self):
        return "windowed-fake"

    def get_reserved_resources(self, username):
        if self._flat is not None:
            return set(self._flat)
        now = _now()
        return {
            w.resource
            for w in self._windows
            if isinstance(w, ReservationWindow)
            and w.start.tzinfo is not None
            and w.end.tzinfo is not None
            and w.start <= now <= w.end
        }

    def who_reserved(self, resource):
        return ["alice"] if resource in self.get_reserved_resources("alice") else []

    def get_reservation_windows(self, username):
        return self._windows


def _window(**overrides):
    now = _now()
    base = {
        "resource": "r1",
        "start": now - timedelta(hours=1),
        "end": now + timedelta(hours=1),
    }
    base.update(overrides)
    return ReservationWindow(**base)


class TestTheWindowHelpersFollowTheClock:
    """Issue #265's shape, injected: the body runs long after the module was imported.

    The validator reads a fresh ``datetime.now()``; a helper anchored to an
    import-time snapshot drifts away from it as the session ages. Moving
    ``_now`` two hours ahead stands in for a two-hour-old import.
    """

    def test_a_default_window_brackets_the_moment_it_is_built(self, monkeypatch):
        # THIS module object, not a re-import: pytest's import mode can register
        # the collected file under another name, and a second copy would take
        # the patch while the helpers under test read the original.
        later = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(sys.modules[__name__], "_now", lambda: later)
        window = _window()
        assert window.start <= later <= window.end, (
            f"_window() is anchored to something other than the clock it is built under: "
            f"{window.start} .. {window.end} does not bracket {later}"
        )
        # The bracket alone is satisfied by ANY wide enough span, an import-time
        # anchor with a hundred-year width included; the span is the claim.
        assert window.end - window.start == timedelta(hours=2), (window.start, window.end)

    def test_the_fake_derives_its_flat_view_from_the_same_clock(self, monkeypatch):
        # THIS module object, not a re-import: pytest's import mode can register
        # the collected file under another name, and a second copy would take
        # the patch while the helpers under test read the original.
        later = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(sys.modules[__name__], "_now", lambda: later)
        assert _WindowedBackend([_window()]).get_reserved_resources("alice") == {"r1"}, (
            "the fake's flat view was derived from a different clock than its windows: "
            "a window built under `later` is not active under it"
        )

    def test_the_seam_reads_the_live_clock(self):
        # The two guards above patch `_now` wholesale, so they prove the helpers
        # CALL the seam and nothing about what it returns. A seam that memoises
        # (`_NOW = datetime.now(); def _now(): return _NOW`) reinstates issue
        # #265 exactly and leaves both of them green; this is the guard that
        # goes red for it. Bracketed, not equal: the seam must answer for the
        # instant it is asked, and only a live read lands between two others.
        before = datetime.now(tz=timezone.utc)
        sampled = _now()
        after = datetime.now(tz=timezone.utc)
        assert before <= sampled <= after, f"_now() answered {sampled}, outside {before}..{after}"


class TestReservationWindowsConformance:
    """One test per validator rule, each violating only that rule."""

    def test_conforming_backend_passes(self):
        assert_reservation_backend_conforms(
            _WindowedBackend([_window()]), known_user="alice", known_resources=["r1"]
        )

    def test_wrong_return_type_fails(self):
        backend = _WindowedBackend([_window()])
        backend.get_reservation_windows = lambda username: {"not": "a list"}
        with pytest.raises(AssertionError, match="must return a list"):
            assert_reservation_backend_conforms(backend)

    def test_non_window_entry_fails(self):
        backend = _WindowedBackend(["just-a-string"])
        with pytest.raises(AssertionError, match="ReservationWindow"):
            assert_reservation_backend_conforms(backend)

    def test_empty_resource_fails(self):
        with pytest.raises(AssertionError, match="non-empty str"):
            assert_reservation_backend_conforms(_WindowedBackend([_window(resource="")]))

    def test_naive_datetime_fails(self):
        naive = _window(start=datetime(2026, 1, 1), end=datetime(2026, 1, 2))  # noqa: DTZ001
        with pytest.raises(AssertionError, match="timezone-aware"):
            assert_reservation_backend_conforms(_WindowedBackend([naive]))

    def test_inverted_range_fails(self):
        now = _now()
        inverted = _window(start=now + timedelta(hours=2), end=now)
        with pytest.raises(AssertionError, match="start <= end"):
            assert_reservation_backend_conforms(_WindowedBackend([inverted]))

    def test_flat_view_disagreement_fails(self):
        backend = _WindowedBackend([_window()], flat={"something-else"})
        with pytest.raises(AssertionError, match="get_reserved_resources"):
            assert_reservation_backend_conforms(
                backend, known_user="alice", known_resources=["something-else"]
            )

    def test_windowless_backend_skips_section(self):
        class _Flat:
            def backend_name(self):
                return "flat-fake"

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

        assert_reservation_backend_conforms(_Flat())


class TestReservationErrorContract:
    """The error-contract rule (§4.3) is exercised by a purpose-built failing
    sample, not the generic helper (which cannot force a healthy backend to fail).
    """

    def test_failure_modes_raise_reservation_backend_error(self):
        class FailingBackend:
            def get_reserved_resources(self, username):
                raise ReservationBackendError("scheduler unreachable")

            def who_reserved(self, resource):
                raise ReservationBackendError("scheduler unreachable")

            def backend_name(self):
                return "failing"

        backend = FailingBackend()
        with pytest.raises(ReservationBackendError):
            backend.get_reserved_resources("anyone")
        with pytest.raises(ReservationBackendError):
            backend.who_reserved("anything")
