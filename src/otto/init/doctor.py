"""The ``otto init`` doctor: :func:`check_project` and the :class:`DoctorReport` it returns.

Every check runs the code otto itself loads a project with — the settings
compile, the lab loader's section, entry and duplicate rules, the host and
link specs, the inventory build, importlib's finder — so a project the doctor
passes is a project otto loads. The doctor never prints and never raises for
what a project holds: ``otto init`` renders the report.
"""

import dataclasses
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from typing_extensions import final, override

from .area_dirs import DEFAULT_EMBEDDED_GCOV_DIR
from .areas import (
    AREA_NAMES,
    SourceFiles,
    deferred_lab_sources,
    detect_areas,
    instruction_libs,
    kmodcov_entries,
    lab_file_groups,
    tests_dirs,
)
from .errors import InitInputError
from .settings_file import declared_init, settings_data, settings_path

if TYPE_CHECKING:
    # Annotation-only: importing ``otto.inventory`` for real pulls ~77 otto
    # modules (see ``otto/inventory/config.py``'s module docstring). Every
    # real use below is a function-local import.
    from ..config.project import Project
    from ..host.element import Element
    from ..host.inventory_ref import InventoryReference
    from ..host.os_profile import ProfileContext
    from ..inventory.config import InventoryDeclaration
    from ..inventory.protocol import Inventory
    from ..inventory.resolve import ResolvedEntry
    from ..inventory.view import InventoryView
    from ..labs.composite import LabStatusReport
    from ..labs.json_repository import LabDocument
    from ..labs.jsonnet_source import JsonnetSourceInputs
    from ..labs.protocol import LabStatus
    from ..vendoring import CheckResult

VerdictState = Literal["ok", "failed", "absent", "blocked"]

_BLOCKED = "settings did not compile"
_ABSENT = {
    "settings": "no .otto/settings.toml",
    "schemas": "no .otto/schemas/*.schema.json",
    "lab": "no lab file found",
    "tests": "no test_*.py found",
    "instructions": "no init modules declared",
    "kmodcov": "no kmodcov dev tool declared and no vendored library",
    "embedded-gcov": "no embedded-gcov patch under " + DEFAULT_EMBEDDED_GCOV_DIR,
}


@final
@dataclasses.dataclass(frozen=True)
class AreaVerdict:
    """The doctor's verdict on one area.

    ``problems`` is non-empty only when ``state`` is ``failed``; ``detail`` is
    the one-line explanation an ``absent`` or ``blocked`` verdict carries.
    """

    name: str
    state: VerdictState
    problems: list[str] = dataclasses.field(default_factory=list)
    detail: str = ""


@final
@dataclasses.dataclass(frozen=True, kw_only=True)
class InventoryLabel:
    """One inventory the doctor resolved: its name, its label and its credentials store's."""

    name: str
    """The inventory's name, ``default`` or an ``[[inventories]]`` entry's."""
    label: str
    """The inventory's own ``label``, naming what answered."""
    creds_label: str | None
    """The credentials store's ``label``, or ``None`` when the inventory has none."""


@final
@dataclasses.dataclass(frozen=True)
class DoctorReport:
    """Everything :func:`check_project` found. Warnings and labels never fail it.

    :attr:`inventory_label` and :attr:`creds_label` are ``default``'s, as
    before named inventories; :attr:`inventories` holds every inventory the
    doctor checked and resolved, ``default`` included, sorted by name.
    """

    verdicts: list[AreaVerdict]
    warnings: list[str]
    inventory_label: str | None
    creds_label: str | None
    inventories: list[InventoryLabel] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no area failed (``absent`` and ``blocked`` do not fail)."""
        return all(verdict.state != "failed" for verdict in self.verdicts)

    def verdict(self, *, name: str) -> AreaVerdict:
        """Return the verdict for area *name*."""
        return next(verdict for verdict in self.verdicts if verdict.name == name)


def check_project(root: Path, *, catalog: "InventoryView | None" = None) -> DoctorReport:
    """Check every area of the project at *root*; never prints, never raises for its content.

    Inventories: ``default`` is the project's own tables, else the user
    file's, as before named inventories (the doctor-versus-dispatch
    difference this keeps is tracked in #1186). Each name the project's own
    file declares is built from that file alone, so ``otto init`` checks
    what it just wrote. Any other name a lab file references comes from
    *catalog*, the active projects' view
    (:func:`~otto.inventory.deferred_inventory_view`), one name at a time;
    without one, from the user file's ``[[inventories]]``. A name that fails
    to build, a catalog conflict included, is one problem: in the lab
    verdict, or, when there is no lab to validate, in the settings verdict
    for each name the project's own file declares. Every finding is
    attributed to its inventory's name once more than ``default`` is
    checked.

    Templating: the doctor's own checks never render a template. An
    inventory they would check that renders a Jsonnet file
    (:func:`~otto.inventory.json_backend.is_templated_inventory`) is left
    unchecked, with one warning naming it, and the host entries that
    reference it with it. A ``jsonnet`` lab source's entries are not parsed
    here. When *root* is one of *catalog*'s projects
    (:func:`catalog_membership`), its jsonnet sources' labs are listed and
    loaded by dispatch's own functions through *catalog*
    (:func:`~otto.labs.sources.lab_listing`, as ``otto --list-labs`` lists,
    and :func:`~otto.session.build_lab`, as ``otto --lab`` loads), each
    failure one lab problem in dispatch's words; otherwise each entry gets
    one warning saying why it was not rendered.

    settings: the loader's own compile (:func:`otto.config.project.validate_settings`),
    then the check the loader runs on each ``[os_profiles]`` table after init
    (:func:`otto.host.os_profile.check_data_profiles`; this doctor imports no
    init module, so a table over a class an init module registers fails here).
    schemas: ``schema_drift``. lab: each source's options as its backend
    parses them, then the runtime loader's section, entry and duplicate
    rules, host entries resolved against the inventory, links via
    ``LinkSpec`` — or ``blocked`` when the settings do not compile, so their
    error is shown once. A source whose backend an init module registers is
    named in the lab detail as checked after init. tests: a light
    check (dirs, ``test_*.py``, ``ast.parse``). instructions: ``absent``
    when ``init`` is omitted or empty (a project with no init modules is
    legitimate), ``blocked`` when the settings are not valid TOML, else
    every entry must resolve. kmodcov: a declared ``source`` must hold the
    library. embedded-gcov: present means the patch is under the default
    directory, and it has nothing to fail. Warnings (lab, inventory, kmodcov and
    embedded-gcov drift) never fail the report.

    Raises:
        InitInputError: *root* is not a directory (``field="root"``).
    """
    from ..config.project import validate_settings
    from ..host.inventory_ref import DEFAULT_INVENTORY
    from ..models.jsonschema import schema_drift

    if not root.is_dir():
        raise InitInputError(f"{root} is not a directory", field="root")
    # One inventory per call, not per asker: the lab check, the warnings and
    # the labels each ask the same view, whose default is _inventory_for's
    # answer from this cache and whose named members it keeps beside it.
    # Owned here, never a module global.
    cache: "dict[Path, Inventory | Exception | None]" = {}
    present = set(detect_areas(root=root))
    settings_problems = validate_settings(root) if "settings" in present else []
    profiles: "ProfileContext | None" = None
    if "settings" in present and not settings_problems:
        from ..config.project import Project
        from ..host.os_profile import ProfileContext

        project = Project(root=root)
        settings_problems = _data_profile_problems(project)
        profiles = None if settings_problems else ProfileContext.from_projects(projects=[project])
    lab_blocked = bool(settings_problems)
    lab_unprepared = [] if lab_blocked else _lab_preparation_problems(root)
    deferred = "" if lab_blocked or lab_unprepared else _deferred_detail(root)
    parsed = _ParsedLab() if lab_blocked or lab_unprepared else _parse_lab_documents(root)
    doctor = _view_for(root, cache, catalog=catalog, references=parsed.references())
    # The lab verdict reports every checked inventory that fails to build
    # (_validate_lab). Without a lab to validate, the project's own
    # declarations are still checked, once, as settings problems: a project
    # that only supplies inventories to its siblings must not pass broken.
    lab_validated = not lab_blocked and not lab_unprepared and "lab" in present
    templating = (
        _templating_rows(
            root, catalog=catalog, sources=parsed.jsonnet, reported=_own_failures(doctor)
        )
        if parsed.jsonnet
        else _TemplatingRows()
    )
    unbuilt = (
        []
        if lab_validated or lab_blocked
        else list(_build_failures(doctor, names=_own_names(doctor)).values())
    )
    verdicts: list[AreaVerdict] = []
    for name in AREA_NAMES:
        if name == "settings":
            verdict = (
                _checked(name, [*settings_problems, *unbuilt])
                if name in present or unbuilt
                else None
            )
        elif name == "lab" and lab_blocked:
            verdict = AreaVerdict(name, "blocked", detail=_BLOCKED)
        elif name == "instructions":
            verdict = _instructions_verdict(root)
        elif name not in present:
            verdict = None
        elif name == "schemas":
            verdict = _checked(name, schema_drift(root / ".otto" / "schemas"))
        elif name == "lab" and lab_unprepared:
            verdict = _checked(name, lab_unprepared)
        elif name == "lab":
            problems = [*_validate_lab(doctor, parsed, profiles), *templating.problems]
            verdict = _checked(name, problems, detail=deferred)
        elif name == "tests":
            verdict = _checked(name, _validate_tests(root))
        else:
            verdict = _checked(name, _AREA_VALIDATORS[name](root))
        if verdict is None and name == "lab" and deferred:
            verdict = AreaVerdict(name, "absent", detail=f"{_ABSENT[name]}; {deferred}")
        verdicts.append(verdict or AreaVerdict(name, "absent", detail=_ABSENT[name]))
    warnings = [] if lab_blocked or lab_unprepared else _lab_warnings(doctor, parsed)
    warnings.extend(templating.warnings)
    warnings.extend(_kmodcov_warnings(root))
    warnings.extend(_embedded_gcov_warnings(root))
    inventories = _labels(doctor)
    default = next((each for each in inventories if each.name == DEFAULT_INVENTORY), None)
    return DoctorReport(
        verdicts,
        warnings,
        None if default is None else default.label,
        None if default is None else default.creds_label,
        inventories,
    )


def _checked(name: str, problems: list[str], *, detail: str = "") -> AreaVerdict:
    return (
        AreaVerdict(name, "failed", problems)
        if problems
        else AreaVerdict(name, "ok", detail=detail)
    )


def _data_profile_problems(project: "Project") -> list[str]:
    """Check *project*'s ``[os_profiles]`` tables as the loader does after init."""
    from ..config.scope import TOML_SETTINGS_PATH
    from ..host.os_profile import check_data_profiles

    try:
        check_data_profiles(projects=[project])
    except ValueError as e:
        return [f"{project.root / TOML_SETTINGS_PATH}: {e}"]
    return []


def _lab_preparation_problems(root: Path) -> list[str]:
    """Return why *root*'s lab sources do not prepare, or ``[]`` when they do.

    A source's options are parsed by its backend's config model when the
    source is prepared, not by the settings compile, so a ``[[lab.sources]]``
    entry the settings accept can still be refused here: a json source
    without ``paths``, an unknown key, a path under an unknown user's home.
    Reported as the lab area's failure, once, rather than raised out of the
    lab check or the warnings pass.
    """
    try:
        lab_file_groups(root)
    except ValueError as e:
        return [str(e)]
    return []


def _deferred_detail(root: Path) -> str:
    """Name each lab source the doctor cannot check: its backend registers in an init module."""
    deferred = deferred_lab_sources(root)
    return "; ".join(f"{source}: checked after init" for source in deferred)


def _instructions_verdict(root: Path) -> AreaVerdict:
    """``absent`` when no init module is declared, else every declared one must resolve.

    A project with no init modules is legitimate (spec 2026-10-03 init-library
    §2), so an omitted or empty ``init`` never fails. A settings file that is
    not valid TOML is the settings area's problem; this verdict reads
    ``blocked`` rather than repeating it, and so does an ``init`` that is not
    a list or holds anything but strings, which the settings compile refuses. Not gated on
    detection: a declared init that resolves nowhere is undetected, and it is
    exactly what this verdict must fail.
    """
    if not settings_path(root).is_file():
        return AreaVerdict("instructions", "absent", detail=_ABSENT["instructions"])
    data = settings_data(root)
    if data is None:
        return AreaVerdict("instructions", "blocked", detail=_BLOCKED)
    modules = data.get("init", [])
    if not isinstance(modules, list):
        return AreaVerdict("instructions", "blocked", detail=_BLOCKED)
    if not modules:
        return AreaVerdict("instructions", "absent", detail=_ABSENT["instructions"])
    declared = declared_init(root)
    if declared is None:
        return AreaVerdict("instructions", "blocked", detail=_BLOCKED)
    return _checked("instructions", _validate_instructions(root, declared))


def _inventory_for(
    root: Path, cache: "dict[Path, Inventory | Exception | None] | None" = None
) -> "Inventory | None":
    """Return the inventory bootstrap would build for *root*: its own override, else the user file.

    Raises ``InventoryError`` / ``ValueError`` for a broken declaration — the
    caller reports that as a problem naming the file; the doctor must never
    traceback on a broken ``[inventory]`` table or user settings file.

    *cache* memoises the result (inventory, ``None``, or the raised
    exception) per *root* — one :func:`check_project` asks this up to three
    times (the "lab" area's own validation, the warnings pass, the labels),
    and while today's construction is I/O-free, a real backend (the netbox
    one, still to land) would otherwise pay a whole-set fetch three times
    over. A plain dict an owning caller creates and threads through by hand —
    never a module global, which would outlive one call and leak across
    processes or tests that import this module.
    """
    if cache is not None and root in cache:
        cached = cache[root]
        if isinstance(cached, Exception):
            raise cached
        return cached

    from ..config.user_settings import load_user_settings
    from ..inventory.config import build_inventory_from_declarations
    from ..inventory.errors import InventoryError

    try:
        declaration = _declaration_of(root)
        result = build_inventory_from_declarations(
            declarations=[] if declaration is None else [declaration],
            user_settings=load_user_settings(),
        )
    except (InventoryError, ValueError) as e:
        if cache is not None:
            cache[root] = e
        raise
    if cache is not None:
        cache[root] = result
    return result


def _declaration_of(root: Path) -> "InventoryDeclaration | None":
    """Return the inventory tables *root*'s own settings file declares, or ``None``.

    The one declaration reader bootstrap uses too
    (:func:`~otto.inventory.config.inventory_declaration`).

    Raises:
        ValueError: A malformed table, naming the file and key; the doctor
            reports it as a problem.
    """
    from ..inventory.config import inventory_declaration

    return inventory_declaration(
        data=settings_data(root) or {}, root=root, origin=str(settings_path(root))
    )


@final
class _DoctorInventories(Mapping[str, "Inventory"]):
    """The inventories one :func:`check_project` checks, by name, each built once on first use.

    ``default`` is :func:`_inventory_for`'s answer. A name the project's own
    file declares is built from that file alone. Any other name comes from
    the *catalog* view when there is one, else from the user file's
    ``[[inventories]]``. A build's result, ``None`` (not declared) or error
    is kept, so every asker sees one answer and a broken name never fails
    another. Iteration lists ``default`` and the project's own names, plus
    each other name a lab file references, when they are declared: the
    names the doctor checks. Membership answers for any name.
    """

    def __init__(
        self,
        *,
        root: Path,
        cache: "dict[Path, Inventory | Exception | None]",
        declaration: "InventoryDeclaration | None",
        catalog: "InventoryView | None",
        referenced: "list[str]",
    ) -> None:
        from ..host.inventory_ref import DEFAULT_INVENTORY
        from ..models.settings import entry_name

        self._root = root
        self._cache = cache
        self.declaration = declaration
        """The project's own inventory tables, or ``None`` when it declares none."""
        self._catalog = catalog
        entries = [] if declaration is None else declaration.inventories
        self.own = sorted(
            {n for e in entries if isinstance(n := entry_name(e), str)} - {DEFAULT_INVENTORY}
        )
        """The names other than ``default`` the project's own file declares."""
        self._referenced = referenced
        self._members: "dict[str, Inventory | Exception | None]" = {}

    def _build(self, name: str) -> "Inventory | None":
        """Build *name* on its route; raise what that route raises.

        ``default``, an own name and a user-file name are otto's own builds,
        and raise ``InventoryError``/``ValueError`` for what a project holds,
        as :func:`_inventory_for` always has. The catalog is any view, whose
        per-name build may raise anything (a third-party backend's
        construction): that one route catches every error, so a catalog
        failure is a problem row and never escapes :func:`check_project`.
        """
        from ..config.user_settings import load_user_settings
        from ..host.inventory_ref import DEFAULT_INVENTORY
        from ..inventory.config import build_named_inventory_from_declarations

        if name == DEFAULT_INVENTORY:
            return _inventory_for(self._root, self._cache)
        if name in self.own and self.declaration is not None:
            return build_named_inventory_from_declarations(
                name=name, declarations=[self.declaration], user_settings=None
            )
        if self._catalog is not None:
            return _catalog_member(self._catalog, name)
        try:
            user = load_user_settings()
        except ValueError:
            return None  # the problem _inventory_for reports, naming the file
        return build_named_inventory_from_declarations(
            name=name, declarations=[], user_settings=user
        )

    def member(self, name: str) -> "Inventory | Exception | None":
        """Return *name*'s inventory, ``None`` when undeclared, or why it failed to build.

        An inventory that renders a template
        (:func:`~otto.inventory.json_backend.is_templated_inventory`) is
        never handed to the doctor's checks: it is a :class:`_NotRendered`
        naming it, which :meth:`not_rendered` reports and :meth:`failure`
        does not.
        """
        from ..inventory.errors import InventoryError
        from ..inventory.json_backend import is_templated_inventory

        if name not in self._members:
            try:
                built = self._build(name)
            except (InventoryError, ValueError) as e:
                self._members[name] = e
            else:
                self._members[name] = (
                    _NotRendered(_NOT_DISCOVERED.format(name=name))
                    if built is not None and is_templated_inventory(built)
                    else built
                )
        return self._members[name]

    def failure(self, name: str) -> "Exception | None":
        """Return why *name* failed to build, or ``None`` (a templated inventory did not fail)."""
        member = self.member(name)
        return member if isinstance(member, Exception) and not _is_not_rendered(member) else None

    def not_rendered(self, name: str) -> "str | None":
        """Return *name*'s row when it is a templated inventory the checks skip, else ``None``."""
        member = self.member(name)
        return str(member) if _is_not_rendered(member) else None

    def skipped(self, names: "list[str]") -> set[str]:
        """Return the names of *names* the checks do not resolve: failed or templated."""
        return {
            name
            for name in names
            if self.failure(name) is not None or self.not_rendered(name) is not None
        }

    @property
    def declared(self) -> list[str]:
        """Return ``default`` and the project's own names, when declared: whose orphans count."""
        from ..host.inventory_ref import DEFAULT_INVENTORY

        return [name for name in self if name == DEFAULT_INVENTORY or name in self.own]

    @override
    def __getitem__(self, name: str) -> "Inventory":
        member = self.member(name)
        if isinstance(member, Exception):
            raise member
        if member is None:
            raise KeyError(name)
        return member

    @override
    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.member(name) is not None

    @override
    def __iter__(self) -> Iterator[str]:
        from ..host.inventory_ref import DEFAULT_INVENTORY

        names = {DEFAULT_INVENTORY, *self.own, *self._referenced}
        return iter(sorted(name for name in names if name in self))

    @override
    def __len__(self) -> int:
        return sum(1 for _ in self)


_NOT_DISCOVERED = "{name}: not rendered by the doctor's own checks; see the templating rows"
"""The warning of a templated inventory the doctor's own checks leave unchecked."""


@final
class _NotRendered(Exception):  # noqa: N818 - a kept outcome, not an error
    """A templated inventory the doctor's checks skip, kept in its place; its text is the row."""


def _is_not_rendered(member: object) -> bool:
    """Return whether *member* is a templated inventory the checks skip."""
    return isinstance(member, _NotRendered)


def _catalog_member(catalog: "InventoryView", name: str) -> "Inventory | None":
    """Return *catalog*'s inventory named *name*, or ``None`` when it has none.

    The doctor's one catch of every error: a catalog is any view, and its
    per-name build may raise whatever a third-party backend raises. Such an
    error is re-raised as an ``InventoryError`` naming its type, so it is one
    problem row like any other build failure.

    Raises:
        otto.inventory.errors.InventoryError: The name failed to build.
    """
    from ..inventory.errors import InventoryError

    try:
        return catalog.inventories.get(name)
    except InventoryError:
        raise
    except Exception as e:  # any backend's error is a problem row, never a traceback
        raise InventoryError(f"{type(e).__name__}: {e}") from e


@final
@dataclasses.dataclass(frozen=True, kw_only=True)
class _DoctorView:
    """The doctor's inventory view of one project, and the inventories behind it."""

    view: "InventoryView"
    """The view, as the project sees it (bare references refused under D1)."""
    inventories: _DoctorInventories
    """The view's inventories, for each name's build failure and the declared names."""


def _view_for(
    root: Path,
    cache: "dict[Path, Inventory | Exception | None]",
    *,
    catalog: "InventoryView | None" = None,
    references: "list[InventoryReference] | None" = None,
) -> _DoctorView:
    """Return the inventory view the doctor checks *root* with; builds and reads nothing yet.

    ``default`` comes from :func:`_inventory_for` over *cache*, unchanged; the
    other names are described under :class:`_DoctorInventories`. *references*
    are the lab files' well-formed references: each qualified one names an
    inventory to check. The project's own settings decide whether it refuses
    bare references (:func:`~otto.inventory.refuses_bare`). A malformed
    declaration reads as none here: :func:`_inventory_for` reports it, from
    the same reader.
    """
    from ..host.inventory_ref import DEFAULT_INVENTORY
    from ..inventory.view import InventoryView, refuses_bare

    try:
        declaration = _declaration_of(root)
    except ValueError:
        declaration = None
    referenced = sorted(
        {r.name for r in references or [] if r.qualified and r.name != DEFAULT_INVENTORY}
    )
    inventories = _DoctorInventories(
        root=root, cache=cache, declaration=declaration, catalog=catalog, referenced=referenced
    )
    refusing: dict[Path, str] = {}
    if declaration is not None and refuses_bare(
        inventory=declaration.table, inventories=declaration.inventories
    ):
        refusing[root] = declaration.origin
    view = InventoryView(inventories=inventories, bare_refused_by=refusing)
    return _DoctorView(view=view.for_project(root=root), inventories=inventories)


def _attributed(*, view: "InventoryView", name: str, text: str) -> str:
    """Return *text* attributed to inventory *name*: ``<name>: <text>``, the one prefix.

    A view that checks ``default`` alone (or nothing) leaves *text* as it
    was before named inventories.
    """
    from ..host.inventory_ref import DEFAULT_INVENTORY

    if set(view.names()) <= {DEFAULT_INVENTORY}:
        return text
    return f"{name}: {text}"


def _labels(doctor: _DoctorView) -> list[InventoryLabel]:
    """Return each checked inventory that resolves, with its label and creds-store label.

    A name that fails to build has no label: it is already a problem
    (:func:`_build_failures`), and repeating it would be noise.
    The creds label names the store when one resolves (spec 2026-09-06 §7.1).
    """
    from ..inventory.creds import creds_store_label

    labels: list[InventoryLabel] = []
    for name in doctor.view.names():
        if name in doctor.inventories.skipped([name]):
            continue
        inventory = doctor.inventories[name]
        creds = creds_store_label(inventory=inventory)
        labels.append(InventoryLabel(name=name, label=inventory.label, creds_label=creds))
    return labels


@final
@dataclasses.dataclass(frozen=True)
class _ParsedLab:
    """The lab files :func:`_parse_lab_documents` read: their problems and parsed documents."""

    problems: list[str] = dataclasses.field(default_factory=list)
    documents: "list[LabDocument]" = dataclasses.field(default_factory=list)
    jsonnet: list[SourceFiles] = dataclasses.field(default_factory=list)
    """The ``jsonnet`` sources, their entries listed and never read here."""

    def template_inventories(self) -> set[str]:
        """Return the inventories some ``jsonnet`` source declares it reads (its ``inventories``).

        Read from the source's parsed settings, never by rendering.
        """
        inputs = (_jsonnet_inputs(group) for group in self.jsonnet)
        return {name for each in inputs if each is not None for name in each.config.inventories}

    def references(self) -> "list[InventoryReference]":
        """Return every well-formed inventory reference the documents' host entries state."""
        from ..inventory.doctor import referenced_references

        return referenced_references(element_lists=(d.elements for d in self.documents))


def _jsonnet_inputs(group: SourceFiles) -> "JsonnetSourceInputs | None":
    """Return *group*'s configuration and environment when it is otto's ``jsonnet`` source.

    Its parsed settings, the one reader the listing rows and the read
    inventories share: nothing is rendered.
    """
    from ..labs.jsonnet_source import JsonnetSourceInputs

    return None if group.state is None else JsonnetSourceInputs.of_prepared(group.state.prepared)


def _parse_lab_documents(root: Path) -> _ParsedLab:
    """Parse every lab file the settings name: their problems and parsed documents.

    Each source's files are read by the SAME source read the runtime loader
    runs (:func:`otto.labs.documents.read_source`): the one lab-document
    reader, the section shape, the ``labs`` table, the ``elements`` entries
    and the in-source duplicate rules, so the doctor cannot drift from what
    otto accepts: an undecodable file, an unknown ``routes`` section, a v1
    top-level ``hosts`` array, a host entry carrying a hoisted key and a lab
    declared twice within one source are all rejected here exactly as they
    are at load, in the loader's words. A file with a problem is reported and
    left out of the documents the warnings pass sees — a half-parsed file
    would only produce warnings about its own breakage.

    The source read runs per SOURCE (:func:`lab_file_groups`), not over the
    flat file list: two sources may each declare the same lab, which is how
    ``[[lab.sources]]`` layering works, and rejecting that would fail projects
    otto loads happily.

    Only a file otto itself decodes (:func:`otto.labs.documents.otto_decodes`)
    is read: a backend whose file rule names another format reads its own
    files, so a ``lab.yaml`` is never reported as malformed JSON. A
    ``jsonnet`` source (:attr:`~otto.init.areas.SourceFiles.is_jsonnet`) is
    listed for the templating rows and never read: its entries render only
    through dispatch (:func:`_templating_rows`).
    """
    from ..labs import documents as lab_documents

    problems: list[str] = []
    documents: list[LabDocument] = []
    jsonnet: list[SourceFiles] = []
    for group in lab_file_groups(root):
        if group.is_jsonnet:
            jsonnet.append(group)
            continue
        decoded = [f for f in group.files if f.is_file() and lab_documents.otto_decodes(f)]
        read = lab_documents.read_source(decoded)
        problems.extend(problem.reason for problem in read.problems)
        documents.extend(read.documents)
    return _ParsedLab(problems, documents, jsonnet)


# ── Templating: the rows dispatch's own functions give ────────────────────────


@final
@dataclasses.dataclass(frozen=True, kw_only=True)
class CatalogMembership:
    """Whether a project is one of an active catalog's projects: THE templating eligibility.

    Exactly one of :attr:`projects` and :attr:`refusal` is set.
    """

    project: "Project | None"
    """The catalog's own project rooted at the inspected root, in the catalog's spelling."""
    projects: "list[Project] | None"
    """Every project the catalog was built over, when the root is one of them."""
    refusal: "str | None"
    """Why the root's templates are not rendered, when it is not a member."""


def catalog_membership(catalog: "InventoryView | None", *, root: Path) -> CatalogMembership:
    """Return whether the project at *root* is one of *catalog*'s projects; never raises.

    *catalog* must be otto's active catalog
    (:func:`~otto.inventory.deferred_inventory_view`), whose projects it
    keeps (:func:`~otto.inventory.config.deferred_catalog_projects`), every
    one of them, those declaring no inventory included. Membership is
    :func:`~otto.config.project.project_at`'s, the predicate ``otto init``
    picks the catalog with, so a symlink spelling of a member's root is a
    member. Membership only: whether the root's declarations are the ones
    the catalog read is not compared.
    """
    from ..config.project import project_at
    from ..inventory.config import deferred_catalog_projects

    if catalog is None:
        return _refused("the project is not one of the active projects")
    projects = deferred_catalog_projects(catalog)
    if projects is None:
        return _refused("the inventory view is not otto's active catalog")
    project = project_at(projects, root=root)
    if project is None:
        return _refused("the project is not one of the catalog's projects")
    return CatalogMembership(project=project, projects=projects, refusal=None)


def _refused(refusal: str) -> CatalogMembership:
    return CatalogMembership(project=None, projects=None, refusal=refusal)


def templated_statuses(
    report: "LabStatusReport", *, project_root: Path, sources: "list[SourceFiles]"
) -> "list[LabStatus]":
    """Return the status of each lab one of *sources*, a project's jsonnet sources, declares.

    *report* is a listing's (:meth:`~otto.labs.sources.LabListing.outcome`)
    and *project_root* the project's root as the report's sources carry it.
    A source is told by its label and its project's root, as the report
    names the sources declaring each lab.
    """
    labels = _source_labels(sources)
    return [
        status
        for status in report.statuses_declared_in(project_root=project_root)
        if any(
            ref.project_root == project_root and ref.label in labels
            for ref in report.declared_by.get(status.name, [])
        )
    ]


def _source_labels(sources: "list[SourceFiles]") -> set[str]:
    return {group.state.pending.label for group in sources if group.state is not None}


@final
@dataclasses.dataclass(frozen=True, kw_only=True)
class TemplatedOutcome:
    """What dispatch's listing says of a project's jsonnet sources: THE templated read."""

    membership: CatalogMembership
    """Whether the project is one of the catalog's projects; nothing was listed when not."""
    outcome: "LabStatusReport | Exception | None"
    """The report the project is listed from, or why listing failed; ``None`` for a non-member."""
    statuses: "list[LabStatus]"
    """The status of each lab one of the project's jsonnet sources declares, by the report."""


def templated_outcome(
    view: "InventoryView | None", *, root: Path, sources: "list[SourceFiles]"
) -> TemplatedOutcome:
    """Return how ``otto --list-labs`` lists *sources*, the jsonnet sources of *root*'s project.

    When *root* is a member of *view* (:func:`catalog_membership`), the labs
    are listed as ``otto --list-labs`` lists them
    (:func:`~otto.labs.sources.lab_listing` over the catalog's projects,
    through *view*), the project's report read under the catalog's own
    spelling of its root (:meth:`~otto.labs.sources.LabListing.outcome`),
    and the statuses kept are those :func:`templated_statuses` picks. A
    listing that raises anything is that error, never a traceback: a
    third-party source may raise whatever it raises. A cancelled render,
    however a source wrapped it (``otto.labs.drops.is_timeout``), is
    the operation's, not the listing's, and propagates. Otherwise nothing is
    listed or evaluated. The doctor's templating rows and the Next steps
    panel both read this; neither lists on its own.
    """
    from ..labs.sources import lab_listing

    membership = catalog_membership(view, root=root)
    if view is None or membership.project is None or membership.projects is None:
        return TemplatedOutcome(membership=membership, outcome=None, statuses=[])
    from ..labs.drops import is_timeout

    project_root = membership.project.root
    try:
        listing = lab_listing(projects=membership.projects, view=view)
    except Exception as e:  # any backend's error is a row, never a traceback
        if is_timeout(e):
            raise  # the operation was given up on: never a row
        return TemplatedOutcome(membership=membership, outcome=e, statuses=[])
    outcome = listing.outcome(root=project_root)
    if isinstance(outcome, Exception) and is_timeout(outcome):
        raise outcome  # the project's read was given up on: never a row
    statuses = (
        []
        if isinstance(outcome, Exception)
        else templated_statuses(outcome, project_root=project_root, sources=sources)
    )
    return TemplatedOutcome(membership=membership, outcome=outcome, statuses=statuses)


def _error_text(error: Exception) -> str:
    """Return *error* as a row states it: otto's own words, or a foreign error's type and text.

    The errors dispatch itself words (``otto.labs.views.LISTING_ERRORS``)
    read as they are; any other is named by its type, as
    :func:`_catalog_member` names a backend's.
    """
    from ..labs.views import LISTING_ERRORS

    return str(error) if isinstance(error, LISTING_ERRORS) else f"{type(error).__name__}: {error}"


_NOT_RENDERED = (
    "{entry}: not rendered: {refusal}; run otto init from inside an active project, "
    "or see otto --list-labs"
)
"""The warning of a jsonnet entry the doctor cannot have dispatch render."""

_NO_CATALOG = (
    "{entry}: not rendered: no protected-file catalog could be built, because a settings "
    "file this report shows a problem with cannot be read"
)
"""The warning of a jsonnet entry under a catalog that refuses to render (an unreadable user file).

The doctor's own checks already report the file, so the warning does not
restate its error."""


@final
@dataclasses.dataclass(frozen=True)
class _TemplatingRows:
    """The templating rows: lab problems in dispatch's words, and not-rendered warnings."""

    problems: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)


def _templating_rows(
    root: Path,
    *,
    catalog: "InventoryView | None",
    sources: "list[SourceFiles]",
    reported: "set[str]",
) -> _TemplatingRows:
    """Return the rows of *root*'s jsonnet *sources*, from dispatch's own functions.

    The doctor renders nothing itself. When *root* is a member of *catalog*
    (:func:`catalog_membership`), the labs are listed as ``otto
    --list-labs`` lists them (:func:`templated_outcome`), and each lab a
    jsonnet source of *root* declares and the listing shows available is
    loaded as ``otto --lab`` loads it (:func:`~otto.session.build_lab`,
    through the same *catalog*, so the whole pass evaluates each entry
    once). A lab a declaration marks unavailable is not a problem, as a
    hand-written one's is not; the report's
    :meth:`~otto.labs.composite.LabStatusReport.refusal` tells it from one a
    failed source's scope holds. A failed listing, a failed source, a lab
    only another failed source's scope holds and a failed load are each one
    problem, in the words the panel or dispatch uses; a problem is reported
    once, and never when its cause is an error the doctor's own checks
    already report (*reported*, a broken user settings file, say). Under a
    catalog that refuses to render (its
    :attr:`~otto.inventory.ProtectedPaths.refusal`: the user settings file
    cannot be read, which the doctor's checks report), each entry is one
    warning and nothing is listed. A cancelled read is the operation's and
    propagates, never a row: a load or listing error
    ``otto.labs.drops.is_timeout`` finds a cancellation in, and an outage
    that timed out
    (:meth:`~otto.labs.composite.SourceUnavailable.cancellation`).
    Otherwise each entry is one warning naming why it was not rendered, and
    nothing is evaluated.
    """
    if not sources:
        return _TemplatingRows()
    from ..labs.drops import is_timeout

    read = templated_outcome(catalog, root=root, sources=sources)
    membership, outcome = read.membership, read.outcome
    if catalog is None or membership.project is None or membership.projects is None:
        refusal = membership.refusal or ""
        return _TemplatingRows(
            warnings=[
                _NOT_RENDERED.format(entry=entry, refusal=refusal)
                for group in sources
                for entry in group.files
            ]
        )
    if catalog.protected().refusal is not None:
        return _TemplatingRows(
            warnings=[_NO_CATALOG.format(entry=entry) for group in sources for entry in group.files]
        )
    projects = membership.projects
    project_root = membership.project.root
    rows: list[_Row] = []
    if isinstance(outcome, Exception):
        text = _error_text(outcome)
        rows.append(_Row(cause=str(outcome), text=f"host source unavailable: {text}"))
    elif outcome is not None:
        labels = _source_labels(sources)
        outages = [
            outage
            for outage in outcome.unavailable_sources
            if outage.source.project_root == project_root and outage.source.label in labels
        ]
        for outage in outages:
            cancelled = outage.cancellation()
            if cancelled is not None:
                raise cancelled  # the operation was given up on: never a row
        rows.extend(
            _Row(
                cause=outage.diagnostic.reason,
                text=f"source {outage.source.label} unavailable: {outage.diagnostic.reason}",
            )
            for outage in outages
        )
        reported_outages = [outage.diagnostic for outage in outages]
        for status in read.statuses:
            # The report's own decision (refusal): a declaration that marks
            # the lab unavailable, or a failed source whose scope holds it.
            refusal = outcome.refusal(name=status.name)
            if refusal is None:
                rows.extend(_load_problem(projects, name=status.name, view=catalog))
            elif refusal.declared:
                continue  # declared unavailable, as a hand-written lab may be: not a problem
            elif is_timeout(refusal):
                raise refusal  # another source's cancelled read: the operation's, never a row
            elif refusal.diagnostic not in reported_outages:
                reason = refusal.diagnostic.reason
                rows.append(_Row(cause=reason, text=f"{status.name} — unavailable: {reason}"))
    problems = [row.text for row in rows if not row.caused_by(reported)]
    return _TemplatingRows(problems=list(dict.fromkeys(problems)))


@final
@dataclasses.dataclass(frozen=True, kw_only=True)
class _Row:
    """One templating problem: its row, and the text of the error that caused it."""

    cause: str
    text: str

    def caused_by(self, reported: "set[str]") -> bool:
        """Return whether one of the *reported* errors is this row's cause, or ends it.

        The cause is the reported error itself, or quotes it last after
        ``": "``: a render whose ``otto`` argument could not be built names
        its entry and the error's class before the inventory's own error
        (``<entry>: <Class>: <error>``), which the doctor's checks already
        report, so that row is the same problem and is not shown twice. A
        cause that merely contains a reported text elsewhere is a distinct
        problem, and is shown.
        """
        return any(
            text and (self.cause == text or self.cause.endswith(f": {text}")) for text in reported
        )


def _load_problem(projects: "list[Project]", *, name: str, view: "InventoryView") -> list[_Row]:
    """Load lab *name* as ``otto --lab`` does, through *view*: ``[]``, or its error's row.

    Any error is one row, never a traceback: dispatch lets a source's own
    error through (an unscoped source's ``InventoryError``, a third-party
    backend's anything), and the doctor reports it. A
    :class:`~otto.session.LabBuildError` reads as dispatch words it; any
    other as :func:`_error_text` states it.
    """
    from ..labs.drops import is_timeout
    from ..session.lab import LabBuildError, build_lab

    try:
        build_lab(projects, labs=[name], view=view)
    except Exception as e:  # any backend's error is a row, never a traceback
        if is_timeout(e):
            raise  # the operation was given up on: never a row
        if isinstance(e, LabBuildError):
            return [_Row(cause=e.detail or str(e), text=str(e))]
        return [_Row(cause=str(e), text=_error_text(e))]
    return []


def _item_problem(validate: Callable[[Any], object], item: Any, prefix: str) -> list[str]:
    """Return ``[f"{prefix} <error>"]`` when *validate* rejects *item*, else ``[]``.

    A one-item helper rather than the loop body its callers would otherwise
    write: a ``try``/``except`` inside a per-item loop is ``PERF203``, and the
    otto's answer (``otto.labs.json_repository._parse_element``) is to move
    the ``try`` into a function the loop calls. ``ValueError`` covers both
    arms — pydantic's ``ValidationError`` is one; ``InventoryError`` covers a
    third — a host entry's :func:`~otto.inventory.resolve_host_entry` call
    hitting a dead key or an inventory-owned field declared inline.

    A ``ValidationError`` is rendered through :func:`compact_validation_error`
    rather than ``str(e)``: *item* here is the RESOLVED host dict, and a
    referenced host with no inline creds carries its store creds LAST (spec
    2026-09-06 creds-store §6.2), so ``str(ValidationError)``'s
    ``input_value=`` repr of the whole dict ends in a store password on the
    common "one bad field" mistake. ``compact_validation_error`` never reads
    ``input``.
    """
    from pydantic import ValidationError

    from ..inventory.errors import InventoryError
    from ..models.base import compact_validation_error

    try:
        validate(item)
    except ValidationError as e:
        return [f"{prefix} {compact_validation_error(e)}"]
    except (ValueError, InventoryError) as e:
        return [f"{prefix} {e}"]
    return []


def _validate_lab(
    doctor: _DoctorView, parsed: _ParsedLab, profiles: "ProfileContext | None" = None
) -> list[str]:
    """Validate every lab file the settings' ``[[lab.sources]]`` name, via the real specs.

    The file shape is :func:`_parse_lab_documents`' job; what is left is the
    two payloads the wrapper models hold opaquely. Each element's host
    entries are resolved through the doctor's view the way the loader
    resolves them (:func:`otto.inventory.resolve_view_entry`, spec §6,
    against the element :meth:`otto.models.lab.ElementSpec.to_element`
    builds), and handed to
    :func:`otto.host.factory.validate_host_dict`, so a bad ``os_type`` or
    field name, a dead inventory key, a bare reference the project refuses,
    or an inventory-owned field declared inline all surface the same error
    the loader would raise. A lookup failure in a named inventory is
    attributed to its name (:func:`_attributed`). Each ``links`` entry is
    validated structurally via :class:`~otto.models.link.LinkSpec`; endpoint
    cross-references (host ids, interface keys) are resolved at load time,
    not here.

    An inventory that fails to build (a broken ``[inventory]`` declaration,
    an unparseable user settings file, a catalog conflict) is reported ONCE,
    as its own problem, rather than once per referencing host entry — the
    entries whose reference selects it are skipped for this pass and
    resolve once it is fixed. An entry that references nothing, or whose
    reference is malformed or refused, selects no inventory
    (:func:`~otto.inventory.doctor.selected_inventory`) and is validated
    regardless: skipping on mere key PRESENCE would swallow its own problems.
    """
    from ..host.factory import validate_host_dict
    from ..models.link import LinkSpec

    view = doctor.view
    failures = _build_failures(doctor, names=view.names())
    problems = [*parsed.problems, *failures.values()]
    # A templated inventory is reported by its warning: its entries resolve
    # through dispatch, never against a record set the checks did not read.
    broken = doctor.inventories.skipped(view.names())

    def _validate_entry(host_data: dict[str, Any], *, element: "Element") -> None:
        validate_host_dict(
            host_data=_resolved(host_data, view=view, element=element).host_data,
            element=element,
            profiles=profiles,
        )

    from ..inventory.doctor import selected_inventory

    for document in parsed.documents:
        lab_file, elements, links = str(document.path), document.elements, document.links
        for element in elements:
            # Bound rather than passed alongside: _item_problem calls its
            # validator with the item and nothing else.
            validate = partial(_validate_entry, element=element.to_element())
            for idx, host_data in enumerate(element.hosts):
                if selected_inventory(host_data, view=view) in broken:
                    continue  # reported once above; these resolve once it is fixed
                prefix = f"{lab_file}: element {element.name!r} hosts[{idx}]"
                problems.extend(_item_problem(validate, host_data, prefix))
        for idx, link_data in enumerate(links):
            problems.extend(
                _item_problem(LinkSpec.model_validate, link_data, f"{lab_file}: links[{idx}]")
            )
    return problems


def _build_failures(doctor: _DoctorView, *, names: "list[str]") -> dict[str, str]:
    """Return the problem row of each of *names* that fails to build: the one wording.

    ``inventory: <text>``, attributed to the name (:func:`_attributed`).
    :func:`_validate_lab` reports every checked name's failure this way; a
    project with no lab to validate reports its own declarations' failures
    the same way under ``settings`` (:func:`_own_names`).
    """
    view = doctor.view
    rows: dict[str, str] = {}
    for name in names:
        failure = doctor.inventories.failure(name)
        if failure is not None:
            rows[name] = f"inventory: {_attributed(view=view, name=name, text=str(failure))}"
    return rows


def _own_failures(doctor: _DoctorView) -> set[str]:
    """Return the text of every checked inventory's build failure, as the checks report it."""
    failures = (doctor.inventories.failure(name) for name in doctor.view.names())
    return {str(failure) for failure in failures if failure is not None}


def _own_names(doctor: _DoctorView) -> list[str]:
    """Return the inventory names the project's own settings file declares.

    Its ``[[inventories]]`` names, and ``default`` when the file binds it
    itself; a ``default`` that comes from the user file is not this
    project's declaration.
    """
    from ..host.inventory_ref import DEFAULT_INVENTORY
    from ..models.settings import binds_default, bound_table

    inventories = doctor.inventories
    declaration = inventories.declaration
    own = list(inventories.own)
    if declaration is not None and binds_default(
        inventory=bound_table(declaration.table), inventories=declaration.inventories
    ):
        own.insert(0, DEFAULT_INVENTORY)
    return own


def _resolved(
    host_data: dict[str, Any], *, view: "InventoryView", element: "Element"
) -> "ResolvedEntry":
    """Resolve *host_data* through *view*, a lookup failure attributed to the inventory's name.

    A reference that selects no inventory (refused, or naming none
    configured) fails with the view's own text, unattributed.
    """
    from ..inventory.doctor import selected_inventory
    from ..inventory.errors import InventoryError
    from ..inventory.resolve import resolve_view_entry

    try:
        return resolve_view_entry(host_data, view=view, element=element)
    except InventoryError as e:
        name = selected_inventory(host_data, view=view)
        text = str(e) if name is None else _attributed(view=view, name=name, text=str(e))
        if text == str(e):
            raise
        raise InventoryError(text) from e


def _lab_warnings(doctor: _DoctorView, parsed: _ParsedLab) -> list[str]:
    """Advisory findings across every lab file — never failing.

    Spec §8.3, §9 and §11, plus the shared-element protection rule of spec
    2026-08-28 three-level-reservations §7: two labs that share an element
    neither of them can reserve below the lab level, while their lab-level sets
    have nothing in common (:func:`otto.labs.doctor.lab_warnings`).

    Separate from :func:`_validate_lab` because the two answer different
    questions: a problem is "otto will not load this", a warning is "otto will
    load this and it is probably not what you meant". Only the first fails the
    report.

    Then, for ``default`` and each inventory the project's own file
    declares, when it resolves: a snapshot served because the backend was
    unreachable, orphan records
    (:func:`~otto.inventory.doctor.orphan_warning`, counting this project's
    references that select that inventory,
    :func:`~otto.inventory.doctor.referenced_by_name`; none for an inventory a
    ``jsonnet`` source reads, :meth:`_ParsedLab.template_inventories`), creds-store keys the
    inventory does not hold, and world-readable creds-store files
    (:func:`~otto.inventory.doctor.creds_mode_warnings`), each attributed to
    the inventory's name (:func:`_attributed`). An inventory that fails to
    build contributes nothing here: it is already a problem
    (:func:`_build_failures`), and repeating it as a warning would be noise.

    The stale-snapshot notice is REPORTED rather than left to the cache's own
    ``logger.warning``, which fires once per snapshot per process and may
    already have been spent by an earlier resolution (``entry()``'s
    completion-cache write runs before the root callback installs a console
    handler at all). Spec §19.2 pitches ``otto init`` as the dead-reference
    gate to run in CI, and a green report against a days-old snapshot is
    exactly what that gate must not give.
    """
    from ..inventory.doctor import referenced_by_name
    from ..labs.doctor import lab_warnings

    documents = parsed.documents
    warnings = lab_warnings([(str(d.path), d.entries, d.elements) for d in documents])
    view = doctor.view
    referenced = referenced_by_name(references=parsed.references(), view=view)
    inventories = doctor.inventories
    read_by_templates = parsed.template_inventories()
    for name in inventories.declared:
        if name in inventories.skipped([name]):
            continue  # a failure is in the lab verdict; a templated one is warned of below
        findings = _inventory_findings(
            inventories[name],
            referenced=referenced.get(name, set()),
            read_by_templates=name in read_by_templates,
        )
        warnings.extend(_attributed(view=view, name=name, text=w) for w in findings)
    warnings.extend(row for name in view.names() if (row := inventories.not_rendered(name)))
    return warnings


def _inventory_findings(
    inventory: "Inventory", *, referenced: set[str], read_by_templates: bool = False
) -> list[str]:
    """Return one inventory's findings: stale notice, orphans, orphan creds, creds modes.

    With *read_by_templates* (a ``jsonnet`` source names the inventory in
    its ``inventories``) the unused-records finding is left out: which
    records a template reads is known only by evaluating it, which the
    doctor never does, so no record can be called unused. The records are
    still listed, as the stale notice and a listing failure come from that
    read (:func:`~otto.inventory.doctor.orphan_warning` does no other).
    """
    from ..inventory.cache import snapshot_cache_of
    from ..inventory.doctor import creds_mode_warnings, orphan_creds_warning, orphan_warning
    from ..inventory.errors import InventoryError

    try:
        if read_by_templates:
            # The read stays: it sets the stale-snapshot notice read below and
            # is where a listing failure surfaces; only its verdict is dropped.
            inventory.list_keys()
            orphan = None
        else:
            orphan = orphan_warning(inventory, referenced=referenced)
        orphan_creds = orphan_creds_warning(inventory)
    except InventoryError as e:
        orphan = f"inventory '{inventory.label}': could not list records: {e}"
        orphan_creds = None
    # Read AFTER the orphan check, never before: the notice is set by the
    # resolution that check performs, and construction touches nothing.
    snapshot = snapshot_cache_of(inventory=inventory)
    stale = snapshot.stale_notice if snapshot is not None else None
    # Staleness first — it is the fact that qualifies every finding under
    # it, orphan list included.
    return [w for w in (stale, orphan, orphan_creds, *creds_mode_warnings(inventory)) if w]


def _validate_tests(root: Path) -> list[str]:
    """Light check of configured test dirs: existence, ``test_*.py`` presence, syntax.

    Deliberately does NOT run pytest's collection (``otto test
    --list-tests``): that imports every test file and conftest, which is too
    heavy for a doctor check. ``ast.parse`` catches syntax errors without
    importing user code.
    """
    import ast

    problems: list[str] = []
    for tests_dir in tests_dirs(root):
        if not tests_dir.is_dir():
            problems.append(f"tests dir not found: {tests_dir}")
            continue
        test_files = sorted(tests_dir.glob("test_*.py"))
        if not test_files:
            problems.append(f"no test files found under {tests_dir}")
            continue
        for test_file in test_files:
            try:
                # Bytes, so a coding cookie is honoured as Python honours it;
                # an undecodable file is a SyntaxError, a NUL byte a ValueError,
                # and what glob lists but nothing can read (a dangling link, a
                # directory named test_*.py) an OSError.
                ast.parse(test_file.read_bytes(), filename=str(test_file))
            except (SyntaxError, ValueError, OSError) as e:  # noqa: PERF203 — per-file resilience, mirrors json_repository.py
                problems.append(f"{test_file}: {e}")
    return problems


def _validate_instructions(root: Path, modules: list[str]) -> list[str]:
    """Every declared init module must resolve the way the loader will import it.

    Resolution only (:func:`otto.config.project.find_init_module`): no user code
    runs, because the doctor runs lab-free and may run before
    ``OTTOPATH`` is set.
    """
    from ..config.project import find_init_module

    libs = instruction_libs(root)
    problems = [f"libs dir not found: {lib}" for lib in libs if not lib.is_dir()]
    searched = ", ".join(str(lib) for lib in libs) or "none declared"
    problems.extend(
        f"init module {module} not found under libs ({searched}) or on sys.path"
        for module in modules
        if find_init_module(module, libs) is None
    )
    return problems


# ── kmodcov ────────────────────────────────────────────────────────────────────


def _kmodcov_anchored(root: Path) -> list[Path | ValueError]:
    """Anchor each kmodcov entry's ``source`` under *root*, or keep why it cannot be.

    A ``source`` under an unknown user's home (``~nosuchuser/...``) cannot
    be anchored; the error is kept in its place so the kmodcov area can
    report it instead of the doctor raising.
    """
    from ..utils import anchor_path

    anchored: list[Path | ValueError] = []
    for entry in kmodcov_entries(root):
        if not isinstance(entry.get("source"), str):
            continue
        try:
            anchored.append(anchor_path(Path(entry["source"]), root))
        except ValueError as e:
            anchored.append(e)
    return anchored


def _kmodcov_sources(root: Path) -> list[Path]:
    """Return the vendored directories the kmodcov entries name via ``source``, under *root*."""
    return [source for source in _kmodcov_anchored(root) if isinstance(source, Path)]


def _validate_kmodcov(root: Path) -> list[str]:
    """Require a declared ``source`` to anchor and to hold the library.

    Drift from the installed otto is advisory, never a failure — see
    :func:`_kmodcov_warnings`.
    """
    from ..kmodcov.library import check_tree

    problems: list[str] = []
    for source in _kmodcov_anchored(root):
        if isinstance(source, ValueError):
            problems.append(f"a kmodcov dev tool's `source`: {source}")
        elif check_tree(source).state == "absent":
            problems.append(
                f"{source}: a kmodcov dev tool names it as `source`, but there is no "
                f"otto_kmodcov there — run `otto cov kmodcov export {source}`"
            )
    return problems


def _kmodcov_warnings(root: Path) -> list[str]:
    """Vendored copies that differ from the installed otto — reported, never a failure."""
    from ..kmodcov.library import check_tree

    warnings: list[str] = []
    for source in _kmodcov_sources(root):
        result = check_tree(source)
        if result.state != "differs":
            continue
        origin = f" (exported by otto {result.exported_by})" if result.exported_by else ""
        warnings.append(_drift_warning(source, origin, result, "kmodcov"))
    return warnings


def _drift_warning(directory: Path, origin: str, result: "CheckResult", verb: str) -> str:
    """Render the advisory line for a vendored copy that differs from the installed otto's.

    One rendering for every vendored area: the absolute *directory*, the
    differing and missing files by name, and the ``otto cov <verb> export``
    remedy naming that same directory.
    """
    names = ", ".join([*result.differing, *result.missing])
    return (
        f"{directory}{origin} differs from the installed otto's copy: {names} — "
        f"run `otto cov {verb} export {directory}` and review the diff"
    )


# ── embedded-gcov ──────────────────────────────────────────────────────────────


def _validate_embedded_gcov(root: Path) -> list[str]:  # noqa: ARG001 — uniform signature
    """Return no problems: the area is present only when the patch is, and drift is advisory.

    Drift from the installed otto is never a failure — see
    :func:`_embedded_gcov_warnings`.
    """
    return []


def _embedded_gcov_warnings(root: Path) -> list[str]:
    """Warn when the patch under the default directory differs from the installed otto's.

    Only :data:`~otto.init.area_dirs.DEFAULT_EMBEDDED_GCOV_DIR` is looked at: no
    settings key names the patch directory, so a copy under a custom
    ``--embedded-gcov-dir`` gets no drift warning.
    """
    from ..coverage.embedded_gcov.library import check_patch

    directory = root / DEFAULT_EMBEDDED_GCOV_DIR
    result = check_patch(directory)
    if result.state != "differs":
        return []
    return [_drift_warning(directory, "", result, "embedded-gcov")]


_AREA_VALIDATORS: dict[str, Callable[[Path], list[str]]] = {
    "kmodcov": _validate_kmodcov,
    "embedded-gcov": _validate_embedded_gcov,
}
"""The validator of every area ``check_project`` has no dedicated branch for."""
