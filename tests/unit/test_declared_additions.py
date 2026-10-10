"""Names the manifest declares at a facade resolve there, from the module that defines them.

Appendix B of ``docs/superpowers/specs/2026-10-04-public-api-manifest-appendix.md``
gives each taught name with no public path a declared home. Its G addenda
retire three facade names (registry spec §8.1) and move the host specs from
``otto.models`` to ``otto.host`` (host construction spec §2.1). A facade binds
lazily (PEP 562), so each addition is imported in a fresh interpreter both
ways round, facade first and defining module first: a cycle shows up as an
ImportError on a partly initialised module.
"""

import importlib
import subprocess
import sys

import pytest

ADDITIONS: dict[str, dict[str, str]] = {
    "otto.coverage": {
        "CoverageDataMismatchError": "otto.coverage.errors",
        "CoverageToolVersionError": "otto.coverage.errors",
        "GitUnavailableError": "otto.coverage.capture.gitio",
        "TierConfig": "otto.coverage.tiers",
        "resolve_get_tier": "otto.coverage.tiers",
        "run_coverage_report": "otto.coverage.reporter",
    },
    "otto.host": {
        "BaseHost": "otto.host.host",
        "Element": "otto.host.element",
        "EmbeddedHostSpec": "otto.models.host",
        "HostCapabilities": "otto.host.capability_grid",
        "HostLoopError": "otto.host.loop_owner",
        "HostSpec": "otto.models.host",
        "Mount": "otto.host.mount",
        "MountNotFoundError": "otto.host.errors",
        "RawLandingError": "otto.host.errors",
        "SessionIdentity": "otto.host.capability_grid",
        "SessionSetupError": "otto.host.errors",
        "TermContext": "otto.host.connections",
        "UnixHostSpec": "otto.models.host",
        "UserSupport": "otto.host.capability_grid",
        "Userland": "otto.host.userland",
        "host_identity": "otto.host.factory",
        "mount_for": "otto.host.mount",
    },
    "otto.inventory": {"SupportsStatPaths": "otto.inventory.protocol"},
    "otto.models": {
        "CredSpec": "otto.models.host",
        "FILLABLE_INVENTORY_FIELDS": "otto.models.inventory",
        "INVENTORY_KEY_FIELDS": "otto.models.inventory",
        "InventoryRecord": "otto.models.inventory",
        "SUPPLIES_EXEMPT_FIELDS": "otto.models.inventory",
    },
    "otto.monitor": {"MonitorTarget": "otto.monitor.collector"},
    "otto.reservations": {"RESERVATION_BACKENDS": "otto.reservations.registry"},
    "otto.suite": {
        "ExpectCollector": "otto.suite.expect",
        "MonitorHandle": "otto.suite.monitor_fixture",
        "prepare_run": "otto.suite.run",
    },
    "otto.tunnel": {"SocatCarrier": "otto.tunnel.socat"},
}

RETIRED = [
    ("otto.models", "EmbeddedHostSpec", "otto.models.host"),
    ("otto.models", "HostSpec", "otto.models.host"),
    ("otto.models", "UnixHostSpec", "otto.models.host"),
]

ROWS = [(facade, name, home) for facade, names in ADDITIONS.items() for name, home in names.items()]


def _groups() -> list[tuple[str, str, list[str]]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for facade, name, home in ROWS:
        grouped.setdefault((facade, home), []).append(name)
    return sorted((facade, home, sorted(names)) for (facade, home), names in grouped.items())


GROUPS = _groups()

# Cross-package orders around the host specs' new home: the models package
# imported first, and both facades in one process.
PACKAGE_ORDERS = [
    "import otto.models\nfrom otto.host import EmbeddedHostSpec, HostSpec, UnixHostSpec\n",
    "from otto.host import HostSpec\nfrom otto.models import CredSpec\n",
]


def _run(code: str) -> None:
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(("facade", "name", "home"), ROWS, ids=[f"{f}:{n}" for f, n, _ in ROWS])
def test_each_addition_is_listed_and_resolves_from_its_defining_module(facade, name, home):
    """Listed in ``__all__``, and the lazy table names the module that DEFINES it.

    An identity check cannot tell a table entry that names a module merely
    re-exporting a constant (``otto.testing.conformance`` re-exports the
    inventory field sets) from one naming the definition. The facade would
    then pull the re-exporter's whole import graph.
    """
    module = importlib.import_module(facade)
    assert name in module.__all__
    assert module._LAZY_ATTRS[name] == home


@pytest.mark.parametrize("facade_first", [True, False], ids=["facade-first", "home-first"])
@pytest.mark.parametrize(
    ("facade", "home", "names"), GROUPS, ids=[f"{f}<-{h}" for f, h, _ in GROUPS]
)
def test_each_addition_imports_in_both_orders(facade, home, names, facade_first):
    from_facade = f"from {facade} import {', '.join(names)}"
    from_home = f"import {home} as home"
    first, second = (from_facade, from_home) if facade_first else (from_home, from_facade)
    checks = "".join(f"assert {n} is home.{n}, {n!r}\n" for n in names)
    _run(f"{first}\n{second}\n{checks}")


@pytest.mark.parametrize("code", PACKAGE_ORDERS)
def test_the_host_specs_import_beside_the_models_package(code):
    _run(code)


@pytest.mark.parametrize(
    ("facade", "name", "home"), RETIRED, ids=[f"{f}:{n}" for f, n, _ in RETIRED]
)
def test_each_retired_name_leaves_its_facade_and_stays_where_it_is_defined(facade, name, home):
    module = importlib.import_module(facade)
    assert name not in module.__all__
    with pytest.raises(AttributeError, match=name):
        getattr(module, name)
    assert hasattr(importlib.import_module(home), name)
