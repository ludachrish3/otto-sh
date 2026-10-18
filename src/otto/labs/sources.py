"""``[[lab.sources]]``: the envelope at settings parse, preparation after init.

Settings parsing checks only a source's envelope (its ``backend``, its
``name``, and that labels are unique) and keeps the rest of the entry as
raw options in a :class:`PendingLabSource`. A backend may be registered by
any project's ``init`` module, so a source is prepared (its options parsed by
its backend's config model) only once every project has registered:
:func:`prepared_lab_sources` refuses while an init import is running.
:func:`build_lab_sources` builds every project's prepared sources into the
process's one host source.

Kept import-light: settings parsing reads this module, and the backends'
modules are imported only when a source is prepared or built.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..config.project import Project
    from ..host.os_profile import ProfileContext
    from ..models.settings import LabConfigSpec, LabSourceSpec
    from ..registry import FrozenMap, Prepared
    from .protocol import LabRepository


@dataclass(frozen=True)
class LabSourceEnv:
    """What otto hands a lab-source backend beside its parsed configuration.

    A backend's config model reads it while parsing (``info.context["env"]``
    in a pydantic validator, to anchor a relative path), and its factory
    reads it as ``Configured.env``.
    """

    project_root: Path
    """The root of the project whose settings declare the source; relative paths anchor here."""

    label: str
    """The source's label, ``<project-name>/<name-or-default>``; it names the source in messages."""

    origin: str
    """Where the source was declared (a settings file), named in errors."""

    profiles: "ProfileContext"
    """The project data profiles of the projects selected with this source.

    A backend that builds hosts passes them on to the host factory
    (``profiles=`` on :func:`~otto.host.factory.create_host_from_dict`,
    :func:`~otto.host.factory.host_identity` and
    :func:`~otto.host.factory.validate_host_dict`, each called with the host's
    ``element=`` as well), so a host's ``os_type``
    may name a table another selected project declares."""


@dataclass(frozen=True)
class PendingLabSource:
    """One ``[[lab.sources]]`` entry as settings parsing leaves it: checked, not prepared."""

    backend: str
    """The backend name the entry selects."""

    label: str
    """The source's label, ``<project-name>/<name-or-default>``; it names the source in messages."""

    raw: "FrozenMap[str, object]"
    """The entry's options (every key but ``backend`` and ``name``), frozen, not yet parsed."""

    origin: str
    """The settings file that declares the source."""

    project_root: Path
    """The root of the declaring project; the backend anchors relative paths here."""


@dataclass(frozen=True)
class LabSourceState:
    """A pending source and, when its backend is registered, its preparation.

    Whether the source reads files has three answers: unknown (its backend
    is not registered, so nothing about it can be said), not file-backed (a
    backend whose configuration names no files), or the files it reads.
    """

    pending: PendingLabSource
    """The source as declared."""

    prepared: "Prepared[LabSourceEnv, LabRepository, None] | None"
    """The parsed configuration, or ``None`` while the backend is not registered."""

    def is_known(self) -> bool:
        """Return whether the source's backend was registered when it was prepared."""
        return self.prepared is not None

    def is_file_backed(self) -> bool:
        """Return whether the source reads lab files (False for an unknown source)."""
        return self._file_inputs() is not None

    def lab_files(self, *, visited: "set[Path] | None" = None) -> list[Path]:
        """Return the lab files the source reads, ``[]`` when it is not file-backed.

        The entry-to-files rule is the json backend's own
        (:func:`~otto.labs.json_repository.expand_lab_paths`: a directory
        contributes its ``lab.json``, a ``.json`` entry is the file, a glob
        expands to the sorted ``.json`` files it matches), so the cache and
        the doctor can never disagree with the backend about which files
        matter. Entries that resolve to no existing file contribute nothing,
        exactly as at load. *visited*, when given, collects every directory
        the enumeration entered.

        Raises:
            LabRepositoryError: The source is unknown; ask :meth:`is_known` first.
        """
        if self.prepared is None:
            from .errors import LabRepositoryError  # lazy: settings parsing imports this module

            raise LabRepositoryError(
                f"source {self.pending.label}: backend {self.pending.backend!r} is not "
                "registered, so the files it reads are unknown"
            )
        inputs = self._file_inputs()
        if inputs is None:
            return []
        from .json_repository import expand_lab_paths  # lazy: keep this module light

        return expand_lab_paths(inputs, visited=visited)

    def _file_inputs(self) -> "tuple[Path, ...] | None":
        facts = self.prepared.facts if self.prepared is not None else None
        inputs = getattr(facts, "file_inputs", None)
        return tuple(inputs) if inputs is not None else None


def compile_lab_sources(
    cfg: "LabConfigSpec | None", *, project_name: str, root: Path
) -> list[PendingLabSource]:
    """Check one project's lab-source envelopes, in declaration order.

    Only the envelope: a source's own options are parsed when it is prepared
    (:func:`prepared_lab_sources`), by its backend's config model.

    Raises ``ValueError`` (a settings error, surfaced at bootstrap) for
    duplicate labels within the project.
    """
    if cfg is None:
        return []
    from ..registry import FrozenMap  # lazy: keep settings parsing light

    origin = str(root / ".otto" / "settings.toml")
    pending = [
        PendingLabSource(
            backend=spec.backend,
            label=_label(spec, ordinal=i, project_name=project_name),
            raw=FrozenMap.freeze_json(dict(spec.model_extra or {})),
            origin=origin,
            project_root=root,
        )
        for i, spec in enumerate(cfg.sources, start=1)
    ]
    labels = [s.label for s in pending]
    if len(set(labels)) != len(labels):
        dupes = sorted({lbl for lbl in labels if labels.count(lbl) > 1})
        raise ValueError(
            f"project {project_name!r}: [[lab.sources]] labels must be unique; duplicated: {dupes}"
        )
    return pending


def _label(spec: "LabSourceSpec", *, ordinal: int, project_name: str) -> str:
    return f"{project_name}/{spec.name or f'{spec.backend}#{ordinal}'}"


def _refuse_during_init_import(caller: str) -> None:
    from ..registry import get_registering_project
    from .errors import LabRepositoryError

    project = get_registering_project()
    if project is not None:
        raise LabRepositoryError(
            f"{caller}() was called during project {project!r}'s init import; lab sources "
            "are prepared after every project registers its backends"
        )


def prepare_lab_sources(
    pending: "Sequence[PendingLabSource]", *, profiles: "ProfileContext"
) -> list[LabSourceState]:
    """Prepare *pending* against the backends registered now, uncached.

    Each source's :class:`LabSourceEnv` carries *profiles*. A source whose
    backend is not registered yields an unknown state; any other preparation
    failure raises.

    Raises:
        LabSourceConstructionError: A source's options do not parse, or its
            backend's config model cannot be imported.
    """
    from .registry import LAB_REPOSITORIES

    states: list[LabSourceState] = []
    for source in pending:
        if source.backend not in LAB_REPOSITORIES:
            states.append(LabSourceState(source, None))
            continue
        prepared = LAB_REPOSITORIES.prepare(
            source.backend,
            source.raw.thaw_json(),
            LabSourceEnv(source.project_root, source.label, source.origin, profiles),
            source=source.origin,
        )
        states.append(LabSourceState(source, prepared))
    return states


_PREPARED: "dict[tuple[Path, ProfileContext], tuple[int, tuple[PendingLabSource, ...], list[LabSourceState]]]" = {}  # noqa: E501 — one annotation string cannot wrap
"""Prepared sources by ``(project root, profiles)``: ``(table revision, declarations, states)``.

One entry per project and selection of projects (the profiles), so a build for the
project alone (its lab panel) and one for the whole selection do not displace
each other. A registration or a replaced backend (the revision), or settings
parsed again (the declarations), prepares afresh and REPLACES that entry."""


def prepared_lab_sources(
    project: "Project", *, profiles: "ProfileContext | None" = None
) -> list[LabSourceState]:
    """Return *project*'s sources, each prepared against the backends registered now.

    *profiles* are the project data profiles of the projects selected with *project*
    (default: *project*'s own), carried in each source's :class:`LabSourceEnv`;
    a caller holding several projects passes theirs, so it shares the entry the
    build that follows uses. Prepared on first read and cached until the
    backend table, the declarations or the profiles change.
    Refuses while an init import is running: registration is not complete
    then, so a source's backend may simply not be registered yet. Outside an
    init import it prepares, so a library caller with a hand-built project works.

    Raises:
        LabRepositoryError: Called during an init import.
        LabSourceConstructionError: A source's options do not parse.
    """
    _refuse_during_init_import("prepared_lab_sources")
    from .registry import LAB_REPOSITORIES

    if profiles is None:
        from ..host.os_profile import ProfileContext

        profiles = ProfileContext.from_projects([project])
    revision = LAB_REPOSITORIES.revision
    declared = tuple(project.lab_sources)
    key = (project.root, profiles)
    held = _PREPARED.get(key)
    if held is not None and held[0] == revision and held[1] == declared:
        return list(held[2])
    states = prepare_lab_sources(declared, profiles=profiles)
    _PREPARED[key] = (revision, declared, states)
    return list(states)


def build_lab_sources(
    projects: "Sequence[Project]", *, profiles: "ProfileContext | None" = None
) -> "LabRepository":
    """Construct the process's host source from every project's sources.

    *profiles* are the project data profiles every source's hosts resolve their
    ``os_type`` through; by default those of *projects* themselves
    (:meth:`~otto.host.os_profile.ProfileContext.from_projects`), so a host of
    one project may select a table another declares.

    Concatenates per-project ``[[lab.sources]]`` lists in the given (OTTOPATH)
    order — later sources override earlier ones inside the composite. Each
    source is built by its backend's factory, through
    :data:`~otto.labs.registry.LAB_REPOSITORIES`.

    ALWAYS a :class:`~otto.labs.composite.CompositeLabRepository`, whatever the
    source count: lab existence and the declared-but-memberless rule live in
    the composite and nowhere else (spec 2026-08-27 lab-definition-v2 §14), so
    returning the bare backend for a single source — as this did before v2 —
    exempted the commonest setup of all (one project, one json source) from both
    rules. Zero sources returns an empty composite whose ``load_lab`` fails
    loud with configuration guidance.

    Raises
    ------
    LabRepositoryError
        If called during an init import.
    LabSourceConstructionError
        If a source names an unregistered backend, its options do not parse,
        or its backend fails to build it.
    """
    _refuse_during_init_import("build_lab_sources")
    from ..host.os_profile import ProfileContext
    from .composite import CompositeLabRepository, LabSource
    from .registry import LAB_REPOSITORIES

    if profiles is None:
        profiles = ProfileContext.from_projects(projects)
    entries: "list[LabSource]" = []
    for project in projects:
        for state in prepared_lab_sources(project, profiles=profiles):
            source = state.pending
            prepared = state.prepared
            if prepared is None:
                # One code path for the message: the registry's own lookup error.
                prepared = LAB_REPOSITORIES.prepare(
                    source.backend,
                    source.raw.thaw_json(),
                    LabSourceEnv(source.project_root, source.label, source.origin, profiles),
                    source=source.origin,
                )
            entries.append(
                LabSource(label=source.label, repository=LAB_REPOSITORIES.build(prepared))
            )
    return CompositeLabRepository(entries)
