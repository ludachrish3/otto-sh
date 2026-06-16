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

from pydantic import ValidationError

from ..models.settings import ReservationEntry, ReservationFile
from .check import ReservationBackendError


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
            if entry.user == username:
                resources.update(entry.resources)
        return resources

    def who_reserved(self,
        resource: str,
    ) -> str | None:
        for entry in self._active_entries():
            if resource in entry.resources:
                # First writer wins — the file is a list, order is authoritative.
                return str(entry.user)
        return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _active_entries(self) -> list[ReservationEntry]:
        """Load the file and return entries that are not past their expiry."""
        data = self._load()
        now = datetime.now(tz=timezone.utc)
        active: list[ReservationEntry] = []
        for entry in data.reservations:
            if entry.expires is None or entry.expires > now:
                active.append(entry)
        return active

    def _load(self) -> ReservationFile:
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
        try:
            return ReservationFile.model_validate(data)
        except ValidationError as e:
            raise ReservationBackendError(
                f"Invalid reservation file {self._path}: {e}"
            ) from e
