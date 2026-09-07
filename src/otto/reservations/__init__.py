"""Lab reservation / scheduler-check subsystem.

See :mod:`otto.reservations.protocol` for the backend contract and
``docs/guide/cli/reservation/`` for the end-user docs and
``docs/library/reservation-backends.md`` for the implementer contract.
"""

import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..utils import anchor_path
from .base import (
    ReservationBackendBase as ReservationBackendBase,
)
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
    ReservationGateResult as ReservationGateResult,
)
from .check import (
    ResourceLevel as ResourceLevel,
)
from .check import (
    ResourceOrigin as ResourceOrigin,
)
from .check import (
    active_reservations as active_reservations,
)
from .check import (
    check_reservations as check_reservations,
)
from .check import (
    required_resource_origins as required_resource_origins,
)
from .check import (
    required_resources as required_resources,
)
from .check import (
    reset_expiry_warnings as reset_expiry_warnings,
)
from .check import (
    warn_expiring_reservations as warn_expiring_reservations,
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
from .null_backend import (
    is_null_backend as is_null_backend,
)
from .protocol import (
    Reservation as Reservation,
)
from .protocol import (
    ReservationBackend as ReservationBackend,
)
from .protocol import (
    SupportsResourceHolders as SupportsResourceHolders,
)
from .protocol import (
    SupportsUsernameCompletion as SupportsUsernameCompletion,
)
from .registry import (
    register_reservation_backend as register_reservation_backend,
)

if TYPE_CHECKING:
    from ..config.repo import Repo

logger = logging.getLogger(__name__)


_warned_half_ported: "set[str]" = set()
"""Backend class names already announced by :func:`_warn_if_half_ported`.

Process-wide, mirroring
:data:`otto.reservations.check._warned_expiring`, because
:func:`build_backend` runs more than once in a single command — the gate
builds one backend, and the completion cache's username collection builds
another — and a team told twice in one invocation that their port is
unfinished learns nothing the second time (spec 2026-09-07
reservation-object-api §8).
"""


def reset_half_ported_warnings() -> None:
    """Forget every half-ported warning already announced this process.

    Exists for tests: the suppression set is module state, so a test that
    warns leaks into the next one unless it is cleared between them.
    """
    _warned_half_ported.clear()


def _warn_if_half_ported(backend: object) -> None:
    """Warn ONCE PER PROCESS when a backend kept ``who_reserved`` but never grew ``holders``.

    Every other removal in this release fails loudly at startup. This one
    cannot: a backend that ported ``fetch_reservations`` and missed the
    rename is still a valid backend, so it starts clean and silently reports
    holders as unknown in refusals it used to answer. Defining the old name
    and not the new one is a reliable signature of a half-finished port.

    Two suppressions, and they are not the same suppression:

    * **Once per process**, keyed on the backend class, because
      :func:`build_backend` is called more than once per command.
    * **Never during shell completion.** Completion returns from the root
      callback before any log handler is installed, so
      :data:`logging.lastResort` would write this line straight into the
      middle of the user's TAB (spec §6.1, §8). The completion path
      suppresses the PAYLOAD only: nothing is recorded in
      :data:`_warned_half_ported`, so the next real invocation — where there
      is a terminal to write to — still announces it.
    """
    if callable(getattr(backend, "who_reserved", None)) and not callable(
        getattr(backend, "holders", None)
    ):
        name = type(backend).__name__
        if name in _warned_half_ported:
            return
        from ..config.completion_cache import is_completion_mode

        if is_completion_mode():
            return
        _warned_half_ported.add(name)
        logger.warning(
            "Reservation backend %r defines who_reserved but not holders. "
            "who_reserved was replaced by holders(resource) -> list[Reservation] "
            "in otto 0.11.0; refusal messages will report holders as unknown "
            "until it is ported.",
            name,
        )


def build_backend(
    settings: dict[str, Any],
    repo_dir: Path,
    *,
    username: "str | None" = None,
) -> ReservationBackend:
    """Construct a reservation backend from a parsed ``[reservations]`` section.

    Parameters
    ----------
    settings : dict[str, Any]
        The ``[reservations]`` sub-dict parsed from ``.otto/settings.toml``.
        Expected keys:

        * ``backend`` — ``"json"``, ``"none"``, or a name registered via
          :func:`register_reservation_backend` from an init module. Required
          whenever the dict has any key: a present ``[reservations]`` table is
          a specified checker and must name its backend. Only an EMPTY dict —
          no ``[reservations]`` table in any repo — resolves to ``"none"``.
        * ``url`` — optional string, forwarded as ``url=...`` to the
          backend constructor when present.
        * ``<backend-name>`` — optional nested table with backend-specific
          keyword arguments (e.g. ``[reservations.json] path = "..."``).

    repo_dir : Path
        The SUT repo root.  Used to expand the JSON backend's ``path``
        setting when it is relative, and forwarded as ``repo_dir=`` to a
        custom backend's constructor, mirroring
        :func:`otto.labs.build_lab_sources`.
    username : str | None
        The identity otto resolved for this invocation. Forwarded as
        ``username=`` to every backend constructed here — built-ins and
        custom backends alike — so
        :attr:`~otto.reservations.ReservationBackendBase.reservations`
        has someone to query for.

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
        (network, bad credentials, etc.), or if the constructed backend does
        not satisfy the :class:`~otto.reservations.protocol.ReservationBackend`
        protocol.
    """
    from pydantic import ValidationError

    from ..models.settings import ReservationConfigSpec

    # An empty dict is the ABSENT table — what build_reservation_gate passes
    # when no repo declares [reservations] — and means no checker. It is the
    # only shape that resolves to "none" implicitly: the spec makes `backend`
    # required, so a table with keys but no backend is refused below rather
    # than silently becoming the allow-all null backend.
    if not settings:
        settings = {"backend": "none"}
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

    try:
        if backend_name == "none":
            backend = cls(username=username)
        elif backend_name == "json":
            json_settings = settings.get("json", {}) or {}
            path_raw = json_settings.get("path")
            if not isinstance(path_raw, str) or not path_raw:
                raise ValueError(
                    "[reservations.json] requires a 'path' string pointing at the reservation file"
                )
            path = anchor_path(Path(path_raw), repo_dir)
            backend = cls(url=url, path=path, username=username)
        else:
            # Custom backend: resolved by registered name
            # (register_reservation_backend from an init module). No
            # dotted-path / importlib resolution.
            extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
            if url is not None:
                backend = cls(url=url, repo_dir=repo_dir, username=username, **extra_kwargs)
            else:
                backend = cls(repo_dir=repo_dir, username=username, **extra_kwargs)
    except TypeError as e:
        if inspect.isabstract(cls):
            # A class that inherits ReservationBackendBase but implements none
            # of the new abstract methods fails here, not at the isinstance
            # check below: Python refuses to instantiate it at all. Give the
            # same migration pointer rather than letting a bare TypeError
            # traceback reach the user. `inspect.isabstract` is what tells
            # this apart from the far more common case below (a config typo
            # or a bug in an otherwise-ported backend's own __init__): it is
            # true only when `cls` itself still has unimplemented abstract
            # methods, independent of the wording of `e`.
            raise ReservationBackendError(
                f"Reservation backend {cls.__name__!r} could not be constructed: {e}. "
                "It looks like an unfinished port to the otto 0.11.0 reservation "
                "contract: implement fetch_reservations(username, start, end) -> "
                "list[Reservation] and backend_name(); see "
                "docs/library/reservation-backends.md."
            ) from e
        # Anything else -- a typo'd key in [reservations.<name>] landing in
        # **extra_kwargs, or a bug in a fully-ported backend's own __init__ --
        # is reported plainly. Do not guess "unfinished port" here; that
        # reading actively misdirects the most likely real-world cause.
        raise ReservationBackendError(
            f"Reservation backend {cls.__name__!r} could not be constructed: {e}"
        ) from e

    if not isinstance(backend, ReservationBackend):
        stale = next(
            (
                name
                for name in ("get_reserved_resources", "get_reservation_windows")
                if callable(getattr(backend, name, None))
            ),
            None,
        )
        if stale is not None:
            raise ReservationBackendError(
                f"Reservation backend {type(backend).__name__!r} still defines "
                f"{stale!r}, which otto 0.11.0 removed. Implement "
                "fetch_reservations(username, start, end) -> list[Reservation] "
                "instead; see docs/library/reservation-backends.md."
            )
        raise ReservationBackendError(
            f"Reservation backend {type(backend).__name__!r} does not satisfy the "
            "ReservationBackend protocol (needs fetch_reservations and backend_name)."
        )

    _warn_if_half_ported(backend)
    return backend  # type: ignore[no-any-return]


def build_reservation_gate(
    repos: "list[Repo]",
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

    # Resolve identity BEFORE constructing the backend: the backend is given
    # the username at construction and queries for it lazily, so there is no
    # backend to build until we know who is asking.
    identity = resolve_username(as_user)

    def _factory() -> ReservationBackend:
        return build_backend(reservation_settings, reservation_repo_dir, username=identity.username)

    backend: ReservationBackend | None = None
    if not skip_reservation_check:
        backend = _factory()  # may raise ReservationBackendError

    return ReservationGate(
        backend=backend,
        identity=identity,
        skip_check=skip_reservation_check,
        backend_factory=_factory,
    )
