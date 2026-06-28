"""Boundary specs for the per-protocol ``*_options`` tables.

Each ``*OptionsSpec`` validates the JSON-serializable curated fields of its
protocol and builds the matching runtime dataclass from
``otto.host.options`` via ``to_runtime()``. The runtime dataclasses (which
carry the library adapters, callables, and open ``extra`` dicts) are never
modified here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from ..host import options as rt
from ..host.transfer import NcListenerCheck, NcPortStrategy
from .base import OttoModel


class SshOptionsSpec(OttoModel):
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
    env: dict[str, str] | None = None
    send_env: list[str] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_runtime(self) -> rt.SftpOptions:
        return rt.SftpOptions(env=self.env, send_env=self.send_env, extra=dict(self.extra))


class ScpOptionsSpec(OttoModel):
    preserve: bool = False
    recurse: bool = True
    block_size: int = 16384
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_runtime(self) -> rt.ScpOptions:
        return rt.ScpOptions(
            preserve=self.preserve,
            recurse=self.recurse,
            block_size=self.block_size,
            extra=dict(self.extra),
        )


class TelnetOptionsSpec(OttoModel):
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
    exec_name: str = "nc"
    port: int = 9000
    port_strategy: NcPortStrategy = "auto"
    port_cmd: str | None = None
    listener_check: NcListenerCheck = "auto"
    listener_cmd: str | None = None
    listener_timeout: float = 30.0

    def to_runtime(self) -> rt.NcOptions:
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
    oids: tuple[str, ...] = ()
    community: str = "public"
    port: int = 161
    version: Literal["1", "2c"] = "2c"
    address: str | None = None

    def to_runtime(self) -> rt.SnmpOptions:
        return rt.SnmpOptions(
            oids=self.oids,
            community=self.community,
            port=self.port,
            version=self.version,
            address=self.address,
        )


class TftpOptionsSpec(OttoModel):
    port: int = 69
    server_ip: str | None = None
    block_size: int = 512
    timeout: float = 5.0

    def to_runtime(self) -> rt.TftpOptions:
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
