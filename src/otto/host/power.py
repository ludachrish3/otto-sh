"""
Pluggable *power control* strategy for hosts.

Powering a host on or off cannot run *on* the host — it's off. A
:class:`PowerController` embodies *where* control happens: it runs commands on a
designated controller host (a hypervisor, a jump host with ``ipmitool``, a PDU
controller) resolved via the target's lab back-reference, or talks to an
external API. otto ships :class:`CommandPowerController` (the generic
command-based backend); projects register richer ones (IPMI/redfish/libvirt/
cloud/PDU) via :func:`register_power_controller`, the same extension hook
:func:`otto.host.binary_loader.register_binary_loader` uses.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from typing_extensions import override

from ..registry import Registry, caller_module
from ..result import Result

if TYPE_CHECKING:
    from ..result import CommandResult
    from .host import Host


class PowerState(Enum):
    """A host's power state as reported by a controller."""

    ON = "on"
    OFF = "off"


class PowerController(ABC):
    """How to power a host on/off from somewhere the host can be reached."""

    type_name: ClassVar[str]
    """Lab-data string for this controller (e.g. ``'command'``)."""

    @abstractmethod
    async def on(self, host: "Host") -> Result:
        """Power *host* on. ``msg`` on the returned Result carries diagnostics."""
        ...

    @abstractmethod
    async def off(self, host: "Host") -> Result:
        """Power *host* off. ``msg`` on the returned Result carries diagnostics."""
        ...

    async def cycle(self, host: "Host") -> Result:
        """Power-cycle *host*: off, then on. Override for a native reset."""
        off_result = await self.off(host)
        if not off_result.is_ok:
            return off_result
        return await self.on(host)

    async def status(self, host: "Host") -> PowerState | None:  # noqa: ARG002 — required by PowerController protocol signature; subclasses use host, this default does not
        """Return the current power state, or ``None`` when this controller can't report it."""
        return None


@dataclass(slots=True)
class CommandPowerController(PowerController):
    """Generic controller that runs configured commands on a *controller* host.

    Commands are ``str.format``-templated with the target host's ``name``/``ip``/
    ``id``. ``controller`` names the lab host the commands run on (via its
    ``oneshot``); ``None`` runs them on the local otto machine.
    """

    type_name: ClassVar[str] = "command"

    on_cmd: str
    off_cmd: str
    status_cmd: str | None = None
    status_on: str = ""
    controller: str | None = None

    async def _runner(self, host: "Host") -> "Host":
        if self.controller is None:
            from .local_host import LocalHost

            return LocalHost()
        lab = getattr(host, "_lab", None)
        if lab is None:
            from ..context import try_get_context

            ctx = try_get_context()
            lab = ctx.lab if ctx is not None else None
        if lab is None or self.controller not in lab.hosts:
            raise ValueError(
                f"power controller for {host.name!r}: controller host "
                f"{self.controller!r} not found in the lab"
            )
        return lab.hosts[self.controller]

    def _fmt(self, template: str, host: "Host") -> str:
        return template.format(name=host.name, ip=getattr(host, "ip", ""), id=host.id)

    async def _exec(self, template: str, host: "Host") -> "CommandResult":
        runner = await self._runner(host)
        return await runner.oneshot(self._fmt(template, host))

    @override
    async def on(self, host: "Host") -> Result:
        cmd_result = await self._exec(self.on_cmd, host)
        return Result(cmd_result.status, msg=cmd_result.value)

    @override
    async def off(self, host: "Host") -> Result:
        cmd_result = await self._exec(self.off_cmd, host)
        return Result(cmd_result.status, msg=cmd_result.value)

    @override
    async def status(self, host: "Host") -> PowerState | None:
        if self.status_cmd is None:
            return None
        cmd_result = await self._exec(self.status_cmd, host)
        return PowerState.ON if self.status_on in cmd_result.value else PowerState.OFF


POWER_CONTROLLERS: Registry[type[PowerController]] = Registry(
    "power controller", register_hint="otto.host.power.register_power_controller()"
)


def register_power_controller(
    type_name: str, cls: type[PowerController], *, overwrite: bool = False
) -> None:
    """Register a :class:`PowerController` subclass for lab-data selection.

    *overwrite* replaces an existing registration under *type_name*
    deliberately (e.g. a built-in); by default a duplicate name raises.
    """
    if cls.type_name != type_name:
        raise ValueError(
            f"register_power_controller: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    POWER_CONTROLLERS.register(type_name, cls, overwrite=overwrite, origin=caller_module())


def build_power_controller(type_name: str) -> type[PowerController]:
    """Return the :class:`PowerController` class registered under *type_name*."""
    return POWER_CONTROLLERS.get(type_name)


def power_control_from_spec(value: Any) -> PowerController | None:
    """Coerce a lab-data value into a :class:`PowerController` instance.

    Accepts ``None`` / an existing instance (pass-through), a ``str`` (a
    config-free controller type name), or a ``dict`` with a ``type`` key plus
    the controller's constructor kwargs.
    """
    if value is None or isinstance(value, PowerController):
        return value
    if isinstance(value, str):
        return build_power_controller(value)()
    if isinstance(value, dict):
        cfg = dict(value)
        type_name = cfg.pop("type")
        return build_power_controller(type_name)(**cfg)
    raise ValueError(f"cannot build a PowerController from {value!r}")


def _register_builtin_power_controllers() -> None:
    register_power_controller(CommandPowerController.type_name, CommandPowerController)


_register_builtin_power_controllers()
