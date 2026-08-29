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

import os
import subprocess
import sys

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
