"""
Pluggable *power control* strategy for hosts.

Powering a host on or off cannot run *on* the host — it's off. A
:class:`PowerController` embodies *where* control happens: it runs commands on a
designated controller host (a hypervisor, a jump host with ``ipmitool``, a PDU
controller) resolved via the target's lab back-reference, or talks to an
external API. otto ships :class:`CommandPowerController` (the generic
command-based backend); projects register richer ones (IPMI/redfish/libvirt/
cloud/PDU) via :func:`register_power_controller` — a configured backend: a
config model parses the host's ``power_control`` table and a factory builds
the controller with the host's id (:class:`PowerEnv`).
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import ConfigDict
from typing_extensions import override

from ..errors import OttoError
from ..models.base import OttoModel
from ..registry import (
    BackendRegistry,
    C,
    Configured,
    Ref,
    configured_backend,
    registration_boundary,
)
from ..result import Result

if TYPE_CHECKING:
    from ..result import CommandResult
    from .host import Host


class PowerControlError(OttoError):
    """A host's power control could not be set up or could not act."""


class PowerConstructionError(PowerControlError, ValueError):
    """A host's ``power_control`` names an unknown controller, or does not parse or build.

    The message names the controller, the module that registered it, the host
    the table came from and the stage that failed (lookup, parse,
    construction or result). A :class:`ValueError`, because it is a
    configuration error.
    """


@dataclass(frozen=True)
class PowerEnv:
    """The environment otto hands a power controller.

    The config model receives it as ``context["env"]`` when a host's
    ``power_control`` table is parsed, and the factory as ``Configured.env``.
    """

    host_id: str
    """The id of the host whose ``power_control`` this is."""


class CommandPowerConfig(OttoModel):
    """What :class:`CommandPowerController` runs, and where.

    Commands are ``str.format``-templated with the target host's ``name``,
    ``ip`` and ``id``.
    """

    model_config = ConfigDict(frozen=True)

    on_cmd: str
    """The command that powers the host on."""

    off_cmd: str
    """The command that powers the host off."""

    status_cmd: str | None = None
    """The command whose output reports the power state; ``None`` when there is none."""

    status_on: str = ""
    """The text in ``status_cmd``'s output that means the host is on."""

    controller: str | None = None
    """The lab host the commands run on; ``None`` runs them on the local otto machine."""


class PowerState(Enum):
    """A host's power state as reported by a controller."""

    ON = "on"
    OFF = "off"


class PowerController(ABC):
    """How to power a host on/off from somewhere the host can be reached.

    The host to act on is passed to every call, so one controller object
    holds only its configuration.
    """

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


class CommandPowerController(PowerController):
    """Generic controller that runs configured commands on a *controller* host.

    Its :class:`CommandPowerConfig` holds the commands,
    ``str.format``-templated with the target host's ``name``/``ip``/``id``, and
    ``controller``, the lab host the commands run on (via its ``exec``);
    ``None`` runs them on the local otto machine.
    """

    def __init__(self, config: CommandPowerConfig) -> None:
        """Hold *config*; nothing runs until a power verb is called."""
        self.config = config

    @override
    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self.config == other.config

    @override
    def __hash__(self) -> int:
        return hash((type(self), self.config))

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.config!r})"

    async def _runner(self, host: "Host") -> "Host":
        controller = self.config.controller
        if controller is None:
            from .local_host import LocalHost

            return LocalHost()
        lab = getattr(host, "_lab", None)
        if lab is None:
            from ..invocation import installed_resolver

            lab = installed_resolver()
        if lab is None or controller not in lab.hosts:
            raise ValueError(
                f"power controller for {host.name!r}: controller host "
                f"{controller!r} not found in the lab"
            )
        return lab.hosts[controller]

    def _fmt(self, template: str, host: "Host") -> str:
        return template.format(name=host.name, ip=getattr(host, "ip", ""), id=host.id)

    async def _exec(self, template: str, host: "Host") -> "CommandResult":
        runner = await self._runner(host)
        return await runner.exec(self._fmt(template, host))

    @override
    async def on(self, host: "Host") -> Result:
        cmd_result = await self._exec(self.config.on_cmd, host)
        return Result(cmd_result.status, msg=cmd_result.value)

    @override
    async def off(self, host: "Host") -> Result:
        cmd_result = await self._exec(self.config.off_cmd, host)
        return Result(cmd_result.status, msg=cmd_result.value)

    @override
    async def status(self, host: "Host") -> PowerState | None:
        if self.config.status_cmd is None:
            return None
        cmd_result = await self._exec(self.config.status_cmd, host)
        return PowerState.ON if self.config.status_on in cmd_result.value else PowerState.OFF


def _command_power(c: Configured[CommandPowerConfig, PowerEnv]) -> CommandPowerController:
    """Build the ``command`` controller from its parsed table."""
    return CommandPowerController(c.config)


def _describe_parse_error(exc: Exception) -> str:
    """Describe a ``power_control`` table that failed to parse, never quoting the rejected value."""
    # Imported at call time, where the other seams' describers import them.
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    if isinstance(exc, ValidationError):
        return compact_validation_error(exc)
    return f"{type(exc).__name__} (its message is not shown: it may quote the rejected value)"


def _check_power_result(name: str, obj: object) -> None:
    """Refuse a built object that is not a :class:`PowerController`."""
    if not isinstance(obj, PowerController):
        raise TypeError(
            f"power controller {name!r} built a {type(obj).__name__}, "
            "not an otto.host.power.PowerController"
        )


POWER_CONTROLLERS: "BackendRegistry[PowerEnv, PowerController, None]" = BackendRegistry(
    "power controller",
    register_hint="otto.host.register_power_controller()",
    error=PowerConstructionError,
    describe_parse_error=_describe_parse_error,
    result=_check_power_result,
)
"""Every power controller, by the ``type`` a host's ``power_control`` names."""

POWER_CONTROLLERS.register(
    "command",
    configured_backend(
        config=Ref("otto.host.power:CommandPowerConfig"),
        factory=Ref("otto.host.power:_command_power"),
        metadata=None,
    ),
)


@registration_boundary
def register_power_controller(
    type_name: str,
    *,
    config: "type[C] | Ref",
    factory: "Callable[[Configured[C, PowerEnv]], PowerController] | Ref",
    overwrite: bool = False,
) -> None:
    """Make a power controller selectable as ``power_control = {type = "<type_name>", ...}``.

    *config* is the model that parses the table's keys (every key but
    ``type``): otto calls its ``model_validate(table, context={"env": env})``
    once per host, with a :class:`PowerEnv` naming the host as *env*. The
    parsed configuration must be deep-copyable. *factory* receives
    ``Configured(config, env)`` and returns the :class:`PowerController`.
    Either may be a :class:`~otto.registry.Ref` (``"module:attr"``),
    imported at first use.

    *overwrite* replaces an existing registration under *type_name*
    deliberately (e.g. a built-in); by default a duplicate name raises.

    Raises:
        otto.registry.DuplicateRegistration: If *type_name* is taken and *overwrite* is false.
    """
    POWER_CONTROLLERS.register(
        type_name,
        configured_backend(config=config, factory=factory, metadata=None),
        overwrite=overwrite,
    )


def build_power_controller(
    type_name: str, config: "Mapping[str, object]", *, host_id: str
) -> PowerController:
    """Prepare and build the controller registered under *type_name* for host *host_id*.

    *config* is the ``power_control`` table without its ``type`` key.

    Raises:
        PowerConstructionError: *type_name* is not registered, *config* does not
            parse, the factory fails, or it builds something that is not a
            :class:`PowerController`.
    """
    return POWER_CONTROLLERS.build(
        POWER_CONTROLLERS.prepare(
            type_name, config, PowerEnv(host_id), source=f"power_control of host {host_id!r}"
        )
    )


def power_control_from_spec(value: object, *, host_id: str) -> PowerController | None:
    """Coerce a lab-data value into a :class:`PowerController` instance for host *host_id*.

    Accepts ``None`` / an existing instance (pass-through), a ``str`` (a
    config-free controller type name), or a ``dict`` with a ``type`` key plus
    the controller's table.

    Raises:
        PowerConstructionError: The controller cannot be prepared or built,
            or a ``dict`` has no ``type`` key.
        ValueError: *value* is none of those shapes.
    """
    if value is None or isinstance(value, PowerController):
        return value
    if isinstance(value, str):
        return build_power_controller(value, {}, host_id=host_id)
    if isinstance(value, dict):
        table = dict(value)
        if "type" not in table:
            raise PowerConstructionError(
                f"power_control of host {host_id!r} has no 'type' key naming its controller "
                f"(registered: {', '.join(sorted(POWER_CONTROLLERS.names()))})"
            )
        type_name = table.pop("type")
        return build_power_controller(type_name, table, host_id=host_id)
    raise ValueError(f"cannot build a PowerController from {value!r}")
