"""A reservation backend for a scheduler that is a text file.

The shape every backend has: three read-only methods, a constructor that
accepts what otto passes, and one exception for every failure. Replace the
file read with your scheduler's API and the rest stands.
"""

# doc: begin team-backend
from pathlib import Path

from otto.reservations import ReservationBackendError


class TeamFileBackend:
    """Read ``<user> <resource>`` lines; one line per holding."""

    def __init__(self, *, repo_dir: Path | None = None, path: str) -> None:
        # otto always passes repo_dir; relative paths anchor to it.
        self._path = (repo_dir or Path()) / path

    def _holdings(self) -> list[tuple[str, str]]:
        try:
            lines = self._path.read_text().splitlines()
        except OSError as exc:
            # Fail closed: an unreadable schedule is not an empty one.
            raise ReservationBackendError(f"cannot read {self._path}: {exc}") from exc
        pairs = []
        for line in lines:
            if line.strip():
                user, resource = line.split()
                pairs.append((user, resource))
        return pairs

    def get_reserved_resources(self, username: str) -> set[str]:
        """Every identifier *username* holds right now."""
        return {r for u, r in self._holdings() if u == username}

    def who_reserved(self, resource: str) -> list[str]:
        """Everyone holding *resource*, sorted."""
        return sorted({u for u, r in self._holdings() if r == resource})

    def backend_name(self) -> str:
        """Return the name ``otto reservation whoami`` shows."""
        return "team-file"


# doc: end team-backend
