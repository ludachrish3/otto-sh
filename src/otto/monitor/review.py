"""Review mode: load a saved monitor export and serve it."""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..console import CONSOLE, print_literal
from .errors import ReviewSourceError

if TYPE_CHECKING:
    from ..config.project import Project
    from ..models.monitor import MonitorExport, SessionRecord


def parse_review_json(data: bytes, *, origin: str) -> "MonitorExport":
    """Parse and validate a format:1 export document.

    The one entry for a ``.json`` source, whether it is read from a path or
    uploaded.

    Args:
        data: The raw JSON bytes.
        origin: Where *data* came from, named in every refusal.

    Returns:
        The validated export document.

    Raises:
        ReviewSourceError: *data* is not a valid, editable format:1 export.
    """
    from pydantic import ValidationError

    from ..models.monitor import MonitorExport

    try:
        document = MonitorExport.model_validate_json(data)
    except ValidationError as err:
        raise ReviewSourceError(
            f"'{origin}' is not a valid format:1 monitor export: {err}"
        ) from err
    check_editable_document(document, origin=origin)
    return document


def check_editable_document(document: "MonitorExport", *, origin: str) -> None:
    """Refuse a document that cannot be held as one editable source.

    Session ids must be unique. Every event id must be a positive integer a
    browser holds exactly. A repeated event id is allowed (see
    :func:`view_only_ids`).

    Args:
        document: The parsed export.
        origin: Where the document came from, named in the refusal.

    Raises:
        ReviewSourceError: a session id repeats, or an event id is outside
            ``1..MAX_SAFE_EVENT_ID``.
    """
    from .event_ops import MAX_SAFE_EVENT_ID

    seen: set[str] = set()
    for session in document.sessions:
        if session.id in seen:
            raise ReviewSourceError(f"'{origin}' has a duplicate session id '{session.id}'")
        seen.add(session.id)
        for event in session.events:
            if event.id is not None and not 0 < event.id <= MAX_SAFE_EVENT_ID:
                raise ReviewSourceError(
                    f"'{origin}' session '{session.id}' has event id {event.id}; "
                    f"event ids run from 1 to {MAX_SAFE_EVENT_ID}"
                )


def view_only_ids(session: "SessionRecord") -> frozenset[int]:
    """Return the event ids that occur more than once in *session*.

    An edit cannot name one of two events sharing an id, so such events are
    shown but never edited.
    """
    seen: set[int] = set()
    repeated: set[int] = set()
    for event in session.events:
        if event.id is None:
            continue
        (repeated if event.id in seen else seen).add(event.id)
    return frozenset(repeated)


def load_review_document(*, path: Path) -> "MonitorExport":
    """Load a saved format:1 export for review mode.

    Args:
        path: A ``.json`` export or a ``.db`` archive.

    Returns:
        The validated export document.

    Raises:
        ReviewSourceError: *path* is not a loadable monitor export.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_review_json(path.read_bytes(), origin=str(path))
    if suffix == ".db":
        from .db import UnsupportedDBError
        from .export import build_db_export

        try:
            document = build_db_export(str(path))
        except UnsupportedDBError as err:
            raise ReviewSourceError(str(err)) from err
        check_editable_document(document, origin=str(path))
        return document
    raise ReviewSourceError(
        f"Unsupported source '{path}' (suffix '{suffix}'); use a .json or .db monitor export."
    )


async def serve_review(*, path: Path, projects: "Sequence[Project]") -> None:
    """Serve a saved export for review until the server is stopped.

    A ``.db`` source is also the archive event edits persist to; a
    ``.json`` source is edited in server memory until an export saves the edits,
    and stopping with edits unsaved prints a warning naming the served source
    and the count (the stop goes ahead). The source served at the stop is the
    one named: an Import may have switched it to another ``.json``.
    Returns ``None``: review has no outcome beyond serving.

    Args:
        path: The export to review.
        projects: The configured projects, whose ``[monitor]`` TLS declaration the
            dashboard honors.

    Raises:
        ReviewSourceError: *path* is not a loadable monitor export.
        MonitorTlsError: the projects' declared dashboard TLS cannot be served.
    """
    from .collector import MetricCollector
    from .server import MonitorServer
    from .tls import resolve_monitor_tls

    export = load_review_document(path=path)
    tls = resolve_monitor_tls(projects=projects)
    server = MonitorServer(
        collector=MetricCollector(targets=[]),
        mode="review",
        document=export,
        source_name=path.name,
        tls_cert=tls.tls_cert if tls else None,
        tls_key=tls.tls_key if tls else None,
        archive_path=path if path.suffix.lower() == ".db" else None,
    )
    try:
        await server.serve()
    finally:
        unsaved = server.unsaved_changes
        if unsaved:
            noun, verb = ("change", "is") if unsaved == 1 else ("changes", "are")
            print_literal(
                f"Warning: {unsaved} unsaved {noun} to {server.source_name}"
                f" since the last export {verb} lost.",
                console=CONSOLE,
            )
