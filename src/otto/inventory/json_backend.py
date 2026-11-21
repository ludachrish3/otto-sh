"""The ``json`` inventory backend — the stage-1 bridge file (spec §9.1).

A flat JSON object mapping key → record with nothing otto-shaped in it, so a
future export from the owning system (``otto inventory export``, or a script
over NetBox) produces the same file. ``$schema`` and ``_``-prefixed top-level
keys are comment space. A document marked ``"$format": "otto.inventory.v2"``
also declares instances (lab templating §6.2); see
:func:`parse_discovery_document`.

A path that resolves to a ``.jsonnet`` file names a Jsonnet file instead
(:attr:`JsonInventory.templated`): the document is what it renders, in
memory, then read exactly as a written file is. Importing this module loads
no templating code: the renderer loads when such a file first renders.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from typing_extensions import final, override

from ..models.base import (
    JsonFileSource,
    compact_validation_error,
    observed_fingerprint,
    wrap_secret_values,
)
from ..models.inventory import (
    INVENTORY_V2_FORMAT,
    SUPPLIES_EXEMPT_FIELDS,
    InventoryInstance,
    InventoryRecord,
)
from ..utils import TEMPLATE_SUFFIX
from .discovery import Discovery
from .errors import InventoryDataError, InventoryError, InventoryKeyError
from .protocol import Inventory, check_supplies

if TYPE_CHECKING:
    from ..registry import Configured
    from .config import JsonInventoryConfig, ProtectedPaths
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
    """Inventory over one JSON file, flat or v2, or the Jsonnet file that renders one.

    The document is read once, on first use. A :attr:`templated` inventory
    renders its ``.jsonnet`` file with no top-level argument, its imports
    served under *import_paths* only, and never a file of the protected
    catalog: *env*'s (:func:`~otto.inventory.config.env_protection`). A
    hand-built inventory with no *env* has no catalog
    (:data:`~otto.inventory.config.NO_CATALOG`), as a hand-built
    :class:`~otto.inventory.InventoryView` has none: its ``.jsonnet`` file is
    refused, never rendered unprotected; a written file reads as before.
    """

    def __init__(
        self,
        path: Path,
        *,
        supplies: "Iterable[str] | None" = None,
        import_paths: "Iterable[Path]" = (),
        env: "InventoryEnv | None" = None,
    ) -> None:
        super().__init__(path)
        self.supplies = check_supplies(supplies)
        self.import_paths = tuple(Path(p) for p in import_paths)
        """The directories a templated inventory's imports resolve under, in order."""
        self.env = env
        """The environment the table was prepared in, whose catalog a render runs under."""
        self._discovery: Discovery | None = None

    @property
    def templated(self) -> bool:
        """Whether the path names a Jsonnet file (``.jsonnet``) that renders the document.

        The suffix is ``otto.utils.TEMPLATE_SUFFIX``, the one decision of
        what is a template, read on the resolved path the inventory holds: a
        symlink to a ``.jsonnet`` file renders, whatever its own name.
        :func:`is_templated_inventory` asks this property through otto's
        wrappers.
        """
        return self.path.suffix == TEMPLATE_SUFFIX

    def _load(self) -> Discovery:
        """Return the document's discovery, read on the first call and kept.

        Only a successful read is kept. A cancelled render
        (:class:`~otto.templating.errors.RenderCancelled`) propagates and
        keeps nothing, so the next access renders afresh: an inventory that
        several projects' operations share stays usable when one of them is
        given up on.
        """
        if self._discovery is None:
            self._discovery = parse_discovery_document(
                data=self.read_document(refuse=InventoryError),
                source=str(self.path),
                supplies=self.supplies,
            )
        return self._discovery

    @override
    def read_document(self, *, refuse: "type[Exception]") -> Any:
        """Return the document, its secrets wrapped; a templated inventory's is rendered.

        A written file is read as :meth:`JsonFileSource.read_document` reads
        it, refused as *refuse*. A templated file renders once per call
        (:func:`~otto.templating.render.render`) and its JSON is read as a
        written file's is. The render reads the operation's cancellation, so
        a render whose operation was given up on never enters the evaluator.

        Raises:
            InventoryDataError: A templated file could not be rendered (a
                Jsonnet error, a refused or missing import, no binding, no
                protected-file catalog because no *env* was given), naming
                the file; whatever *refuse* is. Never served from a
                snapshot.
            RenderCancelled: A templated file's render was cancelled
                (:class:`~otto.templating.errors.RenderCancelled`).
        """
        if not self.templated:
            return super().read_document(refuse=refuse)
        # Function-local: a process that renders no inventory loads no templating code.
        from ..templating.errors import RenderCancelled, TemplatingError, without_entry
        from ..templating.imports import ImportPolicy
        from ..templating.render import RenderRequest, render

        try:
            policy = ImportPolicy(roots=self.import_paths, protected=self._protection())
            data = render(RenderRequest(entry=self.path, tla=None, policy=policy)).value()
        except RenderCancelled:
            raise
        except TemplatingError as e:
            raise InventoryDataError(
                f"{self.path}: could not be rendered: {without_entry(e, self.path)}"
            ) from e
        wrap_secret_values(data)
        return data

    def _protection(self) -> "ProtectedPaths":
        """Return the one catalog this render runs under, resolved once.

        :data:`~otto.inventory.config.NO_CATALOG` with no *env*, under which
        nothing renders.
        """
        from .config import NO_CATALOG, env_protection  # deferred: config imports this module

        return NO_CATALOG if self.env is None else env_protection(self.env)

    def _input_paths(self) -> list[Path]:
        """Return every file a templated render may read, known without rendering.

        The rendering-dependency set a jsonnet lab source's key paths are too
        (:func:`~otto.templating.freshness.rendering_dependencies`): the
        file, each import root and everything under it, and the packaged
        ``otto.libsonnet``, ``import_paths = []`` included.
        """
        from ..templating.freshness import rendering_dependencies

        return rendering_dependencies(entries=[self.path], roots=self.import_paths)

    @override
    def fingerprint(self) -> "str | None":
        """Return the file's freshness; a templated inventory's covers every file it may read.

        For a templated inventory: the JSON list of the packaged library's
        digest (:func:`~otto.templating.imports.library_digest`) and the
        :func:`~otto.models.base.observed_fingerprint` of each of
        :meth:`stat_paths`, in order. It stats, and never renders.
        """
        if not self.templated:
            return super().fingerprint()
        from ..templating.imports import library_digest

        return json.dumps(
            [library_digest(), *(observed_fingerprint(p) for p in self._input_paths())]
        )

    @override
    def stat_paths(self) -> "list[Path] | None":
        """Return the paths the fingerprint stats.

        The file itself; for a templated inventory, the file, each import
        root and every directory and file under each, and the packaged
        ``otto.libsonnet``
        (:func:`~otto.templating.freshness.rendering_dependencies`).
        """
        if not self.templated:
            return super().stat_paths()
        return self._input_paths()

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


def is_templated_inventory(inventory: Inventory) -> bool:
    """Return whether *inventory* is a templated json inventory: THE wrapper-aware query.

    Peeled once with :func:`~otto.inventory.below_creds_overlay`, then
    :attr:`JsonInventory.templated` asked. No other wrapper needs peeling:
    the built-in ``json`` backend never takes the snapshot cache.
    """
    from .creds import below_creds_overlay  # deferred: no reader of a document needs it

    inner = below_creds_overlay(inventory=inventory)
    return isinstance(inner, JsonInventory) and inner.templated


def _json_inventory(c: "Configured[JsonInventoryConfig, InventoryEnv]") -> JsonInventory:
    """Build the built-in json inventory from its parsed configuration.

    ``Path.resolve()`` runs here, on the way into the backend: §9.1 says a
    json inventory's ``fingerprint()`` is the RESOLVED path, and doing it here
    keeps :class:`JsonInventory` a plain reader of the path it is handed. The
    env goes in with the import roots, so a templated file renders under the
    catalog of the view (or the table) that built it.
    """
    return JsonInventory(
        path=c.config.path.resolve(),
        supplies=c.config.supplies,
        import_paths=c.config.import_paths,
        env=c.env,
    )
