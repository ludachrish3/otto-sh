"""Pydantic boundary specs for the host record (a ``hosts.json`` entry).

``HostSpec`` and its family subclasses validate a host dict and build the
unchanged runtime ``UnixHost`` / ``EmbeddedHost`` via ``to_host()``. The specs
nest the per-protocol ``*OptionsSpec``s from ``otto.models.options`` and reuse
their ``to_runtime()`` builders; embedded registry-name fields
(``filesystem`` / ``command_frame`` / ``loader``) resolve through the existing
host registries at build time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..host.binary_loader import build_binary_loader
from ..host.command_frame import build_command_frame
from ..host.embedded_filesystem import build_filesystem
from ..host.embedded_host import EmbeddedHost
from ..host.embedded_transfer import EmbeddedTransferType
from ..host.host import FileTransferType, TermType
from ..host.toolchain import Toolchain
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
    log: bool = True
    log_stdout: bool = True  # common: both UnixHost and EmbeddedHost declare it
    telnet_options: TelnetOptionsSpec = TelnetOptionsSpec()
    snmp: SnmpOptionsSpec | None = None
    toolchain: ToolchainSpec = ToolchainSpec()

    # Lab membership — validated (so a `lab`/`labs` typo errors) but NOT a host
    # constructor argument; the repository uses it to filter hosts into a Lab.
    labs: list[str] = []

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
        if "telnet_options" in s:
            kw["telnet_options"] = self.telnet_options.to_runtime()
        if "snmp" in s:
            kw["snmp"] = self.snmp.to_runtime() if self.snmp is not None else None
        if "toolchain" in s:
            kw["toolchain"] = self.toolchain.to_runtime()
        return kw


class UnixHostSpec(HostSpec):
    creds: dict[str, str]  # override: required for a Unix host (SSH/telnet login)
    hw_version: str | None = None
    sw_version: str | None = None
    term: TermType = "ssh"
    docker_capable: bool = False
    transfer: FileTransferType = "scp"
    ssh_options: SshOptionsSpec = SshOptionsSpec()
    sftp_options: SftpOptionsSpec = SftpOptionsSpec()
    scp_options: ScpOptionsSpec = ScpOptionsSpec()
    ftp_options: FtpOptionsSpec = FtpOptionsSpec()
    nc_options: NcOptionsSpec = NcOptionsSpec()

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
    transfer: EmbeddedTransferType = "console"
    filesystem: str | None = None
    command_frame: str | None = None
    loader: str | None = None

    def to_host(self, cls: type[EmbeddedHost] = EmbeddedHost) -> EmbeddedHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        if "transfer" in s:
            kw["transfer"] = self.transfer
        if "filesystem" in s and self.filesystem is not None:
            kw["filesystem"] = build_filesystem(self.filesystem)
        if "command_frame" in s and self.command_frame is not None:
            kw["command_frame"] = build_command_frame(self.command_frame)
        if "loader" in s and self.loader is not None:
            kw["loader"] = build_binary_loader(self.loader)
        return cls(**kw)


HOST_SPEC_RUNTIME_PAIRS: list[tuple[type[HostSpec], type]] = [
    (UnixHostSpec, UnixHost),
    (EmbeddedHostSpec, EmbeddedHost),
]
"""Each host spec paired with the runtime class it builds. Drives the drift
guard so a spec field that has no constructor counterpart is caught."""
