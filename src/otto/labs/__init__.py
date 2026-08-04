"""Labs module for DB-agnostic lab/host repository pattern."""

import logging
from pathlib import Path
from typing import Any

from .errors import (
    LabNotFoundError as LabNotFoundError,
)
from .errors import (
    LabRepositoryError as LabRepositoryError,
)
from .json_repository import (
    JsonFileLabRepository as JsonFileLabRepository,
)
from .protocol import (
    HostSummary as HostSummary,
)
from .protocol import (
    LabRepository as LabRepository,
)
from .protocol import (
    SupportsHostSummaries as SupportsHostSummaries,
)
from .registry import (
    register_lab_repository as register_lab_repository,
)

logger = logging.getLogger(__name__)


def host_summaries(repository: LabRepository) -> list[HostSummary]:
    """Every host *repository* knows — cheaply when it can, correctly always.

    Uses :class:`~otto.labs.protocol.SupportsHostSummaries` when the backend
    implements it (the built-in JSON backend does, deriving ids without
    constructing hosts). Otherwise falls back to enumerating every lab and
    loading it, which is slower but works for ANY backend — so a custom
    host source gets tab completion and tunnel narrowing with no extra code.

    Best-effort by contract: a lab that fails to load is skipped, because
    every caller is a completion or discovery path that must never crash the
    shell over one bad record.
    """
    if isinstance(repository, SupportsHostSummaries):
        return repository.list_host_summaries()

    # No built-in filtering here: a backend's ``load_lab`` returns exactly its
    # own hosts (otto's ``local`` is injected later, by ``config.lab.load_lab``,
    # not by any backend). Filtering would drop a lab that legitimately defines
    # its own ``local`` — which otto explicitly allows — and would make this
    # path disagree with the ``SupportsHostSummaries`` one.
    by_id: dict[str, HostSummary] = {}
    for name in repository.list_labs():
        try:
            lab = repository.load_lab(name)
        except Exception as e:  # noqa: BLE001 — enumeration is best-effort
            logger.debug(f"skipping lab {name!r} while summarizing hosts: {e}")
            continue
        for host in lab.hosts.values():
            existing = by_id.get(host.id)
            if existing is not None:
                if name not in existing.labs:
                    existing.labs.append(name)
                continue
            by_id[host.id] = HostSummary(
                id=host.id,
                labs=[name],
                ip=getattr(host, "ip", "") or "",
                element=getattr(host, "element", "") or "",
                element_id=getattr(host, "element_id", None),
                docker_capable=bool(getattr(host, "docker_capable", False)),
            )
    return sorted(by_id.values(), key=lambda s: s.id)


def list_host_ids(repository: LabRepository) -> list[str]:
    """Every host id *repository* knows, sorted.

    The id-only view of :func:`host_summaries`.
    """
    return [summary.id for summary in host_summaries(repository)]


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

    # Resolved by registered name for every backend, built-ins included — a
    # re-registered replacement (e.g. register_lab_repository("json", ...,
    # overwrite=True)) takes effect here rather than being bypassed by a
    # hardcoded construction below.
    from .registry import get_lab_repository_class

    cls = get_lab_repository_class(backend_name)  # raises LabRepositoryError if unknown

    if backend_name == "json":
        # The built-in json backend takes the aggregated search paths; a
        # re-registered replacement must accept the same constructor contract.
        return cls(search_paths=list(search_paths or []))

    # Custom backend: resolved by registered name (register_lab_repository from
    # an init module). No dotted-path / importlib resolution.
    extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
    return cls(repo_dir=repo_dir, **extra_kwargs)  # type: ignore[no-any-return]
