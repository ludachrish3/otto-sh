"""``creds_file``: one home for credentials, whatever the backend (spec §9.4)."""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models.host import CredSpec
from ..models.inventory import InventoryRecord
from .errors import InventoryError
from .protocol import Inventory


def _compact(error: ValidationError) -> str:
    """One-line rendering of *error*: ``field: message`` per problem.

    ``str(ValidationError)`` spans several lines, and an error a human reads in
    a log line — or a test matches with a single regex — must not.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<entry>'}: {item['msg']}"
        for item in error.errors()
    )


def _validated(path: Path, key: str, idx: int, entry: Any) -> dict[str, Any]:
    """Check one creds entry against ``CredSpec`` and return it as a plain dict."""
    try:
        CredSpec.model_validate(entry)
    except ValidationError as e:
        raise InventoryError(f"creds_file {path}: key {key!r}: creds[{idx}]: {_compact(e)}") from e
    return dict(entry)


def load_creds_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse ``{key: [CredSpec, ...]}``; errors name the file, the key, the index and the field."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise InventoryError(f"creds_file {path}: {e}") from e
    if not isinstance(data, dict):
        raise InventoryError(
            f"creds_file {path}: must be a JSON object mapping inventory key -> creds list"
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for key, entries in data.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if not isinstance(entries, list):
            raise InventoryError(f"creds_file {path}: key {key!r}: expected a list of creds")
        out[key] = [_validated(path, key, idx, entry) for idx, entry in enumerate(entries)]
    return out


class CredsOverlay:
    """Wrap an inventory so ``creds`` come from the creds file and nowhere else.

    Construction does no I/O (the rule for every inventory object): the file
    is read on the first ``lookup``, so a lab with no referenced entry never
    touches it. ``inner`` and ``creds_path`` are public — the doctor reads
    the file's mode, ``otto inventory refresh`` walks down to the cache.

    ``label``/``supplies`` are properties over ``inner``, not attributes
    copied at construction: an ``inner`` that answers only ``fingerprint``/
    ``stat_paths`` (the shim's opaque-inventory probe, :func:`stat_paths`)
    can still be wrapped and asked ``stat_paths()`` without ever needing a
    ``label`` or ``supplies`` it does not have.
    """

    def __init__(self, inner: Inventory, *, path: Path) -> None:
        self.inner = inner
        self.creds_path = Path(path)
        self._creds: "dict[str, list[dict[str, Any]]] | None" = None

    @property
    def label(self) -> str:
        """Return the inner backend's label."""
        return self.inner.label

    @property
    def supplies(self) -> frozenset[str]:
        """Return the inner backend's supplied fields plus ``creds``."""
        return frozenset(self.inner.supplies) | {"creds"}

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._creds is None:
            self._creds = load_creds_file(self.creds_path)
        return self._creds

    def lookup(self, key: str) -> InventoryRecord:
        """Return the inner record with its ``creds`` replaced by the creds file's."""
        record = self.inner.lookup(key)
        if record.creds:
            raise InventoryError(
                f"inventory key {key!r}: 'creds' come from creds_file {self.creds_path}; "
                "remove them from the record"
            )
        creds = [CredSpec.model_validate(c) for c in self._load().get(key, [])]
        return record.model_copy(update={"creds": creds})

    def list_keys(self) -> list[str]:
        """Every key the inner inventory holds, sorted."""
        return self.inner.list_keys()

    def fingerprint(self) -> "str | None":
        """Return the inner fingerprint combined with the creds file's mtime and size."""
        inner = self.inner.fingerprint()
        if inner is None:
            return None
        try:
            st = self.creds_path.stat()
        except OSError:
            return f"{inner}|creds:missing"
        return f"{inner}|creds:{st.st_mtime_ns}:{st.st_size}"

    def stat_paths(self) -> "list[Path] | None":
        """Return the inner backend's stat paths plus the creds file, or ``None`` when opaque.

        ``None`` when the inner has no ``stat_paths`` at all, or when it has
        one but returns ``None`` (its own fingerprint is not stat-derived) —
        an overlay can never be more stat-checkable than what it wraps.
        """
        inner = getattr(self.inner, "stat_paths", None)
        paths = inner() if callable(inner) else None
        return None if paths is None else [*paths, self.creds_path]
