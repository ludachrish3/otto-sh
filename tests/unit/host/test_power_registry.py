"""Power controllers are prepared from a config model and built with the host's id."""

import pytest

from otto.host import CommandPowerConfig, PowerConstructionError, PowerEnv
from otto.host.power import (
    POWER_CONTROLLERS,
    CommandPowerController,
    build_power_controller,
    power_control_from_spec,
    register_power_controller,
)
from otto.models.base import OttoModel


def test_the_command_controller_is_built_from_its_config_model():
    ctl = power_control_from_spec(
        {"type": "command", "on_cmd": "on {id}", "off_cmd": "off"}, host_id="h1"
    )
    assert isinstance(ctl, CommandPowerController)
    assert ctl.config == CommandPowerConfig(on_cmd="on {id}", off_cmd="off")


def test_an_unknown_key_is_a_parse_error_that_does_not_echo_its_value():
    with pytest.raises(PowerConstructionError, match="parse") as info:
        power_control_from_spec(
            {"type": "command", "on_cmd": "x", "off_cmd": "y", "pasword": "s3cr3t-value"},
            host_id="h1",
        )
    assert "s3cr3t-value" not in str(info.value)


def test_a_custom_controller_receives_the_host_id():
    class Cfg(OttoModel, frozen=True):
        outlet: int

    seen = []
    register_power_controller(
        "pdu",
        config=Cfg,
        factory=lambda c: (
            seen.append(c.env) or CommandPowerController(CommandPowerConfig(on_cmd="", off_cmd=""))
        ),
    )
    build_power_controller("pdu", {"outlet": 3}, host_id="rack-1")
    assert seen == [PowerEnv(host_id="rack-1")]


def test_a_factory_mutating_its_config_cannot_change_the_next_build():
    class Cfg(OttoModel):  # deliberately not frozen
        tags: list[str] = []  # noqa: RUF012 — a pydantic field: each instance gets a copy

    def factory(c):
        c.config.tags.append("x")
        return CommandPowerController(
            CommandPowerConfig(on_cmd=str(len(c.config.tags)), off_cmd="")
        )

    register_power_controller("mut", config=Cfg, factory=factory)
    prepared = POWER_CONTROLLERS.prepare("mut", {}, PowerEnv("h"))
    assert POWER_CONTROLLERS.build(prepared).config.on_cmd == "1"
    assert POWER_CONTROLLERS.build(prepared).config.on_cmd == "1"


def test_none_and_an_instance_pass_through():
    ctl = CommandPowerController(CommandPowerConfig(on_cmd="", off_cmd=""))
    assert power_control_from_spec(None, host_id="h") is None
    assert power_control_from_spec(ctl, host_id="h") is ctl


# -- the hosts pass their own id ------------------------------------------------


def _spy_over_command(seen: list):
    register_power_controller(
        "command",
        config=CommandPowerConfig,
        factory=lambda c: seen.append(c.env) or CommandPowerController(c.config),
        overwrite=True,
    )


_TABLE = {"type": "command", "on_cmd": "on", "off_cmd": "off"}


def test_a_unix_host_passes_its_id_to_power_construction():
    from otto.host.element import Element
    from otto.host.login_proxy import Cred
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode

    seen: list = []
    _spy_over_command(seen)
    host = UnixHost(
        ip="10.0.0.1",
        element=Element("Box_1"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        power_control=dict(_TABLE),
    )
    # The element's slug makes the id differ from the name, so passing the
    # name instead of the id is caught.
    assert (host.id, host.name) == ("box-1", "Box_1")
    assert seen == [PowerEnv(host_id="box-1")]


def test_a_zephyr_host_passes_its_id_to_power_construction():
    from otto.host.element import Element
    from otto.host.embedded_host import ZephyrHost
    from otto.logger.mode import LogMode

    seen: list = []
    _spy_over_command(seen)
    host = ZephyrHost(
        ip="192.0.2.1",
        element=Element("zephyr37_fat"),
        log=LogMode.QUIET,
        power_control=dict(_TABLE),
    )
    assert seen == [PowerEnv(host_id=host.id)]
    assert host.id


def test_a_table_without_a_type_names_the_host():
    with pytest.raises(
        PowerConstructionError, match=r"power_control of host 'rack-1' has no 'type' key"
    ) as info:
        power_control_from_spec({"on_cmd": "on", "off_cmd": "off"}, host_id="rack-1")
    assert "command" in str(info.value)
