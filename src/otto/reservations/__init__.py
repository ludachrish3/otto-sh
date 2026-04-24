"""Lab reservation / scheduler-check subsystem.

See :mod:`otto.reservations.protocol` for the backend contract and
``docs/guide/reservations.md`` for the end-user and implementer docs.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .check import (
    MissingReservationError as MissingReservationError,
)
from .check import (
    ReservationBackendError as ReservationBackendError,
)
from .check import (
    check_reservations as check_reservations,
)
from .check import (
    gate as gate,
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

        * ``backend`` — ``"json"``, ``"none"``, or a dotted path
          ``"pkg.module:ClassName"`` for third-party implementations.
          Defaults to ``"none"`` when absent.
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
        If ``backend`` names an unknown backend or a malformed dotted path.
    ReservationBackendError
        If a third-party backend's construction fails for backend reasons
        (network, bad credentials, etc.).
    """
    backend_name = settings.get("backend", "none")
    url = settings.get("url")

    if backend_name == "none":
        return NullReservationBackend()

    if backend_name == "json":
        json_settings = settings.get("json", {}) or {}
        path_raw = json_settings.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise ValueError(
                "[reservations.json] requires a 'path' string pointing at the "
                "reservation file"
            )
        path = Path(path_raw)
        if not path.is_absolute():
            path = repo_dir / path
        return JsonReservationBackend(url=url, path=path)

    # Dotted path: "pkg.module:ClassName"
    if ":" not in backend_name:
        raise ValueError(
            f"Unknown reservation backend {backend_name!r}. Expected "
            f"'json', 'none', or a dotted path like 'pkg.module:ClassName'."
        )

    module_name, _, class_name = backend_name.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ValueError(
            f"Could not import reservation backend module {module_name!r}: {e}"
        ) from e

    try:
        cls = getattr(module, class_name)
    except AttributeError as e:
        raise ValueError(
            f"Module {module_name!r} has no attribute {class_name!r}"
        ) from e

    # Nested backend-specific kwargs, if any.  Accept either the full dotted
    # name or just the class name as the sub-table key.
    extra_kwargs: dict[str, Any] = (
        settings.get(backend_name)
        or settings.get(class_name)
        or {}
    )

    if url is not None:
        return cls(url=url, **extra_kwargs)  # type: ignore[no-any-return]
    return cls(**extra_kwargs)  # type: ignore[no-any-return]
