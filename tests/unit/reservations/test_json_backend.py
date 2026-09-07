"""Unit tests for the JSON reservation backend."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.reservations import (
    JsonReservationBackend,
    ReservationBackendError,
    SupportsResourceHolders,
)


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _make_backend(tmp_path: Path, data: dict) -> JsonReservationBackend:
    f = _write(tmp_path / "reservations.json", data)
    return JsonReservationBackend(path=f)


def _held(backend: JsonReservationBackend, username: str) -> set[str]:
    return {r.resource for r in backend.fetch_reservations(username)}


class TestFetchReservations:
    def test_single_user(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["rack3-psu", "smartbits-07"]},
                ],
            },
        )
        assert _held(backend, "alice") == {"rack3-psu", "smartbits-07"}

    def test_user_with_no_reservations_returns_empty(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["rack3-psu"]},
                ],
            },
        )
        assert backend.fetch_reservations("bob") == []

    def test_multiple_entries_for_same_user_union(self, tmp_path):
        """A user appearing in multiple records gets every resource, one row each."""
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["rack3-psu"]},
                    {"user": "alice", "resources": ["smartbits-07"]},
                ],
            },
        )
        assert _held(backend, "alice") == {"rack3-psu", "smartbits-07"}

    def test_one_row_per_resource(self, tmp_path):
        """A booking covering two racks yields two Reservation objects."""
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [{"user": "alice", "resources": ["r1", "r2"]}],
            },
        )
        rows = backend.fetch_reservations("alice")
        assert len(rows) == 2
        assert sorted(r.resource for r in rows) == ["r1", "r2"]
        assert {r.user for r in rows} == {"alice"}

    def test_expired_entry_ignored(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {
                        "user": "alice",
                        "resources": ["rack3-psu"],
                        "expires": "2000-01-01T00:00:00Z",
                    },
                ],
            },
        )
        assert _held(backend, "alice") == set()

    def test_future_expires_kept(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {
                        "user": "alice",
                        "resources": ["rack3-psu"],
                        "expires": "3000-01-01T00:00:00Z",
                    },
                ],
            },
        )
        rows = backend.fetch_reservations("alice")
        assert [r.resource for r in rows] == ["rack3-psu"]
        assert rows[0].end == datetime(3000, 1, 1, tzinfo=timezone.utc)


class TestHolders:
    def test_declares_the_capability(self, tmp_path):
        backend = _make_backend(tmp_path, {"version": 1, "reservations": []})
        assert isinstance(backend, SupportsResourceHolders)

    def test_resource_held_returns_single_holder(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["rack3-psu"]},
                    {"user": "bob", "resources": ["rack4-psu"]},
                ],
            },
        )
        assert sorted(h.user for h in backend.holders("rack3-psu")) == ["alice"]
        assert sorted(h.user for h in backend.holders("rack4-psu")) == ["bob"]

    def test_unreserved_returns_empty_list(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [],
            },
        )
        assert backend.holders("rack3-psu") == []

    def test_multiple_holders_aggregated(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["shared-lab"]},
                    {"user": "bob", "resources": ["shared-lab"]},
                ],
            },
        )
        assert sorted(h.user for h in backend.holders("shared-lab")) == ["alice", "bob"]

    def test_expired_holder_omitted(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {
                        "user": "alice",
                        "resources": ["shared-lab"],
                        "expires": "2000-01-01T00:00:00Z",
                    },
                ],
            },
        )
        assert backend.holders("shared-lab") == []

    def test_holder_rows_carry_the_expiry(self, tmp_path):
        future = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["shared-lab"], "expires": future.isoformat()},
                ],
            },
        )
        (row,) = backend.holders("shared-lab")
        assert row.end == future
        assert row.start is None


class TestBackendName:
    def test_stable(self, tmp_path):
        backend = _make_backend(tmp_path, {"version": 1, "reservations": []})
        assert backend.backend_name() == "json"


class TestUrlParameter:
    def test_accepted_and_ignored(self, tmp_path):
        """JSON backend accepts url=... for factory uniformity but ignores it."""
        f = _write(tmp_path / "r.json", {"version": 1, "reservations": []})
        backend = JsonReservationBackend(url="https://ignored.example", path=f)
        # No error — backend still functions normally
        assert backend.fetch_reservations("alice") == []


class TestUsernameParameter:
    def test_forwarded_to_the_base_class(self, tmp_path):
        f = _write(tmp_path / "r.json", {"version": 1, "reservations": []})
        backend = JsonReservationBackend(path=f, username="alice")
        assert backend.username == "alice"

    def test_reservations_property_queries_for_that_user(self, tmp_path):
        f = _write(
            tmp_path / "r.json",
            {
                "version": 1,
                "reservations": [
                    {"user": "alice", "resources": ["rack3-psu"]},
                    {"user": "bob", "resources": ["rack4-psu"]},
                ],
            },
        )
        backend = JsonReservationBackend(path=f, username="alice")
        assert [r.resource for r in backend.reservations] == ["rack3-psu"]


class TestErrors:
    def test_missing_file_raises_backend_error(self, tmp_path):
        backend = JsonReservationBackend(path=tmp_path / "does-not-exist.json")
        with pytest.raises(ReservationBackendError, match="Failed to read"):
            backend.fetch_reservations("alice")

    def test_malformed_json_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json")
        backend = JsonReservationBackend(path=f)
        with pytest.raises(ReservationBackendError, match="Malformed JSON"):
            backend.fetch_reservations("alice")

    def test_wrong_top_level_type_raises(self, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        backend = JsonReservationBackend(path=f)
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.fetch_reservations("alice")

    def test_unsupported_version_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {"version": 99, "reservations": []})
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.fetch_reservations("alice")

    def test_reservations_not_list_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {"version": 1, "reservations": "nope"})
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.fetch_reservations("alice")

    def test_entry_missing_user_raises(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [{"resources": ["x"]}],
            },
        )
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.fetch_reservations("alice")

    def test_resources_not_string_list_raises(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [{"user": "a", "resources": [1, 2]}],
            },
        )
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.fetch_reservations("alice")

    def test_bad_expires_raises(self, tmp_path):
        backend = _make_backend(
            tmp_path,
            {
                "version": 1,
                "reservations": [{"user": "a", "resources": ["x"], "expires": "not-a-date"}],
            },
        )
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.fetch_reservations("alice")

    def test_holders_propagates_the_backend_error(self, tmp_path):
        backend = JsonReservationBackend(path=tmp_path / "does-not-exist.json")
        with pytest.raises(ReservationBackendError, match="Failed to read"):
            backend.holders("rack3-psu")


def test_a_booking_ending_before_the_requested_start_is_excluded(tmp_path):
    """The CALLER's lower bound is honored, not just "now".

    Without this the window argument is decorative: a backend that ignored
    ``start`` and always measured from ``datetime.now()`` would return this
    row, because it is still live at this instant.
    """
    now = datetime.now(timezone.utc)
    path = tmp_path / "res.json"
    path.write_text(
        '{"version": 1, "reservations": [{"user": "alice", "resources": ["rack3"], '
        f'"expires": "{(now + timedelta(hours=1)).isoformat()}"}}]}}'
    )
    backend = JsonReservationBackend(path=path)
    # Live at this instant, so the unbounded call returns it...
    assert [r.resource for r in backend.fetch_reservations("alice")] == ["rack3"]
    # ...and a window opening after it ends must not.
    assert backend.fetch_reservations("alice", start=now + timedelta(hours=2)) == []


def test_a_booking_ending_exactly_at_the_requested_start_is_excluded(tmp_path):
    """Pins the ``<=`` boundary: a window that closes as ours opens is over."""
    edge = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    path = tmp_path / "res.json"
    path.write_text(
        '{"version": 1, "reservations": [{"user": "alice", "resources": ["rack3"], '
        f'"expires": "{edge.isoformat()}"}}]}}'
    )
    backend = JsonReservationBackend(path=path)
    assert backend.fetch_reservations("alice", start=edge) == []
    # One microsecond earlier the booking is still live — the exclusion is the
    # boundary itself, not a blanket refusal of the whole entry.
    rows = backend.fetch_reservations("alice", start=edge - timedelta(microseconds=1))
    assert [r.resource for r in rows] == ["rack3"]


def test_missing_expires_yields_open_ended_end(tmp_path):
    path = tmp_path / "res.json"
    path.write_text('{"version": 1, "reservations": [{"user": "alice", "resources": ["rack3"]}]}')
    backend = JsonReservationBackend(path=path)
    (row,) = backend.fetch_reservations("alice")
    assert row.end is None
    assert row.start is None


def test_expired_entries_are_omitted(tmp_path):
    path = tmp_path / "res.json"
    path.write_text(
        '{"version": 1, "reservations": ['
        '{"user": "alice", "resources": ["rack3"], "expires": "2000-01-01T00:00:00Z"}]}'
    )
    backend = JsonReservationBackend(path=path)
    assert backend.fetch_reservations("alice") == []


def test_booking_straddling_both_window_edges_is_returned(tmp_path):
    """THE fail-open case: containment semantics would drop this row."""
    now = datetime.now(timezone.utc)
    path = tmp_path / "res.json"
    path.write_text(
        '{"version": 1, "reservations": [{"user": "alice", "resources": ["rack3"], '
        f'"expires": "{(now + timedelta(days=1)).isoformat()}"}}]}}'
    )
    backend = JsonReservationBackend(path=path)
    rows = backend.fetch_reservations(
        "alice", start=now - timedelta(minutes=1), end=now + timedelta(minutes=1)
    )
    assert [r.resource for r in rows] == ["rack3"]
