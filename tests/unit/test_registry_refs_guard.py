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
    itself (a ``LoginProxy``, an ``SnmpMetric``, a term
    backend's ``ConnectionManager``, which the registry's own module defines):
    there is no implementation module for a reference to defer.
    """


BUILTINS: list[Builtins] = [
    Builtins("otto.host.binary_loader", "LOADER_CLASSES", {"llext-hex"}),
    Builtins("otto.host.os_profile", "HOST_CLASSES", {"unix", "embedded", "zephyr"}),
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
# CLI_COMMANDS already holds lazy ``"module:attr"`` loaders in each CommandSpec.
OUT_OF_SCOPE: set[tuple[str, str]] = {
    ("otto.cli.registry", "CLI_COMMANDS"),
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
    factory), so a target naming the wrong kind of attribute fails too. A
    ``HOST_CLASSES`` record holds its spec by reference as well, so every
    registered spec is checked to resolve to a ``HostSpec``.
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
            assert _top_level_refs(entry) == [], (registry.defined_in, name, entry)
    for builtins in BUILTINS:
        if not builtins.by_reference:
            continue
        registry = getattr(importlib.import_module(builtins.module), builtins.attr)
        for name in builtins.names:
            entry = registry.get(name)
            targets = _referenced(entry)
            assert targets, (builtins.module, builtins.attr, name, entry)
            for target in targets:
                assert callable(target), (builtins.module, builtins.attr, name, entry)
    for name, spec in registered_host_specs().items():
        assert isinstance(spec, type), (name, spec)
        assert issubclass(spec, HostSpec), (name, spec)


_REF_FIELDS = ("cls", "config", "factory", "fn")
"""The fields a record registry's built-in holds its ``Ref`` in: a class
seam's :class:`~otto.registry.ClassEntry`, a backend's
:class:`~otto.registry.BackendEntry` (its config model and factory, or its
class), a kind's ``KindEntry`` and a session setup's ``SessionSetupEntry``."""


def _referenced(entry: object) -> list[object]:
    """The objects a built-in names: the record's ``Ref`` fields once resolved, or the entry."""
    if not dataclasses.is_dataclass(entry) or isinstance(entry, type):
        return [entry]
    targets = [
        reg.resolved(getattr(entry, field))
        for field in _REF_FIELDS
        if getattr(entry, field, None) is not None
    ]
    return targets or [entry]


def test_the_json_lab_source_names_its_config_model_and_factory_by_reference():
    """Both halves of a configured built-in are references: naming it imports neither."""
    listing = _run_python(
        "import json\n"
        "from otto.labs.registry import LAB_REPOSITORIES\n"
        "from otto.registry import Ref\n"
        "e = LAB_REPOSITORIES.peek('json')\n"
        "print(json.dumps([isinstance(e.config, Ref), isinstance(e.factory, Ref)]))\n"
    )
    assert listing == [True, True]


def _top_level_refs(entry: object) -> list[str]:
    """The record fields of *entry* still holding a ``Ref`` after ``get``."""
    if not dataclasses.is_dataclass(entry) or isinstance(entry, type):
        return []
    return [
        f.name for f in dataclasses.fields(entry) if isinstance(getattr(entry, f.name), reg.Ref)
    ]


_OTTO_ORIGIN_NAMES = """
import json, sys
from tests.unit.test_registry_loading import (
    _import_every_module_that_builds_a_registry, _otto_registries,
)
from otto.registry import RegistryView, Subscription
_import_every_module_that_builds_a_registry()
def by_otto(o):
    return o == "otto" or o.startswith("otto.")
out = {}
for r in _otto_registries():
    if isinstance(r, RegistryView):
        continue  # a view's entries are its sources'
    attrs = [n for n, v in vars(sys.modules[r.defined_in]).items() if v is r]
    if isinstance(r, Subscription):  # no names: an occurrence otto subscribed shows as its value
        names = [
            getattr(s.value, "__qualname__", repr(s.value)) for s in r.items() if by_otto(s.origin)
        ]
    else:
        names = [n for n in r.names() if by_otto(r.origin(n))]
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
    registry must ALSO hold each built-in as an unresolved ``Ref`` (the
    entry itself on a legacy table, the record's ``Ref`` field on a record
    table): an implementation module registering its own class leaves a real
    object there instead.
    """
    listing = _run_python(
        "import dataclasses, importlib, json\n"
        "from otto.registry import Ref\n"
        f"r = getattr(importlib.import_module({builtins.module!r}), {builtins.attr!r})\n"
        "def lazy(e):\n"
        "    if isinstance(e, Ref):\n"
        "        return True\n"
        "    return dataclasses.is_dataclass(e) and any(\n"
        "        isinstance(getattr(e, f.name), Ref) for f in dataclasses.fields(e)\n"
        "    )\n"
        "print(json.dumps({n: lazy(e) for n, e in r.raw_items()}))\n"
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
        register_host_class("unix", PluginUnixHost, overwrite=True)
    assert any(
        "overriding built-in host class 'unix'" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    assert HOST_CLASSES.get("unix").cls is PluginUnixHost
    assert build_host_spec("unix") is UnixHostSpec
