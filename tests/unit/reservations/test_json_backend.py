"""Unit tests for the JSON reservation backend."""

import json
from pathlib import Path

import pytest

from otto.reservations import (
    JsonReservationBackend,
    ReservationBackendError,
)


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _make_backend(tmp_path: Path, data: dict) -> JsonReservationBackend:
    f = _write(tmp_path / "reservations.json", data)
    return JsonReservationBackend(path=f)


class TestGetReservedResources:

    def test_single_user(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu", "smartbits-07"]},
            ],
        })
        assert backend.get_reserved_resources("alice") == {"rack3-psu", "smartbits-07"}

    def test_user_with_no_reservations_returns_empty(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"]},
            ],
        })
        assert backend.get_reserved_resources("bob") == set()

    def test_multiple_entries_for_same_user_union(self, tmp_path):
        """A user appearing in multiple records gets the union of resources."""
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"]},
                {"user": "alice", "resources": ["smartbits-07"]},
            ],
        })
        assert backend.get_reserved_resources("alice") == {"rack3-psu", "smartbits-07"}

    def test_expired_entry_ignored(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"], "expires": "2000-01-01T00:00:00Z"},
            ],
        })
        assert backend.get_reserved_resources("alice") == set()

    def test_future_expires_kept(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"], "expires": "3000-01-01T00:00:00Z"},
            ],
        })
        assert backend.get_reserved_resources("alice") == {"rack3-psu"}


class TestWhoReserved:

    def test_resource_held_returns_single_holder_list(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"]},
                {"user": "bob",   "resources": ["rack4-psu"]},
            ],
        })
        assert backend.who_reserved("rack3-psu") == ["alice"]
        assert backend.who_reserved("rack4-psu") == ["bob"]

    def test_unreserved_returns_empty_list(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [],
        })
        assert backend.who_reserved("rack3-psu") == []

    def test_multiple_holders_aggregated_in_file_order(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["shared-lab"]},
                {"user": "bob",   "resources": ["shared-lab"]},
            ],
        })
        assert backend.who_reserved("shared-lab") == ["alice", "bob"]

    def test_duplicate_holder_deduped(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["shared-lab"]},
                {"user": "alice", "resources": ["shared-lab", "other"]},
            ],
        })
        assert backend.who_reserved("shared-lab") == ["alice"]


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
        assert backend.get_reserved_resources("alice") == set()


class TestErrors:

    def test_missing_file_raises_backend_error(self, tmp_path):
        backend = JsonReservationBackend(path=tmp_path / "does-not-exist.json")
        with pytest.raises(ReservationBackendError, match="Failed to read"):
            backend.get_reserved_resources("alice")

    def test_malformed_json_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json")
        backend = JsonReservationBackend(path=f)
        with pytest.raises(ReservationBackendError, match="Malformed JSON"):
            backend.get_reserved_resources("alice")

    def test_wrong_top_level_type_raises(self, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        backend = JsonReservationBackend(path=f)
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.get_reserved_resources("alice")

    def test_unsupported_version_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {"version": 99, "reservations": []})
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.get_reserved_resources("alice")

    def test_reservations_not_list_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {"version": 1, "reservations": "nope"})
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.get_reserved_resources("alice")

    def test_entry_missing_user_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [{"resources": ["x"]}],
        })
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.get_reserved_resources("alice")

    def test_resources_not_string_list_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [{"user": "a", "resources": [1, 2]}],
        })
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.get_reserved_resources("alice")

    def test_bad_expires_raises(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [{"user": "a", "resources": ["x"], "expires": "not-a-date"}],
        })
        with pytest.raises(ReservationBackendError, match="Invalid reservation file"):
            backend.get_reserved_resources("alice")
