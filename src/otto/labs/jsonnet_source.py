"""Built-in ``"jsonnet"`` lab source: labs rendered from Jsonnet entry files.

A ``[[lab.sources]]`` entry with ``backend = "jsonnet"`` names ``.jsonnet``
entry files in ``paths``. Each entry is rendered once per operation, with the
inventories the source declares as its input (``otto``: the credential-free
projection :func:`~otto.inventory.snapshot.template_inputs` builds), and the
rendered document is read exactly as a hand-written ``lab.json`` is: the one
reader (:func:`~otto.labs.documents.read_lab_document`), the one source read
(:func:`~otto.labs.documents.read_source`) and the one document core
(:class:`~otto.labs.json_repository._DocumentLabRepository`). On top of that,
:func:`validated_source_read` holds a generated source to rules a
hand-written one is not held to: every inventory reference is qualified and
names an inventory the source declares, every canonical host id is unique
within the source, and a scoped source's output stays inside its ``scope``.
The source declares its scope to the composite
(:class:`~otto.labs.protocol.SupportsLabScope`), which asks it only for the
labs inside it, and its ``overrides``
(:class:`~otto.labs.protocol.SupportsOverridePolicy`), without which none of
its elements may replace another source's element with different machines.

Nothing rendered is written anywhere: the one file a scoped source writes
holds the names of the labs its last complete read declared
(:mod:`otto.labs.last_good`), which it lists when it is down. Importing this
module loads no templating code: the renderer loads when a source first
renders.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator
from typing_extensions import Self, final, override

from ..models.base import OttoModel
from ..utils import refuse_empty_entry
from .errors import LabRepositoryError
from .json_repository import (
    LabDocument,
    _anchored_entries,
    _DocumentLabRepository,
    _identified_entries,
    lab_statuses_of,
)
from .protocol import LabScope, LabStatus
from .sources import JSONNET_FILE_RULE, LabSourceEnv, LabSourceFacts

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from ..host.os_profile import ProfileContext
    from ..inventory.view import InventoryView
    from ..models.lab import ElementSpec
    from ..registry import Configured, Prepared
    from ..templating.errors import RenderCancelled
    from ..templating.memo import RenderKey
    from .documents import RenderInputs, SourceRead
    from .drops import Diagnostic
    from .json_repository import IdentifiedEntry


@final
class LabScopeSpec(OttoModel):
    """A ``jsonnet`` source's ``scope``: the lab names it may declare.

    ``names`` are exact lab names and ``prefixes`` literal prefixes, never
    patterns, written as an explicit table so an exact name is never read as
    a prefix. At least one of them is non-empty.
    """

    model_config = ConfigDict(frozen=True)

    names: tuple[str, ...] = ()
    """Lab names the source may declare, exactly."""

    prefixes: tuple[str, ...] = ()
    """Literal prefixes of the lab names the source may declare."""

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        """Refuse a scope that names nothing, which would hold no lab at all."""
        if not self.names and not self.prefixes:
            raise ValueError("a scope names at least one lab name or prefix")
        return self

    def to_scope(self) -> LabScope:
        """Return this scope as the composite reads one (:class:`~otto.labs.LabScope`)."""
        return LabScope(names=frozenset(self.names), prefixes=frozenset(self.prefixes))

    def describe(self) -> str:
        """Return this scope as a message names it: ``names 'bench'; prefixes 'router-lab.'``."""
        parts = [
            f"{kind} " + ", ".join(repr(v) for v in values)
            for kind, values in (("names", self.names), ("prefixes", self.prefixes))
            if values
        ]
        return "; ".join(parts)


@final
class JsonnetLabSourceConfig(OttoModel):
    """A ``backend = "jsonnet"`` source's options.

    ``paths`` and ``import_paths`` are anchored to the root of the project
    that declares the source, and ``~`` is expanded.
    """

    model_config = ConfigDict(frozen=True)

    paths: tuple[Path, ...] = Field(min_length=1)
    """The directories (their ``lab.jsonnet``), ``.jsonnet`` entry files and globs to render."""

    inventories: tuple[str, ...] = ()
    """The inventories the templates read, and the only ones a rendered reference may name."""

    scope: LabScopeSpec | None = None
    """The lab names the source may declare, or ``None`` for an unscoped source."""

    overrides: tuple[str, ...] = ()
    """The elements that may replace another source's element with different machines."""

    import_paths: tuple[Path, ...] = Field(default=(Path("lab_data/lib"),), validate_default=True)
    """The directories a template's imports resolve under, in order.

    ``lab_data/lib`` under the project root by default. A root that does not
    exist is legal: nothing resolves there. The entry file's own directory is
    never a root.
    """

    @field_validator("paths", mode="before")
    @classmethod
    def _no_empty_path(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise read as the project root."""
        return refuse_empty_entry(value, expected="a directory, a .jsonnet file or a glob")

    @field_validator("import_paths", mode="before")
    @classmethod
    def _no_empty_import_path(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise make the project root a root."""
        return refuse_empty_entry(value, expected="a directory")

    @field_validator("paths", "import_paths", mode="after")
    @classmethod
    def _anchor(cls, value: tuple[Path, ...], info: ValidationInfo) -> tuple[Path, ...]:
        """Anchor every relative entry to the root of the project that declares the source."""
        return _anchored_entries(value, info, backend="jsonnet")

    def prepared_facts(self) -> LabSourceFacts:
        """Return the entries the source renders, their file rule and its import roots.

        otto fingerprints the entry files, and every directory and file under
        the import roots, for the completion cache, without rendering.
        """
        return LabSourceFacts(
            file_inputs=self.paths, file_rule=JSONNET_FILE_RULE, key_roots=self.import_paths
        )


@final
@dataclass(frozen=True, kw_only=True)
class JsonnetSourceInputs:
    """One ``jsonnet`` source's configuration and environment: what its every read starts from."""

    config: JsonnetLabSourceConfig
    """The source's parsed options."""

    env: LabSourceEnv
    """The environment otto prepared the source in."""

    @classmethod
    def of_prepared(cls, prepared: "Prepared[LabSourceEnv, Any, Any] | None") -> "Self | None":
        """Return a prepared lab source's inputs when it is otto's ``jsonnet`` source.

        ``None`` for an unprepared source and for any other backend's,
        including a team's own source registered under the ``jsonnet`` name
        before otto shipped one: otto renders only a source its own config
        model parsed.
        """
        if prepared is None or not isinstance(prepared.config, JsonnetLabSourceConfig):
            return None
        return cls.of(config=prepared.config, env=prepared.env)

    @classmethod
    def of(cls, *, config: JsonnetLabSourceConfig, env: LabSourceEnv) -> "Self":
        """Return the inputs of the source *config* parsed in *env*: THE one construction.

        The source's factory (``_jsonnet_lab_source``) and :meth:`of_prepared`
        both build through it, so a field added here reaches both.
        """
        return cls(config=config, env=env)

    def rendered_document(self, entry: Path, *, view: "InventoryView") -> object:
        """Render *entry*, one of this source's entry files, to its decoded JSON value.

        The render a load reads (:func:`~otto.labs.documents.rendered_data`,
        through the view's memo, so it is the operation's one evaluation of
        *entry*), decoded and nothing more: no section is parsed and no
        secret is wrapped. *view* is the operation's view as the source's
        project sees it.

        Raises:
            LabRepositoryError: *entry* is not one of the source's entries,
                the render failed, or the source names no project; or the
                operation was cancelled, which is the source's timeout
                (``_cancel_is_timeout``), never a document.
        """
        from .documents import rendered_data

        with _cancel_is_timeout(label=self.env.label):
            return rendered_data(entry, self.render_inputs(view))

    def entries(self) -> list[Path]:
        """Return the source's entry files, its ``paths`` expanded.

        They expand under :data:`~otto.labs.sources.JSONNET_FILE_RULE` through
        :func:`~otto.labs.documents.expand_lab_files`, the expansion
        :meth:`~otto.labs.sources.LabSourceState.lab_files` applies to the
        same facts, so the files the cache keys on and the files rendered are
        one list.
        """
        from .documents import expand_lab_files

        return expand_lab_files(self.config.paths, rule=JSONNET_FILE_RULE)

    def render_inputs(self, view: "InventoryView") -> "RenderInputs":
        """Return what this source renders with under *view*, its entry files expanded now.

        The source's one read boundary: every read of it (its listing, a
        load, host summaries, declared links, ``otto lab render``) starts
        here. A configured source whose ``paths`` match no entry file now (a
        literal that does not exist, a glob that matches nothing, files
        removed since an earlier read) has nothing to render, so its read
        fails: the composite then refuses a lab inside its scope, as it does
        for any failed source, and a lab outside it is unaffected. A
        hand-written ``json`` source with no file keeps its own rule.

        Raises:
            LabRepositoryError: The source's ``paths`` match no entry file,
                listing the paths searched; or its environment names no
                project, so a template's ``otto.project`` would be invented.
        """
        from .documents import RenderInputs

        project = self.env.project_name
        if project is None:
            raise LabRepositoryError(
                f"source {self.env.label}: a jsonnet lab source renders for a named project, "
                "and its LabSourceEnv names none (project_name)"
            )
        entries = tuple(self.entries())
        if not entries:
            searched = "\n  ".join(str(p) for p in self.config.paths)
            raise LabRepositoryError(
                f"source {self.env.label}: no Jsonnet entry file found in any path (directories "
                f"are searched for {JSONNET_FILE_RULE.directory_file}; .jsonnet entries are read "
                f"directly; globs are expanded):\n  {searched}"
            )
        return RenderInputs(
            view=view,
            inventories=self.config.inventories,
            project=project,
            project_root=self.env.project_root,
            import_roots=self.config.import_paths,
            entries=entries,
        )


@final
@dataclass(frozen=True, kw_only=True)
class _SourceReadKey:
    """Everything one ``jsonnet`` source's validated read depends on, within one operation."""

    label: str
    """The source's label."""
    config: str
    """The source's configuration, as JSON: its inventories, scope and the rest."""
    profiles: "ProfileContext"
    """The data profiles its host ids are composed under."""
    project_root: "Path | None"
    """The project the view resolves references as."""
    renders: "tuple[RenderKey, ...]"
    """Each entry's render key (:func:`~otto.labs.documents.render_key`), in entry order."""


def validated_source_read(*, inputs: JsonnetSourceInputs, view: "InventoryView") -> "SourceRead":
    """Read and validate one ``jsonnet`` source, once per operation: THE generated-source read.

    Kept in the view's render memo
    (:meth:`~otto.templating.memo.RenderMemo.get_or_derive`) under the
    source and its entries' render keys, so the source's listing, every
    load, its host summaries, its declared links and the doctor share one
    parse and one duplicate check, as they share each render. The documents
    are shared, so no reader mutates them. A read that raises (a cancelled
    render, a source with no entry file) is not kept.

    Every entry file is rendered through the view's memo and read by
    :func:`~otto.labs.documents.read_source`, as a hand-written source's
    files are. Then these rules hold the documents that parsed, each breach a
    :class:`~otto.labs.drops.Diagnostic` naming the entry file, with the
    document left out of the read:

    - every host entry's ``inventory`` is a qualified reference
      (:func:`~otto.host.inventory_ref.normalize_reference`) to an inventory
      the source declares;
    - in a scoped source, every lab the document declares is inside the
      scope, and every element membership pattern is one lab name
      (:func:`~otto.models.lab.literal_of_pattern`) inside it: a pattern
      that could match more joins labs the scope cannot vouch for;
    - no two host entries of the source compose one canonical host id (each
      resolved through *view* and identified by
      :func:`~otto.host.factory.host_identity` under the source's profiles);
      the later one is refused, naming both entry files, elements and indices.
    """
    from ..templating.errors import TemplatingError
    from .documents import render_key

    render = inputs.render_inputs(view)
    protected = view.protected()
    try:
        renders = tuple(render_key(e, render, protected=protected) for e in render.entries)
    except TemplatingError:  # no packaged library: each entry's render reports it, unkept
        return _validated_read(inputs=inputs, view=view, render=render)
    key = _SourceReadKey(
        label=inputs.env.label,
        config=inputs.config.model_dump_json(),
        profiles=inputs.env.profiles,
        project_root=view.project_root,
        renders=renders,
    )
    return view.render_memo.get_or_derive(
        key, partial(_validated_read, inputs=inputs, view=view, render=render)
    )


def _validated_read(
    *, inputs: JsonnetSourceInputs, view: "InventoryView", render: "RenderInputs"
) -> "SourceRead":
    """Render and read every entry of *render*, then hold the documents to the source's rules."""
    from .documents import SourceRead, read_lab_document, read_source

    read = read_source(list(render.entries), read=partial(read_lab_document, render=render))
    problems = list(read.problems)
    referenced: list[LabDocument] = []
    for doc in read.documents:
        problem = _reference_problem(doc, declared=inputs.config.inventories)
        if problem is None and inputs.config.scope is not None:
            problem = _scope_problem(doc, scope=inputs.config.scope)
        if problem is None:
            referenced.append(doc)
        else:
            problems.append(problem)
    duplicated = _duplicate_ids(referenced, view=view, inputs=inputs)
    problems.extend(duplicated.values())
    documents = [doc for doc in referenced if doc.path not in duplicated]
    return SourceRead(documents=documents, problems=problems)


def _reference_problem(doc: LabDocument, *, declared: tuple[str, ...]) -> "Diagnostic | None":
    """Return the first host entry of *doc* whose ``inventory`` a generated source may not hold.

    A bare reference, a malformed one, and a qualified one to an inventory
    not in *declared* are each refused; an inline entry (no reference) is
    fine. ``None`` when every entry is.
    """
    from ..host.inventory_ref import normalize_reference
    from .drops import Diagnostic

    for element in doc.elements:
        for index, entry in enumerate(element.hosts):
            try:
                reference = normalize_reference(value=entry.get("inventory"))
            except ValueError as e:
                reason = str(e)
            else:
                if reference is None:
                    continue
                if not reference.qualified:
                    reason = (
                        f"bare reference {reference.key!r}: a generated document names its "
                        'inventory, {"name": "<inventory>", "key": "<key>"}'
                    )
                elif reference.name not in declared:
                    names = ", ".join(repr(n) for n in declared) or "none"
                    reason = (
                        f"inventory {reference.name!r} is not one the source declares "
                        f"(its inventories: {names})"
                    )
                else:
                    continue
            return Diagnostic(
                where=str(doc.path),
                reason=f"Lab file '{doc.path}': {_entry_named(element, index)} field "
                f"'inventory': {reason}",
            )
    return None


def _scope_problem(doc: LabDocument, *, scope: LabScopeSpec) -> "Diagnostic | None":
    """Return the first thing *doc* contributes outside *scope*, or ``None`` when nothing is."""
    from .drops import Diagnostic

    reason = next(_outside_scope(doc, scope=scope), None)
    if reason is None:
        return None
    return Diagnostic(where=str(doc.path), reason=f"Lab file '{doc.path}': {reason}")


def _outside_scope(doc: LabDocument, *, scope: LabScopeSpec) -> "Iterator[str]":
    """Yield why each thing *doc* contributes is outside *scope*, in document order.

    A declared lab outside the scope, a membership pattern that is not one
    lab name, and one naming a lab outside the scope.
    """
    from ..models.lab import literal_of_pattern

    inside = scope.to_scope()
    for name in doc.entries:
        if not inside.contains(name=name):
            yield f"lab {name!r} is outside the source's scope ({scope.describe()})"
    for element in doc.elements:
        for pattern in element.labs:
            literal = literal_of_pattern(pattern)
            if literal is None:
                yield (
                    f"element {element.name!r}: membership {pattern!r} is not one lab name, so a "
                    "scoped source cannot contribute it; shared equipment that joins labs by "
                    "pattern belongs in a hand-written source"
                )
            elif not inside.contains(name=literal):
                yield (
                    f"element {element.name!r}: membership {pattern!r} names lab {literal!r}, "
                    f"which is outside the source's scope ({scope.describe()})"
                )


def _entry_named(element: "ElementSpec", index: int) -> str:
    """Return how an error names host entry *index* of *element*: its labs, element and index."""
    labs = ", ".join(repr(p) for p in element.labs) or "none"
    return f"element {element.name!r} (labs {labs}) hosts[{index}]"


def _duplicate_ids(
    docs: "list[LabDocument]", *, view: "InventoryView", inputs: JsonnetSourceInputs
) -> "dict[Path, Diagnostic]":
    """Return, by document, the first host entry that repeats a canonical id the source holds.

    Ids come from :func:`~otto.labs.json_repository._identified_entries`, the
    resolution and identification host summaries use; an entry that cannot
    be identified is left to the load, which reports it in full.
    """
    from .drops import Diagnostic

    first: dict[str, IdentifiedEntry] = {}
    refused: dict[Path, Diagnostic] = {}
    for found in _identified_entries(docs, view=view, profiles=inputs.env.profiles):
        if found.origin in refused:
            continue
        host_id = found.identity.id
        held = first.get(host_id)
        if held is None:
            first[host_id] = found
            continue
        refused[found.origin] = Diagnostic(
            where=str(found.origin),
            reason=(
                f"Lab file '{found.origin}': {_entry_named(found.element, found.index)} composes "
                f"host id {host_id!r}, which {_entry_named(held.element, held.index)} in "
                f"'{held.origin}' already composes; one source, one host per id"
            ),
        )
    return refused


@final
class _JsonnetLabRepository(_DocumentLabRepository):
    """Serve labs rendered from a ``jsonnet`` source's entry files.

    Everything is :class:`~otto.labs.json_repository._DocumentLabRepository`'s,
    over :func:`validated_source_read`: loads, contributions and their links,
    host summaries and the declared links all read one validated read, and
    each entry renders once per operation (the view's
    :attr:`~otto.inventory.InventoryView.render_memo`). On top of it, the
    source declares its ``scope`` (:meth:`lab_scope`) and its ``overrides``
    (:meth:`declared_overrides`) to the composite.

    Every entry renders on every read, best-effort or not, so a cancelled
    render is never a skipped file: a best-effort call (the host summaries,
    the declared links) raises the source's timeout, a
    :class:`~otto.labs.LabRepositoryError` raised from the
    :class:`~otto.templating.errors.RenderCancelled`, as a strict one does.
    """

    def __init__(self, inputs: JsonnetSourceInputs) -> None:
        super().__init__(search_paths=list(inputs.config.paths), profiles=inputs.env.profiles)
        self.inputs = inputs
        """The source's configuration and environment."""

    @override
    def _read_source(self, *, best_effort: bool, view: "InventoryView") -> "SourceRead":
        """Return :func:`validated_source_read` of this source through *view*.

        *best_effort* is not read: every entry is rendered either way. It
        never raises ``FileNotFoundError``, the hand-written sources' "no lab
        file" signal: a source with no entry file fails
        (:meth:`JsonnetSourceInputs.render_inputs`).

        Raises:
            LabRepositoryError: The source's ``paths`` match no entry file; or
                the operation was cancelled while an entry rendered: the
                source's timeout, raised from the
                :class:`~otto.templating.errors.RenderCancelled`.
        """
        del best_effort
        with _cancel_is_timeout(label=self.inputs.env.label):
            return validated_source_read(inputs=self.inputs, view=view)

    def lab_scope(self) -> LabScope | None:
        """Return the source's ``scope``, or ``None`` for an unscoped source.

        The :class:`~otto.labs.protocol.SupportsLabScope` capability: the
        composite asks the source only for labs inside it, so neither its
        failure nor its links reach a lab outside it.
        """
        scope = self.inputs.config.scope
        return None if scope is None else scope.to_scope()

    def declared_overrides(self) -> frozenset[str]:
        """Return the source's ``overrides``.

        The :class:`~otto.labs.protocol.SupportsOverridePolicy` capability:
        an element of this source replaces another source's element with
        different machines only when it is listed. Each entry is read as the
        element's identity, its slug, as elements are keyed.
        """
        from ..host.remote_host import slug

        return frozenset(slug(name) for name in self.inputs.config.overrides)

    @override
    def list_labs(self) -> list[str]:
        """Return the labs the scoped source's last complete read declared, without rendering.

        The labs a source declares are known from a render, which needs the
        operation's inventory view: :meth:`list_lab_status` lists them, and
        for a scoped source records their names
        (:func:`~otto.labs.last_good.record_last_good`) once its read is
        accepted. These are those names, ``[]`` before the first and always
        ``[]`` for an unscoped source. When a scoped source fails, the
        composite shows the ones inside its scope, each unavailable with the
        source's reason. They are diagnostics: they never authorize a load,
        and only an unavailable scoped source's listing should read them.
        """
        from .last_good import last_good

        if self.lab_scope() is None:
            return []
        return last_good(identity=self._identity())

    def _identity(self) -> str:
        """Return the identity this source's last known lab names are kept under.

        Its project root, label and configuration
        (:func:`~otto.labs.last_good.source_identity`), so a changed
        declaration starts with no names.
        """
        from .last_good import source_identity

        env = self.inputs.env
        return source_identity(
            project_root=env.project_root,
            label=env.label,
            config=self.inputs.config.model_dump(mode="json"),
        )

    @override
    def list_lab_status(self, *, view: "InventoryView") -> list[LabStatus]:
        """Return every lab the source's entries declare, with whether it can be used now.

        Strict: a problem in any entry (a failed or cancelled render, a
        malformed document, a refused reference or a duplicate) raises, never
        a partial list, so the composite sees the source's outage. The
        statuses are returned only once the operation accepts them
        (:meth:`~otto.templating.memo.RenderMemo.publish`), and for a scoped
        source the accepted publication records their names
        (:meth:`list_labs`): an operation given up on while the read ran gets
        the timeout instead, and records nothing.

        Raises:
            LabRepositoryError: An entry has a problem, the source's ``paths``
                match no entry file, or the operation was cancelled before the
                statuses were accepted: the timeout,
                raised from a :class:`~otto.templating.errors.RenderCancelled`.
        """
        from ..templating.errors import RenderCancelled
        from .documents import accepted_documents
        from .last_good import record_last_good

        read = self._read_source(best_effort=False, view=view)
        statuses = lab_statuses_of(accepted_documents(read, best_effort=False))
        record = (
            _record_nothing
            if self.lab_scope() is None
            else partial(
                record_last_good, identity=self._identity(), names=[s.name for s in statuses]
            )
        )
        if not view.render_memo.publish(scope=self.inputs.env.project_root, action=record):
            _raise_timeout(
                label=self.inputs.env.label,
                cause=RenderCancelled("the operation was cancelled before its labs were listed"),
            )
        return statuses


@contextmanager
def _cancel_is_timeout(*, label: str) -> "Generator[None, None, None]":
    """Turn a render the cancellation reached inside the block into source *label*'s timeout.

    The one place a cancelled render of a ``jsonnet`` source becomes the
    source's failure (:func:`_raise_timeout`): its every read, and every
    document :meth:`JsonnetSourceInputs.rendered_document` renders.
    """
    from ..templating.errors import RenderCancelled

    try:
        yield
    except RenderCancelled as e:
        _raise_timeout(label=label, cause=e)


def _raise_timeout(*, label: str, cause: "RenderCancelled") -> NoReturn:
    """Raise the source *label*'s timeout: its read was cancelled, by *cause*.

    The one shape of a cancelled read, for a render the cancel reached and a
    status read whose publication it refused alike: a
    :class:`~otto.labs.errors.LabRepositoryError`, so the composite classifies
    it as the source's failure as any other, raised from the
    :class:`~otto.templating.errors.RenderCancelled`, so the error's
    ``__cause__`` tells a deadline from a fault.
    """
    raise LabRepositoryError(f"source {label}: {cause}") from cause


def _record_nothing() -> None:
    """Record nothing: an unscoped source's names have no reader."""


def _jsonnet_lab_source(
    configured: "Configured[JsonnetLabSourceConfig, LabSourceEnv]",
) -> _JsonnetLabRepository:
    """Build the built-in jsonnet source from its parsed configuration."""
    return _JsonnetLabRepository(
        JsonnetSourceInputs.of(config=configured.config, env=configured.env)
    )
