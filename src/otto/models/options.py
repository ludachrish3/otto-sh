"""Boundary specs for the per-protocol ``*_options`` tables.

Each ``*OptionsSpec`` validates the JSON-serializable curated fields of its
protocol and builds the matching runtime dataclass from
``otto.host.options`` via ``to_runtime()``. The runtime dataclasses (which
carry the library adapters, callables, and open ``extra`` dicts) are never
modified here.
"""

from typing import Any, Literal

from pydantic import Field, field_validator

from ..host import options as rt
from ..host.transfer import NcListenerCheck, NcPortStrategy
from .base import OttoModel


class SshOptionsSpec(OttoModel):
    """Boundary spec for the SSH connection options table (``[ssh_options]`` in lab data).

    Validates the asyncssh-facing tunables: port, authentication settings,
    cipher/host-key/compression algorithm lists, keepalive, local/remote/SOCKS port forwards,
    and an open ``extra`` dict for pass-through kwargs. Builds a ``SshOptions`` runtime
    dataclass via ``to_runtime()``.
    """

    port: int = 22
    known_hosts: Any = None
    connect_timeout: float | None = None
    keepalive_interval: float | None = None
    keepalive_count_max: int | None = None
    client_keys: list[str] | None = None
    client_host_keys: list[str] | None = None
    agent_forwarding: bool = False
    preferred_auth: str | list[str] | None = None
    encryption_algs: list[str] | None = None
    server_host_key_algs: list[str] | None = None
    compression_algs: list[str] | None = None
    local_forwards: list[rt.LocalPortForward] = Field(default_factory=list)
    remote_forwards: list[rt.RemotePortForward] = Field(default_factory=list)
    socks_forwards: list[rt.SocksForward] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_runtime(self) -> rt.SshOptions:
        """Build the ``SshOptions`` runtime dataclass from the validated spec fields."""
        return rt.SshOptions(
            port=self.port,
            known_hosts=self.known_hosts,
            connect_timeout=self.connect_timeout,
            keepalive_interval=self.keepalive_interval,
            keepalive_count_max=self.keepalive_count_max,
            client_keys=self.client_keys,
            client_host_keys=self.client_host_keys,
            agent_forwarding=self.agent_forwarding,
            preferred_auth=self.preferred_auth,
            encryption_algs=self.encryption_algs,
            server_host_key_algs=self.server_host_key_algs,
            compression_algs=self.compression_algs,
            local_forwards=list(self.local_forwards),
            remote_forwards=list(self.remote_forwards),
            socks_forwards=list(self.socks_forwards),
            extra=dict(self.extra),
        )


class FtpOptionsSpec(OttoModel):
    """Boundary spec for the FTP transfer options table (``[ftp_options]`` in lab data).

    Validates port, encoding, timeout tunables, optional SSL config, speed limits, and
    passive-mode command order. Builds a ``FtpOptions`` runtime dataclass via ``to_runtime()``.
    """

    port: int = 21
    encoding: str = "utf-8"
    socket_timeout: float | None = None
    connection_timeout: float | None = None
    path_timeout: float | None = None
    read_speed_limit: int | None = None
    write_speed_limit: int | None = None
    ssl: Any = None
    passive_commands: tuple[str, ...] = ("epsv", "pasv")
    extra: dict[str, Any] = Field(default_factory=dict)

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

    env: dict[str, str] | None = None
    send_env: list[str] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_runtime(self) -> rt.SftpOptions:
        """Build the ``SftpOptions`` runtime dataclass from the validated spec fields."""
        return rt.SftpOptions(env=self.env, send_env=self.send_env, extra=dict(self.extra))


class ScpOptionsSpec(OttoModel):
    """Boundary spec for the SCP transfer options table (``[scp_options]`` in lab data).

    Validates file-preservation, recursion, and transfer block-size tunables, plus an
    open ``extra`` dict for pass-through kwargs. Builds a ``ScpOptions`` runtime dataclass
    via ``to_runtime()``.
    """

    preserve: bool = False
    recurse: bool = True
    block_size: int = 16384
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_runtime(self) -> rt.ScpOptions:
        """Build the ``ScpOptions`` runtime dataclass from the validated spec fields."""
        return rt.ScpOptions(
            preserve=self.preserve,
            recurse=self.recurse,
            block_size=self.block_size,
            extra=dict(self.extra),
        )


class TelnetOptionsSpec(OttoModel):
    """Boundary spec for the Telnet terminal options table (``[telnet_options]`` in lab data).

    Validates port, character-encoding, terminal dimensions, write-chunking delays,
    login prompt delimiter, single-client-console mode, and connect/echo-negotiation
    timeouts. The ``login_prompt`` field accepts a string from JSON and encodes it to
    bytes via ``_encode_login_prompt``. Builds a ``TelnetOptions`` runtime dataclass
    via ``to_runtime()``.
    """

    port: int = 23
    write_chunk_size: int = 0
    write_chunk_delay: float = 0.0
    cols: int = 400
    rows: int = 24
    encoding: str | bool = False
    connect_timeout: float | None = None
    echo_negotiation_timeout: float = 3.0
    login_prompt: bytes = b":"
    login: bool = True
    single_client_console: bool = False
    auto_window_resize: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

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


class NcOptionsSpec(OttoModel):
    """Boundary spec for the netcat (nc) transfer options table (``[nc_options]`` in lab data).

    Validates the nc executable name, port number, port-discovery and listener-detection
    strategies, optional shell commands for each, and the listener-ready timeout. Builds
    a ``NcOptions`` runtime dataclass via ``to_runtime()``.
    """

    exec_name: str = "nc"
    port: int = 9000
    port_strategy: NcPortStrategy = "auto"
    port_cmd: str | None = None
    listener_check: NcListenerCheck = "auto"
    listener_cmd: str | None = None
    listener_timeout: float = 30.0

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
        )


class SnmpOptionsSpec(OttoModel):
    """Boundary spec for the SNMP monitor options table (``[snmp]`` in lab data).

    Validates the OID list, community string, port, SNMP version (``"1"`` or ``"2c"``),
    and an optional override address. Builds a ``SnmpOptions`` runtime dataclass via
    ``to_runtime()``.
    """

    oids: tuple[str, ...] = ()
    community: str = "public"
    port: int = 161
    version: Literal["1", "2c"] = "2c"
    address: str | None = None

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

    port: int = 69
    server_ip: str | None = None
    block_size: int = 512
    timeout: float = 5.0

    def to_runtime(self) -> rt.TftpOptions:
        """Build the ``TftpOptions`` runtime dataclass from the validated spec fields."""
        return rt.TftpOptions(
            port=self.port,
            server_ip=self.server_ip,
            block_size=self.block_size,
            timeout=self.timeout,
        )


OPTION_SPEC_RUNTIME_PAIRS: list[tuple[type[OttoModel], type]] = [
    (SshOptionsSpec, rt.SshOptions),
    (TelnetOptionsSpec, rt.TelnetOptions),
    (SftpOptionsSpec, rt.SftpOptions),
    (ScpOptionsSpec, rt.ScpOptions),
    (FtpOptionsSpec, rt.FtpOptions),
    (NcOptionsSpec, rt.NcOptions),
    (SnmpOptionsSpec, rt.SnmpOptions),
    (TftpOptionsSpec, rt.TftpOptions),
]
"""Each boundary option spec paired with the runtime dataclass it builds.
Drives the drift guard so the duplicated field lists cannot silently diverge.

:meta hide-value:
"""
