"""Lab reservation / scheduler-check subsystem.

See :mod:`otto.reservations.protocol` for the backend contract and
``docs/guide/reservations.md`` for the end-user and implementer docs.
"""

from pathlib import Path
from typing import Any

from .check import (
    MissingReservationError as MissingReservationError,
)
from .check import (
    ReservationBackendError as ReservationBackendError,
)
from .check import (
    ReservationGate as ReservationGate,
)
from .check import (
    ReservationGateOutcome as ReservationGateOutcome,
)
from .check import (
    check_reservations as check_reservations,
)
from .check import (
    required_resources as required_resources,
)
from .identity import (
    ResolvedIdentity as ResolvedIdentity,
)
from .identity import (
    resolve_username as resolve_username,
)
from .json_backend import (
    JsonReservationBackend as JsonReservationBackend,
)
from .null_backend import (
    NullReservationBackend as NullReservationBackend,
)
from .protocol import (
    ReservationBackend as ReservationBackend,
)
from .protocol import (
    SupportsUsernameCompletion as SupportsUsernameCompletion,
)
from .registry import (
    register_reservation_backend as register_reservation_backend,
)


def build_backend(
    settings: dict[str, Any],
    repo_dir: Path,
) -> ReservationBackend:
    """Construct a reservation backend from a parsed ``[reservations]`` section.

    Parameters
    ----------
    settings : dict[str, Any]
        The ``[reservations]`` sub-dict parsed from ``.otto/settings.toml``.
        Expected keys:

        * ``backend`` — ``"json"``, ``"none"``, or a name registered via
          :func:`register_reservation_backend` from an init module. Defaults to
          ``"none"`` when absent.
        * ``url`` — optional string, forwarded as ``url=...`` to the
          backend constructor when present.
        * ``<backend-name>`` — optional nested table with backend-specific
          keyword arguments (e.g. ``[reservations.json] path = "..."``).

    repo_dir : Path
        The SUT repo root.  Used only to expand the JSON backend's
        ``path`` setting when it is relative.

    Returns
    -------
    ReservationBackend
        A ready-to-query backend instance.

    Raises
    ------
    ValueError
        If ``backend`` names an unknown backend.
    ReservationBackendError
        If a third-party backend's construction fails for backend reasons
        (network, bad credentials, etc.).
    """
    from pydantic import ValidationError

    from ..models.settings import ReservationConfigSpec

    try:
        cfg = ReservationConfigSpec.model_validate(settings)
    except ValidationError as e:
        # Keep build_backend's documented exception surface (ValueError for a
        # malformed [reservations] config) and give a contextual message rather
        # than a raw pydantic dump.
        raise ValueError(f"Invalid [reservations] settings: {e}") from e
    backend_name = cfg.backend
    url = cfg.url

    # Resolved by registered name for every backend, built-ins included — a
    # re-registered replacement (e.g. register_reservation_backend("json", ...,
    # overwrite=True)) takes effect here rather than being bypassed by a
    # hardcoded construction below.
    from .registry import get_reservation_backend_class

    cls = get_reservation_backend_class(backend_name)

    if backend_name == "none":
        return cls()  # type: ignore[no-any-return]

    if backend_name == "json":
        json_settings = settings.get("json", {}) or {}
        path_raw = json_settings.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise ValueError(
                "[reservations.json] requires a 'path' string pointing at the reservation file"
            )
        path = Path(path_raw)
        if not path.is_absolute():
            path = repo_dir / path
        return cls(url=url, path=path)  # type: ignore[no-any-return]

    # Custom backend: resolved by registered name (register_reservation_backend
    # from an init module). No dotted-path / importlib resolution.
    extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
    if url is not None:
        return cls(url=url, **extra_kwargs)  # type: ignore[no-any-return]
    return cls(**extra_kwargs)  # type: ignore[no-any-return]


def build_reservation_gate(
    repos: list[Any],
    *,
    as_user: str | None,
    skip_reservation_check: bool,
    cwd_fallback: Path,
) -> ReservationGate:
    """Resolve the per-invocation reservation gate from the active repos.

    The first repo with a ``[reservations]`` section wins. With
    ``skip_reservation_check`` (the ``-R`` break-glass flag) the backend is
    **not** constructed at all — a scheduler that fails or hangs in its
    constructor can never block lab access. A ``backend_factory`` thunk is
    always attached so ``otto reservation`` subcommands can build it on demand.

    Raises
    ------
    ReservationBackendError
        If construction fails and ``skip_reservation_check`` is False.
    """
    reservation_settings: dict[str, Any] = {}
    reservation_repo_dir: Path = repos[0].sut_dir if repos else cwd_fallback
    for repo in repos:
        if repo.reservation_settings:
            reservation_settings = repo.reservation_settings
            reservation_repo_dir = repo.sut_dir
            break

    def _factory() -> ReservationBackend:
        return build_backend(reservation_settings, reservation_repo_dir)

    backend: ReservationBackend | None = None
    if not skip_reservation_check:
        backend = _factory()  # may raise ReservationBackendError

    identity = resolve_username(as_user)
    return ReservationGate(
        backend=backend,
        identity=identity,
        skip_check=skip_reservation_check,
        backend_factory=_factory,
    )
