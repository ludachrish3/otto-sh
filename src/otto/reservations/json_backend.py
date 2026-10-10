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
            {"user": "alice", "resources": ["rack3-psu"], "expires": "2035-01-01T00:00:00Z"},
            {"user": "bob", "resources": ["rack4-psu"]},
        ],
    }

* ``reservations`` is a list so one user may appear multiple times (useful
  when merging reservations from multiple sources).
* ``expires`` is optional; past-dated entries are silently ignored.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ConfigDict, ValidationError, ValidationInfo, field_validator
from typing_extensions import override

from ..models.base import OttoModel
from ..utils import anchor_path
from .base import ReservationBackendBase
from .check import ReservationBackendError
from .protocol import Reservation

# Deferred: these models live in otto.models.settings, which subclasses
# pydantic_settings.BaseSettings and so drags pydantic_settings + dotenv (26
# modules) onto `otto reservation --help`, where no reservation file is ever
# read (import budget). _load() imports them when it actually parses one.
if TYPE_CHECKING:
    from ..models.settings import ReservationFile
    from ..registry import Configured
    from .registry import ReservationEnv


class JsonReservationConfig(OttoModel):
    """The json backend's ``[reservations.json]`` sub-table: the reservation file."""

    model_config = ConfigDict(frozen=True)

    path: Path
    """The reservation file; a relative path anchors to the repo root; ``~`` is expanded."""

    @field_validator("path", mode="before")
    @classmethod
    def _not_empty(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise read as the repo root."""
        if value == "":
            raise ValueError("must name the reservation file")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _anchor(cls, value: Path, info: ValidationInfo) -> Path:
        """Anchor a relative path to the env's ``repo_dir`` when prepared with one."""
        env = (info.context or {}).get("env")
        return anchor_path(value, env.repo_dir, quote=False) if env is not None else value


class JsonReservationBackend(ReservationBackendBase):
    """Read reservations from a JSON file on disk.

    Implements the optional
    :class:`~otto.reservations.protocol.SupportsResourceHolders` capability
    (``holders``) as well as the required contract: the whole file is on
    disk, so the inverted "who holds this resource?" query is a scan away.

    Parameters
    ----------
    url : str | None
        Kept on the base as ``self.url`` and otherwise ignored: the file is
        the whole backend.
    path : Path
        Location of the reservation file on disk.  Required.
    username : str | None
        The identity otto resolved for this invocation, forwarded to the base
        class so :attr:`~otto.reservations.ReservationBackendBase.reservations`
        has someone to query for.
    """

    def __init__(
        self,
        url: "str | None" = None,
        *,
        path: Path,
        username: "str | None" = None,
    ) -> None:
        super().__init__(url=url, username=username)
        self._path = Path(path)

    @override
    def backend_name(self) -> str:
        """Return the registry key for this backend (``"json"``)."""
        return "json"

    @override
    def fetch_reservations(
        self,
        username: str,
        start: "datetime | None" = None,
        end: "datetime | None" = None,
    ) -> "list[Reservation]":
        """Return *username*'s reservations overlapping the window.

        The JSON format records only ``expires``, so ``start`` is always
        ``None`` and an entry without ``expires`` yields ``end=None``.

        Raises
        ------
        ReservationBackendError
            If the file cannot be read or contains malformed data.
        """
        return [r for r in self._all_reservations(start, end) if r.user == username]

    def holders(self, resource: str) -> "list[Reservation]":
        """Return every reservation currently covering *resource*, any user.

        Raises
        ------
        ReservationBackendError
            If the file cannot be read or contains malformed data.
        """
        return [r for r in self._all_reservations(None, None) if r.resource == resource]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _all_reservations(
        self,
        start: "datetime | None",
        end: "datetime | None",  # noqa: ARG002 — see below: a file entry has no start, so only the window's LOWER bound can exclude it
    ) -> "list[Reservation]":
        """Every entry in the file overlapping ``[start, end]``, flattened per resource."""
        window_start = start if start is not None else datetime.now(tz=timezone.utc)
        rows: list[Reservation] = []
        for entry in self._load().reservations:
            # A file entry has no start, so it overlaps unless it ended before
            # the window opened. `expires is None` is open-ended and always
            # overlaps. This is the OVERLAP predicate, not containment: an
            # entry expiring after window_start is active during the window
            # however early it began.
            if entry.expires is not None and entry.expires <= window_start:
                continue
            rows.extend(
                Reservation(
                    user=str(entry.user),
                    resource=str(resource),
                    start=None,
                    end=entry.expires,
                )
                for resource in entry.resources
            )
        return rows

    def _load(self) -> "ReservationFile":
        from ..models.settings import ReservationFile

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
            raise ReservationBackendError(f"Invalid reservation file {self._path}: {e}") from e


def _json_reservations(
    c: "Configured[JsonReservationConfig, ReservationEnv]",
) -> JsonReservationBackend:
    """Build the json backend from its prepared sub-table."""
    return JsonReservationBackend(c.env.url, path=c.config.path, username=c.env.username)
