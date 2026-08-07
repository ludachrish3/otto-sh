"""Structural tests for the optional username-completion capability."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from otto.reservations import (
    ReservationWindow,
    SupportsReservationWindows,
    SupportsUsernameCompletion,
)


def test_class_with_list_usernames_satisfies():
    class B:
        def list_usernames(self):
            return ["alice"]

    assert isinstance(B(), SupportsUsernameCompletion)


def test_class_without_list_usernames_does_not():
    class B:
        pass

    assert not isinstance(B(), SupportsUsernameCompletion)


class _WindowsBackend:
    def get_reservation_windows(self, username: str) -> list[ReservationWindow]:
        return [
            ReservationWindow(
                resource="rack3-psu",
                start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end=datetime(2026, 12, 31, tzinfo=timezone.utc),
            )
        ]


class _NoWindowsBackend:
    def get_reserved_resources(self, username: str) -> set[str]:
        return set()


def test_windows_backend_satisfies_capability():
    assert isinstance(_WindowsBackend(), SupportsReservationWindows)


def test_windowless_backend_lacks_capability():
    assert not isinstance(_NoWindowsBackend(), SupportsReservationWindows)


def test_reservation_window_is_frozen():
    w = _WindowsBackend().get_reservation_windows("alice")[0]

    with pytest.raises(FrozenInstanceError):
        w.resource = "other"  # type: ignore[misc]
