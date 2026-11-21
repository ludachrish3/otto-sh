"""The creds overlay and the by-identity merge (spec 2026-09-06 creds-store §5.4, §6.1)."""

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from typing_extensions import final

from ..host.inventory_ref import recording_fields
from ..models import base as model_base
from ..models.base import CredSpec, compact_validation_error
from ..models.inventory import InventoryRecord
from .errors import InventoryError
from .protocol import Inventory

if TYPE_CHECKING:
    from ..creds.protocol import CredsStore
    from .discovery import Discovery


def merge_creds(
    *,
    lower: list[dict[str, Any]],
    higher: list[dict[str, Any]],
    key: "str | None" = None,
    lower_name: str = "lower",
    higher_name: str = "higher",
) -> list[dict[str, Any]]:
    """Compose two cred layers by identity (spec 2026-09-06 creds-store §6.1).

    Matching is by identity — ``login`` plus scope
    (:func:`~otto.models.base.cred_identity`), so ``protocols`` is part
    of the key and is never composed; each layer is keyed by
    :func:`~otto.models.base.cred_layer_by_identity`. An identity in both layers gets
    ``{**lower_stated, **higher_stated}`` — the higher layer overrides a
    field it states and cannot remove one (``None`` states nothing). The
    result is sequenced HIGHER FIRST: every *higher* identity in its own
    order, then every *lower*-only identity in its own order — the lab
    file, the one layer written with otto's default-login rule in mind,
    decides the order (spec §3 statement 3).

    Plain dicts in, plain dicts out — the join's currency. A password value is
    passed through as the layer holds it: a ``SecretStr`` from a dumped record
    or store cred, a ``str`` from the lab file. Raises an
    :class:`~otto.inventory.errors.InventoryError` carrying the message of
    what :func:`~otto.models.base.cred_layer_by_identity` refuses in either
    layer, named *lower_name* or *higher_name*.
    """
    try:
        low = model_base.cred_layer_by_identity(lower, name=lower_name, key=key)
        high = model_base.cred_layer_by_identity(higher, name=higher_name, key=key)
    except ValueError as e:
        raise InventoryError(str(e)) from None
    merged = [{**low.get(identity, {}), **fields} for identity, fields in high.items()]
    merged.extend(fields for identity, fields in low.items() if identity not in high)
    return merged


def _dump_for_merge(cred: CredSpec) -> dict[str, Any]:
    """Return what *cred* states, its password still the ``SecretStr``.

    Stated fields only, in python mode: the merge result is validated back
    into a :class:`CredSpec`, which takes the ``SecretStr`` as it is, and no
    merge local ever holds the plain password a traceback's locals would show.
    """
    return cred.model_dump(exclude_unset=True, exclude_none=True)


def _revalidate_merged_cred(c: dict[str, Any], *, key: "str | None", label: str) -> CredSpec:
    """Re-validate one store-and-record-merged entry, naming the key and login on failure.

    A one-item helper rather than the loop body its caller would otherwise
    write: a ``try``/``except`` inside a per-entry loop is ``PERF203`` (the
    codebase's answer, e.g. ``otto.init.doctor._item_problem``, is to move the
    ``try`` into a function the loop calls). Per-entry, not one
    ``model_validate`` over the whole merged list: a field-level failure
    (``password: 123``) has no ``CredSpec``-authored message to carry the
    login (unlike the model validator's own ``cred {self.login!r}: …``, spec
    §6.1), so the login must come from the merged dict itself, at the point
    of catching.
    """
    try:
        return CredSpec.model_validate(c)
    except ValidationError as e:
        login = c.get("login") if isinstance(c, dict) else None
        raise InventoryError(
            f"inventory key {key!r}: creds from {label} and the record do not compose "
            f"for login {login!r}: {compact_validation_error(e)}"
        ) from e


@final
class CredsOverlay:
    """Wrap an inventory so ``creds`` compose from the creds store and the record.

    The store is the LOWEST creds layer and the record the middle one (spec
    §3): ``lookup`` returns the record with ``creds = merge_creds(store,
    record)``, so a record may carry creds beside a configured store and
    override the store's by login. Construction does no I/O (the rule for every
    inventory object): the store is first consulted on ``lookup``, so a lab
    with no referenced entry never opens it. ``inner`` and ``store`` are
    public — the doctor reads the store's stat paths, ``otto inventory
    refresh`` walks down to the cache.

    ``label``/``supplies`` are properties over ``inner``, not attributes
    copied at construction: an ``inner`` that answers only ``fingerprint``/
    ``stat_paths`` (the shim's opaque-inventory probe) can still be wrapped
    and asked ``stat_paths()`` without a ``label`` or ``supplies``.
    """

    def __init__(self, inner: Inventory, *, store: "CredsStore") -> None:
        self.inner = inner
        self.store = store

    @property
    def label(self) -> str:
        """Return the inner backend's label."""
        return self.inner.label

    @property
    def supplies(self) -> frozenset[str]:
        """Return the inner backend's supplied fields plus ``creds``."""
        return frozenset(self.inner.supplies) | {"creds"}

    def lookup(self, key: str) -> InventoryRecord:
        """Return the inner record with the store's creds merged UNDER its own (spec §6.1)."""
        return self._merged(key, self.inner.lookup(key))

    def inventory_discovery(self) -> "Discovery":
        """Discover the inner inventory, every record merged exactly as :meth:`lookup` merges it.

        Instances pass through untouched: the store holds creds, not membership.
        """
        # Function-local: otto.inventory is on the bootstrap path.
        from .discovery import discover_inventory

        found = discover_inventory(inventory=self.inner)
        return dataclasses.replace(
            found, records={key: self._merged(key, rec) for key, rec in found.records.items()}
        )

    def _merged(self, key: str, record: InventoryRecord) -> InventoryRecord:
        """Return *record* (the inner one for *key*) with the store's creds merged under its own."""
        # Function-local: otto.inventory is on the bootstrap path.
        from ..creds.errors import CredsError

        try:
            supplied = self.store.lookup(key)
        except CredsError as e:
            raise InventoryError(str(e)) from e
        lower = [_dump_for_merge(c) for c in supplied]
        higher = [_dump_for_merge(c) for c in record.creds]
        merged = merge_creds(
            lower=lower,
            higher=higher,
            key=key,
            lower_name="creds store",
            higher_name="inventory record",
        )
        if recording_fields():
            _note_store_merge(lower=lower, higher=higher)
        creds = [_revalidate_merged_cred(c, key=key, label=self.store.label) for c in merged]
        return record.model_copy(update={"creds": creds})

    def list_keys(self) -> list[str]:
        """Every key the inner inventory holds, sorted."""
        return self.inner.list_keys()

    def fingerprint(self) -> "str | None":
        """Return the inner fingerprint combined with the store's; ``None`` if either is opaque."""
        inner = self.inner.fingerprint()
        if inner is None:
            return None
        store = self.store.fingerprint()
        return None if store is None else f"{inner}|creds:{store}"

    def stat_paths(self) -> "list[Path] | None":
        """Return the inner's stat paths plus the store's, or ``None`` when either is opaque."""
        inner = getattr(self.inner, "stat_paths", None)
        paths = inner() if callable(inner) else None
        if paths is None:
            return None
        store_stat = getattr(self.store, "stat_paths", None)
        store_paths = store_stat() if callable(store_stat) else None
        return None if store_paths is None else [*paths, *store_paths]


def _note_store_merge(*, lower: list[dict[str, Any]], higher: list[dict[str, Any]]) -> None:
    """Attribute the overlay's cred merge, record over creds store, in a recorded build."""
    from ..host import provenance

    provenance.note_cred_merge(
        lower=lower,
        higher=higher,
        lower_layer=provenance.FieldLayer.CREDS_STORE,
        higher_layer=provenance.FieldLayer.RECORD,
    )


def below_creds_overlay(*, inventory: Inventory) -> Inventory:
    """Return what *inventory*'s creds overlays wrap: THE one peel of otto's wrapper stack.

    ``construct_inventory`` puts the snapshot cache on first and the creds
    overlay OUTERMOST (a snapshot must never hold credentials), so whatever
    this returns is the cache when there is one, else the backend itself. A
    loop rather than a single ``.inner``, so a second overlay layer cannot
    silently hide the cache from every caller. An inventory with no overlay is
    returned as it is.
    """
    while isinstance(inventory, CredsOverlay):
        inventory = inventory.inner
    return inventory


def creds_store_label(*, inventory: object) -> "str | None":
    """Return the label of otto's credentials store over *inventory*: the one store-label reader.

    Only a :class:`CredsOverlay` has otto's store; anything else answers
    ``None``, and a third-party backend's own ``store`` attribute is never
    read: it is that backend's business, and it may be offline or no label at
    all.
    """
    return inventory.store.label if isinstance(inventory, CredsOverlay) else None
