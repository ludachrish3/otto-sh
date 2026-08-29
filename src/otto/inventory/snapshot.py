"""The stage-1 document: what the cache stores and ``export``/``diff`` write (§9.1, §9.5, §11).

One shape, three producers/consumers: the ``json`` backend reads it as its
inventory file, :class:`~otto.inventory.cache.SnapshotCache` writes it as a
remote backend's snapshot, and ``otto inventory export``/``diff`` write and
compare it. That is deliberate — a snapshot IS a stage-1 file, so an operator
can copy one out of ``~/.otto`` and point a ``json`` inventory at it.

No credentials, ever: they live in ``creds_file`` (spec §9.4), so a document
is shareable, diffable and committable without leaking one.
"""

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.inventory import InventoryRecord
from .json_backend import parse_inventory_document


def _stated(record: InventoryRecord) -> dict[str, Any]:
    """Return what the record actually SAYS, as JSON types — nothing it was silent about.

    ``exclude_unset``, never ``exclude_defaults``: the two differ on a field
    stated AT its default (a NetBox device saying ``is_virtual: false``), and
    dropping that would make the document lossy in exactly the dimension every
    reader cares about — ``model_fields_set``, which
    :func:`~otto.inventory.resolve.resolve_host_entry` reads to decide what the
    inventory supplied. ``exclude_none`` because an explicit ``None`` is how a
    schema round-trip spells "unset", the same rule the join applies.

    ``creds`` are dropped whatever the record says: they come from
    ``creds_file`` and nowhere else (§9.4), and a document that carried them
    would be a secret in a file the whole point of which is that it can be
    shared.
    """
    dumped = record.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    dumped.pop("creds", None)
    return dumped


def records_to_document(records: "Mapping[str, InventoryRecord]") -> dict[str, Any]:
    """``{key: record}`` as a stage-1 document: sorted keys, stated fields only, no ``creds``.

    Sorted so two dumps of the same inventory are byte-identical — the diff a
    reviewer reads, and the hash the cache keys on, both depend on it.
    """
    return {key: _stated(records[key]) for key in sorted(records)}


def document_to_records(
    data: object, *, source: str, supplies: frozenset[str]
) -> dict[str, InventoryRecord]:
    """Parse a stage-1 document with the ``json`` backend's own parser.

    A thin alias on purpose: the snapshot cache must read back exactly what the
    file backend would, so there is one parser and not two that drift.
    """
    return parse_inventory_document(data, source=source, supplies=supplies)


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

    A UNIQUE temp name in the destination directory rather than a fixed
    ``<name>.tmp``: two otto processes can refresh the same inventory in the
    same second, and a shared temp name would have them interleaving bytes into
    one file before either renamed it. Same directory because ``os.replace`` is
    atomic only within a filesystem.

    The file lands MODE 0600, because that is what ``mkstemp`` creates and
    ``replace`` preserves. Worth stating rather than leaving to be rediscovered:
    a snapshot carries no credentials by construction, but it does describe a
    deployment's whole estate, and a refactor to ``path.write_text`` would
    silently widen it to 0644 on a default umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # Serialized INSIDE the `with`, so a document that will not serialize
        # still closes the descriptor `mkstemp` handed us on its way out.
        with os.fdopen(fd, "wb") as handle:
            payload = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
            handle.write(payload)
        Path(tmp_name).replace(path)
    except BaseException:
        # Including KeyboardInterrupt: a temp file left in the cache directory
        # is litter the next reader has to ignore forever.
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
    return payload


@dataclass(frozen=True)
class RecordDifference:
    """One row of ``otto inventory diff``; ``field=None`` means the key exists on one side only."""

    key: str
    """The inventory key this row is about."""

    field: "str | None"
    """The record field that differs, or ``None`` for a key present on one side only."""

    left: "str | None"
    """The left side's value, rendered; ``None`` when the left side does not state it."""

    right: "str | None"
    """The right side's value, rendered; ``None`` when the right side does not state it."""


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


def diff_records(
    left: "Mapping[str, InventoryRecord]", right: "Mapping[str, InventoryRecord]"
) -> list[RecordDifference]:
    """Record by record, field by field, ``creds`` excluded; sorted by key then field.

    Compares what each side STATES (``_stated``), not the two models: a
    field neither side mentions is not a difference, and a field one side
    states at its default and the other omits IS one — which is the whole
    question ``otto inventory diff`` exists to answer during the bridge (§19).
    """
    out: list[RecordDifference] = []
    for key in sorted(set(left) | set(right)):
        if key not in right:
            out.append(RecordDifference(key=key, field=None, left="present", right=None))
            continue
        if key not in left:
            out.append(RecordDifference(key=key, field=None, left=None, right="present"))
            continue
        a, b = _stated(left[key]), _stated(right[key])
        out.extend(
            RecordDifference(
                key=key, field=field, left=_rendered(a, field), right=_rendered(b, field)
            )
            for field in sorted(set(a) | set(b))
            if a.get(field) != b.get(field)
        )
    return out
