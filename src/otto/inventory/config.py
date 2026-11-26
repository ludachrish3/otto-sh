"""``[inventory]`` → one :class:`~otto.inventory.protocol.Inventory` per process (spec §8).

Resolution, first hit wins: an override in an active project's settings (all
declaring projects must agree), else ``~/.otto/settings.toml``, else none. There
is no implicit discovery — an inventory is always declared, in one of exactly
two places, and a process has exactly one.

Backend construction does no I/O: a lab with no referenced entry never touches
the inventory, so a broken or unreachable inventory cannot break a run that
did not need it.

IMPORT DISCIPLINE: everything this module needs from :mod:`otto.config` and
:mod:`otto.models.settings` is imported INSIDE the function that uses it, and
the annotation-only names sit under ``TYPE_CHECKING``. Measured: importing
them at module level takes a bare ``import otto.inventory`` from 77 to 96
otto modules. ``otto.inventory`` sits on the bootstrap path, so that graph
would land on every CLI surface, the same cost the ``otto.config`` lazy
exports exist to keep off it. The edges are real and declared
in ``tach.toml``; only the *timing* is deferred.
"""

import dataclasses
import functools
import os
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_serializer,
)
from typing_extensions import final, override

from ..errors import describe_refused
from ..host.inventory_ref import DEFAULT_INVENTORY
from ..models.base import OttoModel
from ..utils import anchor_path, parse_cache_ttl, refuse_empty_entry, resolved_path
from .creds import CredsOverlay
from .errors import InventoryConstructionError, InventoryError
from .protocol import Inventory
from .registry import INVENTORY_BACKENDS, InventoryEnv, InventoryMetadata
from .view import BindingKey, InventoryView, binding_key, refuses_bare

if TYPE_CHECKING:
    from ..config.project import Project
    from ..creds.config import CompiledCreds
    from ..models.settings import InventoryConfigSpec, NamedInventorySpec, UserSettingsModel
    from ..registry import FrozenMap, Prepared


def _anchored(value: Path, info: ValidationInfo) -> Path:
    """Anchor *value* to the env's ``anchor_dir`` when the table is prepared with one."""
    env = (info.context or {}).get("env")
    return anchor_path(value, env.anchor_dir, quote=False) if env is not None else value


@final
class JsonInventoryConfig(OttoModel):
    """The json inventory's table: an anchored file path, and the fields it supplies.

    A ``path`` that resolves to a ``.jsonnet`` file names a Jsonnet file that
    renders the inventory document
    (:attr:`~otto.inventory.JsonInventory.templated`, decided on the resolved
    path); its imports resolve under ``import_paths``.
    """

    model_config = ConfigDict(frozen=True)

    path: Path
    """The inventory file; a relative path anchors to its project root; ``~`` is expanded."""

    supplies: list[str] | None = None
    """The record fields this inventory supplies; ``None`` means every fillable field."""

    import_paths: tuple[Path, ...] = ()
    """The directories a ``.jsonnet`` path's imports resolve under, in order; none by default.

    Each is anchored like ``path``. A root that does not exist is legal:
    nothing resolves there. The Jsonnet file's own directory is never a root.
    Only a ``.jsonnet`` path reads them."""

    @field_validator("path", mode="before")
    @classmethod
    def _not_empty(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise read as the project root."""
        if value == "":
            raise ValueError("must name the inventory file")
        return value

    @field_validator("import_paths", mode="before")
    @classmethod
    def _no_empty_import_path(cls, value: object) -> object:
        """Refuse an empty entry, which would otherwise make the project root a root."""
        return refuse_empty_entry(value, expected="a directory")

    @field_validator("path", mode="after")
    @classmethod
    def _anchor(cls, value: Path, info: ValidationInfo) -> Path:
        """Anchor a relative path to the directory the table was declared in."""
        return _anchored(value, info)

    @field_validator("import_paths", mode="after")
    @classmethod
    def _anchor_import_paths(
        cls, value: tuple[Path, ...], info: ValidationInfo
    ) -> tuple[Path, ...]:
        """Anchor each relative import root as ``path`` is anchored."""
        return tuple(_anchored(root, info) for root in value)


@final
class NetBoxInventoryConfig(OttoModel):
    """The NetBox inventory's table; the ``"netbox"`` backend itself checks the values."""

    model_config = ConfigDict(frozen=True)

    url: str
    """Base URL of the NetBox instance."""

    token_env: str = "NETBOX_TOKEN"  # noqa: S105 — the NAME of the variable, not a token
    """Name of the environment variable holding the API token."""

    filter: dict[str, object] | None = None
    """NetBox device filter, forwarded verbatim."""

    ip_source: str = "primary_ip4"
    """Where a device's management address comes from."""

    custom_fields: dict[str, str] | None = None
    """Record field -> NetBox custom field name."""

    extra_custom_fields: list[str] | None = None
    """NetBox custom field names carried through in ``record.extra``."""

    virtual_machines: bool = False
    """Also read NetBox virtual machines, as records with ``is_virtual`` true."""

    vm_filter: dict[str, object] | None = None
    """NetBox virtual machine filter, forwarded verbatim."""

    vm_ip_source: str = "primary_ip4"
    """Where a virtual machine's management address comes from."""

    timeout: float = 30.0
    """Seconds one HTTP request to NetBox may take."""

    @model_serializer(mode="wrap")
    def _omit_later_keys_at_their_defaults(self, handler: SerializerFunctionWrapHandler):  # noqa: ANN202
        """Leave the keys :data:`_NETBOX_KEYS_OMITTED_AT_DEFAULT` names out while at their default.

        The dump IS the normalized configuration, which
        :meth:`CompiledInventory.same_as` compares and the snapshot slug
        hashes. A key added after a table was written would otherwise enter
        both with its default filled in, and the unchanged table would look
        for its snapshot under a new name: a refetch on every project, and no
        offline fallback if NetBox is down during the upgrade. A model
        serializer rather than a field option, because it works across the
        whole pydantic range otto supports.

        NO return annotation, on purpose: pydantic builds the serialization
        JSON schema from it, and ``dict[str, Any]`` would replace the
        model's (its properties, ``url`` required, no extra keys) with an
        unrestricted object.
        """
        dumped: dict[str, Any] = handler(self)
        fields = type(self).model_fields
        for name in _NETBOX_KEYS_OMITTED_AT_DEFAULT:
            if name in dumped and getattr(self, name) == fields[name].default:
                del dumped[name]
        return dumped


_NETBOX_KEYS_OMITTED_AT_DEFAULT: frozenset[str] = frozenset(
    {"virtual_machines", "vm_filter", "vm_ip_source"}
)
"""NetBox keys newer than the first snapshot format: dumped only once set away from the default."""


@final
@dataclass(frozen=True)
class InventoryDeclaration:
    """One settings file's inventory tables and where they came from.

    The tables are ``[inventory]``, ``[creds]`` and ``[[inventories]]``.
    """

    origin: str
    """The settings file that declared it — for error text."""
    anchor_dir: Path
    """Directory relative paths anchor to (the project root; ``~/.otto`` for the user file)."""
    table: dict[str, Any] = field(default_factory=dict)
    creds_table: dict[str, Any] = field(default_factory=dict)
    """The same file's ``[creds]`` table, ``{}`` when absent (spec 2026-09-06 creds-store §4.2)."""
    inventories: list[dict[str, Any]] = field(default_factory=list)
    """The same file's ``[[inventories]]`` entries as raw tables, ``[]`` when none."""


@final
@dataclass(frozen=True)
class CompiledInventory:
    """An ``[inventory]`` table, prepared by its backend's configuration model."""

    prepared: "Prepared[InventoryEnv, Inventory, InventoryMetadata]"
    """The backend's parsed configuration, ready to build."""
    cache_ttl: timedelta
    """How long a snapshot of the inventory stays fresh."""
    anchor_dir: Path
    """Directory relative paths anchored to."""
    origin: str
    """The settings file that declared the table — for error text."""
    creds: "CompiledCreds | None" = None
    """The ``[creds]`` table the inventory's creds come from, if one is declared."""
    name: str = DEFAULT_INVENTORY
    """The name of the inventory, ``default`` for an ``[inventory]`` table.

    Read for the command otto tells the operator to run (``otto inventory
    refresh --inventory <name>``); not part of :meth:`binding_identity`."""

    def binding_identity(self) -> "BindingIdentity":
        """Return what makes two declarations one inventory: backend, configuration, ``cache_ttl``.

        A structural, hashable value: the inputs :meth:`same_as` compares and
        the snapshot slug derives from (less the policy keys).
        """
        return BindingIdentity(
            backend=self.prepared.backend,
            normalized=self.prepared.normalized,
            cache_ttl=self.cache_ttl,
        )

    def same_as(self, *, other: "CompiledInventory") -> bool:
        """Return whether this is the same inventory as *other*.

        Backend, normalized configuration AND ``cache_ttl`` must agree — every
        field the table configures, compared as :meth:`binding_identity`, the
        one definition. ``cache_ttl`` is in here because it is
        behaviour, not decoration: two projects declaring ``"0"`` and ``"7d"``
        would otherwise be "the same", and declaration order would silently
        decide whether the process caches at all (spec §8 requires identical
        tables). ``origin`` and ``anchor_dir`` are excluded, which is the whole
        point — two projects naming the same inventory from their own settings
        files, each anchoring it to its own root, are not a conflict. ``creds``
        is excluded too: ``default``'s is compared per table by
        :meth:`~otto.creds.config.CompiledCreds.same_as` in its own walk,
        which resolves independently of the inventory, and a named entry's
        beside this in ``_agreed``. The snapshot slug is
        derived from these inputs less the policy keys
        (:data:`~otto.inventory.cache.SNAPSHOT_POLICY_KEYS`, ``max_stale``):
        two declarations must agree on ``max_stale``, but changing it keeps
        the snapshot (:func:`~otto.inventory.cache.snapshot_slug_material`).
        """
        return self.binding_identity() == other.binding_identity()


@final
@dataclass(frozen=True, kw_only=True)
class BindingIdentity:
    """What makes two inventory declarations one inventory, as a hashable value.

    Compared by value, so a NetBox filter of ``1`` equals one of ``1.0``, as
    the parsed configurations do.
    """

    backend: str
    """The registered backend name."""
    normalized: "FrozenMap[str, object]"
    """The backend's parsed configuration, normalized (paths anchored, not resolved)."""
    cache_ttl: timedelta
    """How long a snapshot stays fresh."""


@final
@dataclass(frozen=True, kw_only=True)
class BuiltBinding:
    """The ``default`` walk's result: the binding, and the inventory constructed from it."""

    compiled: CompiledInventory | None
    """The winning declaration, its ``creds`` attached; ``None`` when nothing declares one."""
    creds: "CompiledCreds | None"
    """The credentials store ``default``'s walk resolved, or ``None``."""
    inventory: Inventory | None
    """The constructed ``default`` inventory, or ``None``."""


@final
@dataclass(frozen=True, kw_only=True, repr=False)
class ProtectedPaths:
    """The files no template may read: settings files, and every path their tables name.

    A frozen, hashable value, so a memo key can hold the catalog it was
    rendered under; equality and hashing are over :attr:`files` and
    :attr:`refusal`. Every path in :attr:`files` is resolved, symlinks
    collapsed, so an alias of a protected file is the same entry. The repr
    shows only how many files it holds: the paths are built from table
    strings, which may be tokens.
    """

    files: frozenset[Path]
    """The protected files, each resolved (``strict=False``)."""
    refusal: "str | None" = None
    """Why no template may render under this catalog; ``None`` for a catalog a render runs under.

    Set on a catalog that could not be established: :data:`NO_CATALOG`, and
    the catalog of a view whose user settings file cannot be read
    (:func:`unreadable_user_catalog`). The renderer asks this field, so the
    answer is the catalog's own: it survives a copy or a pickle, and "no
    catalog" never equals an empty one. The text names files, never a table
    value."""

    @override
    def __repr__(self) -> str:
        if self.refusal is not None:
            return "ProtectedPaths(<no catalog>)"
        count = len(self.files)
        return f"ProtectedPaths(<{count} file{'' if count == 1 else 's'}>)"

    def contains(self, path: Path) -> bool:
        """Return whether *path*, once resolved, is a protected file.

        A spelling match first. Failing that, an existing *path* is protected
        when it is the same file as an existing catalog entry (equal device and
        inode), so a hardlink, or another spelling on a case-insensitive
        filesystem, is caught. A path that cannot be resolved (a NUL byte, a
        symlink loop) or does not exist names no protected file.
        """
        resolved = resolved_path(path)
        if resolved is None:
            return False
        if resolved in self.files:
            return True
        identity = _identity(resolved)
        return identity is not None and identity in self._identities

    def contains_stat(self, st: os.stat_result) -> bool:
        """Return whether the file *st* describes is a protected file, by device and inode.

        For a caller that holds an open file rather than a path: its
        ``fstat`` is the identity, so the answer cannot be about a different
        file than the one being read. The same identities :meth:`contains`
        matches against.
        """
        return (st.st_dev, st.st_ino) in self._identities

    @functools.cached_property
    def _identities(self) -> frozenset[tuple[int, int]]:
        """Return ``(st_dev, st_ino)`` of every catalog file that exists, statted once.

        Computed on the first identity check, never at construction, and only
        ever by a stat (:func:`_identity`): nothing is opened. A file that
        cannot be statted (gone, unreadable directory) matches nothing. Not a
        field, so neither equality, hashing nor the repr sees it. The set is
        computed once per catalog value, so in a long-lived process a
        protected file that appears later, or is atomically replaced (a new
        inode), keeps only its spelling match, and an inode the filesystem
        reuses after a delete can deny an unrelated file.
        """
        return frozenset(i for file in self.files if (i := _identity(file)) is not None)

    def union(self, other: "ProtectedPaths") -> "ProtectedPaths":
        """Return the catalog protecting every file of this one and of *other*.

        It refuses to render when either does, with the first refusal.
        """
        return ProtectedPaths(
            files=self.files | other.files,
            refusal=self.refusal if self.refusal is not None else other.refusal,
        )


NO_CATALOG: Final = ProtectedPaths(
    files=frozenset(),
    refusal=(
        "no protected-file catalog: this inventory view or inventory was not built "
        "from settings (otto.inventory.build_inventory_view, otto.inventory.build_inventory), "
        "so no template is rendered through it"
    ),
)
"""THE one spelling of "no catalog", which a view or inventory no builder made carries.

A hand-built :class:`~otto.inventory.InventoryView` and a
:class:`~otto.inventory.json_backend.JsonInventory` with no ``env`` carry it.
Its :attr:`~ProtectedPaths.refusal` is set, so no render runs under it
(:class:`~otto.templating.imports.ImportPolicy` refuses it), and a template is
never rendered unprotected by accident. A hand-written lab or inventory file
reads under it exactly as before, because only a render consults a catalog.
Its text holds for every caller that meets it; a caller that knows how it was
reached adds its own remedy (:func:`otto.load_lab <otto.labs.loader.load_lab>`
does, for ``inventory=`` with a ``.jsonnet`` search path)."""


def unreadable_user_catalog(
    partial: ProtectedPaths, *, user_settings_file: "Path | None"
) -> ProtectedPaths:
    """Return the catalog of a view whose user settings file cannot be read: it renders nothing.

    The files that user file names cannot be known, so no complete catalog can
    be established, and the *partial* one (every other file) would let a
    template read them: it keeps *partial*'s files, and refuses to render.
    The refusal names the file only, never the read error, which may quote a
    value of the file.
    """
    path = _user_file(user_settings_file)
    return ProtectedPaths(
        files=partial.files,
        refusal=(
            f"no protected-file catalog: the user settings file {path} cannot be read, "
            "so the files it names cannot be protected; no template is rendered until it reads"
        ),
    )


def _identity(path: Path) -> tuple[int, int] | None:
    """Return *path*'s ``(st_dev, st_ino)``; ``None`` when it is absent or cannot be statted.

    The stat is :func:`~otto.config.corpus_snapshot.classify`'s, the one stat
    classification, so an unreadable path reads as no identity here exactly
    as it reads as unreadable there.
    """
    from ..config.corpus_snapshot import classify  # deferred: see module docstring

    found = classify(path)
    if not isinstance(found, os.stat_result):
        return None
    return (found.st_dev, found.st_ino)


def _as_protected_path(value: str, anchor: Path) -> Path | None:
    """Return the file the table string *value* names, anchored at *anchor* and resolved.

    The anchoring rule is :func:`~otto.utils.anchor_path`'s, the one every
    path in a settings table follows. ``None`` when *value* cannot name a
    file: empty, a ``~user`` that does not expand, a NUL byte, a symlink
    loop. Such a string is skipped silently, and no error carries it,
    because an arbitrary table string may be a token.
    """
    if not value:
        return None
    try:
        anchored = anchor_path(Path(value), anchor, quote=False)
    except ValueError:
        return None
    return resolved_path(anchored)


def _strings(value: object) -> Iterator[str]:
    """Yield every string value in *value*, at any depth; mapping keys are not values.

    A model is walked through its dump, so a parsed table yields what its raw
    form would; a path value yields its spelling.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", warnings=False)
    if isinstance(value, str):
        yield value
    elif isinstance(value, PurePath):
        yield str(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)


def _anchored_strings(values: Iterable[str], anchor: Path) -> Iterator[Path]:
    """Yield the protected path each of *values* names at *anchor*, skipping the rest."""
    for value in values:
        path = _as_protected_path(value, anchor)
        if path is not None:
            yield path


def _declared_file(decl: InventoryDeclaration) -> Path:
    """Return the settings file *decl* was read from: its ``origin``, which names that file."""
    return Path(decl.origin)


def protected_paths(
    *,
    settings_files: Sequence[Path],
    declarations: Sequence[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: Path | None,
) -> ProtectedPaths:
    """Return the catalog of files no template may read, from already-read tables.

    It holds every file of *settings_files* (every active project's, with an
    inventory table or not), the user settings file (*user_settings_file*,
    else the default path), each declaration's own file, and every string,
    at any depth, of each declaration's ``[inventory]``, ``[creds]`` and
    ``[[inventories]]`` tables and of *user_settings*' (the snapshot its
    owner read). Each string is anchored like any table path, at its
    declaration's root or the user file's directory, then resolved.

    Raw strings rather than parsed configurations: nothing is prepared, so a
    broken declaration no operation uses fails nothing here, and a backend's
    path nested at any depth is covered. A string that is not a path names a
    file nobody has, or is skipped. Nothing is opened.
    """
    files: set[Path] = set()
    for path in (*settings_files, _user_file(user_settings_file)):
        resolved = resolved_path(path)
        if resolved is not None:
            files.add(resolved)
    for decl in declarations:
        declared = resolved_path(_declared_file(decl))
        if declared is not None:
            files.add(declared)
        tables = (decl.table, decl.creds_table, decl.inventories)
        files.update(_anchored_strings(_strings(tables), decl.anchor_dir))
    if user_settings is not None:
        tables = (user_settings.inventory, user_settings.creds, user_settings.inventories)
        anchor = _user_file(user_settings_file).parent
        files.update(_anchored_strings(_strings(tables), anchor))
    return ProtectedPaths(files=frozenset(files))


def standalone_protection(env: InventoryEnv) -> ProtectedPaths:
    """Return what a table compiled on its own protects: its settings file and its own paths.

    The rule for an :class:`~otto.inventory.registry.InventoryEnv` whose
    ``protected`` is ``None``, which no view built: the declaring file
    (``settings_file``, resolved, when one is known) and every string of the
    table itself (``own_table``), anchored at ``anchor_dir`` as
    :func:`protected_paths` anchors them. ``origin`` is a label and is never
    read as a path.
    """
    files = set(_anchored_strings(env.own_table, env.anchor_dir))
    declared = None if env.settings_file is None else resolved_path(env.settings_file)
    if declared is not None:
        files.add(declared)
    return ProtectedPaths(files=frozenset(files))


def env_protection(env: InventoryEnv) -> ProtectedPaths:
    """Return the catalog a template rendered for *env*'s table runs under: THE one choice.

    The view's catalog (``env.protected()``) when a view built the table, so
    a template can read no file any active project or the user file
    protects; :func:`standalone_protection` when ``env.protected`` is
    ``None`` (a table compiled on its own). Never a view's empty default: the
    catalog comes with the table, wherever the inventory is later read from.
    """
    return env.protected() if env.protected is not None else standalone_protection(env)


def compile_inventory(
    *, cfg: "InventoryConfigSpec | NamedInventorySpec", anchor_dir: Path, origin: str
) -> CompiledInventory:
    """Prepare the table's backend keys with the selected backend's configuration model.

    *cfg* is an ``[inventory]`` table
    (:class:`~otto.models.settings.InventoryConfigSpec`) or an
    ``[[inventories]]`` entry (:class:`~otto.models.settings.NamedInventorySpec`).
    Every key the envelope does not declare (``cfg.model_extra``) is parsed
    by the model the backend registered, with an
    :class:`~otto.inventory.registry.InventoryEnv` carrying *anchor_dir* and
    *origin*; an unknown key or a bad value fails here, naming the backend,
    the module that registered it and *origin*. On ``[inventory]`` that is
    every key but ``backend`` and ``cache_ttl``; an entry's ``name`` and
    ``creds`` are envelope fields too, so they never reach the backend.

    Anchoring, not resolving: ``anchor_dir`` makes a committed relative path
    resolve stably wherever the project is checked out. Symlink resolution
    happens later, in the backend's factory, where the path reaches the
    object whose ``fingerprint()`` the spec says is the resolved one (§9.1).

    The env's ``own_table`` holds every string of *cfg*, and its
    ``protected`` is ``None``: a table compiled on its own protects what
    :func:`standalone_protection` says. Its ``settings_file`` is *origin*
    when *origin* names an existing file, else ``None``: *origin* is a label,
    so only a file that is there is taken for the declaring one. The view
    builders compile through the same path with their catalog and the
    declaring file instead.

    Raises:
        otto.inventory.errors.InventoryConstructionError: The backend is not
            registered, or its keys do not parse.
    """
    named = Path(origin)
    return _compile_inventory(
        cfg=cfg,
        anchor_dir=anchor_dir,
        origin=origin,
        protected=None,
        # Resolved now, so a later change of working directory cannot move it.
        settings_file=resolved_path(named) if named.is_file() else None,
    )


def _compile_inventory(
    *,
    cfg: "InventoryConfigSpec | NamedInventorySpec",
    anchor_dir: Path,
    origin: str,
    protected: "Callable[[], ProtectedPaths] | None",
    settings_file: "Path | None",
) -> CompiledInventory:
    """Compile *cfg* as :func:`compile_inventory` does, its env carrying *protected*.

    *settings_file* is the declaring file, which the env carries for
    :func:`standalone_protection`.
    """
    from ..models.settings import NamedInventorySpec  # deferred: see module docstring

    prepared = INVENTORY_BACKENDS.prepare(
        name=cfg.backend,
        raw=dict(cfg.model_extra or {}),
        env=InventoryEnv(
            anchor_dir,
            origin,
            protected=protected,
            own_table=tuple(_strings(cfg)),
            settings_file=settings_file,
        ),
        source=origin,
    )
    return CompiledInventory(
        prepared=prepared,
        cache_ttl=parse_cache_ttl(cfg.cache_ttl),
        anchor_dir=anchor_dir,
        origin=origin,
        name=cfg.name if isinstance(cfg, NamedInventorySpec) else DEFAULT_INVENTORY,
    )


def _maybe_cached(inventory: Inventory, compiled: CompiledInventory) -> Inventory:
    """Wrap *inventory* in a snapshot cache if it is a remote backend that wants one (§9.5).

    Three conditions, in the order they are cheapest to check:

    - The backend's registration states ``snapshot_cache=True``. The built-in
      ``json`` states ``False``, so it is passed by before the
      ``fingerprint()`` test below: ``JsonInventory.fingerprint()`` stats the
      file — and construction does no I/O, which is what lets a lab with no
      referenced entry never touch a broken inventory at all.
    - ``cache_ttl`` greater than zero. ``"0"`` means "every process fetches",
      the behaviour of an uncached backend (§9.5).
    - ``fingerprint()`` is ``None``, the backend's own statement that it cannot
      report freshness. NetBox says so unconditionally; a third-party backend
      that returns a string opts OUT of the cache by design, because it has a
      better answer than a timestamp. Asking is cheap because the protocol
      requires ``fingerprint()`` to answer from local state and never fetch
      or probe the network, as both built-ins do.

    A backend that passes all three and ALSO supplies ``creds`` is refused
    rather than wrapped. A snapshot never holds credentials by construction
    (:func:`~otto.inventory.snapshot.stated_fields` drops them, §9.4/§9.5), so caching such a
    backend would answer WITH creds off the wire and WITHOUT them for the rest
    of the TTL — a referenced unix host failing validation on alternate runs,
    with nothing in the message naming the cache. Neither half is negotiable,
    so the configuration is.
    """
    prepared = compiled.prepared
    if not prepared.metadata.snapshot_cache or compiled.cache_ttl <= timedelta(0):
        return inventory
    if inventory.fingerprint() is not None:
        return inventory
    if "creds" in inventory.supplies:
        # The INNER backend's supplies, before `construct_inventory` adds the
        # creds overlay: that overlay is outermost and always claims `creds`,
        # so checking the constructed object would refuse the configuration
        # §9.4 actually recommends.
        raise InventoryError(
            f"{compiled.origin}: backend {prepared.backend!r} supplies 'creds', which a "
            'snapshot cannot carry; set cache_ttl = "0" for this backend, or have the '
            "backend leave creds to a [creds] store"
        )
    # Function-local, both of them: `otto.inventory` sits on the bootstrap path
    # and must not pull `otto.config` in at module scope — see this module's
    # own import-discipline docstring. `snapshot_cache_dir` lives in
    # otto.config.home because that module owns every path under the home.
    from ..config.home import snapshot_cache_dir
    from .cache import SnapshotCache, snapshot_slug_material

    return SnapshotCache(
        inventory,
        ttl=compiled.cache_ttl,
        cache_dir=snapshot_cache_dir(),
        slug_material=snapshot_slug_material(
            prepared.backend, inventory.label, prepared.normalized, cache_ttl=compiled.cache_ttl
        ),
        inventory_name=compiled.name,
    )


def construct_inventory(*, compiled: CompiledInventory) -> Inventory:
    """Build the backend, then the core wrappers (the snapshot cache, then creds).

    The backend's registered factory builds it from the prepared
    configuration; a factory that fails, or builds something that is not an
    inventory, raises :class:`~otto.inventory.errors.InventoryConstructionError`
    naming the settings file, the backend and the module that registered it.

    A compiled ``[creds]`` table means the inventory supplies ``creds`` (spec
    2026-09-06 §5.4): the overlay merges the store's entries UNDER the
    record's, by login. Construction still does no I/O — the overlay reads
    the store on first lookup, and the cache reads nothing until then either.

    ORDER MATTERS: the cache goes on first, the creds overlay OUTERMOST. The
    snapshot must never contain credentials (§9.4/§9.5), and a cache wrapped
    around the overlay would be caching exactly that; the doctor also reads
    ``isinstance(inventory, CredsOverlay)`` to report the creds store's mode.
    """
    inventory = _maybe_cached(INVENTORY_BACKENDS.build(prepared=compiled.prepared), compiled)
    if compiled.creds is not None:
        # Function-local, both: otto.inventory is on the bootstrap path and
        # otto.creds is only needed once a store is actually declared.
        from ..creds.config import construct_creds_store

        with _creds_errors_as_inventory_errors():
            store = construct_creds_store(compiled=compiled.creds)
        inventory = CredsOverlay(inventory, store=store)
    return inventory


@contextmanager
def _creds_errors_as_inventory_errors() -> Generator[None, None, None]:
    """Turn every creds error into an inventory error, so callers catch one type.

    A ``CredsConstructionError`` becomes an ``InventoryConstructionError``, so
    it stays a construction error (and a ``ValueError``) to its catcher; the
    creds error rides along as the cause. Entered only once a store is
    declared: ``otto.creds`` stays off the path of a configuration with none.
    """
    from ..creds.errors import CredsConstructionError, CredsError

    try:
        yield
    except CredsConstructionError as e:
        raise InventoryConstructionError(str(e)) from e
    except CredsError as e:
        raise InventoryError(str(e)) from e


def _shaped_table(data: "Mapping[str, Any]", key: str, origin: str) -> "Mapping[str, Any]":
    """Return the ``[key]`` table of *data*, ``{}`` when absent; refuse any other shape."""
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        # A ValueError, not TypeError: a malformed settings file, which every
        # caller already reports as a configuration problem.
        raise ValueError(  # noqa: TRY004 — a settings-file error, as the doctor reports it
            f"{origin}: [{key}] must be a table, got {describe_refused(value)}"
        )
    return value


def inventory_declaration(
    *, data: "Mapping[str, Any]", root: Path, origin: str
) -> InventoryDeclaration | None:
    """Return the inventory tables the settings file *data* declares; ``None`` when it has none.

    The one reader of which inventory tables a settings file declares:
    ``[inventory]``, ``[creds]`` and ``[[inventories]]``, read raw. Bootstrap
    (:func:`build_inventory`, :func:`build_inventory_view`) calls it over
    each project's parsed settings and the doctor over the file itself, so
    both see one declaration. *root* is the project root, what relative paths
    anchor to; *origin* names the file in error text.

    A file that binds ``default`` only through an ``[[inventories]]`` entry
    named ``default`` is kept, with or without ``[creds]``. An empty table
    counts as absent.

    Raises:
        ValueError: A table has the wrong shape: ``[inventory]`` or ``[creds]``
            that is not a table, ``inventories`` that is not an array of
            tables (``[inventories]`` written with single brackets), or an
            entry that is not a table (``inventories = ["vms"]``). The message
            names *origin* and the key.
    """
    table = _shaped_table(data, "inventory", origin)
    creds = _shaped_table(data, "creds", origin)
    raw = data.get("inventories")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError(  # noqa: TRY004 — a settings-file error, as the doctor reports it
            f"{origin}: inventories must be an array of tables, written [[inventories]] "
            f"with double brackets, got {type(raw).__name__}"
        )
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ValueError(  # noqa: TRY004 — a settings-file error, as the doctor reports it
                f"{origin}: inventories[{index}] must be a table ([[inventories]] with name "
                f"and backend), got {describe_refused(entry)}"
            )
    if not table and not creds and not raw:
        return None
    return InventoryDeclaration(
        origin=origin,
        anchor_dir=root,
        table=dict(table),
        creds_table=dict(creds),
        inventories=[dict(entry) for entry in raw],
    )


def _settings_files(projects: "Sequence[Project]") -> tuple[Path, ...]:
    """Return each project's settings file, whether or not it declares an inventory."""
    # Deferred: see the module docstring. TOML_SETTINGS_PATH rather than a
    # hand-spelled ".otto"/"settings.toml" — one owner for where a project's
    # settings live, so a relocation cannot leave this error text lying.
    from ..config.scope import TOML_SETTINGS_PATH

    return tuple(project.root / TOML_SETTINGS_PATH for project in projects)


def _project_declarations(projects: "Sequence[Project]") -> list[InventoryDeclaration]:
    """Read each project's inventory declaration through :func:`inventory_declaration`."""
    declarations = []
    for project, settings_file in zip(projects, _settings_files(projects), strict=True):
        try:
            declaration = inventory_declaration(
                data=project.settings, root=project.root, origin=str(settings_file)
            )
        except ValueError as e:
            raise InventoryError(str(e)) from e
        if declaration is not None:
            declarations.append(declaration)
    return declarations


def _read_user_settings(path: "Path | None") -> "UserSettingsModel | None":
    """Read the user settings file once; a broken one is an ``InventoryError`` naming it."""
    from ..config.user_settings import load_user_settings  # deferred: see module docstring

    try:
        return load_user_settings(path=path)
    except ValueError as e:
        raise InventoryError(str(e)) from e


def _user_file(user_settings_file: "Path | None") -> Path:
    """Return the user settings file's path: *user_settings_file*, else the default one."""
    if user_settings_file is not None:
        return user_settings_file
    from ..config.user_settings import user_settings_path  # deferred: see module docstring

    return user_settings_path()


def _entry_origin(origin: str, name: str) -> str:
    """Name one ``[[inventories]]`` entry of the settings file *origin*, for error text."""
    return f"{origin} [[inventories]] {name!r}"


def _file_entries(decl: InventoryDeclaration) -> "dict[str, NamedInventorySpec]":
    """Parse *decl*'s ``[[inventories]]`` entries by name, and refuse a contradictory file.

    The refusal is :func:`~otto.models.settings.check_inventory_declarations`
    for a project file, D6 included, so a project whose ``[creds]`` would
    bind to the process ``default`` is refused wherever it is walked.
    """
    from ..models.settings import (  # deferred: see module docstring
        NamedInventorySpec,
        bound_table,
        check_inventory_declarations,
    )

    try:
        specs = [NamedInventorySpec.model_validate(entry) for entry in decl.inventories]
    except ValidationError as e:
        raise InventoryError(f"{decl.origin}: [[inventories]] {e}") from e
    try:
        check_inventory_declarations(
            inventory=bound_table(decl.table),
            creds=bound_table(decl.creds_table),
            inventories=specs,
            project=True,
        )
    except ValueError as e:
        raise InventoryError(f"{decl.origin}: {e}") from e
    return {spec.name: spec for spec in specs}


def _user_entry(
    user_settings: "UserSettingsModel | None", name: str
) -> "NamedInventorySpec | None":
    """Return the user file's ``[[inventories]]`` entry named *name*, if any."""
    if user_settings is None:
        return None
    return next((e for e in user_settings.inventories if e.name == name), None)


def _agreed(
    candidates: list[CompiledInventory], *, what: str, one: str = "inventory"
) -> CompiledInventory | None:
    """Return the one inventory every project declaring a name agrees on; ``None`` for none.

    The agreement check for every name: :meth:`CompiledInventory.same_as`,
    and the credentials binding each candidate carries (both ``None``, or
    :meth:`~otto.creds.config.CompiledCreds.same_as`; a one-sided absence is
    a conflict). ``default``'s candidates carry no credentials here: its
    ``[creds]`` is its own walk, ``_resolve_creds``. *what* names the tables
    and *one* the inventory in the error text.

    Raises:
        otto.inventory.errors.InventoryError: Two candidates disagree; the
            message names both declarations.
    """
    if not candidates:
        return None
    first = candidates[0]
    for other in candidates[1:]:
        creds_agree = (
            first.creds is None and other.creds is None
            if first.creds is None or other.creds is None
            else other.creds.same_as(other=first.creds)
        )
        if not (other.same_as(other=first) and creds_agree):
            raise InventoryError(
                f"two active projects declare different {what}: {first.origin} "
                f"and {other.origin}; a process has exactly one {one}"
            )
    return first


@final
@dataclass(frozen=True)
class _DefaultTables:
    """One project file's ``default`` tables: ``[inventory]``/``[creds]``, or its entry."""

    decl: InventoryDeclaration
    entry: "NamedInventorySpec | None"
    """The file's ``[[inventories]]`` entry named ``default``, if any."""

    def declares_creds(self) -> bool:
        """Return whether the file binds ``default``'s credentials (``[creds]`` or the entry's)."""
        return bool(self.decl.creds_table) or (
            self.entry is not None and self.entry.creds is not None
        )

    def compiled_creds(self) -> "CompiledCreds | None":
        """Compile the file's ``[creds]``, else its ``default`` entry's ``creds``, else ``None``.

        Raises the store's ``CredsError``; ``_resolve_creds`` translates it.
        """
        from ..creds.config import compile_creds, compile_creds_table  # deferred: bootstrap path

        decl = self.decl
        if decl.creds_table:
            return compile_creds_table(
                table=dict(decl.creds_table), anchor_dir=decl.anchor_dir, origin=decl.origin
            )
        if self.entry is None or self.entry.creds is None:
            return None
        return compile_creds(
            cfg=self.entry.creds,
            anchor_dir=decl.anchor_dir,
            origin=f"{_entry_origin(decl.origin, DEFAULT_INVENTORY)} creds",
        )


def _default_tables(declarations: list[InventoryDeclaration]) -> list[_DefaultTables]:
    """Pair each declaration with its ``default`` entry; a file without entries is read as today."""
    return [
        _DefaultTables(
            decl=d, entry=_file_entries(d).get(DEFAULT_INVENTORY) if d.inventories else None
        )
        for d in declarations
    ]


def _compile_declaration(
    tables: _DefaultTables, *, protected: "Callable[[], ProtectedPaths] | None"
) -> CompiledInventory | None:
    """Compile one file's ``default``: its ``[inventory]``, else its ``default`` entry."""
    decl = tables.decl
    if decl.table:
        from ..models.settings import InventoryConfigSpec  # deferred: see module docstring

        try:
            cfg = InventoryConfigSpec.model_validate(decl.table)
        except ValidationError as e:
            raise InventoryError(f"{decl.origin}: [inventory] {e}") from e
        return _compile_inventory(
            cfg=cfg,
            anchor_dir=decl.anchor_dir,
            origin=decl.origin,
            protected=protected,
            settings_file=_declared_file(decl),
        )
    if tables.entry is not None:
        return _compile_inventory(
            cfg=tables.entry,
            anchor_dir=decl.anchor_dir,
            origin=_entry_origin(decl.origin, DEFAULT_INVENTORY),
            protected=protected,
            settings_file=_declared_file(decl),
        )
    return None


def _resolve_creds(
    tables: list[_DefaultTables],
    *,
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None",
) -> "CompiledCreds | None":
    """Run the §4.2 walk for ``default``'s ``[creds]``: projects that declare one, else the user's.

    Independent of the ``[inventory]`` walk — each table finds its own first
    declaration. A ``default`` entry's ``creds`` sub-table is that file's
    ``[creds]`` (one file may not declare both). Every ``CredsError`` becomes
    an ``InventoryError`` here so the callers of ``build_inventory`` keep
    catching one type; a ``CredsConstructionError`` becomes an
    ``InventoryConstructionError``, so it stays a construction error and a
    ``ValueError``.

    Checks emptiness BEFORE importing ``otto.creds``: this runs on every
    ``build_inventory`` call, declared or not, and the import-budget guard
    caps ``otto.creds`` off the bootstrap path — a lab with no ``[creds]``
    anywhere must not pay for the package at all.
    """
    user_entry = _user_entry(user_settings, DEFAULT_INVENTORY)
    user_creds = (
        user_settings.creds
        if user_settings is not None and user_settings.creds is not None
        else user_entry.creds
        if user_entry is not None
        else None
    )
    if not any(t.declares_creds() for t in tables) and user_creds is None:
        return None
    from ..creds.config import compile_creds  # deferred: bootstrap path

    with _creds_errors_as_inventory_errors():
        compiled = [c for t in tables if (c := t.compiled_creds()) is not None]
        for other in compiled[1:]:
            if not other.same_as(other=compiled[0]):
                raise InventoryError(
                    f"two active projects declare different [creds] tables: {compiled[0].origin} "
                    f"and {other.origin}; a process has exactly one creds store"
                )
        if compiled:
            return compiled[0]
        if user_creds is not None:
            path = _user_file(user_settings_file)
            origin = (
                str(path)
                if user_settings is not None and user_settings.creds is not None
                else f"{_entry_origin(str(path), DEFAULT_INVENTORY)} creds"
            )
            return compile_creds(cfg=user_creds, anchor_dir=path.parent, origin=origin)
    return None


def build_binding_from_declarations(
    *,
    declarations: list[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None" = None,
    protected: "Callable[[], ProtectedPaths] | None" = None,
) -> BuiltBinding:
    """Run the §8 walk for ``default`` over already-read tables: the one place that decides it.

    Two independent walks, as before named inventories: ``[inventory]``
    (projects that declare one must agree, else the user file) and
    ``[creds]`` (``_resolve_creds``). A file's ``[[inventories]]`` entry named
    ``default`` takes part as its ``[inventory]`` would, and that entry's
    ``creds`` sub-table as its ``[creds]``. *user_settings_file* names the
    file *user_settings* came from (for error text and anchoring); it
    defaults to :func:`~otto.config.user_settings.user_settings_path`.
    *protected* is the view's protected-file catalog, which the compiled
    table's env carries; ``None`` outside a view.

    Raises:
        otto.inventory.errors.InventoryError: A table does not compile, two
            projects disagree, a ``[creds]`` has no inventory to key against,
            or a project file contradicts itself (D6 included).
    """
    tables = _default_tables(declarations)
    compiled = _agreed(
        [c for t in tables if (c := _compile_declaration(t, protected=protected)) is not None],
        what="[inventory] tables",
    )
    creds = _resolve_creds(
        tables, user_settings=user_settings, user_settings_file=user_settings_file
    )
    if compiled is None and user_settings is not None:
        entry = _user_entry(user_settings, DEFAULT_INVENTORY)
        cfg = user_settings.inventory if user_settings.inventory is not None else entry
        if cfg is not None:
            path = _user_file(user_settings_file)
            origin = (
                str(path)
                if user_settings.inventory is not None
                else _entry_origin(str(path), DEFAULT_INVENTORY)
            )
            compiled = _compile_inventory(
                cfg=cfg,
                anchor_dir=path.parent,
                origin=origin,
                protected=protected,
                settings_file=path,
            )
    if compiled is None:
        if creds is not None:
            raise InventoryError(
                f"{creds.origin}: [creds] is keyed by inventory key and no [inventory] is "
                "declared in either settings file; declare one, or remove [creds]"
            )
        return BuiltBinding(compiled=None, creds=None, inventory=None)
    compiled = dataclasses.replace(compiled, creds=creds)
    return BuiltBinding(
        compiled=compiled, creds=creds, inventory=construct_inventory(compiled=compiled)
    )


def _compile_named(
    spec: "NamedInventorySpec",
    *,
    anchor_dir: Path,
    origin: str,
    protected: "Callable[[], ProtectedPaths] | None",
    settings_file: Path,
) -> CompiledInventory:
    """Compile one ``[[inventories]]`` entry, its own ``creds`` sub-table attached (spec 7.5)."""
    compiled = _compile_inventory(
        cfg=spec,
        anchor_dir=anchor_dir,
        origin=origin,
        protected=protected,
        settings_file=settings_file,
    )
    if spec.creds is None:
        return compiled
    from ..creds.config import compile_creds  # deferred: bootstrap path

    with _creds_errors_as_inventory_errors():
        creds = compile_creds(cfg=spec.creds, anchor_dir=anchor_dir, origin=f"{origin} creds")
    return dataclasses.replace(compiled, creds=creds)


def _named_compiled(
    name: str,
    *,
    declarations: list[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None",
    protected: "Callable[[], ProtectedPaths] | None",
) -> CompiledInventory | None:
    """Run the walk for one name but ``default``: the declaring projects, else the user file."""
    candidates = [
        _compile_named(
            _file_entries(d)[name],
            anchor_dir=d.anchor_dir,
            origin=_entry_origin(d.origin, name),
            protected=protected,
            settings_file=_declared_file(d),
        )
        for d in declarations
        if name in _entry_names(d.inventories)
    ]
    compiled = _agreed(
        candidates, what=f"[[inventories]] entries named {name!r}", one=f"inventory named {name!r}"
    )
    if compiled is not None:
        return compiled
    entry = _user_entry(user_settings, name)
    if entry is None:
        return None
    path = _user_file(user_settings_file)
    return _compile_named(
        entry,
        anchor_dir=path.parent,
        origin=_entry_origin(str(path), name),
        protected=protected,
        settings_file=path,
    )


def _key_of(compiled: CompiledInventory, inventory: Inventory) -> BindingKey:
    """Return the :class:`~otto.inventory.view.BindingKey` of one built name."""
    return binding_key(
        binding=compiled.binding_identity(),
        creds=None if compiled.creds is None else compiled.creds.binding_identity(),
        inventory=inventory,
    )


@final
@dataclass(frozen=True, kw_only=True)
class _BuiltMember:
    """One built name of a view: its inventory and its memo key.

    Not :class:`BuiltBinding`, which is the ``default`` walk's binding (the
    compiled table and its separately resolved creds) rather than a built name.
    """

    inventory: Inventory
    """The constructed inventory."""
    key: BindingKey
    """Its :class:`~otto.inventory.view.BindingKey`."""


def _built_named(
    name: str,
    *,
    declarations: list[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None",
    protected: "Callable[[], ProtectedPaths] | None",
) -> "_BuiltMember | None":
    """Build one name but ``default`` by its own walk, with its key; ``None`` when undeclared."""
    compiled = _named_compiled(
        name,
        declarations=declarations,
        user_settings=user_settings,
        user_settings_file=user_settings_file,
        protected=protected,
    )
    if compiled is None:
        return None
    inventory = construct_inventory(compiled=compiled)
    return _BuiltMember(inventory=inventory, key=_key_of(compiled, inventory))


def build_named_inventory_from_declarations(
    *,
    name: str,
    declarations: list[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None" = None,
) -> "Inventory | None":
    """Build the one inventory named *name* (not ``default``) over already-read tables.

    The walk every view builder runs for that name: the declaring projects
    among *declarations*, which must agree, else *user_settings*' entry.
    ``None`` when neither declares it. Nothing else is built, so another
    name's breakage, ``default``'s included, never fails this one. The
    doctor calls it over a project's own file alone, to check exactly what
    that file declares.

    Raises:
        otto.inventory.errors.InventoryError: The entry does not compile, the
            declaring projects disagree, or a declaring file contradicts itself.
    """
    built = _built_named(
        name,
        declarations=declarations,
        user_settings=user_settings,
        user_settings_file=user_settings_file,
        protected=None,
    )
    return None if built is None else built.inventory


def _entry_names(entries: "Sequence[NamedInventorySpec | Mapping[str, Any]]") -> set[str]:
    """Return the names *entries* declare, read by :func:`~otto.models.settings.entry_name`.

    A raw entry whose ``name`` is not a string names nothing here; parsing it
    (``_file_entries``) refuses it.
    """
    from ..models.settings import entry_name  # deferred: see module docstring

    return {name for entry in entries if isinstance(name := entry_name(entry), str)}


def _named_inventories(
    declarations: list[InventoryDeclaration], user_settings: "UserSettingsModel | None"
) -> set[str]:
    """Return every ``[[inventories]]`` name other than ``default`` the files declare."""
    names = set().union(*(_entry_names(d.inventories) for d in declarations))
    if user_settings is not None:
        names |= _entry_names(user_settings.inventories)
    return names - {DEFAULT_INVENTORY}


def _declares_default(
    declarations: list[InventoryDeclaration], user_settings: "UserSettingsModel | None"
) -> bool:
    """Return whether any file declares a ``default`` table, so building it answers or raises.

    A file binding ``default`` (:func:`~otto.models.settings.binds_default`)
    or declaring ``[creds]``, which the walk refuses without an inventory.
    """
    from ..models.settings import binds_default, bound_table  # deferred: see module docstring

    if any(
        d.creds_table or binds_default(inventory=bound_table(d.table), inventories=d.inventories)
        for d in declarations
    ):
        return True
    return user_settings is not None and (
        user_settings.creds is not None
        or binds_default(inventory=user_settings.inventory, inventories=user_settings.inventories)
    )


def _bare_refused_by(declarations: list[InventoryDeclaration]) -> dict[Path, str]:
    """Return project root -> settings file for each project that refuses bare references."""
    return {
        d.anchor_dir: d.origin
        for d in declarations
        if refuses_bare(inventory=d.table, inventories=d.inventories)
    }


@final
@dataclass(frozen=True, kw_only=True)
class _EagerBuild:
    """What :func:`_build_eagerly` built: ``default``'s binding and every other name."""

    binding: BuiltBinding
    """The ``default`` walk's binding, built unconditionally."""
    named: Mapping[str, _BuiltMember]
    """Every other declared name's inventory and key, in sorted name order."""


def _build_eagerly(
    *,
    declarations: list[InventoryDeclaration],
    user_settings: "Callable[[], UserSettingsModel | None]",
    binding: Callable[[], BuiltBinding],
    named: "Callable[[str], _BuiltMember | None]",
) -> _EagerBuild:
    """Build every inventory a view holds, in the one eager order: THE eager-validation core.

    First the user settings must have read (*user_settings* raises for a
    broken file); then ``default``'s binding is resolved, unconditionally;
    then every other name the files declare, in sorted order. The first
    failure raises, so two broken names always report the same one, whichever
    route asks. The builders are the caller's: a view built now passes its
    direct builders (:func:`build_inventory_view_from_declarations`), a
    deferred view its own memoized ones (:func:`require_eager`).

    Raises:
        otto.inventory.errors.InventoryError: The user file cannot be read, or
            a name fails to build.
    """
    user = user_settings()
    bound = binding()
    members: dict[str, _BuiltMember] = {}
    for name in sorted(_named_inventories(declarations, user)):
        member = named(name)
        if member is None:  # pragma: no cover - every listed name is declared somewhere
            continue
        members[name] = member
    return _EagerBuild(binding=bound, named=members)


def build_inventory_view_from_declarations(
    *,
    declarations: list[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None" = None,
    settings_files: Sequence[Path] = (),
) -> InventoryView:
    """Build every inventory the files declare, by name, over already-read tables.

    ``default`` comes from :func:`build_binding_from_declarations`, the one
    walk that decides it; each other name from its own walk, projects over
    the user file, every declaring project agreeing on backend, settings,
    cache policy and credentials binding. Every project file is checked by
    :func:`~otto.models.settings.check_inventory_declarations` (D6 included)
    as it is walked.

    The view's :attr:`~otto.inventory.InventoryView.protected` is
    :func:`protected_paths` over these tables and *settings_files* (the
    active projects' settings files, those declaring nothing included),
    computed on first call; every inventory's env carries the same one.

    Raises:
        otto.inventory.errors.InventoryError: Any name fails to build, or a
            project file contradicts itself.
    """
    protected = functools.cache(
        functools.partial(
            protected_paths,
            settings_files=tuple(settings_files),
            declarations=tuple(declarations),
            user_settings=user_settings,
            user_settings_file=user_settings_file,
        )
    )
    built = _build_eagerly(
        declarations=declarations,
        user_settings=lambda: user_settings,
        binding=functools.partial(
            build_binding_from_declarations,
            declarations=declarations,
            user_settings=user_settings,
            user_settings_file=user_settings_file,
            protected=protected,
        ),
        named=functools.partial(
            _built_named,
            declarations=declarations,
            user_settings=user_settings,
            user_settings_file=user_settings_file,
            protected=protected,
        ),
    )
    binding = built.binding
    inventories: dict[str, Inventory] = {}
    keys: dict[str, BindingKey] = {}
    if binding.inventory is not None:
        inventories[DEFAULT_INVENTORY] = binding.inventory
        # A binding without its compiled table (a stand-in's) has no key, so
        # memo_identity() refuses the view rather than inventing an identity.
        if binding.compiled is not None:
            keys[DEFAULT_INVENTORY] = _key_of(binding.compiled, binding.inventory)
    for name, member in built.named.items():
        inventories[name] = member.inventory
        keys[name] = member.key
    return InventoryView(
        inventories=inventories,
        keys=keys,
        bare_refused_by=_bare_refused_by(declarations),
        protected=protected,
    )


def build_inventory_view(
    *, projects: "Sequence[Project]", user_settings_path: "Path | None" = None
) -> InventoryView:
    """Resolve the process inventory view from the active projects and the user file.

    Reads the user file once and builds every name from that one snapshot
    (:func:`build_inventory_view_from_declarations`). A broken user file
    raises :class:`~otto.inventory.errors.InventoryError` naming it.
    """
    declarations = _project_declarations(projects)
    return build_inventory_view_from_declarations(
        declarations=declarations,
        user_settings=_read_user_settings(user_settings_path),
        user_settings_file=user_settings_path,
        settings_files=_settings_files(projects),
    )


@final
class _DeferredCatalog(Mapping[str, Inventory]):
    """The deferred view's inventories: each name built on its own, on first access.

    The user file is read on the first catalog access and that snapshot, or
    the error reading it, serves every later one. Each name's result, or its
    error, is memoized and re-raised on every access, so a broken ``default``
    never fails a lookup of another name.

    A user file that cannot be read is that error for every name: the names
    the projects declare raise it when built, and any other name, which only
    the user file could have supplied, is "contained" and raises it too, so a
    reference names the broken file rather than "no inventory is configured".
    Listing (``iter``) shows the projects' names alone.

    The read is single-flight: a caller that arrives while it runs waits on
    the lock and receives its result, so no consumer (the protected-file
    catalog least of all, which keeps what it saw) ever reads a snapshot
    that is not there yet. It is a short local file read, and every
    consumer needs its result, so waiting is the contract.

    The catalog keeps every active project it was built over
    (:attr:`projects`), those declaring no inventory included, and derives
    the settings files it protects from them; keeping them reads nothing.
    """

    def __init__(
        self,
        *,
        projects: "Sequence[Project]",
        declarations: list[InventoryDeclaration],
        user_settings_path: "Path | None",
    ) -> None:
        self.projects: "tuple[Project, ...]" = tuple(projects)
        """Every active project the catalog was built over, in the order given."""
        self._declarations = declarations
        self._user_settings_path = user_settings_path
        self._settings_files = _settings_files(self.projects)
        self._lock = threading.Lock()
        self._user: "UserSettingsModel | InventoryError | None" = None
        self._user_read = False
        self._protected: ProtectedPaths | None = None
        self._binding: "BuiltBinding | Exception | None" = None
        self._built: dict[str, "_BuiltMember | Exception"] = {}

    def _user_settings(self) -> "UserSettingsModel | None":
        """Return the one user-file snapshot, reading it on the first call.

        The snapshot (or the error) is assigned before ``_user_read`` is set,
        under the lock, so the unlocked check never sees a flag without its
        result.
        """
        if not self._user_read:
            with self._lock:
                if not self._user_read:
                    try:
                        self._user = _read_user_settings(self._user_settings_path)
                    except InventoryError as e:
                        self._user = e
                    self._user_read = True
        if isinstance(self._user, InventoryError):
            raise self._user
        return self._user

    def protected(self) -> ProtectedPaths:
        """Return the view's protected-file catalog, computed on the first call.

        From the one user-file snapshot. A user file that cannot be read
        gives :func:`unreadable_user_catalog`, under which nothing renders:
        the files it names cannot be protected, so no partial catalog stands
        in for the complete one. A view that renders nothing (a hand-written
        lab, a ``.json`` inventory) works on as before. Two first callers
        racing compute the same value from that one snapshot, so the memo
        needs no lock.
        """
        if self._protected is None:
            try:
                user = self._user_settings()
                unreadable = False
            except InventoryError:
                user, unreadable = None, True
            catalog = protected_paths(
                settings_files=self._settings_files,
                declarations=self._declarations,
                user_settings=user,
                user_settings_file=self._user_settings_path,
            )
            self._protected = (
                unreadable_user_catalog(catalog, user_settings_file=self._user_settings_path)
                if unreadable
                else catalog
            )
        return self._protected

    def _user_error(self) -> "InventoryError | None":
        """Return the error reading the user file, or ``None`` when it read."""
        try:
            self._user_settings()
        except InventoryError as e:
            return e
        return None

    def _names(self) -> list[str]:
        """Every declared name; the project files' alone when the user file cannot be read."""
        try:
            user = self._user_settings()
        except InventoryError:
            user = None
        names = _named_inventories(self._declarations, user)
        if _declares_default(self._declarations, user):
            names.add(DEFAULT_INVENTORY)
        return sorted(names)

    def default_binding(self) -> BuiltBinding:
        """Return ``default``'s binding, walked once (:func:`build_binding_from_declarations`).

        The walk's result, or its error, is kept and re-raised on every call,
        so ``default``'s built member and an eager check of the view hold
        the same instances.
        """
        if self._binding is None:
            try:
                self._binding = build_binding_from_declarations(
                    declarations=self._declarations,
                    user_settings=self._user_settings(),
                    user_settings_file=self._user_settings_path,
                    protected=self.protected,
                )
            except Exception as e:  # noqa: BLE001 - memoized, then re-raised below
                self._binding = e
        if isinstance(self._binding, Exception):
            raise self._binding
        return self._binding

    def build_eagerly(self) -> None:
        """Build every name now, in the eager order (:func:`_build_eagerly`), keeping each result.

        Over this catalog's own single-flight user snapshot, its memoized
        ``default`` walk and its memoized names, so a later lookup returns
        the instances built here. Raises what the eager core raises.
        """
        _build_eagerly(
            declarations=self._declarations,
            user_settings=self._user_settings,
            binding=self.default_binding,
            named=self.built,
        )

    def built(self, name: str) -> _BuiltMember:
        """Return *name*'s inventory and key, building it on first access."""
        if name not in self._built:
            try:
                self._built[name] = self._build(name)
            except Exception as e:  # noqa: BLE001 - memoized, then re-raised below
                self._built[name] = e
        result = self._built[name]
        if isinstance(result, Exception):
            raise result
        return result

    def _build(self, name: str) -> _BuiltMember:
        user = self._user_settings()
        if name == DEFAULT_INVENTORY:
            binding = self.default_binding()
            if binding.compiled is None or binding.inventory is None:
                raise KeyError(name)
            return _BuiltMember(
                inventory=binding.inventory, key=_key_of(binding.compiled, binding.inventory)
            )
        built = _built_named(
            name,
            declarations=self._declarations,
            user_settings=user,
            user_settings_file=self._user_settings_path,
            protected=self.protected,
        )
        if built is None:
            raise KeyError(name)
        return built

    @override
    def __getitem__(self, name: str) -> Inventory:
        if name not in self._names():
            error = self._user_error()
            if error is not None:
                raise error
            raise KeyError(name)
        return self.built(name).inventory

    @override
    def __contains__(self, name: object) -> bool:
        if name in self._names():
            return True
        return isinstance(name, str) and self._user_error() is not None

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    @override
    def __len__(self) -> int:
        return len(self._names())


@final
class _DeferredKeys(Mapping[str, BindingKey]):
    """The deferred view's keys: each name's, read through the catalog that builds it."""

    def __init__(self, catalog: _DeferredCatalog) -> None:
        self._catalog = catalog

    @override
    def __getitem__(self, name: str) -> BindingKey:
        if name not in self._catalog:
            raise KeyError(name)
        return self._catalog.built(name).key

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._catalog)

    @override
    def __len__(self) -> int:
        return len(self._catalog)


def deferred_inventory_view(
    *, projects: "Sequence[Project]", user_settings_path: "Path | None" = None
) -> InventoryView:
    """Return the process inventory view, building nothing until a name is used.

    Reads only the projects' settings now, to decide which projects refuse
    bare references; no user-file read and no construction. The user file is
    read once, on the first catalog access, and that snapshot (or the error
    reading it) serves every name. Each name is built on its own, from its own
    declarations, on first access, and its result or error is kept: a broken
    or disagreeing ``default`` fails only where ``default`` is used. ``default``
    is built by :func:`build_binding_from_declarations`.

    The view's :attr:`~otto.inventory.InventoryView.protected` catalog is
    computed on its first call, from the same user-file snapshot.
    """
    declarations = _project_declarations(projects)
    catalog = _DeferredCatalog(
        projects=projects, declarations=declarations, user_settings_path=user_settings_path
    )
    return InventoryView(
        inventories=catalog,
        keys=_DeferredKeys(catalog),
        bare_refused_by=_bare_refused_by(declarations),
        protected=catalog.protected,
    )


def deferred_catalog_projects(view: InventoryView) -> "list[Project] | None":
    """Return the active projects a deferred view was built over, or ``None`` for another view.

    The projects :func:`deferred_inventory_view` was given, those declaring
    no inventory included, in their order, as a fresh list on every call; a
    copy of the view (:meth:`~otto.inventory.InventoryView.for_project`)
    answers the same. Reads nothing.
    """
    catalog = view.inventories
    return list(catalog.projects) if isinstance(catalog, _DeferredCatalog) else None


def require_eager(view: InventoryView) -> None:
    """Hold a supplied view to the eager validation a view built now gets.

    For a deferred view (:func:`deferred_inventory_view`), the eager core
    (``_build_eagerly``) runs over the view's own catalog: its one
    user-file snapshot, its memoized ``default`` walk and its memoized names,
    so the instances it checks are the ones every later lookup returns, and
    the user file is not read again. Any other view was built eagerly, or by
    its caller, and is left alone.

    Raises:
        otto.inventory.errors.InventoryError: The user file cannot be read, or
            a name fails to build; the error an eager build reports first.
    """
    catalog = view.inventories
    if not isinstance(catalog, _DeferredCatalog):
        return
    catalog.build_eagerly()


def build_inventory_from_declarations(
    *,
    declarations: list[InventoryDeclaration],
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None" = None,
) -> "Inventory | None":
    """Run the §8 resolution of ``default`` over already-read tables.

    Split out from :func:`build_inventory` so the doctor can report on the
    same resolution without re-reading the files, and so the rules are
    testable without a project on disk. The inventory
    :func:`build_binding_from_declarations` builds; see it for the rules.
    """
    return build_binding_from_declarations(
        declarations=declarations,
        user_settings=user_settings,
        user_settings_file=user_settings_file,
    ).inventory


def build_inventory(
    *, projects: "Sequence[Project]", user_settings_path: "Path | None" = None
) -> "Inventory | None":
    """Resolve the process ``default`` inventory from the active projects and user file (spec §8).

    Called once at bootstrap; the result is what every ``inventory=`` caller
    passes. ``None`` means nothing declared one anywhere — inline hosts work as
    before, and a host that references an inventory key fails naming both
    places it could have been declared.

    A broken user file raises :class:`~otto.inventory.errors.InventoryError`
    naming it, rather than degrading to "no inventory".
    """
    declarations = _project_declarations(projects)
    return build_binding_from_declarations(
        declarations=declarations,
        user_settings=_read_user_settings(user_settings_path),
        user_settings_file=user_settings_path,
    ).inventory
