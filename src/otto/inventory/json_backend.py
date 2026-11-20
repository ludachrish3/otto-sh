"""The ``json`` inventory backend — the stage-1 bridge file (spec §9.1).

A flat JSON object mapping key → record with nothing otto-shaped in it, so a
future export from the owning system (``otto inventory export``, or a script
over NetBox) produces the same file. ``$schema`` and ``_``-prefixed top-level
keys are comment space. A document marked ``"$format": "otto.inventory.v2"``
also declares instances (lab templating §6.2); see
:func:`parse_discovery_document`.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from typing_extensions import final

from ..models.base import JsonFileSource, compact_validation_error
from ..models.inventory import (
    INVENTORY_V2_FORMAT,
    SUPPLIES_EXEMPT_FIELDS,
    InventoryInstance,
    InventoryRecord,
)
from .discovery import Discovery
from .errors import InventoryError, InventoryKeyError
from .protocol import check_supplies

if TYPE_CHECKING:
    from ..registry import Configured
    from .config import JsonInventoryConfig
    from .registry import InventoryEnv


_V2_SECTIONS = ("records", "instances")


def is_comment_key(key: object) -> bool:
    """Return True for a ``_``-prefixed key: annotation space in every map of the document.

    The one rule: the parser skips such a key, and
    :func:`~otto.inventory.snapshot.discovery_to_document` refuses to write
    a record or instance named like one, so no document can lose it.
    """
    return isinstance(key, str) and key.startswith("_")


def _parse_records(
    entries: object, *, source: str, supplies: frozenset[str]
) -> dict[str, InventoryRecord]:
    """``{key: record}`` from one key-to-record map; ``_``-prefixed keys are comments.

    A record carrying a RECORD field outside *supplies* is refused naming the
    key and the field: the file must not hold what the deployment says the
    lab files hold (spec §9.1). The supplies-exempt fields
    (:data:`~otto.models.inventory.SUPPLIES_EXEMPT_FIELDS`) are always
    allowed. An unknown field name falls through to the record's own
    ``extra="forbid"`` error.
    """
    if not isinstance(entries, dict):
        raise InventoryError(f"{source}: must be a JSON object mapping inventory key -> record")
    records: dict[str, InventoryRecord] = {}
    for key, raw in entries.items():
        if is_comment_key(key):
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


def _parse_instances(entries: object, *, source: str) -> dict[str, InventoryInstance]:
    """``{name: instance}`` from a v2 document's ``instances`` map; ``_`` keys are comments."""
    if not isinstance(entries, dict):
        raise InventoryError(f"{source}: must be a JSON object mapping instance name -> instance")
    instances: dict[str, InventoryInstance] = {}
    for name, raw in entries.items():
        if is_comment_key(name):
            continue
        if not isinstance(raw, dict):
            raise InventoryError(f"{source}: instance {name!r}: expected an instance object")
        try:
            instances[name] = InventoryInstance.model_validate(raw)
        except ValidationError as e:
            raise InventoryError(
                f"{source}: instance {name!r}: {compact_validation_error(e)}"
            ) from e
    return instances


def parse_discovery_document(*, data: object, source: str, supplies: frozenset[str]) -> Discovery:
    """Return records and instances from a parsed json inventory document: THE one parser.

    Shared by the file backend and the snapshot cache (they read the same
    shapes). The format is chosen by a marker, never guessed from the keys,
    because every bare top-level name (``instances`` included) is already a
    legal inventory key:

    - A top-level ``"$format"`` whose value is a string selects a versioned
      document. ``"otto.inventory.v2"`` holds a ``records`` map and an
      ``instances`` map, both optional; ``$schema`` and ``_``-prefixed keys
      are comment space and any other top-level key is refused, as is any
      other format string.
    - Anything else is a flat (stage-1) document mapping key to record, with
      ``$schema`` and ``_``-prefixed keys as comment space. A ``$format`` key
      holding a record is a host. A flat document declares no instances:
      ``instances`` is ``None`` (unsupported), where a v2 document without an
      ``instances`` map gives ``{}``.
    """
    if not isinstance(data, dict):
        raise InventoryError(f"{source}: must be a JSON object mapping inventory key -> record")
    marker = data.get("$format")
    if not isinstance(marker, str):
        flat = {k: v for k, v in data.items() if k != "$schema"}
        return Discovery(records=_parse_records(flat, source=source, supplies=supplies))
    if marker != INVENTORY_V2_FORMAT:
        raise InventoryError(
            f"{source}: unknown inventory document format {marker!r} "
            f"(this otto reads {INVENTORY_V2_FORMAT!r}, or a flat document without '$format')"
        )
    unknown = sorted(
        k for k in data if k not in ("$format", "$schema", *_V2_SECTIONS) and not is_comment_key(k)
    )
    if unknown:
        raise InventoryError(
            f"{source}: {unknown[0]!r} is not a section of an {INVENTORY_V2_FORMAT!r} "
            f"document (sections: {list(_V2_SECTIONS)})"
        )
    return Discovery(
        records=_parse_records(
            data.get("records", {}), source=f"{source}: records", supplies=supplies
        ),
        instances=_parse_instances(data.get("instances", {}), source=f"{source}: instances"),
    )


def parse_inventory_document(
    *, data: object, source: str, supplies: frozenset[str]
) -> dict[str, InventoryRecord]:
    """``{key: record}`` from a parsed json inventory document, either format.

    The records half of :func:`parse_discovery_document`, for the readers
    that look up keys and never read instances.
    """
    return parse_discovery_document(data=data, source=source, supplies=supplies).records


@final
class JsonInventory(JsonFileSource):
    """Inventory over one JSON file, flat or v2; parsed once, on first use."""

    def __init__(self, path: Path, *, supplies: "Iterable[str] | None" = None) -> None:
        super().__init__(path)
        self.supplies = check_supplies(supplies)
        self._discovery: Discovery | None = None

    def _load(self) -> Discovery:
        if self._discovery is None:
            self._discovery = parse_discovery_document(
                data=self.read_document(refuse=InventoryError),
                source=str(self.path),
                supplies=self.supplies,
            )
        return self._discovery

    def inventory_discovery(self) -> Discovery:
        """Return the file's records and instances (``None`` for a flat document)."""
        return self._load()

    def lookup(self, key: str) -> InventoryRecord:
        """Return the record for *key*; parses the file on the first call."""
        records = self._load().records
        try:
            return records[key]
        except KeyError:
            raise InventoryKeyError(key, label=self.label) from None

    def list_keys(self) -> list[str]:
        """Every key in the file, sorted; parses the file on the first call."""
        return sorted(self._load().records)


def _json_inventory(c: "Configured[JsonInventoryConfig, InventoryEnv]") -> JsonInventory:
    """Build the built-in json inventory from its parsed configuration.

    ``Path.resolve()`` runs here, on the way into the backend: §9.1 says a
    json inventory's ``fingerprint()`` is the RESOLVED path, and doing it here
    keeps :class:`JsonInventory` a plain reader of the path it is handed.
    """
    return JsonInventory(path=c.config.path.resolve(), supplies=c.config.supplies)
