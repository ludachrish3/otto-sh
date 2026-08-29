"""The ``json`` inventory backend — the stage-1 bridge file (spec §9.1).

A JSON object mapping key → record with nothing otto-shaped in it, so a
future export from the owning system (``otto inventory export``, or a script
over NetBox) produces the same file. ``$schema`` and ``_``-prefixed top-level
keys are comment space.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models.inventory import SUPPLIES_EXEMPT_FIELDS, InventoryRecord
from .creds import _compact
from .errors import InventoryError, InventoryKeyError
from .protocol import check_supplies


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
            raise InventoryError(f"{source}: key {key!r}: {_compact(e)}") from e
    return records


class JsonInventory:
    """Inventory over one stage-1 JSON file; parsed once, on first use."""

    def __init__(self, path: Path, *, supplies: "Iterable[str] | None" = None) -> None:
        self.path = Path(path)
        self.supplies = check_supplies(supplies)
        self.label = f"json:{self.path}"
        self._records: dict[str, InventoryRecord] | None = None

    def _load(self) -> dict[str, InventoryRecord]:
        if self._records is None:
            try:
                data: Any = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                # `<path>: …`, the one prefix this file's errors use: the three
                # in `parse_inventory_document` read `<path>: key '<k>': …`,
                # and a reader scanning a log for the file should not have to
                # know which layer refused it.
                raise InventoryError(f"{self.path}: {e}") from e
            self._records = parse_inventory_document(
                data, source=str(self.path), supplies=self.supplies
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

    def fingerprint(self) -> str | None:
        """Return the file's mtime and size, or ``|missing`` if it does not exist."""
        try:
            st = self.path.stat()
        except OSError:
            return f"{self.path}|missing"
        return f"{self.path}|{st.st_mtime_ns}|{st.st_size}"
