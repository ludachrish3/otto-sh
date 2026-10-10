"""A reservation backend for a scheduler that is a text file.

The shape every backend has: the base class, two required read-only methods,
one optional capability, one exception for every failure, and the config
model and factory otto registers it with. Replace the file read with your
scheduler's API and the rest stands.
"""

# doc: begin team-backend
from datetime import datetime
from pathlib import Path

from pydantic import ConfigDict
from typing_extensions import override

from otto.models import OttoModel
from otto.registry import Configured
from otto.reservations import (
    Reservation,
    ReservationBackendBase,
    ReservationBackendError,
    ReservationEnv,
)


class TeamFileBackend(ReservationBackendBase):
    """Read ``<user> <resource>`` lines; one line per holding."""

    def __init__(
        self,
        *,
        url: str | None = None,
        repo_dir: Path | None = None,
        username: str | None = None,
        path: str,
    ) -> None:
        # url, repo_dir and username come from otto's env; path is this backend's own.
        super().__init__(url=url, repo_dir=repo_dir, username=username)
        # A relative path anchors to the repo root the factory passes.
        self._path = (self.repo_dir or Path()) / path

    def _reservations(self) -> list[Reservation]:
        """Every line as a Reservation. The file records no times, so both are None."""
        try:
            lines = self._path.read_text().splitlines()
        except OSError as exc:
            # Fail closed: an unreadable schedule is not an empty one.
            raise ReservationBackendError(f"cannot read {self._path}: {exc}") from exc
        rows = []
        for line in lines:
            if line.strip():
                user, resource = line.split()
                rows.append(Reservation(user=user, resource=resource))
        return rows

    @override
    def fetch_reservations(
        self,
        username: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Reservation]:
        """Every reservation *username* holds right now.

        The window is ignored: with no times recorded, every row is open-ended
        and so overlaps any window otto can ask for.
        """
        return [r for r in self._reservations() if r.user == username]

    def holders(self, resource: str) -> list[Reservation]:
        """Everyone holding *resource* -- the optional inverted query."""
        return [r for r in self._reservations() if r.resource == resource]

    @override
    def backend_name(self) -> str:
        """Return the name ``otto reservation whoami`` shows."""
        return "team-file"


class TeamFileConfig(OttoModel):
    """The ``[reservations.team-file]`` sub-table: the schedule file."""

    model_config = ConfigDict(frozen=True)

    path: str


def team_file(c: Configured[TeamFileConfig, ReservationEnv]) -> TeamFileBackend:
    """Build the backend from its parsed sub-table and otto's environment."""
    return TeamFileBackend(
        url=c.env.url, repo_dir=c.env.repo_dir, username=c.env.username, path=c.config.path
    )


# doc: end team-backend
