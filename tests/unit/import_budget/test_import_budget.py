"""Deterministic import-budget guard.

See ``docs/superpowers/specs/2026-06-29-import-budget-guard-design.md``.
"""

import importlib.util

import pytest

from tests._fixtures.paths import PROJECT_ROOT

_HARNESS_PATH = PROJECT_ROOT / "scripts" / "import_budget.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("import_budget", _HARNESS_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load_harness()


def test_measure_returns_module_inventory():
    result = harness.measure(["python"])
    assert result["count"] > 0
    assert "otto" in result["otto_modules"]
    # otto_modules is a strict subset of modules, sorted.
    assert set(result["otto_modules"]) <= set(result["modules"])
    assert result["modules"] == sorted(result["modules"])
    # non_stdlib_modules is the gated metric: a subset of modules that always
    # includes otto itself and never the standard library.
    assert set(result["non_stdlib_modules"]) <= set(result["modules"])
    assert "otto" in result["non_stdlib_modules"]


def test_surfaces_table_well_formed():
    keys = [s.key for s in harness.SURFACES]
    assert len(keys) == len(set(keys)), "surface keys must be unique"
    expected = {
        "import_otto",
        "help",
        "run",
        "host",
        "reservation",
        "docker",
        "schema",
        "monitor",
        "test",
        "cov",
        "run_bootstrapped",
    }
    assert set(keys) == expected


def test_exactly_one_surface_covers_the_composition_root():
    """A bootstrap-inclusive surface must exist, and the lazy ones must stay lazy.

    Kills the blind spot this surface was added for: every other surface
    resolves a dispatch target WITHOUT calling `bootstrap()`, so bootstrap-time
    imports went unmeasured. Deleting `bootstrap=True` from the table (or
    letting `measure_surface` drop the flag) restores that hole silently — the
    snapshots would simply be regenerated smaller — so the presence of the
    surface is asserted here rather than inferred from a passing budget.
    """
    bootstrapped = [s for s in harness.SURFACES if s.bootstrap]
    assert [s.key for s in bootstrapped] == ["run_bootstrapped"]
    # And the flag has to reach the child, or the surface measures its twin.
    assert (
        harness.measure_surface(bootstrapped[0])["otto_modules"]
        != harness.measure(bootstrapped[0].argv)["otto_modules"]
    )


def test_check_surface_passes_for_real_measurement():
    surface = harness.SURFACES[0]  # import_otto
    result = harness.measure_surface(surface)
    assert harness.check_surface(surface, result) == []


def test_check_surface_flags_cap_violation():
    import dataclasses

    surface = harness.SURFACES[0]
    result = harness.measure_surface(surface)
    # Force the cap below the real count; the snapshot still matches, so only
    # the cap check fires.
    tight = dataclasses.replace(surface, cap=0)
    violations = harness.check_surface(tight, result)
    assert any("non-stdlib modules >" in v for v in violations)


@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface):
    result = harness.measure_surface(surface)
    violations = harness.check_surface(surface, result)
    assert not violations, "\n".join(violations)


def test_monitor_server_still_resolves():
    # PEP 562 lazy export must still work for library users.
    result = harness.measure(["python"])
    assert "fastapi" not in result["modules"]
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from otto.monitor import MonitorServer; print(MonitorServer.__name__)",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "MonitorServer"


def test_suite_public_api_still_resolves():
    import subprocess
    import sys

    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from otto.suite import OttoSuite, OttoOptionsPlugin; print('ok')",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "ok"


def test_bare_import_otto_is_lazy():
    """Bare `import otto` must not eagerly pull the CLI/config graph (Part D)."""
    import subprocess
    import sys

    code = (
        "import sys; import otto; "
        "print('otto.cli' in sys.modules, "
        "'otto.config' in sys.modules, "
        "'otto.context' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "False False False", out.stdout


def test_library_use_populates_registries():
    """Lazy __init__ must not leave host/transfer registries empty for library
    users: accessing the lab API pulls otto.host, whose backends self-register."""
    import subprocess
    import sys

    code = (
        "import otto; "
        "from otto import all_hosts; "  # triggers config -> host graph
        "from otto.host.transfer.registry import build_transfer_backend; "
        "build_transfer_backend('scp'); build_transfer_backend('tftp'); "
        "print('registries OK')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "registries OK", out.stdout
