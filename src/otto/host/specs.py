"""Pydantic boundary specs for the host record (a ``lab.json`` entry).

``HostSpec`` and its family subclasses validate a host dict, validate it in
context (``validate_in``, which issues a readiness record) and build the
runtime host from that record (``to_host``). One stock builder,
``HostSpec.build_kwargs``, walks a spec's fields by their kind
(:mod:`otto.host.construction`) over the record, so every family, and every
custom spec, builds through it with no ``to_host`` of its own. The specs nest
the per-protocol ``*OptionsSpec``s from ``otto.host.option_specs`` and convert
them with their ``to_runtime()`` builders; registry-name fields
(``filesystem`` / ``command_frame`` / ``loader``) resolve through the host
registries at build time.
"""

import dataclasses
import itertools
from collections.abc import Callable, Mapping
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, final

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    StrictFloat,
    StrictInt,
    Tag,
    field_validator,
    model_validator,
)
from typing_extensions import override

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema

from ..link.impairer import IMPAIRERS, _is_unresolved_stock, build_impairer
from ..logger.mode import LogMode
from ..models.base import (
    Cred,
    CredSpec,
    OttoModel,
    nonnegative,
    resources_nonempty,
    strip_comment_keys,
)
from .binary_loader import LOADER_CLASSES, build_binary_loader
from .capability import (
    IMPAIRER_RESOLVER,
    TERM_RESOLVER,
    TRANSFER_RESOLVER,
    CapabilityResolver,
    authenticating_protocols,
    console_transfer_menu,
)
from .command_frame import FRAME_CLASSES, BashFrame, build_command_frame
from .connections import TERM_BACKENDS
from .construction import (
    Consume,
    Convert,
    FlatPreferences,
    HostBuild,
    HostContext,
    HostReadiness,
    HostValidationError,
    Omit,
    Resolve,
    RuntimeOnly,
    _effective_default,
    _issue_readiness,
    _open_record,
    _required_params,
    class_marker,
    field_kind,
)
from .embedded_filesystem import FILESYSTEM_CLASSES, build_filesystem
from .interface import Interface
from .login_proxy import LOGIN_PROXIES, LoginProxyError, resolve_chain, via_cred
from .option_specs import (
    ConsoleOptionsSpec,
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SnmpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
    UserlandOptionsSpec,
)
from .os_profile import constructing_with
from .power import POWER_CONTROLLERS, PowerConstructionError, PowerEnv
from .remote_host import RemoteHost, make_host_id
from .toolchain import Toolchain, ToolchainTool
from .transfer import TRANSFER_BACKENDS


def coerce_digit_string(v: object) -> object:
    """``"3"`` → ``3``; anything else unchanged (spec 2026-08-28 host-inventory §12).

    ``site`` / ``rack`` are ``int | str``: NetBox names racks, some labs number
    them. Under pydantic's smart union ``"3"`` would stay a string while ``3``
    is an int — two spellings of one value, which the inventory cross-checks
    and every equality would then get wrong. ASCII digits only: ``isdigit()``
    is true for fullwidth and other Unicode digits ``int()`` also accepts, but
    a rack labelled that way is a label, not a number.
    """
    if isinstance(v, str) and v.isascii() and v.isdigit():
        return int(v)
    return v


# ``json_schema_input_type`` is stated rather than left at its default, and the
# reason is documentation rather than schema: autodoc renders an ``Annotated``
# alias by expanding it, so ``BeforeValidator``'s dataclass repr lands verbatim
# in every published signature that uses this type — and the default it would
# otherwise show, ``PydanticUndefined``, is a pydantic-internal SENTINEL VALUE
# with no documentable target in any inventory, which a nitpicky ``-W`` build
# then fails on. (``AfterValidator`` has no such field, which is why
# ``models.settings.ProjectPath`` never hit this.) Naming the input type here is
# the same statement pydantic's default makes implicitly — the JSON schema is
# byte-identical in both validation and serialization mode, and validation
# behaviour is unchanged — but it renders as ``int | str``, which resolves.
IntOrStr = Annotated[
    int | str, BeforeValidator(coerce_digit_string, json_schema_input_type=int | str)
]
"""``int | str`` that reads a digit-only string as the int it spells."""


class ToolchainToolSpec(OttoModel):
    """One ``toolchain.tools`` entry: an artifact the toolchain installs on the host.

    ``user`` and ``mode`` default to a root-owned executable, which is what a
    cross-built binary dropped into ``/usr/local/bin`` needs; a library or a
    config file overrides ``mode`` (and sometimes ``user``).
    """

    name: str
    source: Path
    dest: Path
    user: str = "root"
    mode: str = "755"

    def to_runtime(self) -> ToolchainTool:
        """Build the runtime ``ToolchainTool`` dataclass from the validated fields."""
        return ToolchainTool(
            name=self.name,
            source=self.source,
            dest=self.dest,
            user=self.user,
            mode=self.mode,
        )


class ToolchainSpec(OttoModel):
    """Toolchain paths: the ``sysroot`` directory plus the ``lcov`` and ``gcov`` binaries."""

    sysroot: Path = Path("/")
    lcov: Path = Path("usr/bin/lcov")
    gcov: Path = Path("usr/bin/gcov")
    tools: list[ToolchainToolSpec] = Field(default_factory=list)
    lcov_args: list[str] = Field(default_factory=list)
    """Extra arguments for the ``lcov`` capture of this host's coverage data.
    One argument per entry: otto quotes each one, so a whole command line in a
    single entry would reach lcov as one unknown argument, which otto refuses
    at validation."""

    @field_validator("lcov_args")
    @classmethod
    def _validate_lcov_args(cls, v: list[str]) -> list[str]:
        for arg in v:
            if any(c.isspace() for c in arg):
                raise ValueError(
                    f"toolchain.lcov_args: each entry is one argument; {arg!r} "
                    f'contains whitespace — write it as ["--ignore-errors", "source"]'
                )
        return v

    def to_runtime(self) -> Toolchain:
        """Build the runtime ``Toolchain`` dataclass from the validated fields."""
        return Toolchain(
            sysroot=self.sysroot,
            lcov=self.lcov,
            gcov=self.gcov,
            tools=[t.to_runtime() for t in self.tools],
            lcov_args=list(self.lcov_args),
        )


class InterfaceSpec(OttoModel):
    """One ``interfaces`` entry, keyed by the netdev name (``eth0``, …).

    A bare string value (``"eth0": "10.0.0.5"``) is accepted as shorthand for
    ``{"ip": "10.0.0.5"}`` (coerced in ``HostSpec._coerce_interface_shorthand``).
    """

    ip: str

    subnet: str | None = None
    """Optional network this interface belongs to, in CIDR form
    (``"192.168.1.0/24"``). Declares the interface's L3 neighborhood for
    topology and reachability tooling; when set, ``ip`` must fall inside it."""

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, v: str) -> str:
        try:
            ip_address(v)
        except ValueError:
            raise ValueError(f"interface address {v!r} is not a valid IP") from None
        return v

    @field_validator("subnet")
    @classmethod
    def _validate_subnet(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ip_network(v, strict=True)
        except ValueError:
            raise ValueError(
                f"interface subnet {v!r} is not a network in CIDR form "
                f'(e.g. "192.168.1.0/24" — the network address, not a host address)'
            ) from None
        return v

    @model_validator(mode="after")
    def _ip_inside_subnet(self) -> "InterfaceSpec":
        # Cross-family containment (v4 ip in a v6 net) is simply False, so a
        # family mismatch fails with the same "not inside" error.
        if self.subnet is not None and ip_address(self.ip) not in ip_network(self.subnet):
            raise ValueError(
                f"interface address {self.ip!r} is not inside its declared subnet {self.subnet!r}"
            )
        return self

    def to_runtime(self) -> Interface:
        """Build the runtime ``Interface`` dataclass."""
        return Interface(ip=self.ip, subnet=self.subnet)


def _validate_transfer_for_family(v: str, family: str, host_label: str) -> str:
    """Validate a transfer selector against the registry and host-family applicability."""
    if v not in TRANSFER_BACKENDS:
        known = ", ".join(sorted(TRANSFER_BACKENDS.names()))
        raise ValueError(f"transfer {v!r} is not a registered transfer backend. Known: {known}")
    families = TRANSFER_BACKENDS.peek(v).metadata.host_families
    if family not in families:
        fam = ", ".join(sorted(families))
        raise ValueError(f"transfer {v!r} is not valid on {host_label} (it serves: {fam}).")
    return v


def _validate_term_for_family(v: str, family: str, host_label: str) -> str:
    """Validate a term selector against the registry and host-family applicability."""
    if v not in TERM_BACKENDS:
        known = ", ".join(sorted(TERM_BACKENDS.names()))
        raise ValueError(f"term {v!r} is not a registered term backend. Known: {known}")
    families = TERM_BACKENDS.peek(v).metadata.host_families
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


def _validate_impairer_name(v: str) -> str:
    """Validate an impairer selector against the registry (its family is checked in context)."""
    if v not in IMPAIRERS:
        known = ", ".join(sorted(IMPAIRERS.names()))
        raise ValueError(f"impairer {v!r} is not a registered impairer. Known: {known}")
    return v


def _validate_impairer_menu(v: list[str]) -> list[str]:
    if not v:
        raise ValueError("valid_impairers must be a non-empty list of impairers")
    return [_validate_impairer_name(entry) for entry in v]


class PowerControlSpec(OttoModel):
    """A ``power_control`` table: the controller's ``type`` and the keys its config model parses."""

    model_config = ConfigDict(extra="allow")

    type: str
    """The registered power controller's name."""

    @model_validator(mode="before")
    @classmethod
    def _missing_type_on_its_key(cls, data: object) -> object:
        # pydantic reports a missing field with the whole table as its input,
        # secrets included; a None in its place reports on the key alone.
        if isinstance(data, Mapping) and "type" not in data:
            return {**dict(data), "type": None}
        return data

    @field_validator("type", mode="before")
    @classmethod
    def _type_present(cls, v: object) -> object:
        if v is None:
            raise ValueError(
                "a power_control table needs a 'type' naming a registered power controller"
            )
        return v

    @field_validator("type")
    @classmethod
    def _registered(cls, v: str) -> str:
        if v not in POWER_CONTROLLERS:
            known = ", ".join(sorted(POWER_CONTROLLERS.names())) or "<none>"
            raise ValueError(
                f"power_control {v!r} is not a registered power controller. Known: {known}"
            )
        return v


def _power_form(value: object) -> str:
    """Tag a ``power_control`` value: a table, else a name."""
    return "table" if isinstance(value, (Mapping, PowerControlSpec)) else "name"


@final
class _OneFormByShape:
    """Validate ``power_control`` as a table or a name, chosen by the value's shape.

    Validating one branch only keeps a bad table's errors on the table, never
    adding the name branch's error, whose input is the whole table. Its repr
    is a plain phrase, so the field's rendered annotation names no private
    function.
    """

    __slots__ = ()

    def __get_pydantic_core_schema__(
        self, source: object, handler: "GetCoreSchemaHandler"
    ) -> "CoreSchema":
        del source  # the annotated type is restated below, tagged
        return handler(
            Annotated[
                Annotated[PowerControlSpec, Tag("table")] | Annotated[str, Tag("name")],
                Discriminator(_power_form),
            ]
            | None
        )

    @override
    def __repr__(self) -> str:
        return "'one form, by shape'"


def _value_only(fn: "Callable[[Any], object]") -> "Callable[[Any, Any], object]":
    """Adapt a conversion of the value alone to Convert's ``fn(value, build)``."""

    def convert(value: Any, build: Any) -> object:  # noqa: ARG001 — Convert's signature
        return fn(value)

    return convert


def _to_creds(value: "list[CredSpec]") -> list[Cred]:
    return [c.to_cred() for c in value]


def _to_path(value: object) -> Path:
    return Path(value)  # ty: ignore[invalid-argument-type]


def _to_dict(value: "dict[str, Any]") -> "dict[str, Any]":
    return dict(value)  # copied: the host never shares a container with its spec


def _to_frozenset(value: "set[str]") -> frozenset[str]:
    return frozenset(value)


def _to_list(value: "list[str]") -> list[str]:
    return list(value)


def _to_interfaces(value: "dict[str, InterfaceSpec]") -> "dict[str, Interface]":
    return {k: e.to_runtime() for k, e in value.items()}


def _to_runtime(value: Any) -> object:
    return value.to_runtime()


def _to_snmp(value: "SnmpOptionsSpec | None") -> object:
    return value.to_runtime() if value is not None else None


def _to_command_frame(value: str | None) -> object:
    return Omit if value is None else build_command_frame(value)


def _to_filesystem(value: str | None) -> object:
    return Omit if value is None else build_filesystem(value)


def _to_loader(value: str | None) -> object:
    return Omit if value is None else build_binary_loader(value)


def _build_power(value: object, build: HostBuild) -> object:
    """Build the controller from the record's parse; ``None`` stays ``None``."""
    if value is None or build.power is None:
        return None
    return POWER_CONTROLLERS.build(build.power)


def _menu(name: str) -> "Callable[[HostBuild], object]":
    """Resolve a menu: the entry's list (its default when unset), copied."""

    def resolve(build: HostBuild) -> object:
        return list(build.inputs[name])  # ty: ignore[invalid-argument-type] — a validated menu

    return resolve


def _selection(name: str) -> "Callable[[HostBuild], object]":
    """Resolve an active selection: the one validation chose."""

    def resolve(build: HostBuild) -> object:
        return build.selections[name]

    return resolve


@dataclasses.dataclass(frozen=True)
class _Selected:
    """What the selection checks settled: the selections, or the no-preference witness."""

    selections: "dict[str, str] | None"
    witness: "dict[str, str] | None"


def _message(exc: Exception) -> str | None:
    """Render a ValidationError without its input (its ``str()`` quotes it); else ``None``."""
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    return compact_validation_error(exc) if isinstance(exc, ValidationError) else None


def _describe(exc: Exception) -> str:
    """Render *exc* for a message without quoting its input (a ValidationError's input may)."""
    message = _message(exc)
    return message if message is not None else f"{type(exc).__name__}: {exc}"


def _class_supplies_frame(cls: object) -> bool:
    """Return whether host class *cls* defaults ``command_frame``, to a value or a factory."""
    declared = getattr(cls, "__dataclass_fields__", {}).get("command_frame")
    if declared is None:
        return False
    if declared.default_factory is not dataclasses.MISSING:
        return True
    return declared.default is not dataclasses.MISSING and declared.default is not None


def _check_pins_in_menus(
    spec: "HostSpec", pins: "list[tuple[CapabilityResolver, str, str]]"
) -> None:
    """Refuse a pin outside its menu, per (resolver, pin field, menu field)."""
    for resolver, pin_field, menu_field in pins:
        pin = getattr(spec, pin_field)
        if pin is not None:
            resolver.validate_choice(getattr(spec, menu_field), pin)


def name_slugs_nonempty(v: str) -> str:
    """Refuse a name that slugs to an empty ``[a-z0-9-]`` token: it cannot form an id.

    The host board (:class:`HostSpec`) and the element name
    (:class:`~otto.models.lab.ElementSpec`) are held to the one rule here;
    the runtime :class:`~otto.host.element.Element` restates it with the
    element named. Lives in the host package, beside the slug itself, because
    ``otto.models.base`` imports nothing from otto.
    """
    from .remote_host import slug

    if not slug(v):
        raise ValueError(f"{v!r} slugs to an empty id (needs at least one letter or digit)")
    return v


class HostSpec(OttoModel):
    """Abstract boundary spec for a ``lab.json`` host entry.

    Holds the fields common to both host families (addressing, credentials,
    telnet/SNMP options, toolchain, power control). :meth:`validate_in` checks
    the spec in its :class:`~otto.host.HostContext` and issues the
    :class:`~otto.host.HostReadiness` record that :meth:`to_host` consumes:
    the stock builder, :meth:`build_kwargs`, walks the spec's fields by kind
    over that record and :meth:`to_host` constructs the class it names, so a
    subclass's own fields reach the constructor with no override.

    The element is not a host field: it reaches the host through the context,
    from the factory's ``element=`` argument (spec 2026-09-05 §2.6).
    """

    # --- required addressing (both families) ---
    ip: str

    # --- common optional fields ---
    creds: Annotated[list[CredSpec], Convert(_value_only(_to_creds))] = Field(default_factory=list)
    name: str | None = None
    os_type: str = "unix"
    os_name: str | None = None
    os_version: str | None = None

    hw_version: str | None = None
    """Free-form hardware version. Informational — otto never parses it."""

    sw_version: str | None = None
    """Free-form software version — what the device is DECLARED to run, never
    what a probe observed.

    On the base rather than ``UnixHostSpec`` since spec 2026-08-28
    host-inventory §4 — an embedded target has a firmware version as surely as a
    Unix box has a distro one. Like ``os_version`` above, this is otto's own
    vocabulary rather than a fact an inventory tool natively holds: a deployment
    may still hand it over by mapping a custom field, but no backend supplies it
    by default (``otto.inventory.netbox.NATIVE_SUPPLIES``)."""

    board: str | None = None
    slot: int | None = None
    site: IntOrStr | None = None
    rack: IntOrStr | None = None
    shelf: int | None = None
    hop: str | None = None

    inventory: Annotated[
        str | None, Consume("the loader resolves the entry from this inventory key")
    ] = None
    """Inventory key this entry is resolved from (spec 2026-08-28 host-inventory §5).
    Spec-only: the loader consumes it and never forwards it to the host."""

    is_virtual: bool = False
    has_bash: bool = True
    default_dest_dir: Annotated[Path, Convert(_value_only(_to_path))] = Path()
    max_filename_len: int = 255
    debug_log_globs: Annotated[list[str], Convert(_value_only(_to_list))] = Field(
        default_factory=list
    )

    metadata: Annotated[dict[str, Any], Convert(_value_only(_to_dict))] = Field(
        default_factory=dict
    )
    """Opaque user data (spec §4). A JSON object; otto never reads it. The
    sanctioned home for custom fields, so ``extra='forbid'`` never has to
    give way: a key here can never collide with a field otto adds later."""

    resources: Annotated[set[str], Convert(_value_only(_to_frozenset))] = Field(default_factory=set)
    """This host's own reservation identifiers — a slot (spec 2026-08-28
    three-level-reservations §2).

    A host field again, after the v2 break made the lab the only reservable
    unit. The element's and the lab's are carried separately (the runtime's
    ``element.resources`` and ``lab_info.resources``), so an entry never
    restates them."""

    interfaces: Annotated[dict[str, InterfaceSpec], Convert(_value_only(_to_interfaces))] = Field(
        default_factory=dict
    )
    log: LogMode = LogMode.NORMAL
    telnet_options: Annotated[TelnetOptionsSpec, Convert(_value_only(_to_runtime))] = (
        TelnetOptionsSpec()
    )
    console_options: Annotated[ConsoleOptionsSpec, Convert(_value_only(_to_runtime))] = (
        ConsoleOptionsSpec()
    )
    """The ``console`` term's address and login behaviour: the lab host that
    runs the telnet server, the port, the dial mode and the prompt
    patterns. See :class:`~otto.host.option_specs.ConsoleOptionsSpec`."""

    snmp: Annotated[SnmpOptionsSpec | None, Convert(_value_only(_to_snmp))] = None
    toolchain: Annotated[ToolchainSpec, Convert(_value_only(_to_runtime))] = ToolchainSpec()
    command_frame: Annotated[str | None, Convert(_value_only(_to_command_frame))] = None

    session_setup: dict[str, Any] | str | None = None
    """A registered session-setup hook, by name or as a table whose ``type``
    key is the name and whose other keys are the hook's params (the
    ``power_control`` idiom). Runs once on every shell session, after the
    handshake and every login-proxy hop. See ``otto.host.session_setup``."""

    landing_frame: str | None = None
    """Dialect of the shell otto LANDS in, when it is not the shell
    ``command_frame`` describes; only valid alongside ``session_setup``,
    which is what manoeuvres from the one to the other. ``"raw"`` for a
    landing that answers no frame at all."""

    # ``products`` is deliberately NOT a spec field: it is user product data,
    # independent of lab data, attached to hosts by project logic — the host class
    # marks it RuntimeOnly, see otto.host.construction.
    power_control: Annotated[
        PowerControlSpec | str | None, _OneFormByShape(), Convert(_build_power)
    ] = None
    """The host's power controller, as a registered controller name or a table naming it in
    ``type`` beside the keys its config model parses. Parsed when the host is validated."""

    @model_validator(mode="before")
    @classmethod
    def _strip_comment_keys(cls, data: object) -> object:
        """Drop ``_``-prefixed keys; see :func:`~otto.models.base.strip_comment_keys`."""
        return strip_comment_keys(data)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_user_pin(cls, data: object) -> object:
        """Refuse a ``user`` key, naming the fix (spec 2026-09-13 cred-scope §5.1).

        ``user`` chose the default login back when ``creds`` was a
        ``{login: password}`` map; with an ordered list a protocol logs in as
        the first entry that applies to it, so the pin has nothing left to say.
        Caught here rather than left to ``extra='forbid'`` so an old lab file
        fails with the migration instead of a bare unknown-field error. The
        message never echoes the value; pydantic's own ``input_value`` repr
        still can, so boundary readers render through
        ``compact_validation_error`` (see otto.models.base).
        """
        if isinstance(data, dict) and "user" in data:
            raise ValueError(
                "user was removed: order creds instead; a protocol logs in as "
                "the first entry that applies to it. See the host sources guide."
            )
        return data

    @field_validator("creds", mode="before")
    @classmethod
    def _reject_legacy_creds_dict(cls, v: object) -> object:
        if isinstance(v, dict):
            raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
                "creds is now a list of cred objects: "
                '[{"login": "user", "password": "pw"}, ...] '
                "(was: {user: password}). See the host sources guide."
            )
        return v

    @field_validator("interfaces", mode="before")
    @classmethod
    def _coerce_interface_shorthand(cls, v: object) -> object:
        # "eth0": "10.0.0.5"  ->  "eth0": {"ip": "10.0.0.5"}
        if isinstance(v, dict):
            return {k: ({"ip": e} if isinstance(e, str) else e) for k, e in v.items()}
        return v

    @field_validator("board")
    @classmethod
    def _validate_slugs_nonempty(cls, v: str | None) -> str | None:
        """Reject a ``board`` that slugs to an empty id.

        It is a free human string but must slug to a non-empty ``[a-z0-9-]``
        token (else it cannot form a valid id). The element name is held to the
        same rule by :class:`~otto.host.element.Element`.
        """
        return None if v is None else name_slugs_nonempty(v)

    @field_validator("slot", "shelf")
    @classmethod
    def _validate_nonnegative(cls, v: int | None) -> int | None:
        return nonnegative(v)

    @field_validator("resources")
    @classmethod
    def _resources(cls, v: set[str]) -> set[str]:
        return resources_nonempty(v)

    @field_validator("inventory")
    @classmethod
    def _inventory_key_nonempty(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("'inventory' must name a key (a non-empty string)")
        return v

    @field_validator("command_frame")
    @classmethod
    def _validate_command_frame_name(cls, v: str | None) -> str | None:
        if v is not None and v not in FRAME_CLASSES:
            known = ", ".join(sorted(FRAME_CLASSES.names()))
            raise ValueError(f"command_frame {v!r} is not a registered frame. Known: {known}")
        if v == "raw":
            raise ValueError(
                "command_frame 'raw' is refused: raw is a landing dialect, never a destination "
                "— declare it as landing_frame with a session_setup hook"
            )
        return v

    @field_validator("landing_frame")
    @classmethod
    def _validate_landing_frame_name(cls, v: str | None) -> str | None:
        if v is not None and v not in FRAME_CLASSES:
            known = ", ".join(sorted(FRAME_CLASSES.names()))
            raise ValueError(f"landing_frame {v!r} is not a registered frame. Known: {known}")
        return v

    @field_validator("session_setup")
    @classmethod
    def _validate_session_setup_name(
        cls, v: dict[str, Any] | str | None
    ) -> dict[str, Any] | str | None:
        if v is None:
            return v
        from .session_setup import SESSION_SETUPS  # off the startup graph on purpose

        name = v if isinstance(v, str) else v.get("type")
        if not isinstance(name, str):
            raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
                "session_setup table must carry a string 'type' naming a registered session setup"
            )
        if name not in SESSION_SETUPS:
            known = ", ".join(sorted(SESSION_SETUPS.names())) or "<none>"
            raise ValueError(
                f"session_setup {name!r} is not a registered session setup. Known: {known}"
            )
        return v

    @field_validator("power_control")
    @classmethod
    def _validate_power_type(
        cls, v: "PowerControlSpec | str | None"
    ) -> "PowerControlSpec | str | None":
        # A table checked its own ``type`` (PowerControlSpec); a name is checked here.
        if isinstance(v, str) and v not in POWER_CONTROLLERS:
            known = ", ".join(sorted(POWER_CONTROLLERS.names())) or "<none>"
            raise ValueError(
                f"power_control {v!r} is not a registered power controller. Known: {known}"
            )
        return v

    @model_validator(mode="after")
    def _validate_cred_entries(self) -> "HostSpec":
        # The vocabulary walks two backend registries, and nothing below needs
        # it when no entry is scoped -- which is every lab written before scope
        # existed, so the common path does not pay for the question.
        vocabulary = authenticating_protocols() if any(c.protocols for c in self.creds) else []
        for c in self.creds:
            for p in c.protocols:
                if p not in vocabulary:
                    raise ValueError(
                        f"cred {c.login!r}: {p!r} is not an authenticating protocol; "
                        f"a cred may be scoped to: {', '.join(vocabulary)}"
                    )
        identities = [c.identity for c in self.creds]
        dupes = {n for n in identities if identities.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate cred entries: {sorted(dupes)}")
        runtime = [c.to_cred() for c in self.creds]
        for c in self.creds:
            if c.via is not None and via_cred(self.creds, c) is None:
                raise ValueError(f"cred {c.login!r}: unknown 'via' {c.via!r}")
            if c.proxy is not None and c.proxy not in LOGIN_PROXIES:
                known = ", ".join(sorted(LOGIN_PROXIES.names()))
                raise ValueError(
                    f"cred {c.login!r}: {c.proxy!r} is not a registered login proxy. Known: {known}"
                )
        for c in runtime:
            if c.proxy is not None:
                # One try per cred, around the whole per-protocol walk rather
                # than inside it: the first protocol that cannot resolve is
                # the error, and `protocol` still names it in the handler.
                protocol = None
                try:
                    for protocol in self._pick_protocols(c, vocabulary):
                        resolve_chain(runtime, c.login, protocol)
                except LoginProxyError as e:
                    over = "" if protocol is None else f" over {protocol!r}"
                    raise ValueError(
                        f"cred {c.login!r}: unresolvable via-chain{over}: {e}"
                    ) from None
        return self

    def _pick_protocols(self, cred: Cred, vocabulary: list[str]) -> list[str | None]:
        """Protocols *cred* can be the pick for — its chain must resolve under each.

        A scoped cred is only ever picked for the protocols it names. An
        unscoped one is a candidate for every self-authenticating protocol this
        host can actually use (its term menu and transfer menu, intersected
        with the vocabulary), because ``_default_direct`` answers differently
        per protocol: a chain that terminates at an ftp-scoped cred terminates
        for ftp and for nothing else.

        ``[None]`` when no entry is scoped at all — with no scoped creds the
        per-protocol lookup and the protocol-less one are the same walk, so
        asking once is asking everything, and today's check is preserved
        verbatim for every unscoped lab.
        """
        if cred.protocols:
            return list(cred.protocols)
        if not vocabulary:
            return [None]
        # Pins are in their menus (checked structurally), so the menus hold them.
        menu = {*getattr(self, "valid_terms", []), *getattr(self, "valid_transfers", [])}
        surface = sorted(menu & set(vocabulary))
        return list(surface) or [None]

    @model_validator(mode="after")
    def _validate_landing_rules(self) -> "HostSpec":
        if self.landing_frame is None:
            return self
        if self.session_setup is None:
            raise ValueError(
                f"landing_frame {self.landing_frame!r} requires session_setup: a landing "
                f"dialect with nothing to transition out of it is a contradiction"
            )
        landing = build_command_frame(self.landing_frame)  # frames are stateless values
        if any(c.proxy is not None for c in self.creds) and not isinstance(landing, BashFrame):
            raise ValueError(
                f"landing_frame {self.landing_frame!r} with a proxied cred: login-proxy hops "
                f"run in the landing shell and their identity probe is bash, so the landing "
                f"dialect must be bash-family"
            )
        return self

    @final
    def validate_in(self, ctx: HostContext) -> HostReadiness:
        """Validate this spec in *ctx* and issue the record :meth:`to_host` consumes.

        Runs the context checks, the stock contextual checks, :meth:`check_in`,
        then prepares what construction needs: the ``power_control``
        configuration, parsed by its controller's config model. Raises
        :class:`~otto.host.HostValidationError` naming the host id.
        """
        if ctx.entry.spec is not type(self):
            name = getattr(ctx.entry.spec, "__name__", repr(ctx.entry.spec))
            raise HostValidationError(
                f"host {ctx.host_id!r}: the context was resolved for {name}, not "
                f"{type(self).__name__}; build the context with otto.host.host_context"
            )
        expected = make_host_id(ctx.element.name, self.board, self.slot)
        if ctx.host_id != expected:
            raise HostValidationError(
                f"host {expected!r}: the context names host {ctx.host_id!r}; build the "
                f"context with otto.host.host_context"
            )
        try:
            selected = self._contextual_checks(ctx)
        except HostValidationError:
            raise
        except ValueError as exc:
            message = _message(exc)
            raise HostValidationError(
                f"host {ctx.host_id!r}: {exc if message is None else message}"
            ) from exc
        try:
            self.check_in(ctx)
        except HostValidationError:
            raise
        except Exception as exc:
            raise HostValidationError(
                f"host {ctx.host_id!r}: check_in failed: {_describe(exc)}"
            ) from exc
        power = None
        value = self.power_control
        if value is not None:
            if isinstance(value, str):
                name, raw = value, {}
            else:
                name, raw = value.type, dict(value.model_extra or {})
            try:
                power = POWER_CONTROLLERS.prepare(
                    name,
                    raw,
                    PowerEnv(host_id=ctx.host_id),
                    source=f"power_control of host {ctx.host_id!r}",
                )
            except PowerConstructionError as exc:
                raise HostValidationError(f"host {ctx.host_id!r}: {exc}") from exc
        return _issue_readiness(
            owner=self,
            context=ctx,
            values={name: getattr(self, name) for name in type(self).model_fields},
            fields_set=frozenset(self.model_fields_set),
            selections=selected.selections,
            witness=selected.witness,
            power=power,
        )

    def check_in(self, ctx: HostContext) -> None:
        """Run a custom spec's contextual checks; the stock spec checks nothing here.

        Called once by :meth:`validate_in`, after the stock checks and before
        preparation. Raise any exception to refuse the entry; it reaches the
        caller as a :class:`~otto.host.HostValidationError` naming the host.
        """

    def _resolve_selections(self, preferences: FlatPreferences) -> dict[str, str]:
        """Return the selections construction uses under *preferences*.

        The base spec has no menus, so none; a family with menus overrides it.
        """
        del preferences  # the base spec has no menus to select from
        return {}

    def _label(self) -> str:
        return self.name or self.ip

    def _selection_menus(self) -> dict[str, list[str]]:
        """Return the menus a preference may choose from, in selection order.

        The base spec has none; a family with menus overrides it.
        """
        return {}

    def _check_selection(self, selection: "Mapping[str, str]") -> None:
        """Refuse a resolved *selection* the cred term-candidate check or the console rule fails.

        The cred check (spec 2026-09-13 cred-scope §3.2): a protocol's
        candidates are the creds scoped to it plus the unscoped ones, so a
        term every cred is scoped away from would log in as nobody. Skipped
        when ``creds`` is empty (a loginless host is loginless on purpose)
        and when the selection has no term.

        The console rule: a console term needs ``console_options`` to say
        where the console is. Only the selected term is checked, so
        ``console_options`` on a host that runs ssh is inert data. Server
        existence and the server's ssh cred are lab-wide facts, resolved at
        connect time the way ``hop`` is.
        """
        term = selection.get("term")
        if term is None:
            return
        if self.creds and not any(not c.protocols or term in c.protocols for c in self.creds):
            identities = ", ".join(c.identity for c in self.creds)
            raise ValueError(
                f"no cred can log in over term {term!r}: every cred is scoped elsewhere "
                f"({identities})"
            )
        if term == "console":
            opts = self.console_options
            if not opts.server:
                raise ValueError(
                    "term 'console' requires console_options.server: the lab host ID of the "
                    "telnet server fronting this console"
                )
            if not 1 <= opts.port <= 65535:  # noqa: PLR2004 — port range, not a magic constant
                raise ValueError(
                    f"term 'console' requires console_options.port in 1..65535, got {opts.port}"
                )

    def _static_contextual_checks(self, ctx: HostContext) -> None:
        """Run the checks that read the class or the id but no selection; a family extends it."""

    def _contextual_checks(self, ctx: HostContext) -> _Selected:
        self._static_contextual_checks(ctx)
        if ctx.preferences is not None:
            selections = self._resolve_selections(ctx.preferences)
            self._check_selection(selections)
            return _Selected(selections=selections, witness=None)
        menus = self._selection_menus()
        for combo in itertools.product(*menus.values()):
            # A witness is what the concrete resolver produces for some preferences:
            # one-item preferences reach every menu entry, and pins and the console
            # narrowing still apply, exactly as at load.
            preference = {name: [choice] for name, choice in zip(menus, combo, strict=True)}
            try:
                candidate = self._resolve_selections(preference)
                self._check_selection(candidate)
            except ValueError:
                continue
            return _Selected(selections=None, witness=candidate)
        # Nothing passes: report what a load without preferences would say. That
        # pick is one of the combinations above, so it fails its check too.
        self._check_selection(self._resolve_selections({}))
        raise AssertionError(  # pragma: no cover - the invariant above
            "the empty-preference pick is one of the enumerated combinations"
        )

    @final
    def build_kwargs(self, ready: HostReadiness) -> dict[str, object]:
        """Return the constructor keyword arguments *ready* builds, consuming the record.

        Walks this spec's fields by kind over the record's :class:`~otto.host.HostBuild`
        and adds the element to the class field marked ``RuntimeOnly(supplied_by="element")``.
        Refuses a record from another spec, a no-preference record and a consumed one.
        A field the entry left unset is not passed, so the host class's own default
        applies; a ``Resolve`` field is always passed; a ``Consume`` field never is.

        Raises:
            HostValidationError: The record cannot build, or a ``Convert`` returned
                :data:`~otto.host.Omit` for a parameter the constructor requires.
        """
        build = _open_record(ready, self)
        fields_set = ready._payload.fields_set  # noqa: SLF001 — the record's issuer reads its payload
        required = set(_required_params(ready.cls))
        kw: dict[str, object] = {}
        for name in type(self).model_fields:
            kind = field_kind(type(self), name)
            if isinstance(kind, Consume):
                continue
            if isinstance(kind, Resolve):
                kw[name] = kind.fn(build)
                continue
            if name not in fields_set:
                continue
            value = build.inputs[name]
            if isinstance(kind, Convert):
                value = kind.fn(value, build)
            if value is Omit:
                if name in required:
                    raise HostValidationError(
                        f"host {ready.host_id!r}: field {name!r} is required by "
                        f"{ready.cls.__name__} but its conversion returned Omit"
                    )
                continue
            kw[name] = value
        for f in dataclasses.fields(ready.cls):
            marker = class_marker(ready.cls, f.name)
            if f.init and isinstance(marker, RuntimeOnly) and marker.supplied_by == "element":
                kw[f.name] = build.context.element
        return kw

    def to_host(self, *, ready: HostReadiness) -> RemoteHost:
        """Build the runtime host *ready* describes: ``ready.cls(**self.build_kwargs(ready))``.

        *ready* is the record :meth:`validate_in` issued for this spec; the class
        comes from it. Override for construction that is not keyword arguments,
        or to act on the built host before it leaves the factory; build through
        :meth:`build_kwargs` either way.
        """
        kw: dict[str, Any] = self.build_kwargs(ready)
        payload = ready._payload  # noqa: SLF001 — the record's issuer reads its payload
        # Scoped here, not only by the factory: a host built through
        # otto.host.host_context keeps its data profile's console prompts too.
        with constructing_with(str(payload.values["os_type"]), payload.context.profile.fields):
            return ready.cls(**kw)


_STOCK_UNIX_IMPAIRERS: list[str] = ["netem"]
"""otto's default Unix impairer menu; while it is otto's own registration, context skips it."""


class UnixHostSpec(HostSpec):
    """Boundary spec for a Unix host entry in ``lab.json``.

    Extends ``HostSpec`` with the Unix-specific fields: term/transfer/impairer menus and
    active selections, SSH/SFTP/SCP/FTP/nc option tables, and Docker
    capability. Contextual validation resolves the active term, transfer and
    impairer from preferences; the stock ``to_host()`` builds the ``UnixHost`` (or
    the registered subclass) the readiness record names.
    """

    # optional: a loginless host needs none, and an inventory record or creds store may hold none
    creds: list[CredSpec] = Field(default_factory=list)
    valid_terms: Annotated[list[str], Resolve(_menu("valid_terms"))] = Field(
        default_factory=lambda: ["ssh", "telnet"]
    )
    valid_transfers: Annotated[list[str], Resolve(_menu("valid_transfers"))] = Field(
        default_factory=lambda: ["scp", "sftp", "ftp", "nc"]
    )
    valid_impairers: Annotated[list[str], Resolve(_menu("valid_impairers"))] = Field(
        default_factory=lambda: list(_STOCK_UNIX_IMPAIRERS)
    )
    # optional active pins; resolved at to_host
    term: Annotated[str | None, Resolve(_selection("term"))] = None
    transfer: Annotated[str | None, Resolve(_selection("transfer"))] = None
    impairer: Annotated[str | None, Resolve(_selection("impairer"))] = None
    docker_capable: bool = False
    shell_history: bool = False
    init_timeout: StrictFloat | None = Field(default=None, gt=0, allow_inf_nan=False)
    """Seconds a newly opened shell has to answer otto's readiness probe;
    ``None`` keeps the session's own ceiling. See ``UnixHost.init_timeout``."""
    docker_priority: StrictInt = 0
    """Which docker-capable host a use-case deploys on when ``--parent`` is
    omitted and the lab selection has several. Highest wins; a tie at the top
    refuses. Strictly an integer (JSON ``true`` is not a rank of 1), and only
    on a docker-capable host (``docker_capable`` set, or the host class's own
    default; checked in context). Intent about how THIS LAB uses the host,
    never an inventory fact."""
    ssh_options: Annotated[SshOptionsSpec, Convert(_value_only(_to_runtime))] = SshOptionsSpec()
    sftp_options: Annotated[SftpOptionsSpec, Convert(_value_only(_to_runtime))] = SftpOptionsSpec()
    scp_options: Annotated[ScpOptionsSpec, Convert(_value_only(_to_runtime))] = ScpOptionsSpec()
    ftp_options: Annotated[FtpOptionsSpec, Convert(_value_only(_to_runtime))] = FtpOptionsSpec()
    nc_options: Annotated[NcOptionsSpec, Convert(_value_only(_to_runtime))] = NcOptionsSpec()
    # Spec: docs/superpowers/specs/2026-08-11-busybox-host-support-design.md
    userland_options: Annotated[UserlandOptionsSpec, Convert(_value_only(_to_runtime))] = (
        UserlandOptionsSpec()
    )
    """Declared answers about this host's userland; every key defaults to
    "probe it". Unlike its siblings this table is not per-protocol — it
    describes the DEVICE (which elevation mechanism exists, which ``timeout``
    convention its applet speaks), so one table serves every backend. See the
    BusyBox host-support spec."""

    _host_family: ClassVar[str] = "unix"

    @model_validator(mode="before")
    @classmethod
    def _roles_is_gone(cls, data: Any) -> Any:
        if isinstance(data, dict) and "roles" in data:
            raise ValueError(
                "roles is gone; see docker_priority "
                "(rank one docker-capable host as the default parent)"
            )
        return data

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
        return _validate_impairer_menu(v)

    @model_validator(mode="after")
    def _pins_in_menus(self) -> "UnixHostSpec":
        _check_pins_in_menus(
            self,
            [
                (TERM_RESOLVER, "term", "valid_terms"),
                (TRANSFER_RESOLVER, "transfer", "valid_transfers"),
                (IMPAIRER_RESOLVER, "impairer", "valid_impairers"),
            ],
        )
        return self

    @override
    def _selection_menus(self) -> dict[str, list[str]]:
        return {"term": list(self.valid_terms), "transfer": list(self.valid_transfers)}

    @override
    def _static_contextual_checks(self, ctx: HostContext) -> None:
        # Every menu is checked but otto's stock default, left unset, while its
        # names are still otto's own unimported registrations: their family is
        # otto's own fact, and resolving them would import their backend on
        # every load. A replaced registration, or a custom spec's own default,
        # is checked like any entry's menu.
        stock = (
            "valid_impairers" not in self.model_fields_set
            and list(self.valid_impairers) == _STOCK_UNIX_IMPAIRERS
            and all(_is_unresolved_stock(name) for name in _STOCK_UNIX_IMPAIRERS)
        )
        menu = [] if stock else self.valid_impairers
        for name in menu:
            families = build_impairer(name).host_families
            if self._host_family not in families:
                raise ValueError(
                    f"impairer {name!r} is not valid on a unix host "
                    f"(it serves: {', '.join(sorted(families))})."
                )
        # The key is the mistake, whatever its value: ranking a host that can
        # never be a parent is a lab.json error worth naming at load.
        if "docker_priority" in self.model_fields_set and not _effective_default(
            self, ctx.entry.cls, "docker_capable"
        ):
            raise ValueError(
                f"docker_priority on {self._label()!r}, which is not docker_capable — only a "
                f"docker-capable host can be ranked as a parent"
            )

    @override
    def _resolve_selections(self, preferences: FlatPreferences) -> dict[str, str]:
        # Active selection precedence: the first product preference present in
        # the menu wins; else the lab pin (self.term/.transfer/.impairer, validated
        # against the menu); else the menu's first entry. Out-of-menu preferences
        # are skipped by the resolver.
        term = TERM_RESOLVER.resolve_active(
            self.valid_terms, pin=self.term, preference=preferences.get("term")
        )
        # A console serves one client, so nc (whose listener needs a second
        # session) leaves the menu there: an nc preference falls through to the
        # next kind. Only the resolution narrows; the host keeps its full menu.
        transfers = (
            console_transfer_menu(self._label(), self.valid_transfers, self.transfer)
            if term == "console"
            else self.valid_transfers
        )
        return {
            "term": term,
            "transfer": TRANSFER_RESOLVER.resolve_active(
                transfers, pin=self.transfer, preference=preferences.get("transfer")
            ),
            "impairer": IMPAIRER_RESOLVER.resolve_active(
                self.valid_impairers, pin=self.impairer, preference=preferences.get("impairer")
            ),
        }


class EmbeddedHostSpec(HostSpec):
    """Boundary spec for an embedded host entry in ``lab.json``.

    Extends ``HostSpec`` with the embedded-family fields: term/transfer menus and active
    selections, filesystem and binary loader registry names. Contextual validation
    resolves the active term and transfer; the stock ``to_host()`` looks up the filesystem and
    loader from their registries and builds the ``EmbeddedHost`` (or the registered
    subclass) the readiness record names.
    """

    os_type: str = "embedded"
    valid_terms: Annotated[list[str], Resolve(_menu("valid_terms"))] = Field(
        default_factory=lambda: ["telnet"]
    )
    valid_transfers: Annotated[list[str], Resolve(_menu("valid_transfers"))] = Field(
        default_factory=lambda: ["console"]
    )
    term: Annotated[str | None, Resolve(_selection("term"))] = None
    transfer: Annotated[str | None, Resolve(_selection("transfer"))] = None
    filesystem: Annotated[str | None, Convert(_value_only(_to_filesystem))] = None
    loader: Annotated[str | None, Convert(_value_only(_to_loader))] = None

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

    @model_validator(mode="after")
    def _pins_in_menus(self) -> "EmbeddedHostSpec":
        _check_pins_in_menus(
            self,
            [
                (TERM_RESOLVER, "term", "valid_terms"),
                (TRANSFER_RESOLVER, "transfer", "valid_transfers"),
            ],
        )
        return self

    @field_validator("loader")
    @classmethod
    def _validate_loader_name(cls, v: str | None) -> str | None:
        if v is not None and v not in LOADER_CLASSES:
            known = ", ".join(sorted(LOADER_CLASSES.names())) or "<none>"
            raise ValueError(f"loader {v!r} is not a registered binary loader. Known: {known}")
        return v

    @field_validator("filesystem")
    @classmethod
    def _validate_filesystem_name(cls, v: str | None) -> str | None:
        if v is not None and v not in FILESYSTEM_CLASSES:
            known = ", ".join(sorted(FILESYSTEM_CLASSES.names())) or "<none>"
            raise ValueError(f"filesystem {v!r} is not a registered filesystem. Known: {known}")
        return v

    @override
    def _selection_menus(self) -> dict[str, list[str]]:
        return {"term": list(self.valid_terms), "transfer": list(self.valid_transfers)}

    @override
    def _static_contextual_checks(self, ctx: HostContext) -> None:
        # The spec's frame counts only when it is forwarded: set by the entry (or
        # its profile) and not null. An unset field keeps the class's default.
        forwarded = "command_frame" in self.model_fields_set and self.command_frame is not None
        if not forwarded and not _class_supplies_frame(ctx.entry.cls):
            cls_name = getattr(ctx.entry.cls, "__name__", "the host class")
            raise ValueError(
                f"os_type {self.os_type!r} builds a {cls_name}, which supplies no command_frame. "
                f'Set os_type to a profile that supplies one (e.g. "zephyr"), or set command_frame.'
            )

    @override
    def _resolve_selections(self, preferences: FlatPreferences) -> dict[str, str]:
        # Same precedence as UnixHostSpec: product preference present in the
        # menu -> pin -> menu[0].
        return {
            "term": TERM_RESOLVER.resolve_active(
                self.valid_terms, pin=self.term, preference=preferences.get("term")
            ),
            "transfer": TRANSFER_RESOLVER.resolve_active(
                self.valid_transfers, pin=self.transfer, preference=preferences.get("transfer")
            ),
        }
