"""Replacing the built-in power controller reaches the host factory.

``create_host_from_dict`` is what the lab loader calls, and a host's
construction runs ``power_control_from_spec``; a controller registered over
``command`` with ``overwrite=True`` must be what the host ends up holding.
The root isolation fixture restores the table.
"""

from otto.host import CommandPowerConfig
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.power import POWER_CONTROLLERS, CommandPowerController, register_power_controller

REPLACES = [("otto.host.power:POWER_CONTROLLERS", "command")]
"""Every ``(table, built-in name)`` pair this module replaces."""


class _Spy(CommandPowerController):
    pass


def test_replaces_names_every_built_in_power_controller():
    builtins = {
        ("otto.host.power:POWER_CONTROLLERS", name)
        for name in POWER_CONTROLLERS.names()
        if POWER_CONTROLLERS.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


def test_a_replaced_command_controller_is_what_the_host_holds():
    register_power_controller(
        "command", config=CommandPowerConfig, factory=lambda c: _Spy(c.config), overwrite=True
    )
    host = create_host_from_dict(
        {
            "ip": "10.0.0.11",
            "creds": [{"login": "u", "password": "p"}],
            "os_type": "unix",
            "power_control": {"type": "command", "on_cmd": "on", "off_cmd": "off"},
        },
        element=Element("ne"),
    )
    assert type(host.power_control) is _Spy
