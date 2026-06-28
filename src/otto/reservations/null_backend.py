"""Null reservation backend used when no scheduler is configured.

Selected by setting ``backend = "none"`` in the repo's ``[reservations]``
TOML section.  :func:`otto.reservations.check.check_reservations` recognizes
this type and becomes a no-op, so teams that haven't set up a scheduler yet
aren't blocked.
"""


class NullReservationBackend:
    """Always returns "no reservations known" — the check is a no-op."""

    def get_reserved_resources(
        self,
        username: str,
    ) -> set[str]:
        return set()

    def who_reserved(
        self,
        resource: str,
    ) -> list[str]:
        return []

    def backend_name(self) -> str:
        return "none"
