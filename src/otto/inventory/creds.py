"""The creds overlay and the by-login merge (spec 2026-09-06 creds-store §5.4, §6.1)."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..models.base import compact_validation_error
from ..models.host import CredSpec
from ..models.inventory import InventoryRecord
from .errors import InventoryError
from .protocol import Inventory

if TYPE_CHECKING:
    from ..creds.protocol import CredsStore


def _stated(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the fields *entry* states: present and not ``None`` (R7, one level down)."""
    return {k: v for k, v in entry.items() if v is not None}


def merge_creds(
    lower: list[dict[str, Any]],
    higher: list[dict[str, Any]],
    *,
    key: "str | None" = None,
    lower_name: str = "lower",
    higher_name: str = "higher",
) -> list[dict[str, Any]]:
    """Compose two cred layers by login (spec 2026-09-06 creds-store §6.1).

    Matching is by ``login`` only. A login in both layers gets
    ``{**lower_stated, **higher_stated}`` — the higher layer overrides a
    field it states and cannot remove one (``None`` states nothing). The
    result is sequenced HIGHER FIRST: every *higher* login in its own order,
    then every *lower*-only login in its own order — the lab file, the one
    layer written with otto's default-login rule in mind, decides the order
    (spec §3 statement 3).

    Plain dicts in, plain dicts out — the join's currency. Raises
    :class:`~otto.inventory.errors.InventoryError` for an entry with no
    non-empty string ``login`` and for a login repeated within ONE layer; the
    message names the layer and *key*, and lists an entry's field NAMES only,
    because a value here may be a password.
    """
    where = f" (inventory key {key!r})" if key is not None else ""

    def _by_login(layer: list[dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for entry in layer:
            login = entry.get("login") if isinstance(entry, dict) else None
            if not isinstance(login, str) or not login:
                fields = sorted(entry) if isinstance(entry, dict) else type(entry).__name__
                raise InventoryError(
                    f"cred entry without a 'login' string in the {name} layer{where}: {fields}"
                )
            if login in out:
                raise InventoryError(f"duplicate cred login {login!r} in the {name} layer{where}")
            out[login] = _stated(entry)
        return out

    low = _by_login(lower, lower_name)
    high = _by_login(higher, higher_name)
    merged = [{**low.get(login, {}), **fields} for login, fields in high.items()]
    merged.extend(fields for login, fields in low.items() if login not in high)
    return merged


def _revalidate_merged_cred(c: dict[str, Any], *, key: "str | None", label: str) -> CredSpec:
    """Re-validate one store-and-record-merged entry, naming the key and login on failure.

    A one-item helper rather than the loop body its caller would otherwise
    write: a ``try``/``except`` inside a per-entry loop is ``PERF203`` (the
    repo's answer, e.g. ``otto.cli.init._item_problem``, is to move the
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
        # Function-local: otto.inventory is on the bootstrap path.
        from ..creds.errors import CredsError

        record = self.inner.lookup(key)
        try:
            supplied = self.store.lookup(key)
        except CredsError as e:
            raise InventoryError(str(e)) from e
        lower = [c.model_dump(mode="json", exclude_unset=True, exclude_none=True) for c in supplied]
        higher = [
            c.model_dump(mode="json", exclude_unset=True, exclude_none=True) for c in record.creds
        ]
        merged = merge_creds(
            lower, higher, key=key, lower_name="creds store", higher_name="inventory record"
        )
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
