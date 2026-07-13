"""Pydantic boundary models — the validation layer for external data (lab JSON, settings, env).

These spec models depend on the runtime data modules they validate and build
(``otto.host.options``, ``otto.host.transfer``); those runtime modules do not
import from here, so the dependency runs one way (models -> runtime data) with
no cycle. Higher layers (the host factory, config, monitor collectors)
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

import otto.host.os_profile as _os_profile

from .base import OttoModel
from .host import (
    EmbeddedHostSpec,
    HostSpec,
    ToolchainSpec,
    UnixHostSpec,
)
from .monitor import (
    MIN_INTERVAL_SECONDS,
    ChartSpec,
    ChartSpecRecord,
    ElementRecord,
    EventRecord,
    HostSnapshot,
    LabSnapshot,
    LinkEndpointSnapshot,
    LinkSnapshot,
    LogEventRecord,
    MetricPoint,
    MetricRecord,
    MonitorExport,
    MonitorMeta,
    SessionMeta,
    SessionRecord,
    TabSpec,
    TabSpecRecord,
    validate_interval,
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
    "MIN_INTERVAL_SECONDS",
    "ChartSpec",
    "ChartSpecRecord",
    "DockerComposeSpec",
    "DockerImageSpec",
    "DockerSettingsSpec",
    "ElementRecord",
    "EmbeddedHostSpec",
    "EventRecord",
    "FtpOptionsSpec",
    "HostSnapshot",
    "HostSpec",
    "LabSnapshot",
    "LinkEndpointSnapshot",
    "LinkSnapshot",
    "LogEventRecord",
    "MetricPoint",
    "MetricRecord",
    "MonitorExport",
    "MonitorMeta",
    "NcOptionsSpec",
    "OsProfileSpec",
    "OttoEnvSettings",
    "OttoModel",
    "ReservationConfigSpec",
    "ReservationEntry",
    "ReservationFile",
    "ScpOptionsSpec",
    "SessionMeta",
    "SessionRecord",
    "SettingsModel",
    "SftpOptionsSpec",
    "SnmpOptionsSpec",
    "SshOptionsSpec",
    "TabSpec",
    "TabSpecRecord",
    "TelnetOptionsSpec",
    "TftpOptionsSpec",
    "ToolchainSpec",
    "UnixHostSpec",
    "validate_interval",
]
