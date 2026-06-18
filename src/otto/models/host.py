"""Pydantic boundary specs for the host record (a ``hosts.json`` entry).

``HostSpec`` and its family subclasses validate a host dict and build the
unchanged runtime ``UnixHost`` / ``EmbeddedHost`` via ``to_host()``. The specs
nest the per-protocol ``*OptionsSpec``s from ``otto.models.options`` and reuse
their ``to_runtime()`` builders; embedded registry-name fields
(``filesystem`` / ``command_frame`` / ``loader``) resolve through the existing
host registries at build time.
"""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from typing import Any, ClassVar

from pydantic import field_validator

from ..host.binary_loader import build_binary_loader
from ..host.command_frame import _FRAME_CLASSES, build_command_frame
from ..host.connections import _TERM_BACKENDS
from ..host.embedded_filesystem import _FILESYSTEM_CLASSES, build_filesystem
from ..host.embedded_host import EmbeddedHost
from ..host.remote_host import RemoteHost
from ..host.toolchain import Toolchain
from ..host.transfer import _TRANSFER_BACKENDS
from ..host.unix_host import UnixHost
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
    sysroot: Path = Path("/")
    lcov: Path = Path("usr/bin/lcov")
    gcov: Path = Path("usr/bin/gcov")

    def to_runtime(self) -> Toolchain:
        return Toolchain(sysroot=self.sysroot, lcov=self.lcov, gcov=self.gcov)


# Common fields passed straight through to the host constructor (no conversion).
# Conversions for default_dest_dir/resources/telnet_options/snmp/toolchain are
# applied separately in _common_host_kwargs.
_COMMON_PLAIN_FIELDS = (
    "ip", "element", "creds", "name", "os_type", "os_name", "os_version",
    "user", "element_id", "board", "slot", "hop", "is_virtual",
    "max_filename_len", "log", "log_stdout",
)


def _validate_transfer_for_family(v: str, family: str, host_label: str) -> str:
    """Validate a transfer selector against the registry and host-family applicability."""
    if v not in _TRANSFER_BACKENDS:
        known = ", ".join(sorted(_TRANSFER_BACKENDS))
        raise ValueError(
            f"transfer {v!r} is not a registered transfer backend. Known: {known}"
        )
    if family not in _TRANSFER_BACKENDS[v].host_families:
        fam = ", ".join(sorted(_TRANSFER_BACKENDS[v].host_families))
        raise ValueError(
            f"transfer {v!r} is not valid on {host_label} (it serves: {fam})."
        )
    return v


class HostSpec(OttoModel):
    # --- required identity (both families) ---
    ip: str
    element: str

    # --- common optional fields ---
    creds: dict[str, str] = {}
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
    default_dest_dir: Path = Path()
    max_filename_len: int = 255
    resources: set[str] = set()
    interfaces: dict[str, str] = {}
    log: bool = True
    log_stdout: bool = True  # common: both UnixHost and EmbeddedHost declare it
    telnet_options: TelnetOptionsSpec = TelnetOptionsSpec()
    snmp: SnmpOptionsSpec | None = None
    toolchain: ToolchainSpec = ToolchainSpec()
    command_frame: str | None = None

    # Lab membership — validated (so a `lab`/`labs` typo errors) but NOT a host
    # constructor argument; the repository uses it to filter hosts into a Lab.
    labs: list[str] = []

    @field_validator("interfaces")
    @classmethod
    def _validate_interface_addresses(cls, v: dict[str, str]) -> dict[str, str]:
        # Validate only; runtime keeps the raw string form (like ``ip``), so we
        # return the originals rather than the parsed ip_address objects.
        for name, addr in v.items():
            try:
                ip_address(addr)
            except ValueError:
                raise ValueError(
                    f"interface {name!r} address {addr!r} is not a valid IP"
                ) from None
        return v

    @field_validator("command_frame")
    @classmethod
    def _validate_command_frame_name(cls, v: str | None) -> str | None:
        if v is not None and v not in _FRAME_CLASSES:
            known = ", ".join(sorted(_FRAME_CLASSES))
            raise ValueError(
                f"command_frame {v!r} is not a registered frame. Known: {known}"
            )
        return v

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
        if "default_dest_dir" in s:
            kw["default_dest_dir"] = Path(self.default_dest_dir)
        if "resources" in s:
            kw["resources"] = set(self.resources)
        if "interfaces" in s:
            kw["interfaces"] = dict(self.interfaces)
        if "telnet_options" in s:
            kw["telnet_options"] = self.telnet_options.to_runtime()
        if "snmp" in s:
            kw["snmp"] = self.snmp.to_runtime() if self.snmp is not None else None
        if "toolchain" in s:
            kw["toolchain"] = self.toolchain.to_runtime()
        if "command_frame" in s and self.command_frame is not None:
            kw["command_frame"] = build_command_frame(self.command_frame)
        return kw

    def to_host(self, cls: Any = None) -> RemoteHost:
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
    creds: dict[str, str]  # override: required for a Unix host (SSH/telnet login)
    hw_version: str | None = None
    sw_version: str | None = None
    term: str = "ssh"
    docker_capable: bool = False
    transfer: str = "scp"
    ssh_options: SshOptionsSpec = SshOptionsSpec()
    sftp_options: SftpOptionsSpec = SftpOptionsSpec()
    scp_options: ScpOptionsSpec = ScpOptionsSpec()
    ftp_options: FtpOptionsSpec = FtpOptionsSpec()
    nc_options: NcOptionsSpec = NcOptionsSpec()

    _transfer_host_family: ClassVar[str] = "unix"

    @field_validator("term")
    @classmethod
    def _validate_term_name(cls, v: str) -> str:
        if v not in _TERM_BACKENDS:
            known = ", ".join(sorted(_TERM_BACKENDS))
            raise ValueError(
                f"term {v!r} is not a registered term backend. Known: {known}"
            )
        return v

    @field_validator("transfer")
    @classmethod
    def _validate_unix_transfer_name(cls, v: str) -> str:
        return _validate_transfer_for_family(v, cls._transfer_host_family, "a unix host")

    def to_host(self, cls: type[UnixHost] = UnixHost) -> UnixHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        for n in ("hw_version", "sw_version", "term",
                  "docker_capable", "transfer"):
            if n in s:
                kw[n] = getattr(self, n)
        for n in ("ssh_options", "sftp_options", "scp_options",
                  "ftp_options", "nc_options"):
            if n in s:
                kw[n] = getattr(self, n).to_runtime()
        return cls(**kw)


class EmbeddedHostSpec(HostSpec):
    os_type: str = "embedded"
    transfer: str = "console"
    filesystem: str | None = None
    loader: str | None = None

    _transfer_host_family: ClassVar[str] = "embedded"

    @field_validator("transfer")
    @classmethod
    def _validate_embedded_transfer_name(cls, v: str) -> str:
        return _validate_transfer_for_family(
            v, cls._transfer_host_family, "an embedded host"
        )

    @field_validator("filesystem")
    @classmethod
    def _validate_filesystem_name(cls, v: str | None) -> str | None:
        if v is not None and v not in _FILESYSTEM_CLASSES:
            known = ", ".join(sorted(_FILESYSTEM_CLASSES))
            raise ValueError(
                f"filesystem {v!r} is not a registered filesystem. Known: {known}"
            )
        return v

    def to_host(self, cls: type[EmbeddedHost] = EmbeddedHost) -> EmbeddedHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        if "transfer" in s:
            kw["transfer"] = self.transfer
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
guard so a spec field that has no constructor counterpart is caught."""
