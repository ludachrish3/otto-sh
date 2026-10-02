"""Built-ins register by reference, in the module that defines their registry.

Each registry's built-in entries are :class:`~otto.registry.Ref` values
registered beside the registry itself, so importing a registry lists its
built-ins without importing a single implementation, and a lookup imports only
the one it names (docs/architecture/subsystems/registries.md, "Built-ins").

Two guards hold that shape:

- every reference resolves (a typo or rename in a target fails here, not at a
  user's first lookup);
- in a fresh interpreter, importing ONLY a registry's defining module yields
  its full built-in name set (a built-in still registered by an implementation
  module's import side effect is missing there).
"""

import dataclasses
import json
import logging
import subprocess
import sys

import pytest

from otto import registry as reg
from tests.e2e._otto_subprocess import PROJECT_ROOT, coverage_subprocess_env
from tests.unit.test_registry_loading import (
    _import_every_module_that_builds_a_registry,
    _otto_registries,
)


@dataclasses.dataclass(frozen=True)
class Builtins:
    """One registry's built-ins, as its defining module alone must provide them."""

    module: str
    """The module that defines the registry."""

    attr: str
    """The registry's attribute name in that module."""

    names: set[str]
    """Every built-in name the registry lists on import."""

    by_reference: bool = True
    """Whether each built-in is a :class:`~otto.registry.Ref` until first looked up.

    False for the registries whose values are built beside the registry
    itself (an ``OsProfile``, a ``LoginProxy``, an ``SnmpMetric``, a
    ``TermBackend`` over ``ConnectionManager``, which the registry's own module
    defines): there is no implementation module for a reference to defer.
    """


BUILTINS: list[Builtins] = [
    Builtins("otto.host.binary_loader", "LOADER_CLASSES", {"llext-hex"}),
    Builtins("otto.host.os_profile", "HOST_CLASSES", {"unix", "embedded", "zephyr"}),
    Builtins(
        "otto.host.os_profile",
        "OS_PROFILES",
        {"unix", "embedded", "zephyr", "busybox"},
        by_reference=False,
    ),
    Builtins("otto.host.login_proxy", "LOGIN_PROXIES", {"su"}, by_reference=False),
    Builtins(
        "otto.host.embedded_filesystem", "FILESYSTEM_CLASSES", {"none", "fat-ram", "littlefs"}
    ),
    Builtins(
        "otto.host.connections", "TERM_BACKENDS", {"ssh", "telnet", "console"}, by_reference=False
    ),
    Builtins("otto.host.power", "POWER_CONTROLLERS", {"command"}),
    Builtins(
        "otto.host.command_frame",
        "FRAME_CLASSES",
        {"bash", "ash", "zephyr", "zephyr-serial", "raw"},
    ),
    Builtins(
        "otto.host.transfer.registry",
        "TRANSFER_BACKENDS",
        {"ftp", "scp", "tftp", "nc", "sftp", "shell", "console"},
    ),
    Builtins("otto.host.product", "PRODUCT_KINDS", {"kmod", "embedded", "docker_image", "shell"}),
    Builtins("otto.host.dev_tool", "DEV_TOOL_KINDS", {"shell", "kmod", "kmodcov"}),
    Builtins("otto.inventory.registry", "INVENTORY_BACKENDS", {"json", "netbox"}),
    Builtins("otto.creds.registry", "CREDS_BACKENDS", {"json"}),
    Builtins("otto.labs.registry", "LAB_REPOSITORIES", {"json"}),
    Builtins("otto.reservations.registry", "RESERVATION_BACKENDS", {"none", "json"}),
    Builtins("otto.link.impairer", "IMPAIRERS", {"netem"}),
    Builtins("otto.tunnel.carrier", "CARRIERS", {"socat"}),
    Builtins(
        "otto.monitor.snmp",
        "SNMP_METRICS",
        {
            "1.3.6.1.2.1.1.3.0",
            "1.3.6.1.4.1.63245.1.1.0",
            "1.3.6.1.4.1.63245.1.2.0",
            "1.3.6.1.4.1.63245.1.3.0",
            "1.3.6.1.4.1.63245.1.4.0",
        },
        by_reference=False,
    ),
]

# Registries whose otto-registered entries are not built-ins in this sense:
# CLI_COMMANDS already holds lazy ``"module:attr"`` loaders in each CommandSpec,
# and PROJECT_INSTRUCTIONS is filled by introspecting ProjectActions' own
# methods, so a reference would point back at the class being introspected.
OUT_OF_SCOPE: set[tuple[str, str]] = {
    ("otto.cli.registry", "CLI_COMMANDS"),
    ("otto.instructions", "PROJECT_INSTRUCTIONS"),
}


def _run_python(code: str) -> object:
    """Run *code* in a fresh interpreter and return the JSON it prints last."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=coverage_subprocess_env(),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_every_builtin_reference_resolves():
    """Import every otto registry, then resolve every entry.

    A typo or rename in a Ref fails here, not at a user's first lookup. Every
    by-reference built-in must resolve to something callable (a class or a
    factory), so a target naming the wrong kind of attribute fails too. The
    host-class spec table beside ``HOST_CLASSES`` holds references as well, so
    it is resolved the same way.
    """
    import importlib

    from otto.host.os_profile import registered_host_specs
    from otto.models.host import HostSpec

    assert _import_every_module_that_builds_a_registry(), "the source scan found no registries"
    registries = _otto_registries()
    assert registries, "no otto registries found"
    for registry in registries:
        for name, entry in registry.items():
            assert not isinstance(entry, reg.Ref), (registry.defined_in, name, entry)
    for builtins in BUILTINS:
        if not builtins.by_reference:
            continue
        registry = getattr(importlib.import_module(builtins.module), builtins.attr)
        for name in builtins.names:
            entry = registry.get(name)
            assert callable(entry), (builtins.module, builtins.attr, name, entry)
    for name, spec in registered_host_specs().items():
        assert isinstance(spec, type), (name, spec)
        assert issubclass(spec, HostSpec), (name, spec)


_OTTO_ORIGIN_NAMES = """
import json, sys
from tests.unit.test_registry_loading import (
    _import_every_module_that_builds_a_registry, _otto_registries,
)
_import_every_module_that_builds_a_registry()
out = {}
for r in _otto_registries():
    attrs = [n for n, v in vars(sys.modules[r.defined_in]).items() if v is r]
    names = [n for n, _e, o in r._raw_items() if o == "otto" or o.startswith("otto.")]
    if names:
        out[r.defined_in + ":" + ",".join(attrs)] = sorted(names)
print(json.dumps(out))
"""


def test_the_builtin_table_names_every_registry_with_builtins():
    """:data:`BUILTINS` cannot drift from what otto really registers.

    In a fresh interpreter (so no earlier test's registrations count), every
    otto registry holding an entry whose origin is an ``otto`` module is
    either in the table with exactly those names, or named out of scope.
    """
    seen = _run_python(_OTTO_ORIGIN_NAMES)
    assert isinstance(seen, dict)
    for module, attr in OUT_OF_SCOPE:
        seen.pop(f"{module}:{attr}", None)
    assert seen == {f"{b.module}:{b.attr}": sorted(b.names) for b in BUILTINS}


@pytest.mark.parametrize("builtins", BUILTINS, ids=[f"{b.module}:{b.attr}" for b in BUILTINS])
def test_each_registry_lists_its_builtins_before_any_implementation_is_imported(builtins):
    """A fresh interpreter importing ONLY the defining module lists every built-in.

    A built-in that still relies on an import side effect is missing here.
    While a package ``__init__`` still imports its implementation modules
    eagerly, that import would hide a missing name, so a by-reference
    registry must ALSO hold each built-in as an unresolved ``Ref``: an
    implementation module registering its own class leaves a real object
    there instead.
    """
    listing = _run_python(
        "import importlib, json\n"
        "from otto.registry import Ref\n"
        f"r = getattr(importlib.import_module({builtins.module!r}), {builtins.attr!r})\n"
        "print(json.dumps({n: isinstance(e, Ref) for n, e, _o in r._raw_items()}))\n"
    )
    assert isinstance(listing, dict)
    assert set(listing) == builtins.names
    if builtins.by_reference:
        assert [name for name, is_ref in listing.items() if not is_ref] == []


def test_plugin_overrides_a_referenced_builtin_host_class_with_a_warning(caplog):
    """A plugin's real class replaces the ``unix`` reference, and the override still warns."""
    from otto.host.os_profile import HOST_CLASSES, build_host_spec, register_host_class
    from otto.host.unix_host import UnixHost
    from otto.models.host import UnixHostSpec

    class PluginUnixHost(UnixHost):
        pass

    with caplog.at_level(logging.WARNING, logger="otto.host.os_profile"):
        register_host_class("unix", PluginUnixHost)
    assert any(
        "overriding built-in host class 'unix'" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    assert HOST_CLASSES.get("unix") is PluginUnixHost
    assert build_host_spec("unix") is UnixHostSpec
