"""Storage module for DB-agnostic lab/host repository pattern."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import (
    LabNotFoundError as LabNotFoundError,
)
from .errors import (
    LabRepositoryError as LabRepositoryError,
)
from .factory import (
    create_host_from_dict as create_host_from_dict,
)
from .factory import (
    validate_host_dict as validate_host_dict,
)
from .json_repository import (
    JsonFileLabRepository as JsonFileLabRepository,
)
from .protocol import (
    LabRepository as LabRepository,
)
from .registry import (
    register_lab_repository as register_lab_repository,
)


def build_lab_repository(
    settings: dict[str, Any],
    repo_dir: Path,
    *,
    search_paths: list[Path] | None = None,
) -> LabRepository:
    """Construct a host-source backend from a parsed ``[lab]`` section.

    Parameters
    ----------
    settings : dict[str, Any]
        The ``[lab]`` sub-dict parsed from ``.otto/settings.toml``. ``backend``
        selects a registered name (defaults to ``"json"``); ``[lab.<name>]``
        holds the backend's keyword arguments.
    repo_dir : Path
        The SUT repo root, forwarded as ``repo_dir=`` to a custom backend's
        constructor. The built-in ``json`` backend ignores it and uses
        ``search_paths`` instead.
    search_paths : list[Path] | None
        The aggregated ``labs`` directories. Passed to the built-in ``json``
        backend (preserving today's multi-repo path merge); custom backends
        carry their own config and do not receive it.

    Returns
    -------
    LabRepository
        A ready-to-query backend instance.

    Raises
    ------
    ValueError
        If the ``[lab]`` envelope is malformed.
    LabRepositoryError
        If ``backend`` names an unknown (unregistered) backend.
    """
    from pydantic import ValidationError

    from ..models.settings import LabConfigSpec

    try:
        cfg = LabConfigSpec.model_validate(settings)
    except ValidationError as e:
        # Keep the documented exception surface (ValueError for a malformed
        # [lab] envelope) with a contextual message, not a raw pydantic dump.
        raise ValueError(f"Invalid [lab] settings: {e}") from e

    backend_name = cfg.backend

    if backend_name == "json":
        return JsonFileLabRepository(search_paths=list(search_paths or []))

    # Custom backend: resolved by registered name (register_lab_repository from
    # an init module). No dotted-path / importlib resolution.
    from .registry import get_lab_repository_class

    cls = get_lab_repository_class(backend_name)  # raises LabRepositoryError if unknown
    extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
    return cls(repo_dir=repo_dir, **extra_kwargs)  # type: ignore[no-any-return]
