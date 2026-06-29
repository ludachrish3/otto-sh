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

    # 2. Count cap: total modules must not exceed the post-reduction baseline + headroom.
    assert surface.cap is not None, f"`{surface.key}` has no cap set"
    assert result["count"] <= surface.cap, (
        f"`{surface.key}`: {result['count']} modules > cap {surface.cap}. "
        f"If intentional, re-run `make import-snapshot` and raise the cap."
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
