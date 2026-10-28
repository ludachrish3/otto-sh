"""The ``json`` inventory backend — the stage-1 bridge file (spec §9.1).

A JSON object mapping key → record with nothing otto-shaped in it, so a
future export from the owning system (``otto inventory export``, or a script
over NetBox) produces the same file. ``$schema`` and ``_``-prefixed top-level
keys are comment space.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ..models.base import JsonFileSource, compact_validation_error
from ..models.inventory import SUPPLIES_EXEMPT_FIELDS, InventoryRecord
from .errors import InventoryError, InventoryKeyError
from .protocol import check_supplies

if TYPE_CHECKING:
    from ..registry import Configured
    from .config import JsonInventoryConfig
    from .registry import InventoryEnv


def parse_inventory_document(
    data: object, *, source: str, supplies: frozenset[str]
) -> dict[str, InventoryRecord]:
    """``{key: record}`` from a parsed stage-1 document.

    Shared by the file backend and the snapshot cache (they read the same
    shape). A record carrying a RECORD field outside *supplies* is refused
    naming the key and the field — the file must not hold what the
    deployment says the lab files hold (spec §9.1); keys (``element_id``) and
    ``extra`` are always allowed. An unknown field name falls through to the
    record's own ``extra="forbid"`` error.
    """
    if not isinstance(data, dict):
        raise InventoryError(f"{source}: must be a JSON object mapping inventory key -> record")
    records: dict[str, InventoryRecord] = {}
    for key, raw in data.items():
        if key == "$schema" or (isinstance(key, str) and key.startswith("_")):
            continue
        if not isinstance(raw, dict):
            raise InventoryError(f"{source}: key {key!r}: expected a record object")
        outside = sorted(
            k
            for k in raw
            if k in InventoryRecord.model_fields
            and k not in supplies
            and k not in SUPPLIES_EXEMPT_FIELDS
        )
        if outside:
            raise InventoryError(
                f"{source}: key {key!r}: {outside[0]!r} is not in this inventory's supplies "
                f"{sorted(supplies)} — the lab file owns it; remove it from the record"
            )
        try:
            records[key] = InventoryRecord.model_validate(raw)
        except ValidationError as e:
            raise InventoryError(f"{source}: key {key!r}: {compact_validation_error(e)}") from e
    return records


class JsonInventory(JsonFileSource):
    """Inventory over one stage-1 JSON file; parsed once, on first use."""

    def __init__(self, path: Path, *, supplies: "Iterable[str] | None" = None) -> None:
        super().__init__(path)
        self.supplies = check_supplies(supplies)
        self._records: dict[str, InventoryRecord] | None = None

    def _load(self) -> dict[str, InventoryRecord]:
        if self._records is None:
            self._records = parse_inventory_document(
                self.read_document(InventoryError), source=str(self.path), supplies=self.supplies
            )
        return self._records

    def lookup(self, key: str) -> InventoryRecord:
        """Return the record for *key*; parses the file on the first call."""
        records = self._load()
        try:
            return records[key]
        except KeyError:
            raise InventoryKeyError(key, self.label) from None

    def list_keys(self) -> list[str]:
        """Every key in the file, sorted; parses the file on the first call."""
        return sorted(self._load())


def _json_inventory(c: "Configured[JsonInventoryConfig, InventoryEnv]") -> JsonInventory:
    """Build the built-in json inventory from its parsed configuration.

    ``Path.resolve()`` runs here, on the way into the backend: §9.1 says a
    json inventory's ``fingerprint()`` is the RESOLVED path, and doing it here
    keeps :class:`JsonInventory` a plain reader of the path it is handed.
    """
    return JsonInventory(path=c.config.path.resolve(), supplies=c.config.supplies)
