"""Review mode: load a saved monitor export and serve it."""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ReviewSourceError

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..models import MonitorExport


def load_review_document(path: Path) -> "MonitorExport":
    """Load a saved format:1 export for review mode.

    Args:
        path: A ``.json`` export or a ``.db`` archive.

    Returns:
        The validated export document.

    Raises:
        ReviewSourceError: *path* is not a loadable monitor export.
    """
    from pydantic import ValidationError

    from ..models import MonitorExport

    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return MonitorExport.model_validate_json(path.read_bytes())
        except ValidationError as err:
            raise ReviewSourceError(
                f"'{path}' is not a valid format:1 monitor export: {err}"
            ) from err
    if suffix == ".db":
        from .db import UnsupportedDBError
        from .export import build_db_export

        try:
            return build_db_export(str(path))
        except UnsupportedDBError as err:
            raise ReviewSourceError(str(err)) from err
    raise ReviewSourceError(
        f"Unsupported source '{path}' (suffix '{suffix}'); use a .json or .db monitor export."
    )


async def serve_review(path: Path, *, repos: "Sequence[Repo]") -> None:
    """Serve a saved export for review until the server is stopped.

    A ``.db`` source is also the archive event edits persist to; a
    ``.json`` source stays read-only. Returns ``None``: review has no outcome
    beyond serving.

    Args:
        path: The export to review.
        repos: The configured repos, whose ``[monitor]`` TLS declaration the
            dashboard honors.

    Raises:
        ReviewSourceError: *path* is not a loadable monitor export.
        MonitorTlsError: the repos' declared dashboard TLS cannot be served.
    """
    from .collector import MetricCollector
    from .server import MonitorServer
    from .tls import resolve_monitor_tls

    export = load_review_document(path)
    tls = resolve_monitor_tls(repos)
    server = MonitorServer(
        collector=MetricCollector(targets=[]),
        mode="review",
        document=export,
        source_name=path.name,
        tls_cert=tls.tls_cert if tls else None,
        tls_key=tls.tls_key if tls else None,
        archive_path=path if path.suffix.lower() == ".db" else None,
    )
    await server.serve()
