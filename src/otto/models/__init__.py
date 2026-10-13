"""Pydantic boundary models — the validation layer for external data (lab JSON, settings, env).

The lab, settings, inventory and monitor records live here. The host specs and
the per-protocol option specs live in ``otto.host`` (``otto.host.specs``,
``otto.host.option_specs``), beside the hosts they build; this package still
exports ``ToolchainSpec`` and the eight ``*OptionsSpec`` classes. What both
packages need (``OttoModel``, ``CredSpec``) is in the leaf ``otto.models.base``.
Each model mirroring a runtime type carries the ``Spec`` suffix.

Every name is exported lazily (PEP 562): ``from otto.models import CredSpec``
imports ``otto.models.base`` alone, not the monitor records or the settings
models. The settings models matter most: ``OttoEnvSettings`` subclasses
``pydantic_settings.BaseSettings``, so an eager import would put
pydantic_settings and dotenv on every ``otto ... --help`` path, though only
commands that resolve settings need them. The resolver does not write a
resolved name back into the module dict; see ``otto.config``'s ``__dir__``
for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..host.option_specs import FtpOptionsSpec as FtpOptionsSpec
    from ..host.option_specs import NcOptionsSpec as NcOptionsSpec
    from ..host.option_specs import ScpOptionsSpec as ScpOptionsSpec
    from ..host.option_specs import SftpOptionsSpec as SftpOptionsSpec
    from ..host.option_specs import SnmpOptionsSpec as SnmpOptionsSpec
    from ..host.option_specs import SshOptionsSpec as SshOptionsSpec
    from ..host.option_specs import TelnetOptionsSpec as TelnetOptionsSpec
    from ..host.option_specs import TftpOptionsSpec as TftpOptionsSpec
    from ..host.specs import ToolchainSpec as ToolchainSpec
    from ..utils import MIN_INTERVAL_SECONDS as MIN_INTERVAL_SECONDS
    from ..utils import validate_interval as validate_interval
    from .base import CredSpec as CredSpec
    from .base import OttoModel as OttoModel
    from .inventory import FILLABLE_INVENTORY_FIELDS as FILLABLE_INVENTORY_FIELDS
    from .inventory import INVENTORY_KEY_FIELDS as INVENTORY_KEY_FIELDS
    from .inventory import SUPPLIES_EXEMPT_FIELDS as SUPPLIES_EXEMPT_FIELDS
    from .inventory import InventoryRecord as InventoryRecord
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
    "CredSpec": "otto.models.base",
    "ToolchainSpec": "otto.host.specs",
    "FILLABLE_INVENTORY_FIELDS": "otto.models.inventory",
    "INVENTORY_KEY_FIELDS": "otto.models.inventory",
    "InventoryRecord": "otto.models.inventory",
    "SUPPLIES_EXEMPT_FIELDS": "otto.models.inventory",
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
    "FtpOptionsSpec": "otto.host.option_specs",
    "NcOptionsSpec": "otto.host.option_specs",
    "ScpOptionsSpec": "otto.host.option_specs",
    "SftpOptionsSpec": "otto.host.option_specs",
    "SnmpOptionsSpec": "otto.host.option_specs",
    "SshOptionsSpec": "otto.host.option_specs",
    "TelnetOptionsSpec": "otto.host.option_specs",
    "TftpOptionsSpec": "otto.host.option_specs",
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
    "FILLABLE_INVENTORY_FIELDS",
    "INVENTORY_KEY_FIELDS",
    "MIN_INTERVAL_SECONDS",
    "SUPPLIES_EXEMPT_FIELDS",
    "ChartSpec",
    "ChartSpecRecord",
    "CredSpec",
    "DockerComposeSpec",
    "DockerImageSpec",
    "DockerSettingsSpec",
    "ElementRecord",
    "EventRecord",
    "FtpOptionsSpec",
    "HostSnapshot",
    "InventoryRecord",
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
    "validate_interval",
]
