"""Replacing a built-in host class reaches the host factory.

Each built-in host class name is replaced with ``overwrite=True`` by a
recording subclass of its own class, and a host dict selecting that name is
built through :func:`~otto.host.factory.create_host_from_dict`, the function
the lab loader calls; the replacement must be what comes back. The root
isolation fixture restores the table afterwards.
"""

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.os_profile import HOST_CLASSES, register_host_class
from otto.registry import resolved

_NAMES = ["unix", "embedded", "zephyr"]

REPLACES = [("otto.host.os_profile:HOST_CLASSES", n) for n in _NAMES]
"""Every ``(table, built-in name)`` pair this module replaces."""

_DICTS = {
    "unix": {"ip": "10.0.0.11", "creds": [{"login": "u", "password": "p"}]},
    "embedded": {"ip": "192.0.2.11", "command_frame": "zephyr"},
    "zephyr": {"ip": "192.0.2.12"},
}


def test_replaces_names_every_built_in_host_class():
    builtins = {
        ("otto.host.os_profile:HOST_CLASSES", name)
        for name in HOST_CLASSES.names()
        if HOST_CLASSES.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


@pytest.mark.parametrize("name", _NAMES)
def test_a_replaced_host_class_is_what_the_factory_builds(name):
    entry = HOST_CLASSES.get(name)
    base = resolved(entry.cls)
    recording = type(f"Recording{base.__name__}", (base,), {})
    register_host_class(name, recording, spec=resolved(entry.spec), overwrite=True)

    host = create_host_from_dict({**_DICTS[name], "os_type": name}, element=Element("ne"))

    assert type(host) is recording, f"the factory bypassed the registered {name!r} class"
