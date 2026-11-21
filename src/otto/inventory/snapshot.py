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

The same stated projection is what a Jsonnet template reads as its inputs
(:func:`template_inputs`), narrowed further to the record fields a template
may see (:data:`TEMPLATE_PUBLIC_FIELDS`).
"""

import hashlib
import json
import types
import typing
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from typing_extensions import final

from ..models.inventory import INVENTORY_V2_FORMAT, InventoryInstance, InventoryRecord
from ..utils import annotation_is_subclass, atomic_write_bytes
from .creds import below_creds_overlay
from .discovery import Discovery, discover_inventory
from .errors import InventoryDataError
from .json_backend import is_comment_key, parse_discovery_document

if TYPE_CHECKING:
    from .view import InventoryView

_M = typing.TypeVar("_M", bound=BaseModel)


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
    dumped = _stated_dump(record)
    dumped.pop("creds", None)
    return dumped


def _stated_dump(record: InventoryRecord) -> dict[str, Any]:
    """Dump what *record* states (``exclude_unset``, ``exclude_none``) as JSON types.

    The one spelling of "stated" behind :func:`stated_fields` and
    :func:`public_record`.
    """
    return record.model_dump(mode="json", exclude_unset=True, exclude_none=True)


class _NotPlainError(Exception):
    """A value :func:`_plain` refuses; private, and never carries the value."""


_PLAIN_LEAVES: tuple[type, ...] = (str, int, float, bool, type(None))


def _plain(annotation: Any, value: Any) -> Any:
    """Return *value* as plain data, read the way the base schema's *annotation* declares it.

    The read a template's inputs are validated from: it never serializes,
    formats or converts a value, so a value of a backend's own type (one
    with its own serializer, say) cannot put a secret in the result or in
    an error. A model instance's fields are read from its ``__dict__``
    through ``object``'s own attribute lookup. A leaf must be exactly a ``str``, ``int``, ``float``,
    ``bool`` or ``None``, a list exactly a ``list`` or ``tuple`` and a
    mapping exactly a ``dict`` with ``str`` keys (subclasses refused). Where
    *annotation* declares an otto model, an instance of it (or of a subclass)
    is read as a dict of the fields that model declares and the instance
    states, so a subclass's own fields are dropped; a dict is read with those
    fields' annotations. Whether the result is valid is the schema's to say:
    this only decides what may reach it.

    Raises:
        _NotPlainError: Something in *value* is none of these.
    """
    annotation = _unwrapped(annotation)
    model = _declared_model(annotation)
    if model is not None and isinstance(value, model):
        # Through object's own lookup, so no attribute override on the value's class runs.
        stated = object.__getattribute__(value, "__pydantic_fields_set__")
        held = object.__getattribute__(value, "__dict__")
        return {
            name: _plain(field.annotation, held[name])
            for name, field in model.model_fields.items()
            if name in stated and name in held
        }
    kind = type(value)
    if kind in _PLAIN_LEAVES:
        return value
    if kind in (list, tuple):
        item = _argument(annotation, origins=(list, tuple), index=0)
        return [_plain(item, v) for v in value]
    if kind is dict:
        if not all(type(k) is str for k in value):
            raise _NotPlainError
        if model is not None:
            fields = model.model_fields
            return {
                k: _plain(fields[k].annotation if k in fields else Any, v) for k, v in value.items()
            }
        item = _argument(annotation, origins=(dict,), index=1)
        return {k: _plain(item, v) for k, v in value.items()}
    raise _NotPlainError


def _unwrapped(annotation: Any) -> Any:
    """Return *annotation* without its ``Annotated[...]`` metadata."""
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _declared_model(annotation: Any) -> "type[BaseModel] | None":
    """Return the model *annotation* declares, directly or as a member of a union, else ``None``.

    A generic alias (``list[X]``) declares none
    (:func:`~otto.utils.annotation_is_subclass`).
    """
    if annotation_is_subclass(annotation, BaseModel):
        return annotation
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        for member in typing.get_args(annotation):
            model = _declared_model(_unwrapped(member))
            if model is not None:
                return model
    return None


def _argument(annotation: Any, *, origins: tuple[type, ...], index: int) -> Any:
    """Return *annotation*'s type argument at *index* when it is one of *origins*, else ``Any``."""
    arguments = typing.get_args(annotation) if typing.get_origin(annotation) in origins else ()
    return arguments[index] if len(arguments) > index else Any


def _validated(model: "type[_M]", data: dict[str, Any]) -> "tuple[_M | None, list[str]]":
    """Validate *data*, an instance's plain read, as *model*: the result, or the refused fields.

    The refused fields are only the top-level names: a location's deeper
    parts are the backend's own keys, and an error's message or input may
    quote a value. The schema's error is never returned, so a caller raises
    its refusal outside any handler and keeps no context that quotes it.
    """
    try:
        return model.model_validate(data), []
    except ValidationError as e:
        return None, sorted({str(err["loc"][0]) for err in e.errors() if err["loc"]}) or ["?"]


def _plain_fields(
    model: "type[BaseModel]", instance: BaseModel, fields: "frozenset[str] | tuple[str, ...]"
) -> "tuple[dict[str, Any], list[str]]":
    """Read *instance*'s stated *fields* as *model* declares them: the plain data, and the refused.

    Only fields *model* declares and *instance* states, not ``None``, are
    read (the "stated" rule of :func:`stated_fields`), each through
    :func:`_plain` with *model*'s annotation, never *instance*'s class's, so
    a subclass cannot widen a field. A field :func:`_plain` refuses is named
    in the second list and left out of the first.
    """
    stated = object.__getattribute__(instance, "__pydantic_fields_set__")
    held = object.__getattribute__(instance, "__dict__")
    plain: dict[str, Any] = {}
    refused: list[str] = []
    for name in sorted(fields):
        field = model.model_fields.get(name)
        if field is None or name not in stated or held.get(name) is None:
            continue
        try:
            plain[name] = _plain(field.annotation, held[name])
        except _NotPlainError:
            refused.append(name)
    return plain, refused


TEMPLATE_PUBLIC_FIELDS: frozenset[str] = frozenset(
    {
        "ip",
        "interfaces",
        "hw_version",
        "sw_version",
        "os_name",
        "os_version",
        "board",
        "site",
        "rack",
        "shelf",
        "slot",
        "is_virtual",
        "element_id",
        "groups",
        "labels",
    }
)
"""The :class:`~otto.models.inventory.InventoryRecord` fields a template may read.

Every record field is either here or in :data:`TEMPLATE_PRIVATE_FIELDS`, and
a test fails when a new field is in neither, so a field is never exposed to
templates by default.
"""

TEMPLATE_PRIVATE_FIELDS: frozenset[str] = frozenset({"creds", "extra"})
"""The record fields a template never reads, credentials and the backend's free-form ``extra``."""


@final
@dataclass(frozen=True, kw_only=True)
class InstanceRole:
    """One instance a record's key belongs to, and the role it plays there."""

    instance: str
    """The instance's name."""
    role: "str | None"
    """The role the key plays, or ``None`` for a member without a role."""


def public_record(
    *,
    inventory: str,
    key: str,
    record: InventoryRecord,
    instances: "Sequence[InstanceRole] | None",
) -> dict[str, Any]:
    """Return *record* as a template reads it: its stated public fields, *inventory* and *key*.

    The boundary is an allowlist, validated against otto's own schema before
    anything is serialized. Only :data:`TEMPLATE_PUBLIC_FIELDS` are read, so
    neither ``creds`` nor ``extra`` is ever present, and each is read as
    plain data the way :class:`~otto.models.inventory.InventoryRecord`
    declares it, never through the record's own class: a
    backend's subclass cannot widen a public field (an interface carrying a
    password, say), and no value's own serializer runs. That plain data is
    validated as an ``InventoryRecord``, and what a template reads is that
    validated record's stated fields: a record built without validation
    (``model_construct``) cannot smuggle a dict into ``labels``, a
    credential into ``groups``, or a value whose own serializer would name
    a secret.

    ``instances`` (``[{"instance", "role"}]``, in the order given) is
    present only when *instances* is not ``None``, that is, when the
    inventory's backend supports instances; an empty list says the key
    belongs to none.

    Raises:
        otto.inventory.errors.InventoryDataError: A public field does not
            match otto's record schema. The message names the inventory,
            the key and the fields, never a value.
    """
    public, refused = _plain_fields(InventoryRecord, record, TEMPLATE_PUBLIC_FIELDS)
    validated: "InventoryRecord | None" = None
    if not refused:
        validated, refused = _validated(InventoryRecord, public)
    if validated is None:
        # Raised outside the handler, so the schema's error, which quotes the
        # value, is not even the refusal's __context__.
        raise InventoryDataError(
            f"inventory {inventory!r}, key {key!r}: fields "
            f"{', '.join(repr(f) for f in refused)} are missing or invalid for otto's "
            "inventory record schema, so the record cannot be given to a template"
        )
    projected: dict[str, Any] = {"inventory": inventory, "key": key}
    projected.update(_stated_dump(validated))
    if instances is not None:
        projected["instances"] = [{"instance": i.instance, "role": i.role} for i in instances]
    return projected


def public_instances(
    *, inventory: str, instances: "Mapping[str, InventoryInstance]"
) -> dict[str, InventoryInstance]:
    """Return *instances* as a template may read them: otto's own instance schema, validated.

    The one projection of a backend's instances for a template: both the
    membership a record carries (:func:`public_record`'s ``instances``) and
    the ``instances`` input are built from it. Only an instance's ``members``
    and ``roles`` are read, as plain strings, lists and dicts, so a
    backend's subclass cannot add a field (a password, say), and they are
    validated as a fresh ``InventoryInstance`` before anything is
    serialized, so an instance built without validation
    (``model_construct``) cannot smuggle a value into either, nor run a
    value's own serializer.

    Raises:
        otto.inventory.errors.InventoryDataError: An instance does not match
            otto's instance schema. The message names the inventory and the
            instance, never a value, and keeps no error.
    """
    projected: dict[str, InventoryInstance] = {}
    for name in sorted(instances):
        instance = _public_instance(instances[name])
        if instance is None:
            raise InventoryDataError(
                f"inventory {inventory!r}, instance {name!r}: its 'members' or 'roles' are "
                "invalid for otto's inventory instance schema, so the instance cannot be "
                "given to a template"
            )
        projected[name] = instance
    return projected


def _public_instance(instance: InventoryInstance) -> "InventoryInstance | None":
    """Return *instance*'s plain members and roles, validated; ``None`` when they do not fit.

    ``None`` rather than raising, so neither a refused value nor the
    schema's error, which may quote one, reaches the caller's refusal, not
    even as its context.
    """
    plain, refused = _plain_fields(InventoryInstance, instance, ("members", "roles"))
    if refused:
        return None
    validated, _ = _validated(InventoryInstance, plain)
    return validated


def _roles_by_key(*, instances: "Mapping[str, InventoryInstance]") -> dict[str, list[InstanceRole]]:
    """Map each key to every instance it belongs to, once per role, sorted by instance then role.

    One pass over the instances, so the cost is their total membership. Within
    an instance, a member without a role comes before the roles, which are
    sorted.
    """
    found: defaultdict[str, list[InstanceRole]] = defaultdict(list)
    for name in sorted(instances):
        instance = instances[name]
        for key in instance.members:
            found[key].append(InstanceRole(instance=name, role=None))
        for role in sorted(instance.roles):
            for key in instance.roles[role]:
                found[key].append(InstanceRole(instance=name, role=role))
    return found


def template_inputs(*, view: "InventoryView", names: Sequence[str], project: str) -> dict[str, Any]:
    """Return what a template reads: *project*, and each inventory in *names*, credential-free.

    The result is ``{"project", "inventories", "instances"}``.
    ``inventories`` maps each name to its :func:`public_record` list, sorted
    by key. ``instances`` maps each name to its instances (``{instance:
    {"members", "roles"}}``, :func:`public_instances`), or to ``None`` when
    its backend has no notion of instances; each record's membership is
    read from the same projection.

    Only the inventories *names* declares are read, each one below its creds
    overlay (:func:`~otto.inventory.creds.below_creds_overlay`), so neither a
    record's nor the store's credentials enter.

    Before each inventory's discovery it asks the operation's cancellation
    (``otto.templating.memo.current_cancellation``); once that is set, it
    raises ``otto.templating.errors.RenderCancelled`` and starts neither that
    discovery nor any later one. A worker abandoned at a deadline inside one
    inventory's discovery therefore never starts the next.

    Raises:
        TypeError: *names* is a single string rather than a sequence of names.
        otto.inventory.errors.InventoryError: A name in *names* is not
            configured in *view*, or a record's public fields do not match
            otto's record schema (:func:`public_record`), or an instance
            does not match otto's instance schema (:func:`public_instances`).
    """
    if isinstance(names, str):
        raise TypeError(f"names must be a sequence of inventory names, not the string {names!r}")
    # Imported here, so the import-light inventory paths never load the templating package.
    from ..templating.errors import RenderCancelled
    from ..templating.memo import current_cancellation

    inventories: dict[str, list[dict[str, Any]]] = {}
    instance_maps: dict[str, dict[str, Any] | None] = {}
    for name in dict.fromkeys(names):
        if current_cancellation():
            raise RenderCancelled(
                f"inventory {name!r}: the operation was cancelled before its discovery started"
            )
        discovery = discover_inventory(
            inventory=below_creds_overlay(inventory=view.select_name(name=name))
        )
        declared = (
            None
            if discovery.instances is None
            else public_instances(inventory=name, instances=discovery.instances)
        )
        roles = None if declared is None else _roles_by_key(instances=declared)
        inventories[name] = [
            public_record(
                inventory=name,
                key=key,
                record=discovery.records[key],
                instances=None if roles is None else roles.get(key, []),
            )
            for key in sorted(discovery.records)
        ]
        instance_maps[name] = (
            None
            if declared is None
            else {i: _instance_stated(declared[i]) for i in sorted(declared)}
        )
    return {"project": project, "inventories": inventories, "instances": instance_maps}


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
