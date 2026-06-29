"""Deterministic import-budget guard: see docs/superpowers/specs/2026-06-29-import-budget-guard-design.md."""
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "import_budget.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("import_budget", _HARNESS_PATH)
    assert spec and spec.loader
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
        "import_otto", "help", "run", "host", "reservation",
        "docker", "schema", "monitor", "test", "cov",
    }
    assert set(keys) == expected


@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface):
    result = harness.measure(surface.argv)

    # 1. Denylist: heavy third-party stacks must be absent.
    leaked = [d for d in surface.deny if d in result["modules"]]
    assert not leaked, f"`{surface.key}`: heavy modules leaked onto the path: {leaked}"

    # 2. Count cap: the non-stdlib module count (otto + third-party) must not
    #    exceed the post-reduction baseline + headroom. Stdlib modules are
    #    excluded on purpose: the stdlib import graph grows across Python
    #    versions (3.14 pulls in compression.zstd, annotationlib, asyncio.graph,
    #    ...), which is version noise unrelated to otto's footprint. The
    #    non-stdlib count is stable across 3.10-3.14, so one cap holds on every
    #    interpreter: the same "stable across upgrades" rule the golden
    #    snapshot already follows.
    assert surface.cap is not None, f"`{surface.key}` has no cap set"
    non_stdlib = result["non_stdlib_modules"]
    assert len(non_stdlib) <= surface.cap, (
        f"`{surface.key}`: {len(non_stdlib)} non-stdlib modules > cap {surface.cap}. "
        f"If intentional, re-run `make import-snapshot` and raise the cap.\n"
        f"  non-stdlib modules: {non_stdlib}"
    )

    # 3. Golden snapshot: the set of otto-owned modules must match exactly.
    expected = harness.read_snapshot(surface.key)
    assert result["otto_modules"] == expected, (
        f"`{surface.key}`: otto module set changed. "
        f"If intentional, re-run `make import-snapshot` and review the diff.\n"
        f"  added:   {sorted(set(result['otto_modules']) - set(expected))}\n"
        f"  removed: {sorted(set(expected) - set(result['otto_modules']))}"
    )


def test_monitor_server_still_resolves():
    # PEP 562 lazy export must still work for library users.
    result = harness.measure(["python"])
    assert "fastapi" not in result["modules"]
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-c", "from otto.monitor import MonitorServer; print(MonitorServer.__name__)"],
        capture_output=True, text=True, check=True, env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "MonitorServer"


def test_suite_public_api_still_resolves():
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-c",
         "from otto.suite import OttoSuite, OttoOptionsPlugin, register_suite; print('ok')"],
        capture_output=True, text=True, check=True, env=harness._sanitized_env(),
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
        capture_output=True, text=True, check=True, env=harness._sanitized_env(),
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
        capture_output=True, text=True, check=True, env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "registries OK", out.stdout
