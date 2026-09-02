"""Import-graph laziness guards: ``otto.config``'s PEP 562 exports, and ``otto.inventory``.

``ResolvedDependency`` and the user-settings pair are lazy exports (identity +
laziness asserted here); ``otto.inventory`` is the other side of the same
coin — it must not drag ``otto.models.settings`` onto the bootstrap path.

Mirrors the subprocess-isolation approach in ``test_import_ordering.py``:
within this test *process*, other tests (or ``otto.config`` itself, via
``otto.bootstrap``) may already have imported ``otto.config.dependencies``,
so an in-process check of "not yet in ``sys.modules``" can't be trusted to
mean anything. A fresh subprocess gives a clean ``sys.modules`` to assert
against.
"""

import importlib
import os
import subprocess
import sys

from tests._fixtures.budget_harness import load_harness

harness = load_harness()

_SCRIPT = """
import sys

import otto.config

assert "otto.config.dependencies" not in sys.modules, (
    "bare 'import otto.config' must not eagerly pull in .dependencies"
)

resolved_dependency = otto.config.ResolvedDependency

assert "otto.config.dependencies" in sys.modules, (
    "attribute access must trigger the lazy import"
)

from otto.config.dependencies import ResolvedDependency

assert resolved_dependency is ResolvedDependency
print("LAZY EXPORT OK")
"""


_USER_SETTINGS_SCRIPT = """
import sys

import otto.config

assert "otto.config.user_settings" not in sys.modules, (
    "bare 'import otto.config' must not eagerly pull in .user_settings"
)
assert "otto.models.settings" not in sys.modules, (
    "…which is the point: .user_settings drags otto.models.settings onto every "
    "CLI surface, and the import-budget caps say no"
)

loader, path_fn = otto.config.load_user_settings, otto.config.user_settings_path

assert "otto.config.user_settings" in sys.modules, (
    "attribute access must trigger the lazy import"
)

from otto.config.user_settings import load_user_settings, user_settings_path

assert loader is load_user_settings
assert path_fn is user_settings_path
print("LAZY EXPORT OK")
"""


def test_resolved_dependency_lazy_export_identity_and_laziness():
    out = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "LAZY EXPORT OK" in out.stdout


def test_user_settings_lazy_export_identity_and_laziness():
    """The user-settings pair must stay lazy, or every CLI surface grows by six modules.

    The import-budget snapshots would also catch this, but they catch it as
    "ten caps broke" — this names the cause.
    """
    out = subprocess.run(
        [sys.executable, "-c", _USER_SETTINGS_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "LAZY EXPORT OK" in out.stdout


_INVENTORY_SCRIPT = """
import sys

import otto.inventory

for module in ("otto.config", "otto.config.user_settings", "otto.models.settings"):
    assert module not in sys.modules, (
        f"bare 'import otto.inventory' must not pull in {module}: otto.inventory.config "
        "needs it only inside build_inventory, and otto.inventory sits on the bootstrap "
        "path — a module-level import there undoes otto.config's lazy exports"
    )

# The deferred import must still WORK, not merely be absent. An explicit
# absent path, never the default: the default reads the real ~/.otto.
from pathlib import Path

assert otto.inventory.build_inventory([], user_settings_path=Path("/nonexistent/x.toml")) is None
assert "otto.config.user_settings" in sys.modules, "calling build_inventory must import it"
print("LAZY IMPORT OK")
"""


def test_otto_inventory_does_not_pull_the_settings_graph():
    """R14: ``otto.inventory.config`` defers ``otto.config`` / ``otto.models.settings``.

    Task 6 wires ``build_inventory`` into ``cli/invoke.py``; a module-level
    import here would put ~96 modules on every CLI surface and break the same
    ten import-budget caps the lazy exports defend. Asserted both ways: absent
    after a bare import, present after the call.
    """
    out = subprocess.run(
        [sys.executable, "-c", _INVENTORY_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "LAZY IMPORT OK" in out.stdout


_PYNETBOX_SCRIPT = """
import sys

import otto.inventory

assert "pynetbox" not in sys.modules, (
    "bare 'import otto.inventory' must not import pynetbox: _register_builtins imports "
    "otto.inventory.netbox at package import, so an eager 'import pynetbox' there would "
    "put pynetbox + requests + urllib3 on the bootstrap path of every otto verb"
)
assert "requests" not in sys.modules, "...and requests, which pynetbox drags in with it"

inv = otto.inventory.NetBoxInventory(url="http://127.0.0.1:9")

assert "pynetbox" not in sys.modules, "construction must not import it either"

try:
    inv.list_keys()
except otto.inventory.InventoryError:
    pass
else:
    raise AssertionError("nothing listens on port 9; list_keys must have raised")

assert "pynetbox" in sys.modules, "the first fetch must be what imports it"
print("LAZY IMPORT OK")
"""


def test_pynetbox_is_imported_only_by_the_first_netbox_fetch():
    """R14: ``pynetbox`` stays off every surface that never talks to NetBox.

    ``otto.inventory.registry._register_builtins`` imports
    ``otto.inventory.netbox`` eagerly (a built-in must be reachable by the same
    path a custom backend takes), so the module is on the bootstrap path even
    for a deployment with no NetBox at all. Only the import of ``pynetbox``
    itself is deferred, and this asserts it in both directions: absent after a
    bare import AND after construction, present after the first fetch.
    """
    out = subprocess.run(
        [sys.executable, "-c", _PYNETBOX_SCRIPT],
        capture_output=True,
        text=True,
        env={**os.environ, "NETBOX_TOKEN": "unused-but-set"},
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "LAZY IMPORT OK" in out.stdout


def test_importing_config_does_not_load_host():
    """Measured before: `import otto.config.lab` alone loaded 46 otto.host.* modules."""
    import json
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, json; import otto.config; "
                "print(json.dumps([m for m in sys.modules if m.startswith('otto.host')]))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_help_surface_does_not_load_host():
    """The ENTRY PATH, which a bare ``import otto.config`` cannot speak for.

    Root help loads only ``otto.cli.{main,builtin_commands,invoke,registry}``
    and dispatches every verb lazily, so the cli modules that DO name config at
    module scope today (``callbacks.py``, ``host.py``, ``docker.py``,
    ``monitor.py``) are not on this surface at all — MEASURED, not assumed.

    Which is exactly why this guard is worth its subprocess. A future
    module-level ``from ..config import get_host`` in one of the four modules
    root help DOES load would re-trigger the whole chain through
    ``config.fleet``, and the in-process guard above — which imports
    ``otto.config`` and nothing else — could never see it.
    """
    result = harness.measure_surface(harness.surface_by_key("help"))
    assert [m for m in result["otto_modules"] if m.startswith("otto.host")] == []


# name -> (owning module, attribute) for config's re-exported callables.
# Written out BY HAND rather than read off `_LAZY_EXPORTS`: a test that derives
# its expectation from the table it is checking asserts only that the table
# equals itself, and would follow a typo straight into green.
_PUBLIC_CALLABLES = {
    "all_hosts": ("otto.config.fleet", "all_hosts"),
    "do_for_all_hosts": ("otto.config.fleet", "do_for_all_hosts"),
    "get_host": ("otto.config.fleet", "get_host"),
    "get_lab": ("otto.config.fleet", "get_lab"),
    "run_on_all_hosts": ("otto.config.fleet", "run_on_all_hosts"),
    "load_lab": ("otto.config.lab", "load_lab"),
    "load_otto_env": ("otto.config.env", "load_otto_env"),
}


def test_config_public_names_resolve_to_the_right_object():
    """A lazy export that resolves to the WRONG object breaks users silently.

    Identity against the owning module, never ``is not None``: MUTATION-PROVEN
    that the weaker check cannot fail usefully — rebinding
    ``"get_host": ("otto.config.fleet", "get_lab")`` in ``_LAZY_EXPORTS``
    still hands back a callable, and every caller of ``otto.config.get_host``
    would quietly receive a lab.

    ``dir()`` membership is the second assertion, and it is not decoration: a
    PEP 562 name never enters the module dict, so dropping ``__dir__`` from
    ``otto.config`` would make every LAZY name in the table below invisible to
    introspection while its ``getattr`` kept working. ``load_otto_env`` is in
    the table as the control: it is an ordinary module-dict entry, so it stays
    in ``dir()`` either way, and a mutation that only IT survives would mean
    the check had stopped covering the lazy six.
    """
    import otto.config as c

    for name, (module_name, attr) in _PUBLIC_CALLABLES.items():
        expected = getattr(importlib.import_module(module_name), attr)
        assert getattr(c, name) is expected, f"{name} resolved to the wrong object"
        assert name in dir(c), f"{name} is missing from dir(otto.config)"


_IMPORT_STAR_SCRIPT = """
namespace = {}
exec("from otto.config import *", namespace)

missing = [
    name
    for name in (
        "all_hosts", "do_for_all_hosts", "get_host", "get_lab",
        "run_on_all_hosts", "load_lab", "load_otto_env",
        "load_user_settings", "user_settings_path", "ResolvedDependency",
        "Repo", "Version",
    )
    if name not in namespace
]
assert not missing, (
    "`from otto.config import *` stopped binding " + repr(missing) + ": a PEP 562 "
    "export is invisible to import-star unless __all__ names it"
)
print("IMPORT STAR OK")
"""


def test_import_star_still_binds_every_public_name():
    """``from otto.config import *`` must bind what it bound before the names went lazy.

    Import-star reads ``__all__`` when the module defines one and falls back to
    the module dict when it does not — and a lazy export is in neither place by
    default. Deleting ``__all__`` from ``otto.config`` therefore drops all nine
    lazy names from import-star with no error raised anywhere, which is why
    this is asserted rather than left to the export table's good intentions.

    A subprocess for the usual reason: import-star must run at module scope,
    and the child also keeps the real host subtree out of this worker.
    """
    out = subprocess.run(
        [sys.executable, "-c", _IMPORT_STAR_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert "IMPORT STAR OK" in out.stdout
