"""JSON-file reservation backend — reference implementation and test double.

Intended for two audiences:

1. **Small teams with no scheduler** who just want to hand-edit a JSON file
   checked into the repo (or kept on a shared volume) that lists who holds
   which resources.
2. **Tests** — unit and integration tests construct fixture JSON files and
   point a :class:`JsonReservationBackend` at them.

File format (``version: 1``)::

    {
      "version": 1,
      "reservations": [
        {"user": "alice", "resources": ["rack3-psu"], "expires": "2026-05-01T00:00:00Z"},
        {"user": "bob",   "resources": ["rack4-psu"]}
      ]
    }

* ``reservations`` is a list so one user may appear multiple times (useful
  when merging reservations from multiple sources).
* ``expires`` is optional; past-dated entries are silently ignored.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .check import ReservationBackendError

SUPPORTED_VERSION = 1


class JsonReservationBackend:
    """Read reservations from a JSON file on disk.

    Parameters
    ----------
    url : str | None
        Accepted and ignored.  Kept in the signature so the factory
        (:func:`otto.reservations.build_backend`) can pass ``url=url``
        uniformly to any backend.
    path : Path
        Location of the reservation file on disk.  Required.
    """

    def __init__(self,
        url: str | None = None,  # noqa: ARG002 — protocol/factory uniformity
        *,
        path: Path,
    ) -> None:
        self._path = Path(path)

    def backend_name(self) -> str:
        return "json"

    def get_reserved_resources(self,
        username: str,
    ) -> set[str]:
        resources: set[str] = set()
        for entry in self._active_entries():
            if entry["user"] == username:
                resources.update(entry["resources"])
        return resources

    def who_reserved(self,
        resource: str,
    ) -> str | None:
        for entry in self._active_entries():
            if resource in entry["resources"]:
                # First writer wins — the file is a list, order is authoritative.
                return str(entry["user"])
        return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _active_entries(self) -> list[dict[str, Any]]:
        """Load the file and return entries that are not past their expiry."""
        data = self._load()
        now = datetime.now(tz=timezone.utc)

        active: list[dict[str, Any]] = []
        for entry in data.get("reservations", []):
            expires_raw = entry.get("expires")
            if expires_raw is None:
                active.append(entry)
                continue
            try:
                expires = _parse_iso8601(expires_raw)
            except ValueError as e:
                raise ReservationBackendError(
                    f"Invalid 'expires' timestamp in {self._path}: {expires_raw!r}"
                ) from e
            if expires > now:
                active.append(entry)
        return active

    def _load(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text()
        except OSError as e:
            raise ReservationBackendError(
                f"Failed to read reservation file {self._path}: {e}"
            ) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ReservationBackendError(
                f"Malformed JSON in reservation file {self._path}: {e}"
            ) from e

        if not isinstance(data, dict):
            raise ReservationBackendError(
                f"Reservation file {self._path} top-level value must be an object, "
                f"got {type(data).__name__}"
            )

        version = data.get("version")
        if version != SUPPORTED_VERSION:
            raise ReservationBackendError(
                f"Reservation file {self._path} has unsupported version "
                f"{version!r}; expected {SUPPORTED_VERSION}"
            )

        reservations = data.get("reservations")
        if not isinstance(reservations, list):
            raise ReservationBackendError(
                f"Reservation file {self._path} 'reservations' must be a list, "
                f"got {type(reservations).__name__}"
            )

        for idx, entry in enumerate(reservations):
            if not isinstance(entry, dict):
                raise ReservationBackendError(
                    f"Entry {idx} in {self._path} must be an object, "
                    f"got {type(entry).__name__}"
                )
            entry_d = cast(dict[str, Any], entry)
            if not isinstance(entry_d.get("user"), str):
                raise ReservationBackendError(
                    f"Entry {idx} in {self._path} missing string 'user' field"
                )
            resources = entry_d.get("resources")
            if not isinstance(resources, list) or not all(isinstance(r, str) for r in resources):
                raise ReservationBackendError(
                    f"Entry {idx} in {self._path} 'resources' must be a list of strings"
                )

        return data


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating trailing ``Z`` as UTC."""
    # datetime.fromisoformat accepts "+00:00" but not bare "Z" until 3.11.
    # Normalize both forms so either parses cleanly.
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
