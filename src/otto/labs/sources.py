"""``[[lab.sources]]``: the envelope at settings parse, preparation after init.

Settings parsing checks only a source's envelope (its ``backend``, its
``name``, and that labels are unique) and keeps the rest of the entry as
raw options in a :class:`PendingLabSource`. A backend may be registered by
any project's ``init`` module, so a source is prepared (its options parsed by
its backend's config model) only once every project has registered:
:func:`prepared_lab_sources` refuses while an init import is running.
:func:`build_lab_sources` builds every project's prepared sources into the
process's one host source, and :func:`listing_sources` the one
``otto --list-labs`` lists, each source in its own project's profiles.

Kept import-light: settings parsing reads this module, and the backends'
modules are imported only when a source is prepared or built.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from typing_extensions import final

from ..utils import TEMPLATE_SUFFIX

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..config.project import Project
    from ..host.os_profile import ProfileContext
    from ..inventory.view import InventoryView
    from ..models.settings import LabConfigSpec, LabSourceSpec
    from ..registry import FrozenMap, Prepared
    from .composite import CompositeLabRepository, LabSource, LabStatusReport
    from .protocol import LabRepository


@final
@dataclass(frozen=True, kw_only=True)
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

    project_name: "str | None" = None
    """The declaring project's name (its settings' ``name``), or ``None`` when unknown.

    otto sets it whenever it prepares a source from a project's settings; an
    environment built by hand (a backend's own test, say) may leave it unset.
    The ``jsonnet`` backend passes it to its templates as ``otto.project``,
    and refuses to render without it."""


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

    project_name: "str | None" = None
    """The declaring project's name, carried into :attr:`LabSourceEnv.project_name`."""


@final
@dataclass(frozen=True, kw_only=True)
class LabFileRule:
    """How a file-backed source's ``paths`` entries name its lab files.

    A directory entry contributes its :attr:`directory_file`; an entry ending
    in :attr:`suffix` is the file; a glob expands to the sorted files it
    matches that end in :attr:`suffix`
    (:func:`otto.labs.documents.expand_lab_files` applies it).
    """

    suffix: str
    """A lab file's suffix, dot included, as :attr:`pathlib.PurePath.suffix` reads it.

    An entry with this suffix is the file itself.
    """

    directory_file: str
    """The file a directory entry contributes."""


JSON_FILE_RULE = LabFileRule(suffix=".json", directory_file="lab.json")
"""The json backend's rule, and the rule of every source whose facts name none."""

JSONNET_FILE_RULE = LabFileRule(suffix=TEMPLATE_SUFFIX, directory_file=f"lab{TEMPLATE_SUFFIX}")
"""The jsonnet backend's rule, reading ``.jsonnet`` entries and a directory's ``lab.jsonnet``."""


@final
@dataclass(frozen=True, kw_only=True)
class LabSourceFacts:
    """What otto learns about a source from its parsed configuration.

    A backend's config model returns it from ``prepared_facts()``; otto reads
    it to list the files the source reads, for the completion cache and the
    ``otto init`` doctor, without building the source.
    """

    file_inputs: tuple[Path, ...]
    """The anchored directory, file and glob entries the source reads."""

    file_rule: LabFileRule = JSON_FILE_RULE
    """How :attr:`file_inputs` name lab files; the json backend's rule by default."""

    key_roots: "tuple[Path, ...] | None" = None
    """Directories whose whole tree is a key path of the source's labs; ``None`` by default.

    For a source whose lab files are rendered (a Jsonnet entry and the files
    it imports, say), computed from its parsed configuration, ``()`` when it
    has no import root: :meth:`LabSourceState.key_paths` adds each root and
    every directory and file under it, and the packaged ``otto.libsonnet`` a
    render may import, so a change anywhere a lab file could read moves the
    completion cache's key without anything being evaluated. ``None`` for a
    source whose lab files are read as written.
    """


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

    def file_rule(self) -> LabFileRule:
        """Return the rule the source's entries name its lab files by.

        Its facts' :attr:`~LabSourceFacts.file_rule`, or :data:`JSON_FILE_RULE`
        when the facts carry none (a facts object that names only
        ``file_inputs``) or the source is not prepared.
        """
        rule = getattr(self._facts(), "file_rule", None)
        return JSON_FILE_RULE if rule is None else rule

    def lab_files(self, *, visited: "set[Path] | None" = None) -> list[Path]:
        """Return the lab files the source reads, ``[]`` when it is not file-backed.

        The entry-to-files rule is the backend's own (:meth:`file_rule`,
        applied by :func:`~otto.labs.documents.expand_lab_files`, the
        function the json backend reads its files through), so the cache and
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
        from .documents import expand_lab_files  # lazy: keep this module light

        return expand_lab_files(inputs, rule=self.file_rule(), visited=visited)

    def key_paths(self, *, visited: "set[Path] | None" = None) -> list[Path]:
        """Return every path whose change can change the source's labs.

        The completion cache's key set for the source: its lab files (with
        *visited*, the directories the enumeration entered), then, when its
        facts name :attr:`~LabSourceFacts.key_roots` (its files are
        rendered, under no root or several), every other file a render may
        read (:func:`~otto.templating.freshness.rendering_dependencies`):
        each root and every directory and file under it, walked once, and
        the packaged ``otto.libsonnet``, sorted. Computed from the
        configuration and the filesystem alone: no view, and nothing is
        evaluated. Raises what :meth:`lab_files` raises.
        """
        files = self.lab_files(visited=visited)
        roots = getattr(self._facts(), "key_roots", None)
        if roots is None or not files:
            return files
        # Lazy, and only for a rendered source: the walk is the templating
        # core's freshness rule, which no other source needs.
        from ..templating.freshness import rendering_dependencies

        return rendering_dependencies(entries=files, roots=tuple(roots))

    def _facts(self) -> object:
        """Return the prepared facts, ``None`` when unprepared or when the config model has none."""
        return self.prepared.facts if self.prepared is not None else None

    def _file_inputs(self) -> "tuple[Path, ...] | None":
        inputs = getattr(self._facts(), "file_inputs", None)
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
            raw=FrozenMap.freeze_json(value=dict(spec.model_extra or {})),
            origin=origin,
            project_root=root,
            project_name=project_name,
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
            name=source.backend,
            raw=source.raw.thaw_json(),
            env=LabSourceEnv(
                project_root=source.project_root,
                label=source.label,
                origin=source.origin,
                profiles=profiles,
                project_name=source.project_name,
            ),
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

        profiles = ProfileContext.from_projects(projects=[project])
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
    *, projects: "Sequence[Project]", profiles: "ProfileContext | None" = None
) -> "CompositeLabRepository":
    """Construct the process's host source from every project's sources.

    *profiles* are the project data profiles every source's hosts resolve their
    ``os_type`` through; by default those of *projects* themselves
    (:meth:`~otto.host.os_profile.ProfileContext.from_projects`), so a host of
    one project may select a table another declares.

    Concatenates per-project ``[[lab.sources]]`` lists in the given (OTTOPATH)
    order — later sources override earlier ones inside the composite. Each
    source is built by its backend's factory, through
    :data:`~otto.labs.registry.LAB_REPOSITORIES`, and carries its declaring
    project's root (:attr:`~otto.labs.LabSource.project_root`), so the
    composite calls it through the inventory view as that project sees it.

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
    from .composite import CompositeLabRepository

    if profiles is None:
        profiles = ProfileContext.from_projects(projects=projects)
    return CompositeLabRepository(
        [
            entry
            for project in projects
            for entry in _project_sources(project=project, profiles=profiles)
        ]
    )


def listing_sources(*, projects: "Sequence[Project]") -> "CompositeLabRepository":
    """Construct the host source ``otto --list-labs`` lists, each source in its own profiles.

    :func:`build_lab_sources`, except that each project's sources are built
    with :meth:`~otto.host.os_profile.ProfileContext.from_projects` of that
    project alone, exactly as each project's lab panel builds them on its
    own, so the listing shows what each project declares by itself. Dispatch
    (``--lab``) builds every source with the profiles of the whole selection
    instead, so a profile-dependent backend may expose a lab to dispatch that
    its project's panel omits.

    Raises
    ------
    LabRepositoryError
        If called during an init import.
    LabSourceConstructionError
        As :func:`build_lab_sources`.
    """
    _refuse_during_init_import("listing_sources")
    from ..host.os_profile import ProfileContext
    from .composite import CompositeLabRepository

    return CompositeLabRepository(
        [
            entry
            for project in projects
            for entry in _project_sources(
                project=project, profiles=ProfileContext.from_projects(projects=[project])
            )
        ]
    )


@final
@dataclass(frozen=True, kw_only=True)
class LabListing:
    """What listing every project's labs found: one report, or each project's own outcome."""

    report: "LabStatusReport | None"
    """The one status report over every project's sources, or ``None`` when it failed."""

    per_project: "Mapping[Path, LabStatusReport | Exception]"
    """Each project's own report, or the error it raised, by project root.

    Filled only when :attr:`report` failed (with
    ``otto.labs.views.LISTING_ERRORS``); empty otherwise."""

    def outcome(self, *, root: Path) -> "LabStatusReport | Exception":
        """Return the report the project at *root* is listed from, or why its own one failed.

        :attr:`report` when it exists, else that project's entry of
        :attr:`per_project`. *root* is the project's own root, as the
        listing's projects carry it.
        """
        return self.report if self.report is not None else self.per_project[root]


def project_lab_report(project: "Project", *, view: "InventoryView") -> "LabStatusReport":
    """Return the status report of *project*'s sources built alone, through *view*.

    The project's own sources (:func:`build_lab_sources` of *project* alone)
    asked for their report: what a project's lab panel shows when the report
    over every project's sources cannot be had.

    Raises:
        What :func:`build_lab_sources` and
        :meth:`~otto.labs.composite.CompositeLabRepository.lab_status_report`
        raise; ``otto.labs.views.LISTING_ERRORS`` for what a project holds.
    """
    return build_lab_sources(projects=[project]).lab_status_report(view=view)


def lab_listing(*, projects: "Sequence[Project]", view: "InventoryView") -> LabListing:
    """List every project's labs as ``otto --list-labs`` does: THE listing decision.

    One status report over every project's sources (:func:`listing_sources`)
    through *view*. When it fails with
    ``otto.labs.views.LISTING_ERRORS`` (a source cannot be built, or an
    unscoped source's status call failed), each project is listed on its own
    instead (:func:`project_lab_outcome`, through the same *view*), and a
    project whose own report fails keeps that error. ``otto --list-labs``
    formats the result, and the ``otto init`` doctor reads its project's rows
    from it, so the two never disagree.
    """
    from logging import getLogger
    from types import MappingProxyType

    from .views import LISTING_ERRORS

    try:
        report = listing_sources(projects=projects).lab_status_report(view=view)
    except LISTING_ERRORS as e:
        getLogger(__name__).debug(
            f"listing every project's labs failed, listing each on its own: {e}"
        )
    else:
        return LabListing(report=report, per_project=MappingProxyType({}))
    per_project: "dict[Path, LabStatusReport | Exception]" = {}
    for project in projects:
        per_project[project.root] = project_lab_outcome(project, view=view)
    return LabListing(report=None, per_project=MappingProxyType(per_project))


def project_lab_outcome(
    project: "Project", *, view: "InventoryView | None"
) -> "LabStatusReport | Exception":
    """Return :func:`project_lab_report` of *project*, or the listing error it raised.

    *view* defaults to a deferred view of *project* alone
    (:func:`~otto.inventory.deferred_inventory_view`), built inside the same
    catch. The errors kept are ``otto.labs.views.LISTING_ERRORS``; any other
    propagates.
    """
    from .views import LISTING_ERRORS

    try:
        if view is None:
            from ..inventory.config import deferred_inventory_view

            view = deferred_inventory_view(projects=[project])
        return project_lab_report(project, view=view)
    except LISTING_ERRORS as e:
        return e


def _project_sources(*, project: "Project", profiles: "ProfileContext") -> "list[LabSource]":
    """Build *project*'s prepared sources with *profiles*, in declaration order.

    Each source is built by its backend's factory, through
    :data:`~otto.labs.registry.LAB_REPOSITORIES`, and carries its declaring
    project's root.
    """
    from .composite import LabSource
    from .registry import LAB_REPOSITORIES

    entries: "list[LabSource]" = []
    for state in prepared_lab_sources(project, profiles=profiles):
        source = state.pending
        prepared = state.prepared
        if prepared is None:
            # One code path for the message: the registry's own lookup error.
            prepared = LAB_REPOSITORIES.prepare(
                name=source.backend,
                raw=source.raw.thaw_json(),
                env=LabSourceEnv(
                    project_root=source.project_root,
                    label=source.label,
                    origin=source.origin,
                    profiles=profiles,
                    project_name=source.project_name,
                ),
                source=source.origin,
            )
        entries.append(
            LabSource(
                label=source.label,
                repository=LAB_REPOSITORIES.build(prepared=prepared),
                project_root=source.project_root,
            )
        )
    return entries
