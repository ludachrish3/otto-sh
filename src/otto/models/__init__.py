"""Pydantic boundary models — the validation layer for external data (lab JSON, settings, env).

These spec models depend on the runtime data modules they validate and build
(``otto.host.options``, ``otto.host.transfer``); those runtime modules do not
import from here, so the dependency runs one way (models -> runtime data) with
no cycle. Higher layers (the host factory, config, monitor collectors)
import their specs from this package. Each model mirroring a runtime type
carries the ``Spec`` suffix.

Every name is exported lazily (PEP 562): ``from otto.models import HostSpec``
imports ``otto.models.host`` alone, not the monitor records or the settings
models. The settings models matter most: ``OttoEnvSettings`` subclasses
``pydantic_settings.BaseSettings``, so an eager import would put
pydantic_settings and dotenv on every ``otto ... --help`` path, though only
commands that resolve settings need them. The resolver does not write a
resolved name back into the module dict; see ``otto.config``'s ``__dir__``
for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..utils import MIN_INTERVAL_SECONDS as MIN_INTERVAL_SECONDS
    from ..utils import validate_interval as validate_interval
    from .base import OttoModel as OttoModel
    from .host import EmbeddedHostSpec as EmbeddedHostSpec
    from .host import HostSpec as HostSpec
    from .host import ToolchainSpec as ToolchainSpec
    from .host import UnixHostSpec as UnixHostSpec
    from .monitor import ChartSpec as ChartSpec
    from .monitor import ChartSpecRecord as ChartSpecRecord
    from .monitor import ElementRecord as ElementRecord
    from .monitor import EventRecord as EventRecord
    from .monitor import HostSnapshot as HostSnapshot
    from .monitor import LabSnapshot as LabSnapshot
    from .monitor import LinkEndpointSnapshot as LinkEndpointSnapshot
    from .monitor import LinkSnapshot as LinkSnapshot
    from .monitor import LogEventRecord as LogEventRecord
    from .monitor import MetricPoint as MetricPoint
    from .monitor import MetricRecord as MetricRecord
    from .monitor import MonitorExport as MonitorExport
    from .monitor import MonitorMeta as MonitorMeta
    from .monitor import SessionMeta as SessionMeta
    from .monitor import SessionRecord as SessionRecord
    from .monitor import TabSpec as TabSpec
    from .monitor import TabSpecRecord as TabSpecRecord
    from .monitor import TunnelRecord as TunnelRecord
    from .options import FtpOptionsSpec as FtpOptionsSpec
    from .options import NcOptionsSpec as NcOptionsSpec
    from .options import ScpOptionsSpec as ScpOptionsSpec
    from .options import SftpOptionsSpec as SftpOptionsSpec
    from .options import SnmpOptionsSpec as SnmpOptionsSpec
    from .options import SshOptionsSpec as SshOptionsSpec
    from .options import TelnetOptionsSpec as TelnetOptionsSpec
    from .options import TftpOptionsSpec as TftpOptionsSpec
    from .settings import DockerComposeSpec as DockerComposeSpec
    from .settings import DockerImageSpec as DockerImageSpec
    from .settings import DockerSettingsSpec as DockerSettingsSpec
    from .settings import OsProfileSpec as OsProfileSpec
    from .settings import OttoEnvSettings as OttoEnvSettings
    from .settings import ReservationConfigSpec as ReservationConfigSpec
    from .settings import ReservationEntry as ReservationEntry
    from .settings import ReservationFile as ReservationFile
    from .settings import SettingsModel as SettingsModel

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "OttoModel": "otto.models.base",
    "EmbeddedHostSpec": "otto.models.host",
    "HostSpec": "otto.models.host",
    "ToolchainSpec": "otto.models.host",
    "UnixHostSpec": "otto.models.host",
    "ChartSpec": "otto.models.monitor",
    "ChartSpecRecord": "otto.models.monitor",
    "ElementRecord": "otto.models.monitor",
    "EventRecord": "otto.models.monitor",
    "HostSnapshot": "otto.models.monitor",
    "LabSnapshot": "otto.models.monitor",
    "LinkEndpointSnapshot": "otto.models.monitor",
    "LinkSnapshot": "otto.models.monitor",
    "LogEventRecord": "otto.models.monitor",
    "MIN_INTERVAL_SECONDS": "otto.utils",
    "MetricPoint": "otto.models.monitor",
    "MetricRecord": "otto.models.monitor",
    "MonitorExport": "otto.models.monitor",
    "MonitorMeta": "otto.models.monitor",
    "SessionMeta": "otto.models.monitor",
    "SessionRecord": "otto.models.monitor",
    "TabSpec": "otto.models.monitor",
    "TabSpecRecord": "otto.models.monitor",
    "TunnelRecord": "otto.models.monitor",
    "validate_interval": "otto.utils",
    "FtpOptionsSpec": "otto.models.options",
    "NcOptionsSpec": "otto.models.options",
    "ScpOptionsSpec": "otto.models.options",
    "SftpOptionsSpec": "otto.models.options",
    "SnmpOptionsSpec": "otto.models.options",
    "SshOptionsSpec": "otto.models.options",
    "TelnetOptionsSpec": "otto.models.options",
    "TftpOptionsSpec": "otto.models.options",
    "DockerComposeSpec": "otto.models.settings",
    "DockerImageSpec": "otto.models.settings",
    "DockerSettingsSpec": "otto.models.settings",
    "OsProfileSpec": "otto.models.settings",
    "OttoEnvSettings": "otto.models.settings",
    "ReservationConfigSpec": "otto.models.settings",
    "ReservationEntry": "otto.models.settings",
    "ReservationFile": "otto.models.settings",
    "SettingsModel": "otto.models.settings",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.models' public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


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
    "TunnelRecord",
    "UnixHostSpec",
    "validate_interval",
]
