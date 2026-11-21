"""What ``otto lab`` answers about the lab sources, decided here and only formatted by the CLI.

:func:`render_entry` is ``otto lab render``: the document a Jsonnet entry
file renders to, as the ``jsonnet`` lab source that reads it renders it for a
load. It is the same evaluation (:func:`~otto.labs.documents.render_outcome`,
through the operation's render memo), stopped before the section contract,
so a document the load would refuse still shows.

Importing this module is light: the sources, the renderer and the models
load when a function runs.

:func:`explain_host` is ``otto lab explain``: which layer supplied each of a
host's values, read from the trace the session recorded while it built the
lab (:mod:`otto.host.provenance`), and how old each inventory's snapshot is.
It loads and rebuilds nothing.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import final

from .errors import LabRepositoryError

if TYPE_CHECKING:
    from ..config.project import Project
    from ..host.provenance import FieldTrace, FieldWinner
    from ..inventory.view import InventoryView
    from .jsonnet_source import JsonnetSourceInputs
    from .loader import Lab


@final
@dataclass(frozen=True, kw_only=True)
class RenderedEntry:
    """One Jsonnet entry file as its lab source renders it."""

    source_label: str
    """The label of the ``jsonnet`` lab source that rendered the entry."""

    document: object = field(repr=False)
    """The rendered JSON value, as decoded: its secret values are NOT wrapped.

    Left out of the repr, so a traceback's locals never show it."""

    secret_at: "str | None"
    """Where the document's first literal secret is (``elements[0].creds[0].password``).

    Any value under a ``password`` or ``passphrase`` key that is not
    ``null`` counts, a string or not (``1234``, a list, a table), as
    :func:`~otto.models.base.secret_locations` finds them. ``None`` when the
    document holds none. A rendered document's secrets belong in the
    credentials store, so the CLI refuses to print a document that holds one,
    naming this path and never the value."""


@final
@dataclass(frozen=True, kw_only=True)
class _Reader:
    """A ``jsonnet`` lab source, and the path its own ``paths`` name the entry by."""

    inputs: "JsonnetSourceInputs"
    """The source's configuration and environment."""

    file: Path
    """The entry as the source lists it, which is the path a load renders."""


def render_entry(
    *,
    entry: Path,
    source: "str | None",
    projects: "Sequence[Project]",
    view: "InventoryView",
) -> RenderedEntry:
    """Render the Jsonnet *entry* file through the ``jsonnet`` lab source that reads it.

    The source is the one of *projects*' ``jsonnet`` sources whose files
    (:meth:`~otto.labs.sources.LabSourceState.lab_files`) hold *entry*,
    compared resolved; when several do, *source* names one by its label. The
    source renders the entry itself
    (:meth:`~otto.labs.jsonnet_source.JsonnetSourceInputs.rendered_document`,
    through :func:`~otto.labs.documents.render_outcome`, the one place a
    render's key and import policy are built), under *view* as the source's
    project sees it (:func:`~otto.labs.composite.project_view`), so a load of
    the same entry in the same operation shares this evaluation. The rendered
    text is decoded and nothing else: no section is parsed, so a document
    that fails validation still renders.

    Raises:
        LabRepositoryError: No ``jsonnet`` source reads *entry* (naming the
            sources there are); several do and *source* names none of them
            (naming ``--source`` and their labels); the source names no
            project; the render failed; or it was cancelled, which is the
            source's timeout (raised from the
            :class:`~otto.templating.errors.RenderCancelled`), never a document.
        LabSourceConstructionError: A source's options do not parse.
        InventoryError: An inventory the render reads cannot answer.
    """
    from ..models.base import secret_locations
    from .composite import project_view

    reader = _reader(entry=entry, source=source, projects=projects)
    env = reader.inputs.env
    document = reader.inputs.rendered_document(
        reader.file, view=project_view(view=view, project_root=env.project_root)
    )
    first = next(secret_locations(document), None)
    return RenderedEntry(
        source_label=env.label,
        document=document,
        secret_at=None if first is None else first.path,
    )


def _reader(*, entry: Path, source: "str | None", projects: "Sequence[Project]") -> _Reader:
    """Return the one ``jsonnet`` source of *projects* that reads *entry*, chosen by *source*.

    Raises:
        LabRepositoryError: None reads it, or *source* does not pick exactly one.
    """
    from .errors import LabRepositoryError

    every = _jsonnet_sources(projects)
    target = entry.resolve()
    readers = [
        _Reader(inputs=found.inputs, file=file)
        for found in every
        for file in found.files
        if file.resolve() == target
    ]
    if source is not None:
        chosen = [r for r in readers if r.inputs.env.label == source]
        if chosen:
            return chosen[0]
        if readers:
            raise LabRepositoryError(
                f"{entry}: jsonnet lab source {source!r} does not read this file; "
                f"the ones that do: {_labels(r.inputs for r in readers)}"
            )
    if not readers:
        if not every:
            raise LabRepositoryError(
                f"{entry}: no project declares a jsonnet lab source, so no source reads this file"
            )
        raise LabRepositoryError(
            f"{entry}: no jsonnet lab source reads this file; the jsonnet sources: "
            f"{_labels(found.inputs for found in every)}"
        )
    if len(readers) > 1:
        raise LabRepositoryError(
            f"{entry}: {len(readers)} jsonnet lab sources read this file; choose one with "
            f"--source: {_labels(r.inputs for r in readers)}"
        )
    return readers[0]


@final
@dataclass(frozen=True, kw_only=True)
class _JsonnetSource:
    """One prepared ``jsonnet`` lab source and the entry files it reads."""

    inputs: "JsonnetSourceInputs"
    """The source's configuration and environment."""

    files: "list[Path]"
    """Its entry files, as :meth:`~otto.labs.sources.LabSourceState.lab_files` lists them."""


def _jsonnet_sources(projects: "Sequence[Project]") -> "list[_JsonnetSource]":
    """Return every prepared ``jsonnet`` lab source of *projects*, in declaration order."""
    from .jsonnet_source import JsonnetSourceInputs
    from .sources import prepared_lab_sources

    found: list[_JsonnetSource] = []
    for project in projects:
        for state in prepared_lab_sources(project):
            inputs = JsonnetSourceInputs.of_prepared(state.prepared)
            if inputs is not None:
                found.append(_JsonnetSource(inputs=inputs, files=state.lab_files()))
    return found


def _labels(sources: "Iterable[JsonnetSourceInputs]") -> str:
    """Return the labels of *sources*, quoted and comma-joined, as an error lists them."""
    return ", ".join(repr(s.env.label) for s in sources)


@final
@dataclass(frozen=True, kw_only=True)
class InventoryAge:
    """How old the records one inventory answered with are."""

    name: str
    """The inventory's name in the view (``default``, ``physical``)."""

    age: str
    """The snapshot's age as the stale notice states one (``45m``, ``31h``,
    :func:`~otto.inventory.cache.format_age`); ``live`` for an inventory with no
    snapshot cache, which reads its source on every lookup; ``not read`` for a
    cached one the build never consulted."""

    served_stale: bool
    """Whether the snapshot was served because the backend could not be reached."""


@final
@dataclass(frozen=True, kw_only=True)
class HostExplanation:
    """Why one host of a lab has each of its values: ``otto lab explain``'s answer."""

    host_id: str
    """The host explained."""

    source_label: str
    """The label of the lab source whose entry built the host (``sut/jsonnet#1``)."""

    entry: Path
    """The lab file, or Jsonnet entry file, the host's entry is in."""

    element: str
    """The name of the element the entry belongs to."""

    index: int
    """The entry's position in its element's ``hosts``."""

    rendered: bool
    """Whether :attr:`entry` is a Jsonnet entry, whose lab data is a template's."""

    provenance: "Mapping[str, Any] | None"
    """The element as its rendered document's provenance table describes it
    (``instance``, ``role``, ``host`` label), or ``None`` for a hand-written entry."""

    winners: "list[FieldWinner]"
    """Each value's winning layer, ordered by field."""

    inventories: "list[InventoryAge]"
    """Every inventory of the view, by name, with its snapshot's age."""

    def layer_label(self, winner: "FieldWinner") -> str:
        """Return how *winner*'s layer is shown for this host's entry."""
        return winner.layer.label(rendered=self.rendered)


def explain_host(
    *, host_id: str, lab: "Lab", view: "InventoryView", trace: "FieldTrace"
) -> HostExplanation:
    """Explain which layer supplied each value of host *host_id* of *lab*.

    *lab* and *view* are the ones the session built for ``-l``, and *trace*
    is what :func:`~otto.host.provenance.recording` collected while the
    session built them: the winners are read from the trace, the source,
    entry and provenance from the winning entry it recorded, and each
    inventory's age from its snapshot cache
    (:meth:`~otto.inventory.SnapshotCache.consumed_snapshot`, through
    :func:`~otto.inventory.snapshot_cache_of`). Nothing is loaded, built or
    fetched.

    Raises:
        LabRepositoryError: *lab* has no host *host_id* (naming the hosts it
            has), or the host was not built from a lab file entry (a declared
            container, or a source that builds its hosts without otto's lab
            file loader), so there is nothing to attribute.
    """
    from .sources import JSONNET_FILE_RULE

    if host_id not in lab.hosts:
        known = ", ".join(sorted(lab.hosts)) or "none"
        raise LabRepositoryError(f"no host {host_id!r} in lab {lab.name!r}; its hosts: {known}")
    entry = trace.entry_for(host_id=host_id)
    if entry is None:
        raise LabRepositoryError(
            f"host {host_id!r} in lab {lab.name!r} was not built from a lab file entry, "
            "so otto has no layers to show for it"
        )
    key = entry.key
    return HostExplanation(
        host_id=host_id,
        source_label=key.source,
        entry=key.origin,
        element=key.element,
        index=key.index,
        rendered=key.origin.suffix == JSONNET_FILE_RULE.suffix,
        provenance=entry.provenance,
        winners=entry.winners,
        inventories=[_age(name=name, view=view) for name in view.names()],
    )


def _age(*, name: str, view: "InventoryView") -> InventoryAge:
    """Return how old the records of *view*'s inventory *name* are, reading only memory."""
    from datetime import datetime, timezone

    from ..inventory.cache import format_age, snapshot_cache_of

    cache = snapshot_cache_of(inventory=view.select_name(name=name))
    if cache is None:
        return InventoryAge(name=name, age="live", served_stale=False)
    consumed = cache.consumed_snapshot()
    if consumed is None:
        return InventoryAge(name=name, age="not read", served_stale=False)
    return InventoryAge(
        name=name,
        age=format_age(datetime.now(timezone.utc) - consumed.fetched_at),
        served_stale=consumed.served_stale,
    )
