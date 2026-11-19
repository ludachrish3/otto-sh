"""The ``netbox`` inventory backend (spec §9.2) — meets NetBox where it is.

Nothing is required of NetBox beyond a device per referenced host whose
``name`` is the key, an address ``ip_source`` can read, and a read-only token.
Virtual machines (NetBox's virtualization app) are opt-in: with
``virtual_machines = true`` they join the devices as records with
``is_virtual`` set, their address read per ``vm_ip_source``, and without it
the backend never asks the virtualization app anything, so a token scoped to
devices keeps working. ``supplies`` is what NetBox models natively plus ONLY
the custom fields you map; every other custom field is ignored, so a
deployment's own NetBox schema cannot leak fields into otto's records by
accident.

The whole filtered set is fetched once, on first use, into a dict keyed by
device (or virtual machine) name (pynetbox paginates), and kept for the
object's lifetime; the snapshot cache (``otto.inventory.cache``) keeps that to
one round of requests per TTL window. Every object is read through
``_Payload``: one the list response left any field this backend reads out
of (a ``fields``/``omit``/``brief`` filter, an older serializer) is loaded
whole from its detail page, once, BEFORE any of it is read, so no value from
the list outlives the load (save one the detail page omits, when only a
``url`` was there to load by); a full list response costs no such request, and
neither does an object the list already shows is skipped (unnamed, or with
an empty address). Every record is mapped inside the fetch, while its
session is still open, so that detail GET rides the same bounded,
OS-trusting session as the listing. ``pynetbox`` is
imported lazily inside the fetch so no other verb pays for it, and NOTHING —
not the token, not a socket — is touched at construction: a lab with no
referenced entry never talks to NetBox at all.

An argument error is a plain ``ValueError`` (otto's house style for a rejected
argument), which the registry's construction stage reports as an
:class:`~otto.inventory.errors.InventoryConstructionError` naming the
settings file and the backend. Everything that can only fail later — a missing
token, an unreachable host, a device or virtual machine NetBox describes in a
way no record accepts — is an :class:`~otto.inventory.errors.InventoryError`
naming the URL.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from typing_extensions import final

from ..models.base import compact_validation_error, wrap_secret_values
from ..models.inventory import FILLABLE_INVENTORY_FIELDS, INVENTORY_KEY_FIELDS, InventoryRecord
from ..tls import DEFAULT_TIMEOUT_SECONDS, _checked_timeout, os_trust_session
from .errors import InventoryError, InventoryKeyError
from .protocol import check_supplies

if TYPE_CHECKING:
    from ..registry import Configured
    from .config import NetBoxInventoryConfig
    from .registry import InventoryEnv

NATIVE_SUPPLIES: frozenset[str] = frozenset(
    {"ip", "site", "rack", "shelf", "board", "os_name", "is_virtual"}
)
"""What this backend can state without a custom field (spec §9.2's mapping table).

A device can state every one; a virtual machine states all but ``rack``,
``shelf`` and ``board``, which it has no NetBox field for.
"""

_CF_PREFIX = "cf:"

_CUSTOM_FIELD_EXCLUDED: frozenset[str] = frozenset({"ip", "creds", "interfaces"})
"""Record fields a custom field may never fill (spec §9.2, controller ruling R21).

``ip`` has ``ip_source`` — two ways to say the same thing is a way for them to
disagree; ``creds`` come from the ``[creds]`` store or the lab file and
nowhere else (§9.4); and ``interfaces`` is a structure, not a scalar a NetBox
custom field can hold.
"""

_SHARED_EXTRA_KEYS: frozenset[str] = frozenset({"id", "status", "tags"})
"""What the shared mapping puts in ``record.extra`` for every kind."""

_DEFAULT_IP_SOURCE = "primary_ip4"
"""Where an address comes from unless ``ip_source`` / ``vm_ip_source`` says otherwise."""


_REQUIRED: Any = object()
"""A read's default: a field that is still absent after the detail load is an error."""

_SHARED_READS: "list[str]" = ["name", "id", "site", "platform", "custom_fields", "status", "tags"]
"""The top-level fields every kind's mapping reads; ``name`` and ``id`` first."""


def _present(record: Any, name: str, default: Any) -> Any:
    """Return field *name* of *record*; absent, *default*, or pynetbox's own error.

    Takes the record, not its values dict, and every reader here does the
    same: a record's values dict is never held in a local. Traceback locals
    reach both log files (see the logging page's warning on local
    variables), and that dict holds the raw custom fields — a ``password``
    one included — plus pynetbox's own copies of them, which no
    :func:`~otto.models.base.wrap_secret_values` call reaches. A pynetbox
    record itself renders as its name.
    """
    if name in vars(record):
        return vars(record)[name]
    if default is _REQUIRED:
        raise AttributeError(f'object has no attribute "{name}"')
    return default


def _field(record: Any, name: str, default: Any = _REQUIRED) -> Any:
    """Return field *name* of a NESTED pynetbox *record* (a site, an IP address, a tag).

    Read from the record's own values, loading its detail page first if
    *name* is not among them and it has a ``url`` to load from
    (:meth:`pynetbox.core.response.Record.full_details`, once). NetBox's
    nested representations carry everything this backend reads, so a full
    response costs no request here. A top-level object goes through
    :class:`_Payload` instead, which can also load by id.
    """
    if name not in vars(record) and record.url and not record.has_details:
        record.full_details()
    return _present(record, name, default)


@final
class _Payload:
    """One top-level object as its listing returned it, completed from NetBox at most once.

    ONE rule for every kind: :meth:`complete` runs before anything is read,
    and if any field in *reads* (every field this object's mapping reads) is
    not among the record's own values, it loads the whole object, once.
    Every read after that (:meth:`field`) is of the completed record, so the
    name, the duplicate check and the mapped record all see one version of
    the object, never a list value the detail page has since replaced. An
    explicit null is a value, and costs no request.

    The load is pynetbox's own: by id from the endpoint it was listed from
    (``endpoint.get(id)``), which REPLACES the record with a fresh one, so a
    field the detail page leaves out is absent rather than the list's.
    Only with no id to go by is it the record's ``url``
    (:meth:`pynetbox.core.response.Record.full_details`), which updates in
    place: a value the list served and the detail page omits survives.
    Either way through the API's session, while the fetch still holds it
    open.

    Not attribute access, because pynetbox models declare some fields as
    class attributes (``primary_ip4`` on devices and virtual machines,
    ``oob_ip`` and ``device_type`` on devices): reading one the payload
    lacked returns the model class and never triggers pynetbox's own lazy
    load, so a brief record would silently read as addressless.
    """

    def __init__(self, record: Any, endpoint: Any, reads: "list[str]") -> None:
        self.record = record
        self._endpoint = endpoint
        self._reads = reads

    def has(self, names: "list[str]") -> bool:
        """Whether the listing served every field in *names* (a null counts as served)."""
        return all(n in vars(self.record) for n in names)

    def complete(self) -> None:
        """Load the whole object, once, if the listing lacked any field it will be read for.

        By id from its endpoint, else by its detail URL (see the class
        docstring for how the two differ). With neither, nothing can say
        what the missing field holds, and the fetch fails naming it: a
        silent "unnamed" or "addressless" here would publish a snapshot
        missing the object.
        """
        # Only scalars and records in this frame's locals (see _present):
        # the GET below can raise, and a raising frame's locals are logged.
        missing = next((n for n in self._reads if n not in vars(self.record)), None)
        if missing is None:
            return
        id_ = vars(self.record).get("id")
        if id_ is None:
            if not self.record.url:
                raise AttributeError(
                    f'object has no attribute "{missing}", and neither a url nor an id to '
                    "load it by"
                )
            self.record.full_details()
            return
        loaded = self._endpoint.get(id_)
        if loaded is None:  # pynetbox's answer to a 404
            raise LookupError(f"its detail page (id {id_}) does not exist")
        self.record = loaded

    def field(self, name: str, default: Any = _REQUIRED) -> Any:
        """Return field *name* of the completed object.

        Still absent after :meth:`complete`, the field is *default*, or,
        with none given, an ``AttributeError`` in pynetbox's own words, which
        the per-object guard turns into an error naming the object and the
        URL. A field outside *reads* is refused: it could not have decided
        the load, and a projection lacking it would read as absent.
        """
        if name not in self._reads:
            raise RuntimeError(f"field {name!r} is read but missing from the object's reads")
        return _present(self.record, name, default)


def _key(raw_name: Any) -> "str | None":
    """Return the key a NetBox *name* gives, or ``None`` for an unnamed object.

    NetBox allows an unnamed device and the default filter is "everything".
    Keyed by name, these would ALL collide on the literal string 'None'. An
    unnamed VM is skipped the same way.
    """
    return None if raw_name is None or not str(raw_name).strip() else str(raw_name)


def _name_of(nested: Any, field: str = "name") -> "str | None":
    """Return *field* of a nested NetBox object as a string, or ``None`` when there is none."""
    return None if nested is None else str(_field(nested, field))


def _device_fields(d: _Payload) -> "dict[str, Any]":
    """Return the record fields only a device states: its rack, shelf and board."""
    fields: dict[str, Any] = {}
    rack = d.field("rack")
    if rack is not None:
        fields["rack"] = _name_of(rack)
    position = d.field("position")
    if position is not None:
        # NetBox's `position` is a DRF DecimalField — serialised as a
        # STRING by default, and half-U positions ("3.5") are ordinary.
        # `int("3.5")` raises; the shelf is the integer part.
        fields["shelf"] = int(float(position))
    device_type = d.field("device_type")
    if device_type is not None:
        fields["board"] = _name_of(device_type, "model")
    return fields


def _device_extra(d: _Payload) -> "dict[str, Any]":
    """Return the ``record.extra`` keys only a device states."""
    return {"serial": d.field("serial") or None, "asset_tag": d.field("asset_tag") or None}


def _virtual_machine_extra(v: _Payload) -> "dict[str, Any]":
    """Return the ``record.extra`` keys only a virtual machine states.

    Its cluster has no record field, and ``extra`` is opaque to otto.
    """
    return {"cluster": _name_of(v.field("cluster"))}


@final
@dataclass(frozen=True)
class _Kind:
    """One kind of NetBox object this backend turns into records."""

    noun: str
    """What every message calls one: ``"device"`` or ``"virtual machine"``."""
    plural: str
    """What a message calls the listing of them."""
    is_virtual: bool
    """``True``: every record of this kind is virtual, whatever a custom field says."""
    ip_source_key: str
    """The setting this kind's address source comes from, as messages name it."""
    endpoint: "Callable[[Any], Any]"
    """The pynetbox endpoint this kind is listed from, given the API object."""
    fields_of: "Callable[[_Payload], dict[str, Any]]"
    """Reads the record fields only this kind states."""
    extra_of: "Callable[[_Payload], dict[str, Any]]"
    """Reads the ``record.extra`` keys only this kind states, placed right after ``id``."""
    extra_keys: frozenset[str]
    """The keys ``extra_of`` writes; reserved against ``extra_custom_fields``."""
    reads: frozenset[str]
    """The top-level fields ``fields_of`` and ``extra_of`` read (see :func:`_reads`)."""


_DEVICE = _Kind(
    noun="device",
    plural="devices",
    is_virtual=False,
    ip_source_key="ip_source",
    endpoint=lambda api: api.dcim.devices,
    fields_of=_device_fields,
    extra_of=_device_extra,
    extra_keys=frozenset({"serial", "asset_tag"}),
    reads=frozenset({"rack", "position", "device_type", "serial", "asset_tag"}),
)
_VIRTUAL_MACHINE = _Kind(
    noun="virtual machine",
    plural="virtual machines",
    is_virtual=True,
    ip_source_key="vm_ip_source",
    endpoint=lambda api: api.virtualization.virtual_machines,
    # No rack, position or device type on a VM: no rack, shelf or board.
    fields_of=lambda _v: {},
    extra_of=_virtual_machine_extra,
    extra_keys=frozenset({"cluster"}),
    reads=frozenset({"cluster"}),
)


@final
@dataclass(frozen=True)
class _Selection:
    """What one kind's listing is asked for, and what each object of it is read for.

    Its filter, verbatim, its address source, and :func:`_reads` of the two.
    """

    filter: "dict[str, Any]"
    ip_source: str
    reads: "list[str]"


def _reads(kind: _Kind, ip_source: str) -> "list[str]":
    """Every top-level field an object of *kind* is read for, ``name`` and ``id`` first.

    The ONE list :meth:`_Payload.complete` decides the detail load from;
    :meth:`_Payload.field` refuses a field it lacks, so a reader added
    without it fails every test that maps a record.
    """
    head = [*_SHARED_READS[:2], _address_field(ip_source)]
    return head + sorted({*_SHARED_READS[2:], *kind.reads} - set(head))


def _address_field(ip_source: str) -> str:
    """Return the top-level field an address is read from under *ip_source*."""
    return "custom_fields" if ip_source.startswith(_CF_PREFIX) else ip_source


@final
@dataclass(frozen=True)
class _Seen:
    """Which object a name came from: its kind and its NetBox id."""

    kind: _Kind
    id: int


def _checked_ip_source(key: str, value: str, named: "list[str]") -> str:
    """Return *value* if it is one of *named* or ``cf:<custom field>``; refuse it otherwise."""
    if value in named or (value.startswith(_CF_PREFIX) and len(value) > len(_CF_PREFIX)):
        return value
    choices = [repr(n) for n in named] + ["'cf:<custom field>'"]
    listed = ", ".join(choices[:-1]) + ("," if choices[1:-1] else "") + f" or {choices[-1]}"
    raise ValueError(f"{key} must be {listed}, got {value!r}")


@final
class _NetBoxInventory:
    """Inventory over the devices (and, opted in, virtual machines) NetBox filters select.

    Parameters
    ----------
    url : str
        Base URL of the NetBox instance, e.g. ``https://netbox.example.com``.
    token_env : str
        Name of the environment variable holding the API token. The token
        itself never sits in a settings file; the variable is read at the first
        fetch, not at construction.
    filter : dict[str, Any] | None
        NetBox device filter, forwarded verbatim (``{"site": "lab-a"}``). No
        filter means every device.
    ip_source : str
        Where a device's management address comes from: ``"primary_ip4"``,
        ``"oob_ip"``, or ``"cf:<custom field>"``. Virtual machines have their
        own, ``vm_ip_source``.
    custom_fields : dict[str, str] | None
        Record field -> NetBox custom field name. Each mapped field JOINS
        ``supplies``; nothing else about an object's custom fields reaches the
        record. A mapped ``is_virtual`` applies to devices only: a virtual
        machine is virtual.
    extra_custom_fields : list[str] | None
        NetBox custom field names to carry through opaquely in
        ``record.extra``. They are not record fields and never join
        ``supplies``, and none of them may shadow a key this backend already
        puts there (``reserved_extra_keys``).
    virtual_machines : bool
        Also fetch NetBox virtual machines, as records with ``is_virtual``
        true. Off by default, and off means the virtualization app is never
        asked anything. Device and VM names share one key space: a name both
        use fails the fetch, naming both objects.
    vm_filter : dict[str, Any] | None
        NetBox virtual machine filter, forwarded verbatim as ``filter`` is to
        devices. Only meaningful with ``virtual_machines``; given without it,
        it is refused rather than silently ignored.
    vm_ip_source : str
        Where a virtual machine's management address comes from:
        ``"primary_ip4"`` (the default) or ``"cf:<custom field>"``. NetBox has
        no out-of-band address on a VM, so ``"oob_ip"`` is refused. Refused
        too, at anything but the default, without ``virtual_machines``.
    timeout : float
        Seconds one HTTP request to NetBox may take, default
        :data:`otto.tls.DEFAULT_TIMEOUT_SECONDS`. Must be a positive number.
        Applied to every request the fetch makes through the session
        :func:`otto.tls.os_trust_session` builds — pynetbox issues the calls
        itself and offers no seam to pass a timeout through, so the bound
        and the OS-store trust both ride on the session it is handed.

    Attributes
    ----------
    unnamed_device_ids : list[int]
        Ids of devices the filter selected that have no ``name`` (NetBox
        allows it, and the default filter is "everything"). They are skipped
        rather than keyed as the string ``'None'``; the list is empty until the
        first fetch, and is here so ``otto inventory list`` can say how many
        devices it passed over.
    addressless_device_names : list[str]
        Names of selected devices with no address at ``ip_source`` — the other
        half of what ``otto inventory list`` reports as skipped. See the
        property below.
    unnamed_virtual_machine_ids : list[int]
        ``unnamed_device_ids`` for virtual machines; always empty without
        ``virtual_machines``.
    addressless_virtual_machine_names : list[str]
        Names of selected virtual machines with no address at
        ``vm_ip_source``.
    reserved_extra_keys : frozenset[str]
        Every key this backend itself writes into ``record.extra`` for the
        kinds it fetches.
    """

    def __init__(
        self,
        *,
        url: str,
        token_env: str = "NETBOX_TOKEN",  # noqa: S107 — the NAME of the variable, not a token
        filter: "dict[str, Any] | None" = None,  # noqa: A002 — the NetBox term
        ip_source: str = _DEFAULT_IP_SOURCE,
        custom_fields: "dict[str, str] | None" = None,
        extra_custom_fields: "list[str] | None" = None,
        virtual_machines: bool = False,
        vm_filter: "dict[str, Any] | None" = None,
        vm_ip_source: str = _DEFAULT_IP_SOURCE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not url or not isinstance(url, str):
            raise ValueError("'url' must be a non-empty NetBox base URL")
        _checked_ip_source("ip_source", ip_source, ["primary_ip4", "oob_ip"])
        if not virtual_machines:
            # Given without VMs, either key would be silently ignored; a
            # setting that does nothing is a setting someone thinks works.
            if vm_filter is not None:
                raise ValueError(
                    "vm_filter has no effect unless virtual_machines = true; "
                    "set both, or drop vm_filter"
                )
            if vm_ip_source != _DEFAULT_IP_SOURCE:
                raise ValueError(
                    "vm_ip_source has no effect unless virtual_machines = true; "
                    "set both, or drop vm_ip_source"
                )
        if vm_ip_source == "oob_ip":
            raise ValueError(
                "vm_ip_source cannot be 'oob_ip': NetBox has no out-of-band address on a "
                "virtual machine; use 'primary_ip4' or 'cf:<custom field>'"
            )
        _checked_ip_source("vm_ip_source", vm_ip_source, ["primary_ip4"])
        if custom_fields is not None and not isinstance(custom_fields, dict):
            raise ValueError(
                "custom_fields must be a table of record field -> NetBox custom field name, got "
                f"{type(custom_fields).__name__} {custom_fields!r}"
            )
        self.custom_fields = dict(custom_fields or {})
        unknown = sorted(set(self.custom_fields) - FILLABLE_INVENTORY_FIELDS - INVENTORY_KEY_FIELDS)
        if unknown:
            raise ValueError(
                f"custom_fields maps {unknown[0]!r}, which is not an inventory record field "
                f"(record fields: {sorted(FILLABLE_INVENTORY_FIELDS | INVENTORY_KEY_FIELDS)})"
            )
        excluded = sorted(set(self.custom_fields) & _CUSTOM_FIELD_EXCLUDED)
        if excluded:
            raise ValueError(
                f"custom_fields may not map {excluded[0]!r} "
                f"(excluded: {sorted(_CUSTOM_FIELD_EXCLUDED)}): 'ip' comes from ip_source, "
                "'creds' come from the [creds] store or the lab file, and 'interfaces' is a "
                "structure no NetBox custom field holds"
            )
        # A bare string here is the accident this catches: `list("owner")` is
        # ['o','w','n','e','r'], five custom fields NetBox has never heard of,
        # and every record would silently carry five None-valued extra keys.
        if extra_custom_fields is not None and (
            isinstance(extra_custom_fields, str)
            or not isinstance(extra_custom_fields, list)
            or not all(isinstance(name, str) for name in extra_custom_fields)
        ):
            raise ValueError(
                "extra_custom_fields must be a list of NetBox custom field names, got "
                f"{type(extra_custom_fields).__name__} {extra_custom_fields!r}"
            )
        self.extra_custom_fields = list(extra_custom_fields or [])
        self.virtual_machines = virtual_machines
        self.filter = dict(filter or {})
        self.vm_filter = dict(vm_filter or {})
        self.ip_source = ip_source
        self.vm_ip_source = vm_ip_source
        # The ONE place that decides which kinds a fetch lists, each with its
        # filter and address source; devices first, so a cross-kind duplicate
        # names the device before the VM.
        self._selected: dict[_Kind, _Selection] = {
            _DEVICE: _Selection(self.filter, ip_source, _reads(_DEVICE, ip_source))
        }
        if virtual_machines:
            self._selected[_VIRTUAL_MACHINE] = _Selection(
                self.vm_filter, vm_ip_source, _reads(_VIRTUAL_MACHINE, vm_ip_source)
            )
        self.reserved_extra_keys = _SHARED_EXTRA_KEYS.union(
            *(kind.extra_keys for kind in self._selected)
        )
        clash = sorted(set(self.extra_custom_fields) & self.reserved_extra_keys)
        if clash:
            source = " or ".join(kind.noun for kind in self._selected)
            raise ValueError(
                f"extra_custom_fields names {clash[0]!r}, which this backend already puts in "
                f"record.extra from the {source} itself "
                f"(reserved: {sorted(self.reserved_extra_keys)})"
            )
        self.timeout = _checked_timeout(timeout)
        # Normalised once, here: `label`, every error message and the snapshot
        # cache's slug all read `self.url`, so a trailing slash must not be
        # able to make two spellings of one inventory look like two.
        self.url = url.rstrip("/")
        self.token_env = token_env
        self.supplies = check_supplies(NATIVE_SUPPLIES | set(self.custom_fields))
        self.label = f"netbox:{self.url}"
        self._unnamed: list[_Seen] = []
        self._records: "dict[str, InventoryRecord] | None" = None
        self._no_address: dict[str, _Seen] = {}

    @property
    def unnamed_device_ids(self) -> "list[int]":
        """Ids of selected devices with no ``name``, in fetch order; see the class docstring."""
        return [seen.id for seen in self._unnamed if seen.kind is _DEVICE]

    @property
    def unnamed_virtual_machine_ids(self) -> "list[int]":
        """Ids of selected virtual machines with no ``name``, in fetch order."""
        return [seen.id for seen in self._unnamed if seen.kind is _VIRTUAL_MACHINE]

    @property
    def addressless_virtual_machine_names(self) -> "list[str]":
        """Names of selected virtual machines with no address at ``vm_ip_source``, sorted.

        The VM half of :attr:`addressless_device_names`, under the same rules.
        """
        return sorted(n for n, seen in self._no_address.items() if seen.kind is _VIRTUAL_MACHINE)

    @property
    def addressless_device_names(self) -> "list[str]":
        """Names of selected devices that have no address at ``ip_source``, sorted.

        The twin of ``unnamed_device_ids``, and public for the same
        reason: ``otto inventory list`` reports what the fetch passed over, and
        an operator whose device is "missing from otto" needs to be told it was
        selected and skipped rather than never seen. Read-only — the fetch owns
        the underlying map, and hands out a fresh list here so a caller cannot
        edit what the next lookup will consult.

        Empty until the first fetch, which also means empty whenever the
        snapshot cache answered from disk: nothing was selected that run, so
        there is nothing to report.
        """
        return sorted(n for n, seen in self._no_address.items() if seen.kind is _DEVICE)

    # -- fetch -------------------------------------------------------------
    def _token(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise InventoryError(
                f"netbox inventory {self.url}: environment variable {self.token_env!r} is not set "
                "(the API token never sits in a settings file)"
            )
        return token

    def _fetch(self) -> "dict[str, InventoryRecord]":
        token = self._token()
        import pynetbox  # lazy: no other verb pays for it (spec §16)

        api = None
        listing: "_Kind | None" = None
        records: dict[str, InventoryRecord] = {}
        # ONE name space for every kind: a device and a VM named alike collide
        # here exactly as two devices do, addressless entries included.
        seen: dict[str, _Seen] = {}
        no_address: dict[str, _Seen] = {}
        unnamed: list[_Seen] = []
        try:
            api = pynetbox.api(self.url, token=token)
            # Before any request: an unreachable NetBox must be bounded on the
            # very first one, which is the only one a down instance ever sees.
            # pynetbox builds a plain requests.Session in its constructor and
            # documents http_session as the seam to supply one; otto.tls owns
            # what that session trusts and how long a request may take.
            api.http_session.close()
            api.http_session = os_trust_session(timeout=self.timeout)
            # Kind by kind, all inside this one guard and this one session:
            # the VM listing fails as the device listing does, and the MAPPING
            # runs here too, because reading a field the list response lacked
            # makes pynetbox GET that object's detail page through this
            # session. Without `virtual_machines`, `api.virtualization` is
            # never so much as read.
            for kind, selection in self._selected.items():
                listing = kind
                endpoint = kind.endpoint(api)
                objects = _select(endpoint, selection.filter)
                listing = None
                for record in objects:
                    d = _Payload(record, endpoint, selection.reads)
                    # Every read of an object's payload is inside this guard:
                    # NetBox's serializer varies by version and by plugin, and
                    # a field this backend expects but a given instance omits
                    # must come back naming the object and the URL, never as a
                    # raw AttributeError (§9.2). Per object on purpose: one try
                    # around the whole loop could only ever say "some device
                    # did".
                    try:
                        self._ingest(kind, d, records, seen, no_address, unnamed)
                    except InventoryError:
                        raise  # already names the URL and the object
                    except Exception as e:
                        raise InventoryError(
                            f"netbox inventory {self.url}: {_named_record(kind, d)}: "
                            f"{type(e).__name__}: {e}"
                        ) from e
        except InventoryError:
            raise  # an object's own error, already naming the URL and the object
        # Broad on purpose: pynetbox raises its own RequestError, and requests'
        # connection/TLS/timeout families underneath it. Every one of them means
        # the same thing to a caller — this inventory could not answer.
        except Exception as e:
            # Which listing failed, once there is more than one to choose from:
            # a 403 for a token scoped to devices names no URL of its own.
            step = f"listing {listing.plural}: " if listing and len(self._selected) > 1 else ""
            raise InventoryError(
                f"netbox inventory {self.url}: {step}{type(e).__name__}: {e}"
            ) from e
        finally:
            # pynetbox builds its own requests.Session and never closes it.
            # Closed only once every record is mapped: a detail GET for a
            # field the list lacked must never go out through a closed session.
            if api is not None:
                api.http_session.close()
        # Assigned only once the whole set parsed: a fetch that raised halfway
        # must leave no half-built view behind for the retry to read.
        self._no_address = no_address
        self._unnamed = unnamed
        return records

    def _ingest(
        self,
        kind: _Kind,
        d: _Payload,
        records: "dict[str, InventoryRecord]",
        seen: "dict[str, _Seen]",
        no_address: "dict[str, _Seen]",
        unnamed: "list[_Seen]",
    ) -> None:
        """Fold one object of *kind* into the fetch's buckets: keyed, addressless, or unnamed.

        A separate method rather than an inline loop body so the caller's
        ``try`` wraps a CALL: every read of the payload below is guarded, and
        the one deliberate ``raise`` here is not buried inside the handler that
        re-raises it unchanged.
        """
        # One order for every object: complete it unless the listing
        # already shows it is skipped, then its name and the duplicate check,
        # then its address and record. Completing first means every value
        # below is the detail page's when one was loaded (a rename, a
        # corrected address); naming before mapping means a duplicate is
        # reported as one, whatever else is wrong with it.
        if not self._skipped_as_listed(kind, d):
            d.complete()
        name = _key(d.field("name", None))  # still absent after the detail load: unnamed
        this = _Seen(kind, int(d.field("id")))
        if name is None:
            unnamed.append(this)
            return
        first = seen.get(name)
        if first is not None:
            raise InventoryError(f"netbox inventory {self.url}: {_duplicate(name, first, this)}")
        seen[name] = this
        address = self._address(d, self._selected[kind].ip_source)
        if address is None:
            no_address[name] = this
            return
        records[name] = self._record_for(kind, d, address)

    def _skipped_as_listed(self, kind: _Kind, d: _Payload) -> bool:
        """Whether the listing alone shows *d* is skipped: unnamed, or with an empty address.

        Such an object is skipped as it was served, with no detail request:
        what it would take from its detail page is never read. Anything else
        (published, or with its name or address left out) is completed.
        """
        if not d.has(["id", "name"]):
            return False
        if _key(d.field("name")) is None:
            return True  # unnamed as listed: its address is never read
        ip_source = self._selected[kind].ip_source
        return d.has([_address_field(ip_source)]) and self._address(d, ip_source) is None

    def _address(self, d: _Payload, ip_source: str) -> "str | None":
        """Return the object's address per *ip_source*, or ``None`` if it has none.

        One rule for every kind; each kind brings its own source. A source
        field the object does not have even on its detail page (NetBox before
        3.6 has no device ``oob_ip``) is no address, as a null one is.
        """
        if ip_source.startswith(_CF_PREFIX):
            value = (d.field("custom_fields") or {}).get(ip_source[len(_CF_PREFIX) :])
            return str(value) if value else None
        ip = d.field(ip_source, None)
        address = None if ip is None else _field(ip, "address", None)
        return str(address) if address else None

    def _record_for(self, kind: _Kind, d: _Payload, address: str) -> InventoryRecord:
        """One object -> one record: §9.2's mapping table, and nothing else.

        The mapping every kind shares lives here, read in one order and under
        one policy (:class:`_Payload`); what only one kind states (a device's
        rack, a VM's cluster) comes from ``kind.fields_of`` and
        ``kind.extra_of``.
        """
        raw: dict[str, Any] = {"ip": address.split("/", 1)[0], "is_virtual": False}
        site = d.field("site")
        if site is not None:
            raw["site"] = _name_of(site)
        raw.update(kind.fields_of(d))
        platform = d.field("platform")
        if platform is not None:
            raw["os_name"] = _name_of(platform)
        # Wrapped before anything else holds it: an extra custom field may be
        # named like a secret (``password``), and this frame's locals reach the
        # log files if the record below fails to validate.
        cf = dict(d.field("custom_fields") or {})
        wrap_secret_values(cf)
        for field, cf_name in self.custom_fields.items():
            if cf.get(cf_name) is not None:
                raw[field] = cf[cf_name]
        if kind.is_virtual:
            # After the mappings, on purpose: a device may be mapped virtual
            # (an emulator modelled as a device), but a virtual machine IS
            # virtual, and no custom field can make its record say otherwise.
            raw["is_virtual"] = True
        # Built step by step so the reads keep their order (and a payload
        # missing two fields names the same one it always did): id, the
        # kind's own keys, status, tags.
        extra: dict[str, Any] = {"id": int(d.field("id"))}
        extra.update(kind.extra_of(d))
        extra["status"] = _name_of(d.field("status"), "value")
        extra["tags"] = [_name_of(t) for t in (d.field("tags") or [])]
        extra.update({name: cf.get(name) for name in self.extra_custom_fields})
        raw["extra"] = extra
        try:
            return InventoryRecord.model_validate(raw)
        except ValidationError as e:
            # `ip` is read from a NetBox field the record does not name, so
            # say which one: a custom field holds whatever an editor typed.
            bad_ip = any(err["loc"][:1] == ("ip",) for err in e.errors(include_input=False))
            ip_source = self._selected[kind].ip_source
            source = f" (ip read from {kind.ip_source_key} {ip_source!r})" if bad_ip else ""
            raise InventoryError(
                f"netbox inventory {self.url}: {_named_record(kind, d)}: "
                f"{compact_validation_error(e)}{source}"
            ) from e

    # -- protocol ----------------------------------------------------------
    def _load(self) -> "dict[str, InventoryRecord]":
        if self._records is None:
            self._records = self._fetch()
        return self._records

    def lookup(self, key: str) -> InventoryRecord:
        """Return the record for *key*; fetches the whole selected set on the first call."""
        records = self._load()
        if key in records:
            return records[key]
        if key in self._no_address:
            skipped = self._no_address[key]
            kind = skipped.kind
            raise InventoryError(
                f"netbox inventory {self.url}: {_named(kind, key, skipped.id)} "
                f"has no address at {kind.ip_source_key} {self._selected[kind].ip_source!r}"
            )
        raise InventoryKeyError(key, label=self.label)

    def list_keys(self) -> list[str]:
        """Every device (and VM) name the filters select that has an address, sorted."""
        return sorted(self._load())

    def fingerprint(self) -> "str | None":
        """``None`` — not cacheable on its own; the snapshot cache supplies one (spec §9.5)."""
        return None


def _select(endpoint: Any, filter: "dict[str, Any]") -> "list[Any]":  # noqa: A002 — the NetBox term
    """Every object *endpoint* returns for *filter* (forwarded verbatim), materialised.

    One spelling for every kind's listing: no filter means everything.
    """
    return list(endpoint.filter(**filter) if filter else endpoint.all())


def _named(kind: _Kind, name: object, id_: object) -> str:
    """Name one object for a message: ``device 'd1' (id 1)``."""
    return f"{kind.noun} {name!r} (id {id_})"


def _named_record(kind: _Kind, d: _Payload) -> str:
    """Name the object *d* for a message, from what its payload carries (``'?'`` if not).

    Read from the record's own values, not its attributes: an error message
    must never send a detail request of its own, which could raise from
    inside the handler building it.
    """
    return _named(kind, vars(d.record).get("name", "?"), vars(d.record).get("id", "?"))


def _duplicate(name: str, first: _Seen, second: _Seen) -> str:
    """Name both objects that claim *name*: two of one kind, or one of each."""
    if first.kind is second.kind:
        noun = first.kind.noun
        return f"duplicate {noun} name {name!r} ({noun} ids {first.id} and {second.id})"
    return (
        f"duplicate name {name!r} "
        f"({first.kind.noun} id {first.id} and {second.kind.noun} id {second.id})"
    )


def _netbox_inventory(c: "Configured[NetBoxInventoryConfig, InventoryEnv]") -> _NetBoxInventory:
    """Build the built-in NetBox inventory from its parsed configuration."""
    return _NetBoxInventory(**c.config.model_dump())
