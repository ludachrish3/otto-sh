"""The ``netbox`` inventory backend (spec §9.2) — meets NetBox where it is.

Nothing is required of NetBox beyond a device per referenced host whose
``name`` is the key, an address ``ip_source`` can read, and a read-only token.
``supplies`` is what NetBox models natively plus ONLY the custom fields you
map; every other custom field is ignored, so a deployment's own NetBox schema
cannot leak fields into otto's records by accident.

The whole filtered set is fetched once, on first use, into a dict keyed by
device name (pynetbox paginates), and kept for the object's lifetime; the
snapshot cache (``otto.inventory.cache``, a later task) keeps that to one
round of requests per TTL window. ``pynetbox`` is imported lazily inside the
fetch so no other verb pays for it, and NOTHING — not the token, not a socket
— is touched at construction: a lab with no referenced entry never talks to
NetBox at all.

An argument error is a plain ``ValueError`` (otto's house style for a rejected
argument), which is what
:func:`~otto.inventory.config.construct_inventory` wraps into an
:class:`~otto.inventory.errors.InventoryError` naming the settings file and
the backend. Everything that can only fail later — a missing token, an
unreachable host, a device NetBox describes in a way no record accepts — is
an :class:`~otto.inventory.errors.InventoryError` naming the URL.
"""

import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from typing_extensions import override

from ..models.inventory import FILLABLE_INVENTORY_FIELDS, INVENTORY_KEY_FIELDS, InventoryRecord
from .creds import _compact
from .errors import InventoryError, InventoryKeyError
from .protocol import check_supplies

NATIVE_SUPPLIES: frozenset[str] = frozenset(
    {"ip", "site", "rack", "shelf", "board", "os_name", "is_virtual"}
)
"""What every NetBox device can state without a custom field (spec §9.2's mapping table)."""

_IP_SOURCES = ("primary_ip4", "oob_ip")
_CF_PREFIX = "cf:"

_CUSTOM_FIELD_EXCLUDED: frozenset[str] = frozenset({"ip", "creds", "interfaces"})
"""Record fields a custom field may never fill (spec §9.2, controller ruling R21).

``ip`` has ``ip_source`` — two ways to say the same thing is a way for them to
disagree; ``creds`` come from ``creds_file`` and nowhere else (§9.4); and
``interfaces`` is a structure, not a scalar a NetBox custom field can hold.
"""

_RESERVED_EXTRA_KEYS: frozenset[str] = frozenset({"id", "serial", "asset_tag", "status", "tags"})
"""What this backend itself puts in ``record.extra``; an opt-in name may not shadow one."""

DEFAULT_TIMEOUT_SECONDS = 30.0
"""Seconds one NetBox request may take before it is given up on (spec §9.2).

Generous rather than snappy: a filtered fetch over a large instance is a real
query, and the point of the bound is not speed. It is that an UNREACHABLE host
— the exact case §9.5's stale snapshot exists to cover — otherwise blocks a
lab-bound command for the kernel's TCP connect timeout (~2 minutes on Linux)
BEFORE the snapshot is served, and blocks again on the completion writer's
re-resolution afterwards.
"""


def _checked_timeout(timeout: float) -> float:
    """Return *timeout* as a positive float, refusing anything else.

    A plain ``ValueError``, this module's house style for a rejected argument,
    which :func:`~otto.inventory.config.construct_inventory` wraps naming the
    settings file and the backend.

    ``bool`` is refused explicitly because it is an ``int`` subclass:
    ``timeout = true`` in a TOML table would otherwise quietly mean one
    second.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")
    return float(timeout)


def _checked_verify(verify: "bool | str") -> "bool | str":
    """Return *verify* ready for ``requests``, refusing a relative CA bundle path.

    ``verify`` is the one argument that can name a file, and an ``[inventory]``
    table is COMMITTED: a relative path there resolves against whatever
    directory otto was run from, not the repo the table lives in — the same
    class of bug the settings-path anchoring rule exists for. It is refused
    rather than anchored to ``repo_dir``, because
    :meth:`~otto.inventory.config.CompiledInventory.same_as` deliberately
    ignores ``anchor_dir``: anchoring it would make two repos with identical
    tables silently share whichever repo declared first.
    """
    if not isinstance(verify, str):
        return verify
    expanded = Path(verify).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            f"verify must be an absolute path to a CA bundle (or true/false); got {verify!r} "
            "— a relative path resolves against the process working directory, not the repo"
        )
    return str(expanded)


class NetBoxInventory:
    """Inventory over the devices a NetBox filter selects.

    Parameters
    ----------
    repo_dir : Path | None
        The declaring repo's root, passed by
        :func:`~otto.inventory.config.construct_inventory` under the registry's
        uniform constructor contract. Deliberately unused: this backend
        interprets no path, so nothing here depends on which repo declared the
        table (``CompiledInventory.same_as`` ignores ``anchor_dir``, and a
        backend that anchored a path to it would make two repos with identical
        ``[inventory]`` tables silently share the first one's directory).
    url : str
        Base URL of the NetBox instance, e.g. ``https://netbox.example.com``.
    token_env : str
        Name of the environment variable holding the API token. The token
        itself never sits in a settings file; the variable is read at the first
        fetch, not at construction.
    verify : bool | str
        Passed to ``requests``: ``False`` disables TLS verification, a string
        is a CA bundle path and must be ABSOLUTE (``~`` is expanded) — a
        relative one in a committed settings file would resolve against
        whatever directory otto happened to be run from.
    filter : dict[str, Any] | None
        NetBox device filter, forwarded verbatim (``{"site": "lab-a"}``). No
        filter means every device.
    ip_source : str
        Where a device's management address comes from: ``"primary_ip4"``,
        ``"oob_ip"``, or ``"cf:<custom field>"``.
    custom_fields : dict[str, str] | None
        Record field -> NetBox custom field name. Each mapped field JOINS
        ``supplies``; nothing else about a device's custom fields reaches the
        record.
    extra_custom_fields : list[str] | None
        NetBox custom field names to carry through opaquely in
        ``record.extra``. They are not record fields and never join
        ``supplies``, and none of them may shadow a key this backend already
        puts there.
    timeout : float
        Seconds one HTTP request to NetBox may take, default
        :data:`DEFAULT_TIMEOUT_SECONDS`. Must be a positive number. Applied to
        every request the fetch makes, through an HTTP adapter mounted on
        pynetbox's session — pynetbox issues the calls itself and offers no
        seam to pass a timeout through.

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
    """

    def __init__(
        self,
        repo_dir: "Path | None" = None,  # noqa: ARG002 — the registry's uniform constructor contract
        *,
        url: str,
        token_env: str = "NETBOX_TOKEN",  # noqa: S107 — the NAME of the variable, not a token
        verify: bool | str = True,
        filter: "dict[str, Any] | None" = None,  # noqa: A002 — the NetBox term
        ip_source: str = "primary_ip4",
        custom_fields: "dict[str, str] | None" = None,
        extra_custom_fields: "list[str] | None" = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not url or not isinstance(url, str):
            raise ValueError("'url' must be a non-empty NetBox base URL")
        if ip_source not in _IP_SOURCES and not (
            ip_source.startswith(_CF_PREFIX) and len(ip_source) > len(_CF_PREFIX)
        ):
            raise ValueError(
                f"ip_source must be 'primary_ip4', 'oob_ip', or 'cf:<custom field>', "
                f"got {ip_source!r}"
            )
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
                "'creds' only ever from creds_file, and 'interfaces' is a structure no NetBox "
                "custom field holds"
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
        clash = sorted(set(self.extra_custom_fields) & _RESERVED_EXTRA_KEYS)
        if clash:
            raise ValueError(
                f"extra_custom_fields names {clash[0]!r}, which this backend already puts in "
                f"record.extra from the device itself (reserved: {sorted(_RESERVED_EXTRA_KEYS)})"
            )
        self.verify = _checked_verify(verify)
        self.timeout = _checked_timeout(timeout)
        # Normalised once, here: `label`, every error message and the snapshot
        # cache's slug all read `self.url`, so a trailing slash must not be
        # able to make two spellings of one inventory look like two.
        self.url = url.rstrip("/")
        self.token_env = token_env
        self.filter = dict(filter or {})
        self.ip_source = ip_source
        self.supplies = check_supplies(NATIVE_SUPPLIES | set(self.custom_fields))
        self.label = f"netbox:{self.url}"
        self.unnamed_device_ids: list[int] = []
        self._records: "dict[str, InventoryRecord] | None" = None
        self._no_address: dict[str, int] = {}

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
        return sorted(self._no_address)

    # -- fetch -------------------------------------------------------------
    def _token(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise InventoryError(
                f"netbox inventory {self.url}: environment variable {self.token_env!r} is not set "
                "(the API token never sits in a settings file)"
            )
        return token

    def _mount_timeout(self, session: Any) -> None:
        """Give every request *session* sends this backend's timeout.

        AN ADAPTER, NOT A CALL ARGUMENT, because pynetbox owns the calls: it
        issues ``http_session.get(...)`` with no ``timeout`` and offers no
        seam to pass one through. The adapter is the only place left.

        It REPLACES a ``timeout`` that is already ``None`` rather than setting
        a default, because requests' ``Session.send`` passes ``timeout=None``
        EXPLICITLY when the caller named none — a ``setdefault`` on the
        keyword arguments would never fire, and the bound would silently not
        exist.

        ``requests`` is imported here for the same reason ``pynetbox`` is: it
        is pynetbox's own HTTP client, it arrives with it, and nothing outside
        a fetch should pay to import either.
        """
        from requests.adapters import HTTPAdapter

        default = self.timeout

        class _TimeoutAdapter(HTTPAdapter):
            # The full signature rather than ``**kwargs``: ``HTTPAdapter.send``
            # declares six parameters, and an override that narrowed them would
            # be a Liskov violation the type checker refuses.
            @override
            def send(
                self,
                request: Any,
                stream: bool = False,
                timeout: Any = None,
                verify: "bool | str" = True,
                cert: Any = None,
                proxies: Any = None,
            ) -> Any:
                return super().send(
                    request,
                    stream=stream,
                    timeout=default if timeout is None else timeout,
                    verify=verify,
                    cert=cert,
                    proxies=proxies,
                )

        adapter = _TimeoutAdapter()
        # Both schemes: pynetbox mounts one adapter per scheme, and a redirect
        # from http to https (or a `url` written either way) must not escape
        # the bound.
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    def _fetch(self) -> "dict[str, InventoryRecord]":
        token = self._token()
        import pynetbox  # lazy: no other verb pays for it (spec §16)

        api = None
        try:
            api = pynetbox.api(self.url, token=token)
            api.http_session.verify = self.verify
            # Before any request: an unreachable NetBox must be bounded on the
            # very first one, which is the only one a down instance ever sees.
            self._mount_timeout(api.http_session)
            endpoint = api.dcim.devices
            devices = list(endpoint.filter(**self.filter) if self.filter else endpoint.all())
        # Broad on purpose: pynetbox raises its own RequestError, and requests'
        # connection/TLS/timeout families underneath it. Every one of them means
        # the same thing to a caller — this inventory could not answer.
        except Exception as e:
            raise InventoryError(f"netbox inventory {self.url}: {type(e).__name__}: {e}") from e
        finally:
            # pynetbox builds its own requests.Session and never closes it; the
            # whole set is already materialised by `list()`, so nothing below
            # needs the connection pool.
            if api is not None:
                api.http_session.close()

        records: dict[str, InventoryRecord] = {}
        ids: dict[str, int] = {}
        no_address: dict[str, int] = {}
        unnamed: list[int] = []
        for d in devices:
            # Every read of a device's payload is inside this guard: NetBox's
            # serializer varies by version and by plugin, and a field this
            # backend expects but a given instance omits must come back naming
            # the device and the URL, never as a raw AttributeError (§9.2).
            try:
                self._ingest(d, records, ids, no_address, unnamed)
            # PERF203 is suppressed below because hoisting this out of the loop
            # is exactly what it must not do: the guard exists to name WHICH
            # device failed, and one try around the whole loop could only ever
            # say "some device did".
            except InventoryError:  # noqa: PERF203
                raise  # already names the URL and the device
            except Exception as e:
                raise InventoryError(
                    f"netbox inventory {self.url}: device {getattr(d, 'name', '?')!r} "
                    f"(id {getattr(d, 'id', '?')}): {type(e).__name__}: {e}"
                ) from e
        # Assigned only once the whole set parsed: a fetch that raised halfway
        # must leave no half-built view behind for the retry to read.
        self._no_address = no_address
        self.unnamed_device_ids = unnamed
        return records

    def _ingest(
        self,
        d: Any,
        records: "dict[str, InventoryRecord]",
        ids: dict[str, int],
        no_address: dict[str, int],
        unnamed: list[int],
    ) -> None:
        """Fold one device into the fetch's buckets: keyed, addressless, or unnamed.

        A separate method rather than an inline loop body so the caller's
        ``try`` wraps a CALL: every read of the payload below is guarded, and
        the one deliberate ``raise`` here is not buried inside the handler that
        re-raises it unchanged.
        """
        raw_name = getattr(d, "name", None)
        if raw_name is None or not str(raw_name).strip():
            # NetBox allows an unnamed device and the default filter is
            # "everything". Keyed by name, these would ALL collide on the
            # literal string 'None'.
            unnamed.append(int(d.id))
            return
        name = str(raw_name)
        if name in ids or name in no_address:
            first = ids.get(name, no_address.get(name))
            raise InventoryError(
                f"netbox inventory {self.url}: duplicate device name {name!r} "
                f"(device ids {first} and {d.id})"
            )
        address = self._address(d)
        if address is None:
            no_address[name] = int(d.id)
            return
        records[name] = self._record_for(d, address)
        ids[name] = int(d.id)

    def _address(self, d: Any) -> "str | None":
        """Return the device's address per ``ip_source``, or ``None`` if it has none."""
        if self.ip_source.startswith(_CF_PREFIX):
            value = (d.custom_fields or {}).get(self.ip_source[len(_CF_PREFIX) :])
            return str(value) if value else None
        ip = getattr(d, self.ip_source, None)
        return str(ip.address) if ip is not None and getattr(ip, "address", None) else None

    def _record_for(self, d: Any, address: str) -> InventoryRecord:
        """One device -> one record: §9.2's mapping table, and nothing else."""
        raw: dict[str, Any] = {"ip": address.split("/", 1)[0], "is_virtual": False}
        if d.site is not None:
            raw["site"] = str(d.site.name)
        if d.rack is not None:
            raw["rack"] = str(d.rack.name)
        if d.position is not None:
            # NetBox's `position` is a DRF DecimalField — serialised as a
            # STRING by default, and half-U positions ("3.5") are ordinary.
            # `int("3.5")` raises; the shelf is the integer part.
            raw["shelf"] = int(float(d.position))
        if d.device_type is not None:
            raw["board"] = str(d.device_type.model)
        if d.platform is not None:
            raw["os_name"] = str(d.platform.name)
        cf = dict(d.custom_fields or {})
        for field, cf_name in self.custom_fields.items():
            if cf.get(cf_name) is not None:
                raw[field] = cf[cf_name]
        raw["extra"] = {
            "id": int(d.id),
            "serial": d.serial or None,
            "asset_tag": d.asset_tag or None,
            "status": str(d.status.value) if d.status is not None else None,
            "tags": [str(t.name) for t in (d.tags or [])],
            **{name: cf.get(name) for name in self.extra_custom_fields},
        }
        try:
            return InventoryRecord.model_validate(raw)
        except ValidationError as e:
            raise InventoryError(
                f"netbox inventory {self.url}: device {d.name!r} (id {d.id}): {_compact(e)}"
            ) from e

    # -- protocol ----------------------------------------------------------
    def _load(self) -> "dict[str, InventoryRecord]":
        if self._records is None:
            self._records = self._fetch()
        return self._records

    def lookup(self, key: str) -> InventoryRecord:
        """Return the record for *key*; fetches the whole device set on the first call."""
        records = self._load()
        if key in records:
            return records[key]
        if key in self._no_address:
            raise InventoryError(
                f"netbox inventory {self.url}: device {key!r} (id {self._no_address[key]}) "
                f"has no address at ip_source {self.ip_source!r}"
            )
        raise InventoryKeyError(key, self.label)

    def list_keys(self) -> list[str]:
        """Every device name the filter selects that has an address, sorted."""
        return sorted(self._load())

    def fingerprint(self) -> "str | None":
        """``None`` — not cacheable on its own; the snapshot cache supplies one (spec §9.5)."""
        return None
