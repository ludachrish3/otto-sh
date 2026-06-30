"""Deterministic import-budget guard.

See ``docs/superpowers/specs/2026-06-29-import-budget-guard-design.md``.
"""

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "import_budget.py"


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
    }
    assert set(keys) == expected


def test_check_surface_passes_for_real_measurement():
    surface = harness.SURFACES[0]  # import_otto
    result = harness.measure(surface.argv)
    assert harness.check_surface(surface, result) == []


def test_check_surface_flags_cap_violation():
    import dataclasses

    surface = harness.SURFACES[0]
    result = harness.measure(surface.argv)
    # Force the cap below the real count; the snapshot still matches, so only
    # the cap check fires.
    tight = dataclasses.replace(surface, cap=0)
    violations = harness.check_surface(tight, result)
    assert any("non-stdlib modules >" in v for v in violations)


@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface):
    result = harness.measure(surface.argv)
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
            "from otto.suite import OttoSuite, OttoOptionsPlugin, register_suite; print('ok')",
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
        "'otto.configmodule' in sys.modules, "
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
        "from otto import all_hosts; "  # triggers configmodule -> host graph
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
