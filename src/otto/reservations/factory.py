"""Build a reservation backend and the per-invocation gate from ``[reservations]`` settings.

Re-exported from :mod:`otto.reservations`, which is where callers import these
from. They live in their own module so that importing another name from the
package does not import this one, nor the backends and settings it reaches.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .check import ReservationGate
from .identity import resolve_username

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..registry import Prepared
    from .protocol import ReservationBackend
    from .registry import ReservationEnv


def _prepare(
    settings: "dict[str, Any]",
    repo_dir: Path,
    *,
    username: "str | None",
    origin: "str | None",
) -> "Prepared[ReservationEnv, ReservationBackend, None]":
    """Parse the ``[reservations]`` envelope, then prepare the backend it names."""
    from pydantic import ValidationError

    from ..models.base import compact_validation_error
    from ..models.settings import ReservationConfigSpec
    from .registry import RESERVATION_BACKENDS, ReservationEnv

    # An empty dict is the ABSENT table, which build_reservation_gate passes
    # when no repo declares [reservations]: no checker, so the null backend.
    # A table with keys must name its backend; the envelope refuses one
    # that does not, rather than silently allowing everything.
    try:
        cfg = ReservationConfigSpec.model_validate(settings or {"backend": "none"})
    except ValidationError as e:
        raise ValueError(f"Invalid [reservations] settings: {compact_validation_error(e)}") from e
    source = origin or str(repo_dir / ".otto" / "settings.toml")
    return RESERVATION_BACKENDS.prepare(
        cfg.backend,
        settings.get(cfg.backend) or {},
        ReservationEnv(repo_dir, username, source, cfg.url),
        source=source,
    )


def build_backend(
    settings: "dict[str, Any]",
    repo_dir: Path,
    *,
    username: "str | None" = None,
    origin: "str | None" = None,
) -> "ReservationBackend":
    """Build a reservation backend from a parsed ``[reservations]`` section.

    Parameters
    ----------
    settings : dict[str, Any]
        The ``[reservations]`` table parsed from ``.otto/settings.toml``:
        ``backend`` (``"json"``, ``"none"``, or a name registered via
        :func:`~otto.reservations.register_reservation_backend`), an optional
        ``url``, and the backend's own ``[reservations.<backend>]`` sub-table.
        ``backend`` is required whenever the table has any key; only an EMPTY
        dict (no ``[reservations]`` table in any repo) means ``"none"``.
    repo_dir : Path
        The root of the repo the table came from. A relative path in the
        sub-table anchors to it; the backend receives it as
        :attr:`~otto.reservations.ReservationEnv.repo_dir`.
    username : str | None
        The identity otto resolved for this invocation, handed to the backend
        as :attr:`~otto.reservations.ReservationEnv.username`.
    origin : str | None
        The settings file the table came from, named in every error; by
        default ``<repo_dir>/.otto/settings.toml``.

    Returns
    -------
    ReservationBackend
        A ready-to-query backend instance.

    Raises
    ------
    ValueError
        The ``[reservations]`` table itself is malformed (no ``backend``, or
        a value of the wrong type).
    ReservationConstructionError
        The backend is not registered, its sub-table does not parse, its
        factory fails, or what it builds is not a reservation backend. The
        message names the backend, its registering module, the settings file
        and the failed stage.
    """
    from .registry import RESERVATION_BACKENDS

    return RESERVATION_BACKENDS.build(
        _prepare(settings, repo_dir, username=username, origin=origin)
    )


def gate_from_settings(
    settings: "dict[str, Any]",
    repo_dir: Path,
    *,
    holder: "str | None",
    skip_reservation_check: bool,
) -> ReservationGate:
    """Build the reservation gate from one ``[reservations]`` table.

    The rules every caller shares: the identity is resolved first, because the
    backend is built for that username. Under ``skip_reservation_check`` (the
    ``-R`` break-glass) the table is neither prepared nor built, so a
    scheduler that fails or hangs in its constructor, or a sub-table that does
    not parse, can never block lab access. A lazy ``backend_factory`` is
    always attached for status reports: its first call prepares the table,
    and every call builds a fresh backend from that one preparation.

    Raises
    ------
    ReservationBackendError
        The backend cannot be built and ``skip_reservation_check`` is False.
    """
    identity = resolve_username(holder)
    prepared: "list[Prepared[ReservationEnv, ReservationBackend, None]]" = []

    def _factory() -> "ReservationBackend":
        from .registry import RESERVATION_BACKENDS

        if not prepared:
            prepared.append(_prepare(settings, repo_dir, username=identity.username, origin=None))
        return RESERVATION_BACKENDS.build(prepared[0])

    backend = None if skip_reservation_check else _factory()
    return ReservationGate(
        backend=backend,
        identity=identity,
        skip_check=skip_reservation_check,
        backend_factory=_factory,
    )


def build_reservation_gate(
    repos: "list[Repo]",
    *,
    holder: str | None,
    skip_reservation_check: bool,
    cwd_fallback: Path,
) -> ReservationGate:
    """Resolve the per-invocation reservation gate from the active repos.

    The first repo with a ``[reservations]`` section wins. Construction follows
    :func:`gate_from_settings`.

    Raises
    ------
    ReservationBackendError
        If construction fails and ``skip_reservation_check`` is False.
    """
    settings: dict[str, Any] = {}
    repo_dir: Path = repos[0].sut_dir if repos else cwd_fallback
    for repo in repos:
        if repo.reservation_settings:
            settings, repo_dir = repo.reservation_settings, repo.sut_dir
            break
    return gate_from_settings(
        settings, repo_dir, holder=holder, skip_reservation_check=skip_reservation_check
    )
