"""The last lab names each ``jsonnet`` lab source declared, kept for diagnostics.

A complete, problem-free status read of a ``jsonnet`` source records the
names of the labs it declared, one file per source under
:func:`~otto.config.home.lab_names_cache_dir`. When the source later fails
(an inventory it renders from is down, a template breaks), its
:meth:`~otto.labs.LabRepository.list_labs` answers with these names, and the
composite shows each one, inside the source's ``scope``, unavailable with the
source's reason.

The names are diagnostics only. They never authorize a load (a listed name
still fails with the outage) and never prove a lab is outside a failed
source's reach (a name inside the scope that is not listed fails as well).
The record holds lab names and the time they were first recorded, nothing
else: nothing rendered is written. Only a scoped source records, since only
a failed scoped source's listing reads the names. ``otto cache clear --all``
and ``otto cache prune`` remove the records
(:func:`otto.config.cache_maintenance.prune`).
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


def source_identity(*, project_root: Path, label: str, config: "Mapping[str, object]") -> str:
    """Return the identity a source's record is kept under: a sha256 hex digest.

    It is taken over the JSON of the declaring project's root, the source's
    label and *config*, the source's configuration as its model dumps it
    (``model_dump(mode="json")``, the normalized form the backend registry
    prepares). A changed declaration is a new identity, so it starts with no
    names rather than with another declaration's.
    """
    material = json.dumps([str(project_root), label, config], sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


def _record_path(identity: str) -> Path:
    """Return the file the record of the source *identity* lives in.

    Raises:
        RuntimeError: otto's home cannot be resolved.
    """
    from ..config.home import lab_names_cache_dir

    return lab_names_cache_dir() / f"{identity}.json"


def record_last_good(*, identity: str, names: Sequence[str]) -> None:
    """Record *names* as the labs the source *identity* last declared, unless they already are.

    A record that already holds exactly *names* is left as it is, so an
    unchanged source costs one read and never a write; ``recorded_at`` is
    therefore when these names were first recorded, not when they were last
    seen. Otherwise the record is replaced through
    :func:`otto.utils.atomic_write_bytes`, so a reader sees the old record or
    the new one, never a torn one. A failure to write (no home to resolve, a
    home that cannot hold the directory or the file) is logged at debug and
    ignored: the record is a diagnostic, and a render never fails for it.
    """
    from datetime import datetime, timezone

    from ..utils import atomic_write_bytes

    if last_good(identity=identity) == list(names):
        return
    payload = {"names": list(names), "recorded_at": datetime.now(timezone.utc).isoformat()}
    try:
        path = _record_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(payload).encode())
    except (OSError, RuntimeError) as e:
        logger.debug(f"could not record the last known lab names of source {identity}: {e}")


def last_good(*, identity: str) -> list[str]:
    """Return the lab names last recorded for the source *identity*, or ``[]``.

    ``[]`` when there is no record, or it cannot be read or is not one
    :func:`record_last_good` wrote.
    """
    try:
        data = json.loads(_record_path(identity).read_bytes())
    except (OSError, RuntimeError, ValueError):
        return []
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return []
    return names
