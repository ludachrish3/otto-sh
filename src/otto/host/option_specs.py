"""Boundary specs for the per-protocol ``*_options`` tables.

Each ``*OptionsSpec`` validates the JSON-serializable curated fields of its
protocol and builds the matching runtime dataclass from
``otto.host.options`` via ``to_runtime()``. The runtime dataclasses (which
carry the library adapters, callables, and open ``extra`` dicts) are never
modified here.
"""

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator

from ..models.base import OttoModel
from . import options as rt
from .transfer import NcListenerCheck, NcPortStrategy

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def _extra_names_no_field(
    extra: dict[str, Any], runtime: "type[DataclassInstance]"
) -> dict[str, Any]:
    """Refuse an ``extra`` key that names a field of *runtime*, or a name it reserves.

    The same rule the runtime dataclass applies when it is built, run here so
    a lab file is refused when it is read, naming the key (never its value),
    rather than when the host is first built.
    """
    shadowed = rt.shadowed_fields(extra, runtime)
    if shadowed:
        msg = (
            f"extra sets {shadowed}, which otto sets from a field of this table or "
            "the host's creds; set those instead"
        )
        raise ValueError(msg)
    return extra


class SshOptionsSpec(OttoModel):
    """Boundary spec for the SSH connection options table (``[ssh_options]`` in lab data).

    Validates the asyncssh-facing tunables: port, authentication settings,
    cipher/host-key/compression/key-exchange/MAC algorithm lists, keepalive,
    local/remote/SOCKS port forwards, and an open ``extra`` dict for
    pass-through kwargs. Builds a ``SshOptions`` runtime dataclass via
    ``to_runtime()``.
    """

    port: int = rt.field_default(rt.SshOptions, "port")
    known_hosts: str = rt.field_default(rt.SshOptions, "known_hosts")
    connect_timeout: float | None = rt.field_default(rt.SshOptions, "connect_timeout")
    keepalive_interval: float | None = rt.field_default(rt.SshOptions, "keepalive_interval")
    keepalive_count_max: int | None = rt.field_default(rt.SshOptions, "keepalive_count_max")
    client_keys: list[str] | None = rt.field_default(rt.SshOptions, "client_keys")
    passphrase: SecretStr | None = rt.field_default(rt.SshOptions, "passphrase")
    client_host_keys: list[str] | None = rt.field_default(rt.SshOptions, "client_host_keys")
    agent_forwarding: bool = rt.field_default(rt.SshOptions, "agent_forwarding")
    preferred_auth: str | list[str] | None = rt.field_default(rt.SshOptions, "preferred_auth")
    encryption_algs: list[str] | None = rt.field_default(rt.SshOptions, "encryption_algs")
    server_host_key_algs: list[str] | None = rt.field_default(rt.SshOptions, "server_host_key_algs")
    compression_algs: list[str] | None = rt.field_default(rt.SshOptions, "compression_algs")
    kex_algs: list[str] | None = rt.field_default(rt.SshOptions, "kex_algs")
    mac_algs: list[str] | None = rt.field_default(rt.SshOptions, "mac_algs")
    local_forwards: list[rt.LocalPortForward] = Field(default_factory=list)
    remote_forwards: list[rt.RemotePortForward] = Field(default_factory=list)
    socks_forwards: list[rt.SocksForward] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    verify_host_key: bool = rt.field_default(rt.SshOptions, "verify_host_key")

    @field_validator("known_hosts", mode="before")
    @classmethod
    def _known_hosts_not_null(cls, value: object) -> object:
        return rt.refuse_null_known_hosts(value)

    @field_validator("extra")
    @classmethod
    def _extra_free_of_fields(cls, extra: dict[str, Any]) -> dict[str, Any]:
        return _extra_names_no_field(extra, rt.SshOptions)

    def to_runtime(self) -> rt.SshOptions:
        """Build the ``SshOptions`` runtime dataclass from the validated spec fields."""
        return rt.SshOptions(
            port=self.port,
            known_hosts=self.known_hosts,
            connect_timeout=self.connect_timeout,
            keepalive_interval=self.keepalive_interval,
            keepalive_count_max=self.keepalive_count_max,
            client_keys=self.client_keys,
            passphrase=self.passphrase,
            client_host_keys=self.client_host_keys,
            agent_forwarding=self.agent_forwarding,
            preferred_auth=self.preferred_auth,
            encryption_algs=self.encryption_algs,
            server_host_key_algs=self.server_host_key_algs,
            compression_algs=self.compression_algs,
            kex_algs=self.kex_algs,
            mac_algs=self.mac_algs,
            local_forwards=list(self.local_forwards),
            remote_forwards=list(self.remote_forwards),
            socks_forwards=list(self.socks_forwards),
            extra=dict(self.extra),
            verify_host_key=self.verify_host_key,
        )


class FtpOptionsSpec(OttoModel):
    """Boundary spec for the FTP transfer options table (``[ftp_options]`` in lab data).

    Validates port, encoding, timeout tunables, optional SSL config, speed limits, and
    passive-mode command order. Builds a ``FtpOptions`` runtime dataclass via ``to_runtime()``.
    """

    port: int = rt.field_default(rt.FtpOptions, "port")
    encoding: str = rt.field_default(rt.FtpOptions, "encoding")
    socket_timeout: float | None = rt.field_default(rt.FtpOptions, "socket_timeout")
    connection_timeout: float | None = rt.field_default(rt.FtpOptions, "connection_timeout")
    path_timeout: float | None = rt.field_default(rt.FtpOptions, "path_timeout")
    read_speed_limit: int | None = rt.field_default(rt.FtpOptions, "read_speed_limit")
    write_speed_limit: int | None = rt.field_default(rt.FtpOptions, "write_speed_limit")
    ssl: Any = rt.field_default(rt.FtpOptions, "ssl")
    passive_commands: tuple[str, ...] = ("epsv", "pasv")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_free_of_fields(cls, extra: dict[str, Any]) -> dict[str, Any]:
        return _extra_names_no_field(extra, rt.FtpOptions)

    def to_runtime(self) -> rt.FtpOptions:
        """Build the ``FtpOptions`` runtime dataclass from the validated spec fields."""
        return rt.FtpOptions(
            port=self.port,
            encoding=self.encoding,
            socket_timeout=self.socket_timeout,
            connection_timeout=self.connection_timeout,
            path_timeout=self.path_timeout,
            read_speed_limit=self.read_speed_limit,
            write_speed_limit=self.write_speed_limit,
            ssl=self.ssl,
            passive_commands=self.passive_commands,
            extra=dict(self.extra),
        )


class SftpOptionsSpec(OttoModel):
    """Boundary spec for the SFTP transfer options table (``[sftp_options]`` in lab data).

    Validates environment variables to set on the remote SFTP session and an open ``extra``
    dict for pass-through kwargs. Builds a ``SftpOptions`` runtime dataclass via ``to_runtime()``.
    """

    env: dict[str, str] | None = rt.field_default(rt.SftpOptions, "env")
    send_env: list[str] | None = rt.field_default(rt.SftpOptions, "send_env")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_free_of_fields(cls, extra: dict[str, Any]) -> dict[str, Any]:
        return _extra_names_no_field(extra, rt.SftpOptions)

    # Bound as a `Field` kwarg, not as `Annotated[int, Field(ge=1)]`: autodoc's
    # annotation stringifier turns the metadata into dotted py:class targets
    # (`Ge`, `FieldInfo`, `NoneType`) and nitpicky -W fails the docs build on
    # them. Same constraint, and the same reason as `CoverageReportSpec.high`.
    max_concurrent_transfers: int | None = Field(default=None, ge=1)

    def to_runtime(self) -> rt.SftpOptions:
        """Build the ``SftpOptions`` runtime dataclass from the validated spec fields."""
        return rt.SftpOptions(
            env=self.env,
            send_env=self.send_env,
            extra=dict(self.extra),
            max_concurrent_transfers=self.max_concurrent_transfers,
        )


class ScpOptionsSpec(OttoModel):
    """Boundary spec for the SCP transfer options table (``[scp_options]`` in lab data).

    Validates file-preservation, recursion, and transfer block-size tunables, plus an
    open ``extra`` dict for pass-through kwargs. Builds a ``ScpOptions`` runtime dataclass
    via ``to_runtime()``.
    """

    preserve: bool = rt.field_default(rt.ScpOptions, "preserve")
    recurse: bool = rt.field_default(rt.ScpOptions, "recurse")
    block_size: int = rt.field_default(rt.ScpOptions, "block_size")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_free_of_fields(cls, extra: dict[str, Any]) -> dict[str, Any]:
        return _extra_names_no_field(extra, rt.ScpOptions)

    # Bound as a `Field` kwarg, not as `Annotated[int, Field(ge=1)]`: autodoc's
    # annotation stringifier turns the metadata into dotted py:class targets
    # (`Ge`, `FieldInfo`, `NoneType`) and nitpicky -W fails the docs build on
    # them. Same constraint, and the same reason as `CoverageReportSpec.high`.
    max_concurrent_transfers: int | None = Field(default=None, ge=1)

    def to_runtime(self) -> rt.ScpOptions:
        """Build the ``ScpOptions`` runtime dataclass from the validated spec fields."""
        return rt.ScpOptions(
            preserve=self.preserve,
            recurse=self.recurse,
            block_size=self.block_size,
            extra=dict(self.extra),
            max_concurrent_transfers=self.max_concurrent_transfers,
        )


class TelnetOptionsSpec(OttoModel):
    """Boundary spec for the Telnet terminal options table (``[telnet_options]`` in lab data).

    Validates port, character-encoding, terminal dimensions, write-chunking delays,
    login prompt delimiter, single-client-console mode, and connect/echo-negotiation
    timeouts. The ``login_prompt`` field accepts a string from JSON and encodes it to
    bytes via ``_encode_login_prompt``. Builds a ``TelnetOptions`` runtime dataclass
    via ``to_runtime()``.
    """

    port: int = rt.field_default(rt.TelnetOptions, "port")
    write_chunk_size: int = rt.field_default(rt.TelnetOptions, "write_chunk_size")
    write_chunk_delay: float = rt.field_default(rt.TelnetOptions, "write_chunk_delay")
    cols: int = rt.field_default(rt.TelnetOptions, "cols")
    rows: int = rt.field_default(rt.TelnetOptions, "rows")
    encoding: str | bool = rt.field_default(rt.TelnetOptions, "encoding")
    connect_timeout: float | None = rt.field_default(rt.TelnetOptions, "connect_timeout")
    echo_negotiation_timeout: float = rt.field_default(rt.TelnetOptions, "echo_negotiation_timeout")
    login_prompt: bytes = rt.field_default(rt.TelnetOptions, "login_prompt")
    login: bool = rt.field_default(rt.TelnetOptions, "login")
    single_client_console: bool = rt.field_default(rt.TelnetOptions, "single_client_console")
    auto_window_resize: bool = rt.field_default(rt.TelnetOptions, "auto_window_resize")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_free_of_fields(cls, extra: dict[str, Any]) -> dict[str, Any]:
        return _extra_names_no_field(extra, rt.TelnetOptions)

    @field_validator("login_prompt", mode="before")
    @classmethod
    def _encode_login_prompt(cls, v: object) -> object:
        """Lab JSON carries the delimiter as a string; encode to bytes."""
        return v.encode() if isinstance(v, str) else v

    def to_runtime(self) -> rt.TelnetOptions:
        """Build the ``TelnetOptions`` runtime dataclass from the validated spec fields."""
        return rt.TelnetOptions(
            port=self.port,
            write_chunk_size=self.write_chunk_size,
            write_chunk_delay=self.write_chunk_delay,
            cols=self.cols,
            rows=self.rows,
            encoding=self.encoding,
            connect_timeout=self.connect_timeout,
            echo_negotiation_timeout=self.echo_negotiation_timeout,
            login_prompt=self.login_prompt,
            login=self.login,
            single_client_console=self.single_client_console,
            auto_window_resize=self.auto_window_resize,
            extra=dict(self.extra),
        )


class ConsoleOptionsSpec(OttoModel):
    """Boundary spec for the ``console`` term's options table (``[console_options]`` in lab data).

    Validates the server/port address, the dial mode, the prompt regexes
    (compiled here so a bad pattern fails at lab load, not at first
    connect), and the paced-write and terminal fields shared with telnet.
    Builds a ``ConsoleOptions`` runtime dataclass via ``to_runtime()``.
    """

    server: str = rt.field_default(rt.ConsoleOptions, "server")
    port: int = Field(default=0, ge=0, le=65535)
    dial: Literal["ssh", "direct"] = rt.field_default(rt.ConsoleOptions, "dial")
    login: bool = rt.field_default(rt.ConsoleOptions, "login")
    login_prompt: str | None = rt.field_default(rt.ConsoleOptions, "login_prompt")
    password_prompt: str | None = rt.field_default(rt.ConsoleOptions, "password_prompt")
    login_timeout: float = Field(default=10.0, gt=0)
    settle: float = Field(default=0.5, ge=0)
    logout: bool = rt.field_default(rt.ConsoleOptions, "logout")
    write_chunk_size: int = rt.field_default(rt.ConsoleOptions, "write_chunk_size")
    write_chunk_delay: float = rt.field_default(rt.ConsoleOptions, "write_chunk_delay")
    cols: int = rt.field_default(rt.ConsoleOptions, "cols")
    rows: int = rt.field_default(rt.ConsoleOptions, "rows")
    encoding: str | bool = rt.field_default(rt.ConsoleOptions, "encoding")
    echo_negotiation_timeout: float = rt.field_default(
        rt.ConsoleOptions, "echo_negotiation_timeout"
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_free_of_fields(cls, extra: dict[str, Any]) -> dict[str, Any]:
        return _extra_names_no_field(extra, rt.ConsoleOptions)

    @field_validator("login_prompt", "password_prompt")
    @classmethod
    def _compiles(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None:
            return v
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"{info.field_name} is not a valid regex: {exc}") from None
        return v

    def to_runtime(self) -> rt.ConsoleOptions:
        """Build the ``ConsoleOptions`` runtime dataclass from the validated spec fields."""
        return rt.ConsoleOptions(
            server=self.server,
            port=self.port,
            dial=self.dial,
            login=self.login,
            login_prompt=self.login_prompt,
            password_prompt=self.password_prompt,
            login_timeout=self.login_timeout,
            settle=self.settle,
            logout=self.logout,
            write_chunk_size=self.write_chunk_size,
            write_chunk_delay=self.write_chunk_delay,
            cols=self.cols,
            rows=self.rows,
            encoding=self.encoding,
            echo_negotiation_timeout=self.echo_negotiation_timeout,
            extra=dict(self.extra),
        )


class NcOptionsSpec(OttoModel):
    """Boundary spec for the netcat (nc) transfer options table (``[nc_options]`` in lab data).

    Validates the nc executable name, port number, port-discovery and listener-detection
    strategies, optional shell commands for each, and the listener-ready timeout. Builds
    a ``NcOptions`` runtime dataclass via ``to_runtime()``.
    """

    exec_name: str = rt.field_default(rt.NcOptions, "exec_name")
    port: int = rt.field_default(rt.NcOptions, "port")
    port_strategy: NcPortStrategy = rt.field_default(rt.NcOptions, "port_strategy")
    port_cmd: str | None = rt.field_default(rt.NcOptions, "port_cmd")
    listener_check: NcListenerCheck = rt.field_default(rt.NcOptions, "listener_check")
    listener_cmd: str | None = rt.field_default(rt.NcOptions, "listener_cmd")
    listener_timeout: float = rt.field_default(rt.NcOptions, "listener_timeout")
    # Bound as a `Field` kwarg, not as `Annotated[int, Field(ge=1)]`: autodoc's
    # annotation stringifier turns the metadata into dotted py:class targets
    # (`Ge`, `FieldInfo`, `NoneType`) and nitpicky -W fails the docs build on
    # them. Same constraint, and the same reason as `CoverageReportSpec.high`.
    max_concurrent_transfers: int | None = Field(default=None, ge=1)

    def to_runtime(self) -> rt.NcOptions:
        """Build the ``NcOptions`` runtime dataclass from the validated spec fields."""
        return rt.NcOptions(
            exec_name=self.exec_name,
            port=self.port,
            port_strategy=self.port_strategy,
            port_cmd=self.port_cmd,
            listener_check=self.listener_check,
            listener_cmd=self.listener_cmd,
            listener_timeout=self.listener_timeout,
            max_concurrent_transfers=self.max_concurrent_transfers,
        )


class SnmpOptionsSpec(OttoModel):
    """Boundary spec for the SNMP monitor options table (``[snmp]`` in lab data).

    Validates the OID list, community string, port, SNMP version (``"1"`` or ``"2c"``),
    and an optional override address. Builds a ``SnmpOptions`` runtime dataclass via
    ``to_runtime()``.
    """

    oids: tuple[str, ...] = ()
    community: str = rt.field_default(rt.SnmpOptions, "community")
    port: int = rt.field_default(rt.SnmpOptions, "port")
    version: Literal["1", "2c"] = rt.field_default(rt.SnmpOptions, "version")
    address: str | None = rt.field_default(rt.SnmpOptions, "address")

    def to_runtime(self) -> rt.SnmpOptions:
        """Build the ``SnmpOptions`` runtime dataclass from the validated spec fields."""
        return rt.SnmpOptions(
            oids=self.oids,
            community=self.community,
            port=self.port,
            version=self.version,
            address=self.address,
        )


class TftpOptionsSpec(OttoModel):
    """Boundary spec for the TFTP transfer options table (``[tftp_options]`` in lab data).

    Validates port, optional server IP override, transfer block size, and per-block timeout.
    Builds a ``TftpOptions`` runtime dataclass via ``to_runtime()``.
    """

    port: int = rt.field_default(rt.TftpOptions, "port")
    server_ip: str | None = rt.field_default(rt.TftpOptions, "server_ip")
    block_size: int = rt.field_default(rt.TftpOptions, "block_size")
    timeout: float = rt.field_default(rt.TftpOptions, "timeout")

    def to_runtime(self) -> rt.TftpOptions:
        """Build the ``TftpOptions`` runtime dataclass from the validated spec fields."""
        return rt.TftpOptions(
            port=self.port,
            server_ip=self.server_ip,
            block_size=self.block_size,
            timeout=self.timeout,
        )


class UserlandOptionsSpec(OttoModel):
    """Boundary spec for the ``[userland_options]`` lab-data table.

    Every field defaults to ``None`` = probe. Constraints are expressed as
    ``Literal`` unions rather than free strings so a lab-data typo fails here,
    naming the key, instead of silently selecting a fallback at runtime.
    """

    shell_dialect: str | None = rt.field_default(rt.UserlandOptions, "shell_dialect")
    elevation: Literal["sudo", "su", "none"] | None = rt.field_default(
        rt.UserlandOptions, "elevation"
    )
    base64_flag: Literal["-d", "--decode", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "base64_flag"
    )
    stat_size: Literal["stat", "wc", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "stat_size"
    )
    checksum: Literal["md5sum", "absent"] | None = rt.field_default(rt.UserlandOptions, "checksum")
    timeout_style: Literal["coreutils", "dash-t", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "timeout_style"
    )
    # One field per name in `otto.host.userland.PROBED_APPLETS`, spelled out
    # rather than generated: `extra='forbid'` is what turns `applet_scpp` in
    # lab.json into an error naming the key, and it can only do that for fields
    # the model actually declares. The list is not imported — a boundary that
    # read its field names from the runtime could not be caught drifting from
    # it — so `tests/unit/host/test_userland.py` pins the two sets equal.
    applet_base64: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_base64"
    )
    applet_nc: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_nc"
    )
    applet_poweroff: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_poweroff"
    )
    applet_scp: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_scp"
    )
    applet_shutdown: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_shutdown"
    )
    applet_uudecode: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_uudecode"
    )
    applet_uuencode: Literal["present", "absent"] | None = rt.field_default(
        rt.UserlandOptions, "applet_uuencode"
    )
    version: str | None = rt.field_default(rt.UserlandOptions, "version")

    def to_runtime(self) -> rt.UserlandOptions:
        """Build the ``UserlandOptions`` runtime dataclass from validated fields."""
        return rt.UserlandOptions(
            shell_dialect=self.shell_dialect,
            elevation=self.elevation,
            base64_flag=self.base64_flag,
            stat_size=self.stat_size,
            checksum=self.checksum,
            timeout_style=self.timeout_style,
            applet_base64=self.applet_base64,
            applet_nc=self.applet_nc,
            applet_poweroff=self.applet_poweroff,
            applet_scp=self.applet_scp,
            applet_shutdown=self.applet_shutdown,
            applet_uudecode=self.applet_uudecode,
            applet_uuencode=self.applet_uuencode,
            version=self.version,
        )


OPTION_SPEC_RUNTIME_PAIRS: list[tuple[type[OttoModel], type]] = [
    (SshOptionsSpec, rt.SshOptions),
    (TelnetOptionsSpec, rt.TelnetOptions),
    (ConsoleOptionsSpec, rt.ConsoleOptions),
    (SftpOptionsSpec, rt.SftpOptions),
    (ScpOptionsSpec, rt.ScpOptions),
    (FtpOptionsSpec, rt.FtpOptions),
    (NcOptionsSpec, rt.NcOptions),
    (SnmpOptionsSpec, rt.SnmpOptions),
    (TftpOptionsSpec, rt.TftpOptions),
    (UserlandOptionsSpec, rt.UserlandOptions),
]
"""Each boundary option spec paired with the runtime dataclass it builds.
Drives the drift guard so the duplicated field lists cannot silently diverge.

:meta hide-value:
"""
