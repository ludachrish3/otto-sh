"""The stage-1 document: what the cache stores and ``export``/``diff`` write (§9.1, §9.5, §11).

One shape, three producers/consumers: the ``json`` backend reads it as its
inventory file, :class:`~otto.inventory.cache.SnapshotCache` writes it as a
remote backend's snapshot, and ``otto inventory export``/``diff`` write and
compare it. That is deliberate — a snapshot IS a stage-1 file, so an operator
can copy one out of ``~/.otto`` and point a ``json`` inventory at it. A
backend that declares instances writes the ``otto.inventory.v2`` form of the
same document (:func:`discovery_to_document`); every other backend writes the
flat form, byte for byte as before instances existed.

No credentials, ever: they live in the ``[creds]`` store or the record
(spec §9.4), so a document is shareable, diffable and committable without
leaking one.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typing_extensions import final

from ..models.inventory import INVENTORY_V2_FORMAT, InventoryInstance, InventoryRecord
from ..utils import atomic_write_bytes
from .discovery import Discovery
from .errors import InventoryDataError
from .json_backend import is_comment_key, parse_discovery_document


def stated_fields(record: InventoryRecord) -> dict[str, Any]:
    """Return what the record actually SAYS, as JSON types — nothing it was silent about.

    ``exclude_unset``, never ``exclude_defaults``: the two differ on a field
    stated AT its default (a NetBox device saying ``is_virtual: false``), and
    dropping that would make the document lossy in exactly the dimension every
    reader cares about — ``model_fields_set``, which
    :func:`~otto.inventory.resolve.resolve_host_entry` reads to decide what the
    inventory supplied. ``exclude_none`` because an explicit ``None`` is how a
    schema round-trip spells "unset", the same rule the join applies.

    ``creds`` are dropped whatever the record says: they live in the
    ``[creds]`` store or the record (§9.4), and a document that carried them
    would be a secret in a file the whole point of which is that it can be
    shared.
    """
    dumped = record.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    dumped.pop("creds", None)
    return dumped


def records_to_document(*, records: "Mapping[str, InventoryRecord]") -> dict[str, Any]:
    """``{key: record}`` as a stage-1 document: sorted keys, stated fields only, no ``creds``.

    Sorted so two dumps of the same inventory are byte-identical — the diff a
    reviewer reads, and the hash the cache keys on, both depend on it.
    """
    return {key: stated_fields(records[key]) for key in sorted(records)}


def _instance_stated(instance: InventoryInstance) -> dict[str, Any]:
    """Return an instance as JSON types, both fields always present.

    Whole rather than ``exclude_unset``: an instance's fields are canonical
    lists that default to empty, so ``{}`` and ``{"members": []}`` state the
    same instance and must write, hash and diff as one.
    """
    return instance.model_dump(mode="json")


def _refuse_comment_names(*, names: "Mapping[str, Any]", what: str) -> None:
    """Refuse the first of *names* a document would read as a comment."""
    comment = next((n for n in sorted(names) if is_comment_key(n)), None)
    if comment is not None:
        raise InventoryDataError(
            f"{what} {comment!r}: a '_'-prefixed name is a comment in an inventory document, "
            "so it cannot be snapshotted or exported; rename it at the source"
        )


def discovery_to_document(*, discovery: Discovery) -> dict[str, Any]:
    """Return *discovery* as a document: flat without instances, ``otto.inventory.v2`` with them.

    ``instances is None`` (the backend has no notion of instances) writes
    exactly :func:`records_to_document`, so every snapshot and export written
    before instances existed keeps its bytes and its hash. Otherwise the
    document carries the ``$format`` marker, the records under ``records``
    (the same stated, credential-free projection) and every instance, sorted
    by name, under ``instances`` (``{}`` included: "supported but empty"
    survives the round trip).

    Raises:
        otto.inventory.errors.InventoryDataError: A record or instance is
            named like a comment (``_``-prefixed,
            :func:`~otto.inventory.json_backend.is_comment_key`): every reader
            of the document would skip it, so a snapshot or export would
            silently lose it. Refused before anything is written, so a cold
            result, a warm read and an export never disagree.
    """
    _refuse_comment_names(names=discovery.records, what="record")
    if discovery.instances is not None:
        _refuse_comment_names(names=discovery.instances, what="instance")
    records = records_to_document(records=discovery.records)
    if discovery.instances is None:
        return records
    instances = discovery.instances
    return {
        "$format": INVENTORY_V2_FORMAT,
        "records": records,
        "instances": {name: _instance_stated(instances[name]) for name in sorted(instances)},
    }


def document_to_discovery(*, data: object, source: str, supplies: frozenset[str]) -> Discovery:
    """Parse a document, either format, with the ``json`` backend's own parser.

    A thin alias on purpose: the snapshot cache must read back exactly what the
    file backend would, so there is one parser and not two that drift.
    """
    return parse_discovery_document(data=data, source=source, supplies=supplies)


def document_to_records(
    *, data: object, source: str, supplies: frozenset[str]
) -> dict[str, InventoryRecord]:
    """Return the records of :func:`document_to_discovery`, for readers that skip instances."""
    return document_to_discovery(data=data, source=source, supplies=supplies).records


def document_hash(doc: "Mapping[str, Any]") -> str:
    """sha256 of the canonical JSON — a cached backend's ``fingerprint()`` (§9.5, §11).

    Canonical (sorted keys, no whitespace) so re-serialising an unchanged
    document cannot move the hash, and so the value survives a round trip
    through the file on disk.
    """
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_document_atomically(path: Path, doc: "Mapping[str, Any]") -> bytes:
    """Write-then-rename, so a reader never sees a half-written document.

    Returns the EXACT bytes that landed on disk. A caller that has to record a
    digest of the file — the snapshot cache's meta does — must hash what it
    wrote rather than re-read the path, because between the write and the
    re-read another process's rename can land, and the digest would then
    describe a file this call never produced.

    The write itself is :func:`otto.utils.atomic_write_bytes` (unique temp
    name in the destination directory, so two otto processes refreshing the
    same inventory in the same second never interleave bytes).

    The file lands MODE 0600, because that is what the shared writer's
    ``mkstemp`` creates and ``replace`` preserves. Worth stating rather than
    leaving to be rediscovered: a snapshot carries no credentials by
    construction, but it does describe a deployment's whole estate, and a
    refactor to ``path.write_text`` would silently widen it to 0644 on a
    default umask.
    """
    # Serialized before any temporary exists, so a document that will not
    # serialize leaves nothing behind.
    payload = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, payload)
    return payload


@final
@dataclass(frozen=True)
class RecordDifference:
    """One row of ``otto inventory diff``.

    ``field=None`` is a whole-entry row: the key (record or instance) exists
    on one side only, or, with ``whole_map`` set, one side has an instance map
    and the other has no notion of instances.
    """

    key: str
    """The inventory key (or, from :func:`diff_instances`, the instance name) this row is about."""

    field: "str | None"
    """The field that differs, or ``None`` for a key present on one side only."""

    left: "str | None"
    """The left side's value, rendered; ``None`` when the left side does not state it."""

    right: "str | None"
    """The right side's value, rendered; ``None`` when the right side does not state it."""

    whole_map: bool = False
    """``True`` only on :func:`diff_instances`' row for "one side has no notion of instances".

    The discriminator for that row, which no record key or instance name can
    produce: its ``key`` is a display label, and a legal key may spell it too.
    """


_CELL_ESCAPES = str.maketrans({"\n": "\\n", "\r": "\\r", "\t": "\\t"})
"""The three whitespace controls that would break a diff table's rows.

A targeted translation, NOT ``encode("unicode_escape")``: that also mangles
every non-ASCII character, and a site or rack name written in the operator's
own script must survive a diff intact.
"""


def _rendered(stated: "Mapping[str, Any]", field: str) -> "str | None":
    """Render one field of one side as a diff cell: ``None`` unstated, the value otherwise.

    A string is passed through, so ``lab-a`` reads as ``lab-a`` rather than
    ``"lab-a"`` — except for the three whitespace controls, which are escaped:
    a diff is a TABLE, and a raw newline or tab inside a cell breaks the row
    the reader is trying to line up against its neighbours. Everything else —
    numbers, booleans, structures — is canonical JSON with sorted keys, so two
    dicts differing only in key order are not reported as a difference.

    Deliberately NOT ``json.dumps(...).strip('"')``, which looks equivalent and
    is not. Given a value that itself contains a quote, that form kept every
    inner escape AND — since ``strip`` removes every trailing quote character,
    not one — swallowed the escape of the last one too, leaving a dangling
    backslash. A diff an operator cannot read is a diff they will not trust.
    """
    if field not in stated:
        return None
    value = stated[field]
    if isinstance(value, str):
        return value.translate(_CELL_ESCAPES)
    return json.dumps(value, sort_keys=True)  # already escapes controls inside strings


def _diff_stated(
    left: "Mapping[str, Mapping[str, Any]]", right: "Mapping[str, Mapping[str, Any]]"
) -> list[RecordDifference]:
    """Entry by entry, field by field, over what each side STATES; sorted by key then field.

    The one comparison behind :func:`diff_records` and :func:`diff_instances`:
    a key on one side only is one row with ``field=None``, and a field differs
    when the two sides state it differently (or one does not state it).
    """
    out: list[RecordDifference] = []
    for key in sorted(set(left) | set(right)):
        if key not in right:
            out.append(RecordDifference(key=key, field=None, left="present", right=None))
            continue
        if key not in left:
            out.append(RecordDifference(key=key, field=None, left=None, right="present"))
            continue
        a, b = left[key], right[key]
        out.extend(
            RecordDifference(
                key=key, field=field, left=_rendered(a, field), right=_rendered(b, field)
            )
            for field in sorted(set(a) | set(b))
            if a.get(field) != b.get(field)
        )
    return out


def diff_records(
    *, left: "Mapping[str, InventoryRecord]", right: "Mapping[str, InventoryRecord]"
) -> list[RecordDifference]:
    """Record by record, field by field, ``creds`` excluded; sorted by key then field.

    Compares what each side STATES (:func:`stated_fields`), not the two models: a
    field neither side mentions is not a difference, and a field one side
    states at its default and the other omits IS one — which is the whole
    question ``otto inventory diff`` exists to answer during the bridge (§19).
    """
    return _diff_stated(
        {key: stated_fields(record) for key, record in left.items()},
        {key: stated_fields(record) for key, record in right.items()},
    )


_WHOLE_MAP_LABEL = "(instances)"
"""The display ``key`` of the ``whole_map`` row; never how that row is recognised."""


def diff_instances(
    *,
    left: "Mapping[str, InventoryInstance] | None",
    right: "Mapping[str, InventoryInstance] | None",
) -> list[RecordDifference]:
    """Instance by instance, over ``members`` and ``roles``; sorted by name then field.

    Keyed by instance name, through the same comparison as
    :func:`diff_records`. When one side has no notion of instances (``None``)
    and the other has a map, even an empty one, the answer is one row with
    ``whole_map=True``, ``field=None`` and the display key ``"(instances)"``:
    "unsupported" and "supported but empty" are different statements (lab
    templating §6.2). Recognise that row by ``whole_map``, never by its key,
    which an instance may also be named.
    """
    if left is None or right is None:
        if left is None and right is None:
            return []
        return [
            RecordDifference(
                key=_WHOLE_MAP_LABEL,
                field=None,
                left=None if left is None else "present",
                right=None if right is None else "present",
                whole_map=True,
            )
        ]
    return _diff_stated(
        {name: _instance_stated(instance) for name, instance in left.items()},
        {name: _instance_stated(instance) for name, instance in right.items()},
    )
