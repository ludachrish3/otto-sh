"""``[inventory]`` → one :class:`~otto.inventory.protocol.Inventory` per process (spec §8).

Resolution, first hit wins: a project override in an active repo (all
declaring repos must agree), else ``~/.otto/settings.toml``, else none. There
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, ValidationError, ValidationInfo, field_validator

from ..models.base import OttoModel
from ..utils import anchor_path, parse_cache_ttl
from .creds import CredsOverlay
from .errors import InventoryConstructionError, InventoryError
from .protocol import Inventory
from .registry import INVENTORY_BACKENDS, InventoryEnv, InventoryMetadata

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..creds.config import CompiledCreds
    from ..models.settings import InventoryConfigSpec, UserSettingsModel
    from ..registry import Prepared


def _anchored(value: Path, info: ValidationInfo) -> Path:
    """Anchor *value* to the env's ``anchor_dir`` when the table is prepared with one."""
    env = (info.context or {}).get("env")
    return anchor_path(value, env.anchor_dir, quote=False) if env is not None else value


class JsonInventoryConfig(OttoModel):
    """The json inventory's table: an anchored file path, and the fields it supplies."""

    model_config = ConfigDict(frozen=True)

    path: Path
    """The inventory file; a relative path anchors to the declaring repo; ``~`` is expanded."""

    supplies: list[str] | None = None
    """The record fields this inventory supplies; ``None`` means every fillable field."""

    @field_validator("path", mode="before")
    @classmethod
    def _not_empty(cls, value: object) -> object:
        """Refuse an empty string, which would otherwise read as the repo root."""
        if value == "":
            raise ValueError("must name the inventory file")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _anchor(cls, value: Path, info: ValidationInfo) -> Path:
        """Anchor a relative path to the directory the table was declared in."""
        return _anchored(value, info)


class NetBoxInventoryConfig(OttoModel):
    """The NetBox inventory's table; ``NetBoxInventory`` itself checks the values."""

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

    timeout: float = 30.0
    """Seconds one HTTP request to NetBox may take."""


@dataclass(frozen=True)
class InventoryDeclaration:
    """One settings file's ``[inventory]`` and ``[creds]`` tables and where they came from."""

    origin: str
    """The settings file that declared it — for error text."""
    anchor_dir: Path
    """Directory relative paths anchor to (the repo root; ``~/.otto`` for the user file)."""
    table: dict[str, Any] = field(default_factory=dict)
    creds_table: dict[str, Any] = field(default_factory=dict)
    """The same file's ``[creds]`` table, ``{}`` when absent (spec 2026-09-06 creds-store §4.2)."""


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

    def same_as(self, other: "CompiledInventory") -> bool:
        """Return whether this is the same inventory as *other*.

        Backend, normalized configuration AND ``cache_ttl`` must agree — every
        field the table configures. ``cache_ttl`` is in here because it is
        behaviour, not decoration: two repos declaring ``"0"`` and ``"7d"``
        would otherwise be "the same", and declaration order would silently
        decide whether the process caches at all (spec §8 requires identical
        tables). ``origin`` and ``anchor_dir`` are excluded, which is the whole
        point — two repos naming the same inventory from their own settings
        files, each anchoring it to its own root, are not a conflict. ``creds``
        is excluded too: it is compared per table by
        :meth:`~otto.creds.config.CompiledCreds.same_as` in ``_resolve_creds``,
        which resolves independently of the inventory. The snapshot slug is
        derived from these same inputs.
        """
        return (self.prepared.backend, self.prepared.normalized, self.cache_ttl) == (
            other.prepared.backend,
            other.prepared.normalized,
            other.cache_ttl,
        )


def compile_inventory(
    cfg: "InventoryConfigSpec", *, anchor_dir: Path, origin: str
) -> CompiledInventory:
    """Prepare the table's backend keys with the selected backend's configuration model.

    Every key but ``backend`` and ``cache_ttl`` is parsed by the model the
    backend registered, with an :class:`~otto.inventory.registry.InventoryEnv`
    carrying *anchor_dir* and *origin*; an unknown key or a bad value fails
    here, naming the backend, the module that registered it and *origin*.

    Anchoring, not resolving: ``anchor_dir`` makes a committed relative path
    resolve stably wherever the repo is checked out. Symlink resolution
    happens later, in the backend's factory, where the path reaches the
    object whose ``fingerprint()`` the spec says is the resolved one (§9.1).

    Raises:
        otto.inventory.errors.InventoryConstructionError: The backend is not
            registered, or its keys do not parse.
    """
    prepared = INVENTORY_BACKENDS.prepare(
        cfg.backend,
        dict(cfg.model_extra or {}),
        InventoryEnv(anchor_dir, origin),
        source=origin,
    )
    return CompiledInventory(
        prepared=prepared,
        cache_ttl=parse_cache_ttl(cfg.cache_ttl),
        anchor_dir=anchor_dir,
        origin=origin,
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
    (``snapshot.py``'s ``_stated`` drops them, §9.4/§9.5), so caching such a
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
    )


def construct_inventory(compiled: CompiledInventory) -> Inventory:
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
    inventory = _maybe_cached(INVENTORY_BACKENDS.build(compiled.prepared), compiled)
    if compiled.creds is not None:
        # Function-local, both: otto.inventory is on the bootstrap path and
        # otto.creds is only needed once a store is actually declared.
        from ..creds.config import construct_creds_store
        from ..creds.errors import CredsConstructionError, CredsError

        try:
            store = construct_creds_store(compiled.creds)
        except CredsConstructionError as e:
            # Still a construction error (and a ValueError) to its catcher;
            # the creds error rides along as the cause.
            raise InventoryConstructionError(str(e)) from e
        except CredsError as e:
            raise InventoryError(str(e)) from e
        inventory = CredsOverlay(inventory, store=store)
    return inventory


def _compile_declaration(decl: InventoryDeclaration) -> CompiledInventory:
    from ..models.settings import InventoryConfigSpec  # deferred: see module docstring

    try:
        cfg = InventoryConfigSpec.model_validate(decl.table)
    except ValidationError as e:
        raise InventoryError(f"{decl.origin}: [inventory] {e}") from e
    return compile_inventory(cfg, anchor_dir=decl.anchor_dir, origin=decl.origin)


def _resolve_creds(
    declarations: list[InventoryDeclaration],
    *,
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None",
) -> "CompiledCreds | None":
    """Run the §4.2 walk for ``[creds]``: repos that declare it must agree, else the user file.

    Independent of the ``[inventory]`` walk — each table finds its own first
    declaration. Every ``CredsError`` becomes an ``InventoryError`` here so
    the callers of ``build_inventory`` keep catching one type; a
    ``CredsConstructionError`` becomes an ``InventoryConstructionError``, so it
    stays a construction error and a ``ValueError``.

    Checks emptiness BEFORE importing ``otto.creds``: this runs on every
    ``build_inventory`` call, declared or not, and the import-budget guard
    caps ``otto.creds`` off the bootstrap path — a lab with no ``[creds]``
    anywhere must not pay for the package at all.
    """
    if not any(d.creds_table for d in declarations) and (
        user_settings is None or user_settings.creds is None
    ):
        return None
    from ..creds.config import compile_creds, compile_creds_table  # deferred: bootstrap path
    from ..creds.errors import CredsConstructionError, CredsError

    try:
        compiled = [
            compile_creds_table(dict(d.creds_table), anchor_dir=d.anchor_dir, origin=d.origin)
            for d in declarations
            if d.creds_table
        ]
        for other in compiled[1:]:
            if not other.same_as(compiled[0]):
                raise InventoryError(
                    f"two active repos declare different [creds] tables: {compiled[0].origin} "
                    f"and {other.origin}; a process has exactly one creds store"
                )
        if compiled:
            return compiled[0]
        if user_settings is not None and user_settings.creds is not None:
            from ..config.user_settings import user_settings_path  # deferred: see module docstring

            path = user_settings_file if user_settings_file is not None else user_settings_path()
            return compile_creds(user_settings.creds, anchor_dir=path.parent, origin=str(path))
    except CredsConstructionError as e:
        raise InventoryConstructionError(str(e)) from e
    except CredsError as e:
        raise InventoryError(str(e)) from e
    return None


def build_inventory_from_declarations(
    declarations: list[InventoryDeclaration],
    *,
    user_settings: "UserSettingsModel | None",
    user_settings_file: "Path | None" = None,
) -> "Inventory | None":
    """Run the §8 resolution over already-read tables.

    Split out from :func:`build_inventory` so the doctor can report on the
    same resolution without re-reading the files, and so the rules are
    testable without a repo on disk. *user_settings_file* names the file
    *user_settings* came from (for error text); it defaults to
    :func:`~otto.config.user_settings.user_settings_path`.
    """
    compiled = [_compile_declaration(d) for d in declarations if d.table]
    for other in compiled[1:]:
        if not other.same_as(compiled[0]):
            raise InventoryError(
                f"two active repos declare different [inventory] tables: {compiled[0].origin} "
                f"and {other.origin}; a process has exactly one inventory"
            )
    creds = _resolve_creds(
        declarations, user_settings=user_settings, user_settings_file=user_settings_file
    )
    if compiled:
        return construct_inventory(dataclasses.replace(compiled[0], creds=creds))
    if user_settings is not None and user_settings.inventory is not None:
        from ..config.user_settings import user_settings_path  # deferred: see module docstring

        path = user_settings_file if user_settings_file is not None else user_settings_path()
        return construct_inventory(
            dataclasses.replace(
                compile_inventory(
                    user_settings.inventory, anchor_dir=path.parent, origin=str(path)
                ),
                creds=creds,
            )
        )
    if creds is not None:
        raise InventoryError(
            f"{creds.origin}: [creds] is keyed by inventory key and no [inventory] is declared "
            "in either settings file; declare one, or remove [creds]"
        )
    return None


def build_inventory(
    repos: "Sequence[Repo]", *, user_settings_path: "Path | None" = None
) -> "Inventory | None":
    """Resolve the process inventory from the active repos and the user file (spec §8).

    Called once at bootstrap; the result is what every ``inventory=`` caller
    passes. ``None`` means nothing declared one anywhere — inline hosts work as
    before, and a host that references an inventory key fails naming both
    places it could have been declared.

    A broken user file raises :class:`~otto.inventory.errors.InventoryError`
    naming it, rather than degrading to "no inventory".
    """
    # Deferred: see the module docstring. TOML_SETTINGS_PATH rather than a
    # hand-spelled ".otto"/"settings.toml" — one owner for where a repo's
    # settings live, so a relocation cannot leave this error text lying.
    from ..config.repo import TOML_SETTINGS_PATH
    from ..config.user_settings import load_user_settings

    declarations = [
        InventoryDeclaration(
            origin=str(repo.sut_dir / TOML_SETTINGS_PATH),
            anchor_dir=repo.sut_dir,
            table=dict(repo.inventory_settings),
            creds_table=dict(repo.creds_settings),
        )
        for repo in repos
        if repo.inventory_settings or repo.creds_settings
    ]
    try:
        user = load_user_settings(user_settings_path)
    except ValueError as e:
        raise InventoryError(str(e)) from e
    return build_inventory_from_declarations(
        declarations, user_settings=user, user_settings_file=user_settings_path
    )
