"""Pydantic boundary specs for the host record (a ``lab.json`` entry).

``HostSpec`` and its family subclasses validate a host dict and build the
unchanged runtime ``UnixHost`` / ``EmbeddedHost`` via ``to_host()``. The specs
nest the per-protocol ``*OptionsSpec``s from ``otto.models.options`` and reuse
their ``to_runtime()`` builders; embedded registry-name fields
(``filesystem`` / ``command_frame`` / ``loader``) resolve through the existing
host registries at build time.
"""

from ipaddress import ip_address
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field, field_validator, model_validator
from typing_extensions import override

from ..host.binary_loader import build_binary_loader
from ..host.capability import IMPAIRER_RESOLVER, TERM_RESOLVER, TRANSFER_RESOLVER
from ..host.command_frame import FRAME_CLASSES, build_command_frame
from ..host.connections import TERM_BACKENDS
from ..host.embedded_filesystem import FILESYSTEM_CLASSES, build_filesystem
from ..host.embedded_host import EmbeddedHost
from ..host.interface import Interface
from ..host.login_proxy import LOGIN_PROXIES, Cred, LoginProxyError, resolve_chain
from ..host.remote_host import RemoteHost
from ..host.toolchain import Toolchain
from ..host.transfer import TRANSFER_BACKENDS
from ..host.unix_host import UnixHost
from ..link import IMPAIRERS
from ..logger.mode import LogMode
from .base import OttoModel
from .options import (
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SnmpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
)


class ToolchainSpec(OttoModel):
    """Toolchain paths: the ``sysroot`` directory plus the ``lcov`` and ``gcov`` binaries."""

    sysroot: Path = Path("/")
    lcov: Path = Path("usr/bin/lcov")
    gcov: Path = Path("usr/bin/gcov")

    def to_runtime(self) -> Toolchain:
        """Build the runtime ``Toolchain`` dataclass from the validated path fields."""
        return Toolchain(sysroot=self.sysroot, lcov=self.lcov, gcov=self.gcov)


class InterfaceSpec(OttoModel):
    """One ``interfaces`` entry, keyed by the netdev name (``eth0``, …).

    A bare string value (``"eth0": "10.0.0.5"``) is accepted as shorthand for
    ``{"ip": "10.0.0.5"}`` (coerced in ``HostSpec._coerce_interface_shorthand``).
    """

    ip: str

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, v: str) -> str:
        try:
            ip_address(v)
        except ValueError:
            raise ValueError(f"interface address {v!r} is not a valid IP") from None
        return v

    def to_runtime(self) -> Interface:
        """Build the runtime ``Interface`` dataclass."""
        return Interface(ip=self.ip)


# Common fields passed straight through to the host constructor (no conversion).
# Conversions for default_dest_dir/resources/telnet_options/snmp/toolchain are
# applied separately in _common_host_kwargs.
_COMMON_PLAIN_FIELDS = (
    "ip",
    "element",
    "name",
    "os_type",
    "os_name",
    "os_version",
    "user",
    "element_id",
    "board",
    "slot",
    "hop",
    "is_virtual",
    "has_bash",
    "max_filename_len",
    "log",
    "log_stdout",
    "power_control",
)


def _validate_transfer_for_family(v: str, family: str, host_label: str) -> str:
    """Validate a transfer selector against the registry and host-family applicability."""
    if v not in TRANSFER_BACKENDS:
        known = ", ".join(sorted(TRANSFER_BACKENDS.names()))
        raise ValueError(f"transfer {v!r} is not a registered transfer backend. Known: {known}")
    families = TRANSFER_BACKENDS.get(v).host_families
    if family not in families:
        fam = ", ".join(sorted(families))
        raise ValueError(f"transfer {v!r} is not valid on {host_label} (it serves: {fam}).")
    return v


def _validate_term_for_family(v: str, family: str, host_label: str) -> str:
    """Validate a term selector against the registry and host-family applicability."""
    if v not in TERM_BACKENDS:
        known = ", ".join(sorted(TERM_BACKENDS.names()))
        raise ValueError(f"term {v!r} is not a registered term backend. Known: {known}")
    families = TERM_BACKENDS.get(v).host_families
    if family not in families:
        fam = ", ".join(sorted(families))
        raise ValueError(f"term {v!r} is not valid on {host_label} (it serves: {fam}).")
    return v


def _coerce_menu(v: object) -> object:
    """Normalize a scalar menu value to a 1-element list (before-validator)."""
    return [v] if isinstance(v, str) else v


def _validate_term_menu(v: list[str], family: str, label: str) -> list[str]:
    if not v:
        raise ValueError("valid_terms must be a non-empty list of term backends")
    return [_validate_term_for_family(t, family, label) for t in v]


def _validate_transfer_menu(v: list[str], family: str, label: str) -> list[str]:
    if not v:
        raise ValueError("valid_transfers must be a non-empty list of transfer backends")
    return [_validate_transfer_for_family(t, family, label) for t in v]


def _validate_impairer_for_family(v: str, family: str, host_label: str) -> str:
    """Validate an impairer selector against the registry and host-family applicability."""
    if v not in IMPAIRERS:
        known = ", ".join(sorted(IMPAIRERS.names()))
        raise ValueError(f"impairer {v!r} is not a registered impairer. Known: {known}")
    families = IMPAIRERS.get(v).host_families
    if family not in families:
        fam = ", ".join(sorted(families))
        raise ValueError(f"impairer {v!r} is not valid on {host_label} (it serves: {fam}).")
    return v


def _validate_impairer_menu(v: list[str], family: str, label: str) -> list[str]:
    if not v:
        raise ValueError("valid_impairers must be a non-empty list of impairers")
    return [_validate_impairer_for_family(entry, family, label) for entry in v]


class CredSpec(OttoModel):
    """One ``creds`` entry: a login plus (optionally) how to become it."""

    login: str
    password: str | None = None
    proxy: str | None = None
    via: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _proxy_field_rules(self) -> "CredSpec":
        if self.proxy is None and (self.via is not None or self.params):
            raise ValueError(f"cred {self.login!r}: 'via' and 'params' require 'proxy'")
        if self.via is not None and self.via == self.login:
            raise ValueError(f"cred {self.login!r}: 'via' cannot reference itself")
        return self

    def to_cred(self) -> Cred:
        """Build the runtime ``Cred`` dataclass from the validated fields."""
        return Cred(
            login=self.login,
            password=self.password,
            proxy=self.proxy,
            via=self.via,
            params=dict(self.params),
        )


class HostSpec(OttoModel):
    """Abstract boundary spec for a ``lab.json`` host entry.

    Holds the fields common to both host families (identity, credentials, telnet/SNMP
    options, toolchain, power control) and builds the constructor kwargs via
    ``_common_host_kwargs()``. Concrete subclasses (``UnixHostSpec``,
    ``EmbeddedHostSpec``) override ``to_host()`` to produce the appropriate runtime
    class.
    """

    # --- required identity (both families) ---
    ip: str
    element: str

    # --- common optional fields ---
    creds: list[CredSpec] = Field(default_factory=list)
    name: str | None = None
    os_type: str = "unix"
    os_name: str | None = None
    os_version: str | None = None
    user: str | None = None
    element_id: int | None = None
    board: str | None = None
    slot: int | None = None
    hop: str | None = None
    is_virtual: bool = False
    has_bash: bool = True
    default_dest_dir: Path = Path()
    max_filename_len: int = 255
    resources: set[str] = Field(default_factory=set)
    interfaces: dict[str, InterfaceSpec] = Field(default_factory=dict)
    log: LogMode = LogMode.NORMAL
    log_stdout: bool = True  # common: both UnixHost and EmbeddedHost declare it
    telnet_options: TelnetOptionsSpec = TelnetOptionsSpec()
    snmp: SnmpOptionsSpec | None = None
    toolchain: ToolchainSpec = ToolchainSpec()
    command_frame: str | None = None

    # ``power_control`` is lab-infrastructure data (which controller host runs
    # the on/off/status commands, and the commands themselves), so it is a spec
    # field: it takes the lab-data ``[power]`` form — a controller type-name
    # string or a table dict — and the runtime host's __post_init__ coerces it
    # via power_control_from_spec. Passed straight through _common_host_kwargs
    # when set. (``products`` is deliberately NOT a spec field: it is user product
    # data, independent of lab data, attached to hosts by repo logic — the drift
    # guard's _NON_SPEC_RUNTIME_FIELDS excludes it.)
    power_control: dict[str, Any] | str | None = None

    # Lab membership — validated (so a `lab`/`labs` typo errors) but NOT a host
    # constructor argument; the repository uses it to filter hosts into a Lab.
    labs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _strip_comment_keys(cls, data: object) -> object:
        """Drop ``_``-prefixed keys before validation — the JSON comment idiom.

        lab.json cannot carry real comments, so keys like ``_comment`` are
        sanctioned annotation space. Only the leading-underscore form is
        exempt from ``extra='forbid'``; any other unknown key still errors.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}
        return data

    @field_validator("creds", mode="before")
    @classmethod
    def _reject_legacy_creds_dict(cls, v: object) -> object:
        if isinstance(v, dict):
            raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
                "creds is now a list of cred objects: "
                '[{"login": "user", "password": "pw"}, ...] '
                "(was: {user: password}). See the host-database guide."
            )
        return v

    @field_validator("interfaces", mode="before")
    @classmethod
    def _coerce_interface_shorthand(cls, v: object) -> object:
        # "eth0": "10.0.0.5"  ->  "eth0": {"ip": "10.0.0.5"}
        if isinstance(v, dict):
            return {k: ({"ip": e} if isinstance(e, str) else e) for k, e in v.items()}
        return v

    @field_validator("element", "board")
    @classmethod
    def _validate_slugs_nonempty(cls, v: str | None) -> str | None:
        """Reject an ``element``/``board`` that slugs to an empty id.

        They are free human strings but must slug to a non-empty ``[a-z0-9-]``
        token (else they cannot form a valid id).
        """
        if v is None:
            return v
        from ..host.remote_host import slug

        if not slug(v):
            raise ValueError(f"{v!r} slugs to an empty id (needs at least one letter or digit)")
        return v

    @field_validator("element_id", "slot")
    @classmethod
    def _validate_nonnegative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"must be >= 0, got {v}")
        return v

    @field_validator("command_frame")
    @classmethod
    def _validate_command_frame_name(cls, v: str | None) -> str | None:
        if v is not None and v not in FRAME_CLASSES:
            known = ", ".join(sorted(FRAME_CLASSES.names()))
            raise ValueError(f"command_frame {v!r} is not a registered frame. Known: {known}")
        return v

    @field_validator("log", mode="before")
    @classmethod
    def _coerce_log_bool(cls, v: object) -> object:
        # Backward-compat: lab data may still declare log = true/false.
        if isinstance(v, bool):
            return LogMode.QUIET if v is False else LogMode.NORMAL
        return v

    @model_validator(mode="after")
    def _validate_cred_entries(self) -> "HostSpec":
        logins = [c.login for c in self.creds]
        dupes = {n for n in logins if logins.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate cred logins: {sorted(dupes)}")
        by = set(logins)
        for c in self.creds:
            if c.via is not None and c.via not in by:
                raise ValueError(f"cred {c.login!r}: unknown 'via' {c.via!r}")
            if c.proxy is not None and c.proxy not in LOGIN_PROXIES:
                known = ", ".join(sorted(LOGIN_PROXIES.names()))
                raise ValueError(
                    f"cred {c.login!r}: {c.proxy!r} is not a registered login proxy. Known: {known}"
                )
        runtime = [c.to_cred() for c in self.creds]
        for c in runtime:
            if c.proxy is not None:
                try:
                    resolve_chain(runtime, c.login)
                except LoginProxyError as e:
                    raise ValueError(f"cred {c.login!r}: unresolvable via-chain: {e}") from None
        if self.user is not None and self.creds and self.user not in by:
            raise ValueError(f"user {self.user!r} is not a cred login: {sorted(by)}")
        return self

    def _common_host_kwargs(self) -> dict[str, Any]:
        """Build constructor kwargs for the common fields the spec *explicitly set*.

        Mirrors the factory: a field absent from the source dict is omitted so
        the host class's own default applies — including subclass overrides
        (``UnixHost.os_name='Linux'``, ``ZephyrHost.os_name='Zephyr'``). Passing
        every field unconditionally would clobber those defaults with the spec's
        neutral ones. ``labs`` is never a constructor argument.
        """
        s = self.model_fields_set
        kw: dict[str, Any] = {n: getattr(self, n) for n in _COMMON_PLAIN_FIELDS if n in s}
        if "creds" in s:
            kw["creds"] = [c.to_cred() for c in self.creds]
        if "default_dest_dir" in s:
            kw["default_dest_dir"] = Path(self.default_dest_dir)
        if "resources" in s:
            kw["resources"] = set(self.resources)
        if "interfaces" in s:
            kw["interfaces"] = {k: e.to_runtime() for k, e in self.interfaces.items()}
        if "telnet_options" in s:
            kw["telnet_options"] = self.telnet_options.to_runtime()
        if "snmp" in s:
            kw["snmp"] = self.snmp.to_runtime() if self.snmp is not None else None
        if "toolchain" in s:
            kw["toolchain"] = self.toolchain.to_runtime()
        if "command_frame" in s and self.command_frame is not None:
            kw["command_frame"] = build_command_frame(self.command_frame)
        return kw

    def to_host(
        self, cls: Any = None, *, preferences: dict[str, list[str]] | None = None
    ) -> RemoteHost:
        """Build the runtime host this spec describes.

        Overridden by the concrete family specs (:class:`UnixHostSpec`,
        :class:`EmbeddedHostSpec`), each of which knows the runtime class to
        construct. The abstract base carries the contract so the storage
        factory can call ``spec.to_host(cls)`` against a ``HostSpec`` reference.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement to_host(); use a "
            f"concrete host spec (UnixHostSpec / EmbeddedHostSpec)."
        )


class UnixHostSpec(HostSpec):
    """Boundary spec for a Unix host entry in ``lab.json``.

    Extends ``HostSpec`` with the Unix-specific fields: term/transfer/impairer menus and
    active selections, SSH/SFTP/SCP/FTP/nc option tables, Docker capability, and
    hardware/software version strings. ``to_host()`` resolves the active term, transfer, and
    impairer from preferences and builds a ``UnixHost`` (or a custom subclass passed as
    ``cls``).
    """

    creds: list[CredSpec] = Field(min_length=1)  # required for a Unix host (SSH/telnet login)
    hw_version: str | None = None
    sw_version: str | None = None
    valid_terms: list[str] = Field(default_factory=lambda: ["ssh", "telnet"])
    valid_transfers: list[str] = Field(default_factory=lambda: ["scp", "sftp", "ftp", "nc"])
    valid_impairers: list[str] = Field(default_factory=lambda: ["netem"])
    term: str | None = None  # optional active pin; resolved at to_host
    transfer: str | None = None  # optional active pin; resolved at to_host
    impairer: str | None = None  # optional active pin; resolved at to_host
    docker_capable: bool = False
    ssh_options: SshOptionsSpec = SshOptionsSpec()
    sftp_options: SftpOptionsSpec = SftpOptionsSpec()
    scp_options: ScpOptionsSpec = ScpOptionsSpec()
    ftp_options: FtpOptionsSpec = FtpOptionsSpec()
    nc_options: NcOptionsSpec = NcOptionsSpec()

    _host_family: ClassVar[str] = "unix"

    @field_validator("valid_terms", "valid_transfers", "valid_impairers", mode="before")
    @classmethod
    def _coerce_unix_menus(cls, v: object) -> object:
        return _coerce_menu(v)

    @field_validator("valid_terms")
    @classmethod
    def _validate_unix_valid_terms(cls, v: list[str]) -> list[str]:
        return _validate_term_menu(v, cls._host_family, "a unix host")

    @field_validator("valid_transfers")
    @classmethod
    def _validate_unix_valid_transfers(cls, v: list[str]) -> list[str]:
        return _validate_transfer_menu(v, cls._host_family, "a unix host")

    @field_validator("valid_impairers")
    @classmethod
    def _validate_unix_valid_impairers(cls, v: list[str]) -> list[str]:
        return _validate_impairer_menu(v, cls._host_family, "a unix host")

    @override
    def to_host(
        self, cls: type[UnixHost] = UnixHost, *, preferences: dict[str, list[str]] | None = None
    ) -> UnixHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        prefs = preferences or {}
        kw["valid_terms"] = list(self.valid_terms)
        kw["valid_transfers"] = list(self.valid_transfers)
        kw["valid_impairers"] = list(self.valid_impairers)
        # Active selection precedence: the first product preference present in
        # the menu wins; else the lab pin (self.term/.transfer/.impairer, validated
        # against the menu); else the menu's first entry. Out-of-menu preferences
        # are skipped by the resolver.
        kw["term"] = TERM_RESOLVER.resolve_active(
            self.valid_terms, pin=self.term, preference=prefs.get("term")
        )
        kw["transfer"] = TRANSFER_RESOLVER.resolve_active(
            self.valid_transfers, pin=self.transfer, preference=prefs.get("transfer")
        )
        kw["impairer"] = IMPAIRER_RESOLVER.resolve_active(
            self.valid_impairers, pin=self.impairer, preference=prefs.get("impairer")
        )
        for n in ("hw_version", "sw_version", "docker_capable"):
            if n in s:
                kw[n] = getattr(self, n)
        for n in ("ssh_options", "sftp_options", "scp_options", "ftp_options", "nc_options"):
            if n in s:
                kw[n] = getattr(self, n).to_runtime()
        return cls(**kw)


class EmbeddedHostSpec(HostSpec):
    """Boundary spec for an embedded host entry in ``lab.json``.

    Extends ``HostSpec`` with the embedded-family fields: term/transfer menus and active
    selections, filesystem and binary loader registry names. ``to_host()`` resolves the
    active term and transfer, looks up the filesystem and loader from their registries,
    and builds an ``EmbeddedHost`` (or a custom subclass passed as ``cls``).
    """

    os_type: str = "embedded"
    valid_terms: list[str] = Field(default_factory=lambda: ["telnet"])
    valid_transfers: list[str] = Field(default_factory=lambda: ["console"])
    term: str | None = None
    transfer: str | None = None
    filesystem: str | None = None
    loader: str | None = None

    _host_family: ClassVar[str] = "embedded"

    @field_validator("valid_terms", "valid_transfers", mode="before")
    @classmethod
    def _coerce_embedded_menus(cls, v: object) -> object:
        return _coerce_menu(v)

    @field_validator("valid_terms")
    @classmethod
    def _validate_embedded_valid_terms(cls, v: list[str]) -> list[str]:
        return _validate_term_menu(v, cls._host_family, "an embedded host")

    @field_validator("valid_transfers")
    @classmethod
    def _validate_embedded_valid_transfers(cls, v: list[str]) -> list[str]:
        return _validate_transfer_menu(v, cls._host_family, "an embedded host")

    @field_validator("filesystem")
    @classmethod
    def _validate_filesystem_name(cls, v: str | None) -> str | None:
        if v is not None and v not in FILESYSTEM_CLASSES:
            known = ", ".join(sorted(FILESYSTEM_CLASSES.names()))
            raise ValueError(f"filesystem {v!r} is not a registered filesystem. Known: {known}")
        return v

    @override
    def to_host(
        self,
        cls: type[EmbeddedHost] = EmbeddedHost,
        *,
        preferences: dict[str, list[str]] | None = None,
    ) -> EmbeddedHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        prefs = preferences or {}
        kw["valid_terms"] = list(self.valid_terms)
        kw["valid_transfers"] = list(self.valid_transfers)
        # Same precedence as UnixHostSpec.to_host: pin -> product preference
        # present in the menu -> menu[0].
        kw["term"] = TERM_RESOLVER.resolve_active(
            self.valid_terms, pin=self.term, preference=prefs.get("term")
        )
        kw["transfer"] = TRANSFER_RESOLVER.resolve_active(
            self.valid_transfers, pin=self.transfer, preference=prefs.get("transfer")
        )
        if "filesystem" in s and self.filesystem is not None:
            kw["filesystem"] = build_filesystem(self.filesystem)
        if "loader" in s and self.loader is not None:
            kw["loader"] = build_binary_loader(self.loader)
        return cls(**kw)


HOST_SPEC_RUNTIME_PAIRS: list[tuple[type[HostSpec], type]] = [
    (UnixHostSpec, UnixHost),
    (EmbeddedHostSpec, EmbeddedHost),
]
"""Each host spec paired with the runtime class it builds. Drives the drift
guard so a spec field that has no constructor counterpart is caught.

:meta hide-value:
"""
