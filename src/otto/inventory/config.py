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
would land on every CLI surface and break the same import-budget caps the
``otto.config`` lazy exports exist to defend. The edges are real and declared
in ``tach.toml``; only the *timing* is deferred.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..models.inventory import parse_cache_ttl
from ..utils import anchor_path
from .creds import CredsOverlay
from .errors import InventoryError
from .protocol import Inventory
from .registry import get_inventory_backend_class

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..models.settings import InventoryConfigSpec, UserSettingsModel


@dataclass(frozen=True)
class InventoryDeclaration:
    """One ``[inventory]`` table and where it came from."""

    origin: str
    """The settings file that declared it — for error text."""
    anchor_dir: Path
    """Directory relative paths anchor to (the repo root; ``~/.otto`` for the user file)."""
    table: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompiledInventory:
    """A validated, anchored ``[inventory]`` table."""

    backend: str
    kwargs: dict[str, Any]
    creds_file: "Path | None"
    cache_ttl: timedelta
    anchor_dir: Path
    origin: str

    def same_as(self, other: "CompiledInventory") -> bool:
        """Return whether this is the same inventory as *other*.

        Backend, anchored kwargs, creds file AND ``cache_ttl`` must agree —
        every field the table configures. ``cache_ttl`` is in here because it
        is behaviour, not decoration: two repos declaring ``"0"`` and ``"7d"``
        would otherwise be "the same", and declaration order would silently
        decide whether the process caches at all (spec §8 requires identical
        tables). ``origin`` and ``anchor_dir`` are excluded, which is the whole
        point — two repos naming the same inventory from their own settings
        files, each anchoring it to its own root, are not a conflict.
        """
        return (self.backend, self.kwargs, self.creds_file, self.cache_ttl) == (
            other.backend,
            other.kwargs,
            other.creds_file,
            other.cache_ttl,
        )


def compile_inventory(
    cfg: "InventoryConfigSpec", *, anchor_dir: Path, origin: str
) -> CompiledInventory:
    """Validate the backend's kwargs knowing the backend; anchor paths to *anchor_dir*.

    The json backend takes ``path`` (required) and ``supplies`` (optional);
    anything else is an error naming it. Other backends validate their own
    kwargs in their constructor (the ``compile_lab_sources`` precedent —
    otto core cannot type a third-party backend's kwargs).

    Anchoring, not resolving: ``anchor_dir`` makes a committed relative path
    resolve stably wherever the repo is checked out. Symlink resolution
    happens later, in :func:`construct_inventory`, where the path reaches the
    object whose ``fingerprint()`` the spec says is the resolved one (§9.1).
    """
    extras: dict[str, Any] = dict(cfg.model_extra or {})
    if cfg.backend == "json":
        path = extras.pop("path", None)
        if not isinstance(path, str) or not path:
            raise InventoryError(f"{origin}: [inventory] backend 'json' requires a 'path' string")
        supplies = extras.pop("supplies", None)
        if supplies is not None and not (
            isinstance(supplies, list) and all(isinstance(s, str) for s in supplies)
        ):
            raise InventoryError(
                f"{origin}: [inventory] 'supplies' must be a list of record field names"
            )
        if extras:
            raise InventoryError(
                f"{origin}: [inventory] unknown key(s) for the json backend: {sorted(extras)}"
            )
        kwargs: dict[str, Any] = {"path": anchor_path(Path(path), anchor_dir), "supplies": supplies}
    else:
        kwargs = extras
    creds = anchor_path(Path(cfg.creds_file), anchor_dir) if cfg.creds_file else None
    return CompiledInventory(
        backend=cfg.backend,
        kwargs=kwargs,
        creds_file=creds,
        cache_ttl=parse_cache_ttl(cfg.cache_ttl),
        anchor_dir=anchor_dir,
        origin=origin,
    )


def _maybe_cached(inventory: Inventory, compiled: CompiledInventory) -> Inventory:
    """Wrap *inventory* in a snapshot cache if it is a remote backend that wants one (§9.5).

    Three conditions, in the order they are cheapest to check:

    - Not the ``json`` backend. Short-circuited BY NAME rather than left to
      the ``fingerprint()`` test below, because ``JsonInventory.fingerprint()``
      stats the file — and construction does no I/O, which is what lets a lab
      with no referenced entry never touch a broken inventory at all.
    - ``cache_ttl`` greater than zero. ``"0"`` means "every process fetches",
      the behaviour of an uncached backend (§9.5).
    - ``fingerprint()`` is ``None``, the backend's own statement that it cannot
      report freshness. NetBox says so unconditionally; a third-party backend
      that returns a string opts OUT of the cache by design, because it has a
      better answer than a timestamp. A third-party backend that probes the
      network inside ``fingerprint()`` pays for it here — the protocol says
      that method changes when the records may have, not that it is free, and
      the two built-ins both answer from local state.

    A backend that passes all three and ALSO supplies ``creds`` is refused
    rather than wrapped. A snapshot never holds credentials by construction
    (``snapshot.py``'s ``_stated`` drops them, §9.4/§9.5), so caching such a
    backend would answer WITH creds off the wire and WITHOUT them for the rest
    of the TTL — a referenced unix host failing validation on alternate runs,
    with nothing in the message naming the cache. Neither half is negotiable,
    so the configuration is.
    """
    if compiled.backend == "json" or compiled.cache_ttl <= timedelta(0):
        return inventory
    if inventory.fingerprint() is not None:
        return inventory
    if "creds" in inventory.supplies:
        # The INNER backend's supplies, before `construct_inventory` adds the
        # creds overlay: that overlay is outermost and always claims `creds`,
        # so checking the constructed object would refuse the configuration
        # §9.4 actually recommends.
        raise InventoryError(
            f"{compiled.origin}: backend {compiled.backend!r} supplies 'creds', which a "
            'snapshot cannot carry; set cache_ttl = "0" for this backend, or have the '
            "backend leave creds to creds_file"
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
        slug_material=snapshot_slug_material(compiled.backend, inventory.label, compiled.kwargs),
    )


def construct_inventory(compiled: CompiledInventory) -> Inventory:
    """Instantiate the backend, then the core wrappers (the snapshot cache, then creds).

    ``Path.resolve()`` runs here, on the way into the backend: §9.1 says a
    json inventory's ``fingerprint()`` is the RESOLVED path, and doing it here
    rather than inside :class:`~otto.inventory.json_backend.JsonInventory`
    keeps the backend a plain reader of the path it was handed.

    ``creds_file`` configured means the inventory supplies ``creds`` (§9.4):
    the overlay is added only when the file is declared, and when it is, the
    backend's own records may not carry ``creds`` at all. Without it, records
    carry their own. Construction still does no I/O — the overlay reads the
    file on first lookup, and the cache reads nothing until then either.

    ORDER MATTERS: the cache goes on first, the creds overlay OUTERMOST. The
    snapshot must never contain credentials (§9.4/§9.5), and a cache wrapped
    around the overlay would be caching exactly that; the doctor also reads
    ``isinstance(inventory, CredsOverlay)`` to report the creds file's mode.

    A non-json backend validates its own kwargs in its constructor, so a
    typo'd key surfaces as its ``TypeError``/``ValueError``. That is wrapped
    here naming the origin and the backend, because the raw
    ``__init__() got an unexpected keyword argument 'urll'`` names neither the
    settings file nor the backend the user selected — the json arm already
    says both.
    """
    try:
        cls = get_inventory_backend_class(compiled.backend)
    except ValueError as e:
        raise InventoryError(f"{compiled.origin}: {e}") from e
    inventory: Inventory
    if compiled.backend == "json":
        inventory = cls(
            path=Path(compiled.kwargs["path"]).resolve(), supplies=compiled.kwargs["supplies"]
        )
    else:
        try:
            inventory = cls(repo_dir=compiled.anchor_dir, **compiled.kwargs)
        except (TypeError, ValueError) as e:
            raise InventoryError(
                f"{compiled.origin}: [inventory] backend {compiled.backend!r}: {e}"
            ) from e
    inventory = _maybe_cached(inventory, compiled)
    if compiled.creds_file is not None:
        inventory = CredsOverlay(inventory, path=compiled.creds_file.resolve())
    return inventory


def _compile_declaration(decl: InventoryDeclaration) -> CompiledInventory:
    from ..models.settings import InventoryConfigSpec  # deferred: see module docstring

    try:
        cfg = InventoryConfigSpec.model_validate(decl.table)
    except ValidationError as e:
        raise InventoryError(f"{decl.origin}: [inventory] {e}") from e
    return compile_inventory(cfg, anchor_dir=decl.anchor_dir, origin=decl.origin)


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
    compiled = [_compile_declaration(d) for d in declarations]
    for other in compiled[1:]:
        if not other.same_as(compiled[0]):
            raise InventoryError(
                f"two active repos declare different [inventory] tables: {compiled[0].origin} "
                f"and {other.origin}; a process has exactly one inventory"
            )
    if compiled:
        return construct_inventory(compiled[0])
    if user_settings is not None and user_settings.inventory is not None:
        from ..config.user_settings import user_settings_path  # deferred: see module docstring

        path = user_settings_file if user_settings_file is not None else user_settings_path()
        return construct_inventory(
            compile_inventory(user_settings.inventory, anchor_dir=path.parent, origin=str(path))
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
        )
        for repo in repos
        if repo.inventory_settings
    ]
    try:
        user = load_user_settings(user_settings_path)
    except ValueError as e:
        raise InventoryError(str(e)) from e
    return build_inventory_from_declarations(
        declarations, user_settings=user, user_settings_file=user_settings_path
    )
