"""Replacing a built-in class seam entry reaches the real product path.

Every built-in name of every class seam is replaced with ``overwrite=True``
and built through the function otto itself calls; the replacement must be
what comes back. The root isolation fixture restores each table afterwards.
"""

import pytest

from otto.host.binary_loader import build_binary_loader, register_binary_loader
from otto.host.command_frame import build_command_frame, register_command_frame
from otto.host.embedded_filesystem import build_filesystem, register_filesystem
from otto.link.impairer import build_impairer, register_impairer
from otto.tunnel.carrier import build_carrier, register_carrier

_FRAMES = ["bash", "ash", "zephyr", "zephyr-serial", "raw"]
_LOADERS = ["llext-hex"]
_FILESYSTEMS = ["none", "fat-ram", "littlefs"]
_IMPAIRERS = ["netem"]
_CARRIERS = ["socat"]

REPLACES = (
    [("otto.host.command_frame:FRAME_CLASSES", n) for n in _FRAMES]
    + [("otto.host.binary_loader:LOADER_CLASSES", n) for n in _LOADERS]
    + [("otto.host.embedded_filesystem:FILESYSTEM_CLASSES", n) for n in _FILESYSTEMS]
    + [("otto.link.impairer:IMPAIRERS", n) for n in _IMPAIRERS]
    + [("otto.tunnel.carrier:CARRIERS", n) for n in _CARRIERS]
)
"""Every ``(table, built-in name)`` pair this module replaces."""


def test_replaces_names_every_built_in_class_seam_entry():
    from otto.host.binary_loader import LOADER_CLASSES
    from otto.host.command_frame import FRAME_CLASSES
    from otto.host.embedded_filesystem import FILESYSTEM_CLASSES
    from otto.link.impairer import IMPAIRERS
    from otto.tunnel.carrier import CARRIERS

    live = {
        f"{t.defined_in}:{attr}": t
        for attr, t in [
            ("FRAME_CLASSES", FRAME_CLASSES),
            ("LOADER_CLASSES", LOADER_CLASSES),
            ("FILESYSTEM_CLASSES", FILESYSTEM_CLASSES),
            ("IMPAIRERS", IMPAIRERS),
            ("CARRIERS", CARRIERS),
        ]
    }
    builtins = {
        (key, name)
        for key, table in live.items()
        for name in table.names()
        if table.origin(name).startswith("otto.")
    }
    assert set(REPLACES) == builtins


@pytest.mark.parametrize("name", _FRAMES)
def test_a_replaced_frame_is_what_build_command_frame_builds(name):
    base = type(build_command_frame(name))
    replacement = type("Replacement", (base,), {"type_name": name})
    register_command_frame(name, replacement, overwrite=True)
    assert type(build_command_frame(name)) is replacement


@pytest.mark.parametrize("name", _LOADERS)
def test_a_replaced_loader_is_what_build_binary_loader_builds(name):
    base = type(build_binary_loader(name))
    replacement = type("Replacement", (base,), {"type_name": name})
    register_binary_loader(name, replacement, overwrite=True)
    assert type(build_binary_loader(name)) is replacement


@pytest.mark.parametrize("name", _FILESYSTEMS)
def test_a_replaced_filesystem_is_what_build_filesystem_builds(name):
    base = type(build_filesystem(name))
    replacement = type("Replacement", (base,), {"type_name": name})
    register_filesystem(name, replacement, overwrite=True)
    assert type(build_filesystem(name)) is replacement


@pytest.mark.parametrize("name", _IMPAIRERS)
def test_a_replaced_impairer_is_what_build_impairer_returns(name):
    replacement = type("Replacement", (build_impairer(name),), {})
    register_impairer(name, replacement, overwrite=True)
    assert build_impairer(name) is replacement


@pytest.mark.parametrize("name", _CARRIERS)
def test_a_replaced_carrier_is_what_build_carrier_returns(name):
    replacement = type("Replacement", (build_carrier(name),), {})
    register_carrier(name, replacement, overwrite=True)
    assert build_carrier(name) is replacement
