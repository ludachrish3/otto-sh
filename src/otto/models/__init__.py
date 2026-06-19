"""Pydantic boundary models — the validation layer for external data
(lab JSON, settings.toml, OTTO_* env, monitor import/export).

These spec models depend on the runtime data modules they validate and build
(``otto.host.options``, ``otto.host.transfer``); those runtime modules do not
import from here, so the dependency runs one way (models -> runtime data) with
no cycle. Higher layers (the storage factory, config, monitor collectors)
import their specs from this package. Each model mirroring a runtime type
carries the ``Spec`` suffix.

Import-order note: ``otto.host.os_profile`` is imported first, before
``.host``. ``models.host`` imports runtime host classes, which trigger
``otto.host``'s package init; that init eagerly runs
``os_profile._register_builtin_host_classes()``, which imports back from
``models.host``. If ``models.host`` is mid-init when that callback fires, it
sees a partially initialized module (ImportError). Loading ``os_profile``
first runs that registration while ``models.host`` has not started, so the
host→models edge resolves cleanly. Root cause is os_profile's eager
module-load registration; see ``todo/registry_builtin_registration_symmetry.md``.
"""

import otto.host.os_profile as _os_profile  # noqa: F401 — boot host registry before models.host (breaks an import cycle)

from .base import OttoModel
from .host import (
    EmbeddedHostSpec,
    HostSpec,
    ToolchainSpec,
    UnixHostSpec,
)
from .monitor import (
    EventRecord,
    MetricPoint,
    MetricRecord,
)
from .options import (
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SnmpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
    TftpOptionsSpec,
)
from .settings import (
    DockerComposeSpec,
    DockerImageSpec,
    DockerSettingsSpec,
    OsProfileSpec,
    OttoEnvSettings,
    ReservationConfigSpec,
    ReservationEntry,
    ReservationFile,
    SettingsModel,
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
    "ToolchainSpec",
    "HostSpec",
    "UnixHostSpec",
    "EmbeddedHostSpec",
    "DockerComposeSpec",
    "DockerImageSpec",
    "DockerSettingsSpec",
    "OsProfileSpec",
    "OttoEnvSettings",
    "ReservationConfigSpec",
    "ReservationEntry",
    "ReservationFile",
    "SettingsModel",
    "MetricPoint",
    "MetricRecord",
    "EventRecord",
]
