"""Pydantic boundary models — the validation layer for external data
(lab JSON, settings.toml, OTTO_* env, monitor import/export).

These spec models depend on the runtime data modules they validate and build
(``otto.host.options``, ``otto.host.transfer``); those runtime modules do not
import from here, so the dependency runs one way (models -> runtime data) with
no cycle. Higher layers (the storage factory, config, monitor collectors)
import their specs from this package. Each model mirroring a runtime type
carries the ``Spec`` suffix.
"""

from .base import OttoModel
from .options import (
    FtpOptionsSpec,
    LocalPortForwardSpec,
    NcOptionsSpec,
    RemotePortForwardSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SnmpOptionsSpec,
    SocksForwardSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
    TftpOptionsSpec,
)

__all__ = [
    "OttoModel",
    "SshOptionsSpec",
    "TelnetOptionsSpec",
    "SftpOptionsSpec",
    "ScpOptionsSpec",
    "FtpOptionsSpec",
    "NcOptionsSpec",
    "SnmpOptionsSpec",
    "TftpOptionsSpec",
    "LocalPortForwardSpec",
    "RemotePortForwardSpec",
    "SocksForwardSpec",
]
