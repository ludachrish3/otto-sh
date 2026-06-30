"""Measure otto's import footprint per CLI surface — deterministic, host-independent.

The metric is *module count / module identity*, never wall-clock. Each surface is
measured in a fresh subprocess with a sanitized env (all OTTO_* vars stripped) so
the footprint reflects otto-core only, regardless of the dev's labs / SUT dirs.

Usage:
    python scripts/import_budget.py            # print a per-surface count table
    python scripts/import_budget.py --update    # regenerate golden snapshots
    python scripts/import_budget.py --check      # enforce the budget; exit non-zero on a breach
    python scripts/import_budget.py --hyperfine  # also show wall-clock stats (manual)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "tests" / "unit" / "import_budget" / "snapshots"


@dataclass(frozen=True)
class Surface:
    """One measured CLI surface: its argv, denied heavy stacks, and non-stdlib module cap."""

    key: str
    argv: list[str]
    deny: tuple[str, ...]
    cap: int | None = None


# Heavy third-party stacks that must stay off the surfaces that don't own them.
_ALL_HEAVY = ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")

# Caps are on the NON-STDLIB module count (otto + third-party), never the full
# sys.modules total. The stdlib import graph drifts across Python versions
# (e.g. 3.14 pulls in compression.zstd, annotationlib, asyncio.graph, ...):
# noise unrelated to otto's own footprint. The non-stdlib count is identical
# across 3.10-3.14, so one cap (baseline + ~15 headroom) holds on every gated
# interpreter. This is the same "stable across dependency/version upgrades"
# rule the design already applies to the otto-only golden snapshot.
SURFACES: list[Surface] = [
    Surface("import_otto", ["python"], _ALL_HEAVY, cap=19),  # lazy __init__ (Part D)
    Surface("help", ["otto", "--help"], _ALL_HEAVY, cap=298),
    Surface("run", ["otto", "run", "--help"], _ALL_HEAVY, cap=260),
    Surface("host", ["otto", "host", "--help"], _ALL_HEAVY, cap=262),
    Surface("reservation", ["otto", "reservation", "--help"], _ALL_HEAVY, cap=260),
    Surface("docker", ["otto", "docker", "--help"], _ALL_HEAVY, cap=265),
    Surface("schema", ["otto", "schema", "--help"], _ALL_HEAVY, cap=260),
    # monitor owns the dashboard, so fastapi/uvicorn/starlette are allowed here.
    Surface("monitor", ["otto", "monitor", "--help"], ("pytest", "jinja2"), cap=272),
    # test runs the suite, so pytest is allowed here.
    Surface(
        "test", ["otto", "test", "--help"], ("fastapi", "uvicorn", "starlette", "jinja2"), cap=260
    ),
    # cov templates the HTML report, so jinja2 is allowed here.
    Surface(
        "cov", ["otto", "cov", "--help"], ("fastapi", "uvicorn", "starlette", "pytest"), cap=272
    ),
]

# non_stdlib_modules is the gated metric: total sys.modules minus the stdlib
# (classified via the *child's own* sys.stdlib_module_names, so each Python
# version self-classifies). Excluding the stdlib makes the count version-robust:
# the stdlib graph grows release to release, otto's footprint does not.

# Child script for `import otto` surface: bare import, no CLI invocation.
_CHILD_IMPORT = """
import sys, json
import otto
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
print(json.dumps({"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                  "non_stdlib_modules": non_std}))
"""

# Child script for CLI surfaces: access otto.app to trigger the lazy __init__
# __getattr__ → imports otto.cli → cli.main runs _register_subcommands(argv).
# Measures import footprint, not CLI invocation.
_CHILD_CLI = """
import sys, json
sys.argv = {argv!r}
import otto
_ = otto.app  # triggers lazy cli.main import; measures import footprint, not invocation
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
print(json.dumps({{"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                   "non_stdlib_modules": non_std}}))
"""


def _sanitized_env() -> dict[str, str]:
    """Env with all OTTO_* vars stripped, so measurement is lab/host independent."""
    return {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}


def measure(argv: list[str]) -> dict:
    """Import otto in a fresh sanitized subprocess for *argv*; return its module inventory."""
    code = _CHILD_IMPORT if argv[:1] == ["python"] else _CHILD_CLI.format(argv=argv)
    out = subprocess.run(  # noqa: S603 (fixed interpreter + measured argv, no shell)
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=_sanitized_env(),
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def snapshot_path(key: str) -> Path:
    """Path to the golden snapshot file for surface *key*."""
    return SNAPSHOT_DIR / f"{key}.txt"


def write_snapshot(key: str, otto_modules: list[str]) -> None:
    """Write *otto_modules* as the golden snapshot for surface *key*."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path(key).write_text("\n".join(otto_modules) + "\n")


def read_snapshot(key: str) -> list[str]:
    """Return the otto-owned module list recorded in surface *key*'s golden snapshot."""
    return [ln for ln in snapshot_path(key).read_text().splitlines() if ln]


def check_surface(surface: Surface, result: dict) -> list[str]:
    """Return human-readable import-budget violations for a surface (empty = pass).

    Runs the three checks the unit test enforces, so the script (`--check`) and
    `tests/unit/import_budget/` share one source of truth:
      1. denylist     — heavy third-party stacks must be absent
      2. count cap     — non-stdlib module count must not exceed surface.cap
      3. golden snapshot — the otto-owned module set must match exactly
    """
    violations: list[str] = []

    leaked = [d for d in surface.deny if d in result["modules"]]
    if leaked:
        violations.append(f"`{surface.key}`: heavy modules leaked onto the path: {leaked}")

    if surface.cap is None:
        violations.append(f"`{surface.key}` has no cap set")
    else:
        non_stdlib = result["non_stdlib_modules"]
        if len(non_stdlib) > surface.cap:
            violations.append(
                f"`{surface.key}`: {len(non_stdlib)} non-stdlib modules > cap {surface.cap}. "
                f"If intentional, re-run `make import-snapshot` and raise the cap.\n"
                f"  non-stdlib modules: {non_stdlib}"
            )

    expected = read_snapshot(surface.key)
    if result["otto_modules"] != expected:
        violations.append(
            f"`{surface.key}`: otto module set changed. "
            f"If intentional, re-run `make import-snapshot` and review the diff.\n"
            f"  added:   {sorted(set(result['otto_modules']) - set(expected))}\n"
            f"  removed: {sorted(set(expected) - set(result['otto_modules']))}"
        )

    return violations


def _run_hyperfine(surface: Surface) -> None:
    hyperfine = shutil.which("hyperfine")
    if hyperfine is None:
        print("  (hyperfine not found — run `make hyperfine` to install it)")
        return
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if surface.argv[:1] == ["python"]:
        cmd = f'{venv_py} -c "import otto"'
    else:
        cmd = f"{REPO_ROOT / '.venv' / 'bin' / 'otto'} {' '.join(surface.argv[1:])}"
    subprocess.run(  # noqa: S603 (dev tool, resolved exe + fixed argv, no shell)
        [hyperfine, "--warmup", "5", "--min-runs", "20", "--shell=none", "--ignore-failure", cmd],
        check=False,
    )


def main() -> int:
    """Print the per-surface count table; optionally --update / --check / --hyperfine."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update", action="store_true", help="regenerate golden snapshots")
    ap.add_argument(
        "--check",
        action="store_true",
        help="enforce the import budget (caps + snapshots + denylist); exit non-zero on violation",
    )
    ap.add_argument("--hyperfine", action="store_true", help="also show wall-clock stats (manual)")
    args = ap.parse_args()

    # flush=True so these lines interleave correctly with hyperfine's (unbuffered)
    # subprocess output when stdout is piped/redirected (e.g. `make profile > log`).
    print(f"{'surface':14} {'total':>6} {'non_std':>7} {'otto':>5}  heavy_present", flush=True)
    failed = False
    for s in SURFACES:
        r = measure(s.argv)
        present = [d for d in s.deny if d in r["modules"]]
        non_std, otto = len(r["non_stdlib_modules"]), len(r["otto_modules"])
        print(f"{s.key:14} {r['count']:6d} {non_std:7d} {otto:5d}  {present}", flush=True)
        if args.update:
            write_snapshot(s.key, r["otto_modules"])
            print(
                f"  -> wrote {snapshot_path(s.key).relative_to(REPO_ROOT)} "
                f"({len(r['otto_modules'])} modules)",
                flush=True,
            )
        if args.check:
            violations = check_surface(s, r)
            for v in violations:
                print(f"  FAIL {v}", flush=True)
            failed = failed or bool(violations)
        if args.hyperfine:
            _run_hyperfine(s)
    if failed:
        print("\nimport budget: FAILED — see FAIL lines above.", flush=True)
        return 1
    if args.check:
        print("\nimport budget: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
