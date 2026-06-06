# Merge-readiness stability verification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove `feature/embedded-host` runs every kind of test suite reliably under graduated repeat execution (`--count` 1 → 3 → 10) with a genuinely clean result, then open the PR with evidence.

**Architecture:** Fix the two test-infra defects (a stale `stability` path; a missing `stability-embedded COUNT` knob), build a thin campaign runner that drives the graduated × tiered matrix and classifies JUnit output, then run the campaign gate-by-gate — fixing the two required defects it surfaces (the inner-pytest race flake and the async ResourceWarning leak) before the COUNT=10 stage is allowed to pass.

**Tech Stack:** Python 3.10–3.14, GNU Make, nox + nox_uv, pytest (+ pytest-repeat `--count`, pytest-xdist `-n auto --dist loadgroup`, pytest-timeout), `uv`, Vagrant lab (telnet/SSH + Zephyr QEMU beds).

**Spec:** [docs/superpowers/specs/2026-06-06-merge-readiness-stability-verification-design.md](../specs/2026-06-06-merge-readiness-stability-verification-design.md)

**No-self-commit:** This repo's `prepare-commit-msg` hook needs `/dev/tty`; do **not** run `git commit` here. Each "Commit" step gives a paste-able message for the user (Chris) to run. The hook handles AI-attribution.

**Lab guardrails:** The dev VM is the only copy — run in-place, no destructive probes, scratch only in `tmp_path`. Never SIGTERM a live-bed run at a tight timeout (it wedges single-client consoles); let runs finish or `make qemu-restart`. Recover wedged beds with `make qemu-restart` between stages.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| [Makefile](../../../Makefile) | `stability`, `stability-embedded`, `stability-all` targets | marker-based selection (drop hardcoded paths); add `COUNT` knob |
| [pyproject.toml](../../../pyproject.toml) | pytest `markers` list | register the `concurrency` marker |
| [tests/unit/host/test_session_concurrency.py](../../../tests/unit/host/test_session_concurrency.py) | tier-1 soak | add `pytestmark = pytest.mark.concurrency` |
| [tests/unit/host/test_unixHost.py](../../../tests/unit/host/test_unixHost.py) | oneshot deadlock test | add `@pytest.mark.concurrency` |
| `scripts/stability_campaign.py` | campaign runner: tier command builder, JUnit classifier, stage aggregator, CLI | **create** |
| [scripts/junit_failures.py](../../../scripts/junit_failures.py) | existing JUnit parser (`iter_problems`) | reuse, no change |
| `tests/unit/scripts/test_stability_campaign.py` | unit tests for the classifier/aggregator/command-builder | **create** |
| [tests/unit/suite/test_otto_suite.py](../../../tests/unit/suite/test_otto_suite.py) | inner-pytest race flake (Workstream D) | add `xdist_group` + tighten logger patch (exact fix gated on repro) |
| `src/otto/host/…` (site TBD by Workstream A repro) | async ResourceWarning leak origin | close the leaked loop/sockets (exact site gated on repro) |
| `reports/junit/campaign/count{N}/…` | per-stage JUnit + appendix artifacts | generated |

---

## Phase 1: Marker-based selection + test-infra (Makefile)

### Task 1: Convert stability test selection from paths to markers

The branch renamed `test_remoteHost.py → test_unixHost.py`; the `stability` target (and `stability-all` tier 1, and `nightly.yml`) still names the old path, so `make stability` errors at collection (`exit 4`). Rather than patch the path, eliminate path-based selection: mark the tier-1 soak with a new `concurrency` marker (kept *in* coverage) and select every stability tier by marker. The cross-OS contract suite already tags only its embedded backends with `embedded`, so the expressions partition existing tests by OS with no test dropped.

**Files:**
- Modify: [pyproject.toml](../../../pyproject.toml) (the `markers = [...]` list)
- Modify: [tests/unit/host/test_session_concurrency.py](../../../tests/unit/host/test_session_concurrency.py) (module `pytestmark`)
- Modify: [tests/unit/host/test_unixHost.py](../../../tests/unit/host/test_unixHost.py) (`test_oneshot_telnet_concurrent_does_not_deadlock`)
- Modify: [Makefile](../../../Makefile) (`stability`, `stability-all` tier 2, `stability-embedded`)

- [ ] **Step 1: Capture the current path-based selection sets (equivalence baseline)**

Run:
```bash
uv run pytest tests/unit/host/test_session_concurrency.py "tests/unit/host/test_unixHost.py::TestOneshot::test_oneshot_telnet_concurrent_does_not_deadlock" --collect-only -q --no-cov 2>/dev/null | tail -1
uv run pytest tests/unit/host/test_session_stability_integration.py -m integration --collect-only -q --no-cov 2>/dev/null | tail -1
uv run pytest tests/integration/host/test_host_stability_contract.py -m integration --collect-only -q --no-cov 2>/dev/null | tail -1
```
Expected: record the three counts (tier1; tier2 = 15; tier3 = 24). The marker expressions must reproduce these (tier3 splits into unix + embedded params).

- [ ] **Step 2: Register the `concurrency` marker**

In `pyproject.toml`, add to the `markers = [` list:

```toml
    "concurrency: fast, no-VM SessionManager concurrency/soak tests (run via `make stability`; stays in coverage)",
```

- [ ] **Step 3: Mark the tier-1 soak tests**

In `tests/unit/host/test_session_concurrency.py`, after the imports, add a module-level mark:

```python
pytestmark = pytest.mark.concurrency
```

In `tests/unit/host/test_unixHost.py`, decorate the oneshot test (keep the existing `asyncio` mark):

```python
    @pytest.mark.concurrency
    @pytest.mark.asyncio
    async def test_oneshot_telnet_concurrent_does_not_deadlock(self):
```

- [ ] **Step 4: Convert the `stability` target to `-m concurrency`**

Replace the two hardcoded paths in the `stability:` recipe with marker selection:

```make
stability: ## Run no-VM SessionManager concurrency/soak tests by marker. Override iterations with COUNT=N (default 50).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m concurrency \
	    --count=$(or $(COUNT),50) \
	    -p no:cacheprovider
```

- [ ] **Step 5: Convert `stability-all` tier 2 and `stability-embedded` to markers**

In `stability-all:`, replace the tier-2 invocation's path + `-m integration` with:

```make
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and integration and not embedded" \
	    --count=$(or $(COUNT),10) \
	    -p no:cacheprovider \
	    -n0
```

In `stability-embedded:`, replace the path + `-m integration` with `-m "stability and embedded"` (the `--count` knob is added in Task 2):

```make
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and embedded" \
	    -p no:cacheprovider \
	    -n0 \
	    --junitxml=reports/junit/stability-embedded.xml
```

- [ ] **Step 6: Verify collect-equivalence (no test dropped; correct regrouping)**

Run:
```bash
uv run pytest -m concurrency --collect-only -q --no-cov 2>/dev/null | tail -1
uv run pytest -m "stability and integration and not embedded" --collect-only -q --no-cov 2>/dev/null | tail -1
uv run pytest -m "stability and embedded" --collect-only -q --no-cov 2>/dev/null | tail -1
```
Expected: `-m concurrency` matches the Step-1 tier-1 count. The two `stability` expressions together collect 39 (= tier2 15 + tier3 24), regrouped by OS: the unix expression = 15 + the contract's unix params; the embedded expression = the contract's embedded params. Nothing unexpected appears.

- [ ] **Step 7: Smoke-run the no-VM target**

Run: `make stability COUNT=2`
Expected: the `concurrency`-marked tests run twice each and pass; exit 0 — no collection error.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml Makefile tests/unit/host/test_session_concurrency.py tests/unit/host/test_unixHost.py
git commit -m "refactor(test): select stability tiers by marker, not path

Adds a concurrency marker for the no-VM tier-1 soak (kept in coverage)
and converts stability/stability-all/stability-embedded to marker
selection. Fixes make stability (it named the renamed test_remoteHost.py
and errored at collection) and makes selection rename-proof.
Collect-equivalence verified against the prior path sets."
```

### Task 2: Add a `COUNT` knob to `stability-embedded` and thread it from `stability-all`

`stability-embedded` runs the embedded contract exactly once — no `--count`, so it cannot scale to ×10. Add the same `COUNT` pattern the other stability targets use, on top of the now-marker-based recipe from Task 1.

**Files:**
- Modify: [Makefile](../../../Makefile) (`stability-embedded:`; `stability-all:` tier-3 call)

- [ ] **Step 1: Add `--count` to `stability-embedded`**

In the now-marker-based `stability-embedded:` recipe, insert a `--count` line (default 1 so the standalone behavior is unchanged at one pass):

```make
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and embedded" \
	    -p no:cacheprovider \
	    -n0 \
	    --count=$(or $(COUNT),1) \
	    --junitxml=reports/junit/stability-embedded.xml
```

- [ ] **Step 2: Thread `COUNT` from `stability-all` tier 3**

In the `stability-all:` recipe, change the tier-3 call:

```make
	@$(MAKE) stability-embedded
```

to:

```make
	@$(MAKE) stability-embedded COUNT=$(or $(COUNT),10)
```

- [ ] **Step 3: Update the help text**

In the `stability-embedded:` `##` help comment, append: `Override iterations with COUNT=N (default 1).`

- [ ] **Step 4: Verify the knob expands**

Run: `make -n stability-embedded COUNT=3 | grep -- '--count=3' && make -n stability-embedded | grep -- '--count=1'`
Expected: first grep matches `--count=3`; second matches `--count=1` (default).

- [ ] **Step 5: Commit**

```bash
git add Makefile
git commit -m "feat(make): COUNT knob on stability-embedded for repeat scaling

Threads --count=\$(or \$(COUNT),1) into the embedded contract run and
passes COUNT through from stability-all's tier 3 so the embedded
stability suite scales with the rest of the campaign."
```

---

## Phase 2: Campaign runner (`scripts/stability_campaign.py`)

The runner is the campaign's single source of truth: it builds each tier's command, runs the graduated stages, and classifies the JUnit output into buckets (`leak` / `wedge` / `flake` / `real`) so a dirty stage is instantly triageable. The testable core is the **classifier**, the **aggregator**, and the **command builder**; the orchestration is thin glue verified by a dry-run.

### Task 3: JUnit problem classifier

**Files:**
- Create: `scripts/stability_campaign.py`
- Test: `tests/unit/scripts/test_stability_campaign.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/scripts/test_stability_campaign.py
from scripts.stability_campaign import classify_problem


def test_classifies_async_leak_from_text():
    assert classify_problem(
        "tests.x::test_a", "multiple unraisable exception warnings", ""
    ) == "leak"
    assert classify_problem(
        "tests.x::test_b", "", "ResourceWarning: unclosed event loop <_UnixSelectorEventLoop ...>"
    ) == "leak"


def test_classifies_x86_telnet_wedge():
    assert classify_problem("tests.x::test_c", "console wedged", "") == "wedge"
    assert classify_problem(
        "tests.x::test_d", "ConnectionError: shell never became ready after open", ""
    ) == "wedge"


def test_classifies_known_inner_pytest_flake():
    assert classify_problem(
        "tests.unit.suite.test_otto_suite.TestOttoTestDir::test_test_dir_created_per_test",
        "AssertionError", "",
    ) == "flake"


def test_anything_else_is_real():
    assert classify_problem("tests.x::test_e", "AssertionError: 1 != 2", "") == "real"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.stability_campaign'`.

- [ ] **Step 3: Write the classifier**

```python
# scripts/stability_campaign.py
#!/usr/bin/env python3
"""Graduated × tiered stability campaign runner for feature/embedded-host.

Drives every kind of test suite under pytest-repeat --count, classifies the
JUnit output, and gates escalation (COUNT 1 -> 3 -> 10) on a clean stage.

See docs/superpowers/specs/2026-06-06-merge-readiness-stability-verification-design.md
The tier commands mirror the Makefile stability/nox targets; keep them in sync.
"""
from __future__ import annotations

# Substrings that identify the two known, separately-tracked phenomena and the
# one required-fix flake. Anything unmatched is a real, blocking problem.
_LEAK_SIGNATURES = (
    "unraisable exception",
    "unclosed event loop",
    "ResourceWarning: unclosed",
)
_WEDGE_SIGNATURES = (
    "console wedged",
    "shell never became ready",
)
_KNOWN_FLAKES = ("test_test_dir_created_per_test",)


def classify_problem(name: str, message: str, text: str) -> str:
    """Return one of: 'leak', 'wedge', 'flake', 'real'."""
    blob = f"{message}\n{text}"
    if any(sig in blob for sig in _LEAK_SIGNATURES):
        return "leak"
    if any(sig in blob for sig in _WEDGE_SIGNATURES):
        return "wedge"
    if any(flake in name for flake in _KNOWN_FLAKES):
        return "flake"
    return "real"
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/stability_campaign.py tests/unit/scripts/test_stability_campaign.py
git commit -m "feat(scripts): JUnit problem classifier for the stability campaign"
```

### Task 4: Stage aggregator

**Files:**
- Modify: `scripts/stability_campaign.py`
- Test: `tests/unit/scripts/test_stability_campaign.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/scripts/test_stability_campaign.py
import textwrap
from pathlib import Path

from scripts.stability_campaign import summarize_stage


def _write_junit(path: Path, cases: list[tuple[str, str, str]]) -> None:
    """cases = [(classname, name, failure_message_or_empty), ...]."""
    body = []
    for classname, name, msg in cases:
        if msg:
            body.append(
                f'<testcase classname="{classname}" name="{name}">'
                f'<failure message="{msg}"></failure></testcase>'
            )
        else:
            body.append(f'<testcase classname="{classname}" name="{name}"/>')
    path.write_text(
        f'<testsuite tests="{len(cases)}">{"".join(body)}</testsuite>'
    )


def test_summarize_green_when_no_problems(tmp_path):
    p = tmp_path / "clean.xml"
    _write_junit(p, [("tests.x", "test_ok", "")])
    report = summarize_stage([p])
    assert report.total == 0
    assert report.green is True


def test_summarize_buckets_and_not_green(tmp_path):
    p = tmp_path / "dirty.xml"
    _write_junit(p, [
        ("tests.x", "test_real", "AssertionError: boom"),
        ("tests.y", "test_leak", "multiple unraisable exception warnings"),
        ("tests.z", "test_wedge", "console wedged"),
    ])
    report = summarize_stage([p])
    assert report.counts == {"leak": 1, "wedge": 1, "flake": 0, "real": 1}
    assert report.total == 3
    assert report.green is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov -k summarize`
Expected: FAIL — `ImportError: cannot import name 'summarize_stage'`.

- [ ] **Step 3: Implement the aggregator**

```python
# add to scripts/stability_campaign.py (imports at top of file)
from dataclasses import dataclass, field
from pathlib import Path

try:  # importable both as `scripts.stability_campaign` and as a script
    from scripts.junit_failures import iter_problems
except ImportError:  # pragma: no cover - script-relative fallback
    from junit_failures import iter_problems


@dataclass
class StageReport:
    counts: dict[str, int] = field(
        default_factory=lambda: {"leak": 0, "wedge": 0, "flake": 0, "real": 0}
    )

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def green(self) -> bool:
        # The spec's definition of done is a genuinely clean stage: zero
        # problems of any bucket (the leak and flake are *fixed*, not tolerated).
        return self.total == 0


def summarize_stage(xml_paths: list[Path]) -> StageReport:
    report = StageReport()
    for xml_path in xml_paths:
        if not Path(xml_path).exists():
            continue
        for _kind, name, message, text in iter_problems(Path(xml_path)):
            report.counts[classify_problem(name, message, text)] += 1
    return report
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov -k summarize`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/stability_campaign.py tests/unit/scripts/test_stability_campaign.py
git commit -m "feat(scripts): stage aggregator with GREEN verdict for the campaign"
```

### Task 5: Tier command builder

**Files:**
- Modify: `scripts/stability_campaign.py`
- Test: `tests/unit/scripts/test_stability_campaign.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/scripts/test_stability_campaign.py
from scripts.stability_campaign import build_tiers


def test_build_tiers_threads_count():
    tiers = build_tiers(count=3, breadth=False)
    names = {t.name for t in tiers}
    assert {"unit", "full-deep", "concurrency", "integration-stability",
            "embedded-contract"} <= names
    for t in tiers:
        assert any("3" in str(a) for a in t.argv), t.name  # count reached argv


def test_breadth_tier_only_when_requested():
    assert not any(t.name == "full-breadth" for t in build_tiers(count=1, breadth=False))
    assert any(t.name == "full-breadth" for t in build_tiers(count=1, breadth=True))


def test_deep_tier_pins_python_3_10():
    deep = next(t for t in build_tiers(count=10, breadth=False) if t.name == "full-deep")
    assert "tests_all-3.10" in deep.argv
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov -k tier`
Expected: FAIL — `ImportError: cannot import name 'build_tiers'`.

- [ ] **Step 3: Implement the command builder**

```python
# add to scripts/stability_campaign.py
PYTHONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
DEEP_PYTHON = "3.10"  # pinned deep-escalation version (oldest supported floor)


@dataclass
class Tier:
    name: str
    argv: list[str]
    junit: list[str]          # JUnit path(s) this tier produces
    env: dict[str, str] = field(default_factory=dict)


def build_tiers(count: int, *, breadth: bool) -> list[Tier]:
    """Mirror of the Makefile stability/nox targets, parameterized by --count.

    breadth=True adds the all-Pythons full-suite pass (Stage 1 only).
    """
    cdir = f"reports/junit/campaign/count{count}"
    repeat = [f"--count={count}", "--repeat-scope=session"]
    leak_env = {"OTTO_DETECT_ASYNCIO_LEAKS": "1"}

    tiers: list[Tier] = [
        # T1 unit — all Pythons via nox (no VMs); nox writes per-session JUnit.
        Tier(
            name="unit",
            argv=["uv", "run", "nox", "-s", "tests", "--", *repeat],
            junit=[f"reports/junit/tests-{py}.xml" for py in PYTHONS],
        ),
        # T2 full lab — deep, pinned Python.
        Tier(
            name="full-deep",
            argv=["uv", "run", "nox", "-s", f"tests_all-{DEEP_PYTHON}", "--", *repeat],
            junit=[f"reports/junit/tests_all-{DEEP_PYTHON}.xml"],
        ),
        # T3a concurrency soak — direct pytest, marker-selected, controlled JUnit.
        Tier(
            name="concurrency",
            argv=[
                "uv", "run", "pytest",
                "-m", "concurrency",
                f"--count={count}", "-p", "no:cacheprovider",
                f"--junitxml={cdir}/concurrency.xml",
            ],
            junit=[f"{cdir}/concurrency.xml"],
            env=leak_env,
        ),
        # T3b unix stability (real telnet/SSH) — direct pytest, marker, serial.
        Tier(
            name="integration-stability",
            argv=[
                "uv", "run", "pytest",
                "-m", "stability and integration and not embedded",
                f"--count={count}",
                "-p", "no:cacheprovider", "-n0",
                f"--junitxml={cdir}/integration-stability.xml",
            ],
            junit=[f"{cdir}/integration-stability.xml"],
            env=leak_env,
        ),
        # T3c embedded contract — via make (COUNT knob from Task 2).
        Tier(
            name="embedded-contract",
            argv=["make", "stability-embedded", f"COUNT={count}"],
            junit=["reports/junit/stability-embedded.xml"],
        ),
    ]
    if breadth:
        tiers.insert(2, Tier(
            name="full-breadth",
            argv=["uv", "run", "nox", "-s", "tests_all", "--",
                  "--count=1", "--repeat-scope=session"],
            junit=[f"reports/junit/tests_all-{py}.xml" for py in PYTHONS],
        ))
    return tiers
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov -k tier`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/stability_campaign.py tests/unit/scripts/test_stability_campaign.py
git commit -m "feat(scripts): tiered campaign command builder (mirrors make targets)"
```

### Task 6: CLI orchestration with `--dry-run`

**Files:**
- Modify: `scripts/stability_campaign.py`
- Test: `tests/unit/scripts/test_stability_campaign.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/scripts/test_stability_campaign.py
from scripts.stability_campaign import main


def test_dry_run_prints_each_tier_command(capsys):
    rc = main(["run", "--count", "1", "--breadth", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    for tier_name in ("unit", "full-deep", "full-breadth", "embedded-contract"):
        assert tier_name in out
    assert "--count=1" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov -k dry_run`
Expected: FAIL — `ImportError: cannot import name 'main'`.

- [ ] **Step 3: Implement the CLI**

```python
# add to scripts/stability_campaign.py
import argparse
import os
import shlex
import subprocess
import sys


def _run_tier(tier: Tier) -> None:
    Path(f"reports/junit/campaign").mkdir(parents=True, exist_ok=True)
    for j in tier.junit:
        Path(j).parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **tier.env}
    # Never hard-kill: a SIGKILL'd embedded run wedges single-client consoles.
    subprocess.run(tier.argv, env=env, check=False)


def run_stage(count: int, *, breadth: bool, dry_run: bool) -> StageReport:
    tiers = build_tiers(count, breadth=breadth)
    all_junit: list[Path] = []
    for tier in tiers:
        line = " ".join(shlex.quote(a) for a in tier.argv)
        env_pfx = " ".join(f"{k}={v}" for k, v in tier.env.items())
        print(f"── tier '{tier.name}' (count={count}) ──")
        print(f"  $ {env_pfx + ' ' if env_pfx else ''}{line}")
        if not dry_run:
            _run_tier(tier)
        all_junit.extend(Path(j) for j in tier.junit)
    if dry_run:
        return StageReport()
    report = summarize_stage(all_junit)
    print(f"\n== stage count={count}: {report.counts} "
          f"=> {'GREEN' if report.green else 'DIRTY'} ==")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one stage (all tiers at --count)")
    run.add_argument("--count", type=int, required=True)
    run.add_argument("--breadth", action="store_true",
                     help="add the all-Pythons full-suite pass (Stage 1)")
    run.add_argument("--dry-run", action="store_true")
    esc = sub.add_parser("escalate", help="run 1 -> 3 -> 10, stop on a dirty stage")
    esc.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        report = run_stage(args.count, breadth=args.breadth, dry_run=args.dry_run)
        return 0 if (args.dry_run or report.green) else 1
    if args.cmd == "escalate":
        for i, count in enumerate((1, 3, 10)):
            report = run_stage(count, breadth=(i == 0), dry_run=args.dry_run)
            if not args.dry_run and not report.green:
                print(f"stopping: stage count={count} is DIRTY")
                return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_stability_campaign.py -q --no-cov`
Expected: PASS (all tests).

- [ ] **Step 5: Sanity-check the real dry-run**

Run: `uv run python scripts/stability_campaign.py escalate --dry-run | grep -E "tier|count="`
Expected: prints the tier command lines for counts 1, 3, 10 (count=1 includes `full-breadth`); no suites actually run.

- [ ] **Step 6: Commit**

```bash
git add scripts/stability_campaign.py tests/unit/scripts/test_stability_campaign.py
git commit -m "feat(scripts): stability-campaign CLI (run/escalate, dry-run, gating)"
```

---

## Phase 3: Stage 1 smoke (COUNT=1) — runbook

Goal: a single clean-ish pass of every tier to catch obvious gotchas before hammering, and to capture the breadth run that surfaces the async leak and the flake. **Operational** — long, lab-dependent; run the heavy tiers in the background.

- [ ] **Step 1: Bring the lab up and health-check**

Run: `make vm-health`
Expected: every lab VM + Zephyr QEMU instance answers; note any clock drift. If a bed is wedged: `make qemu-restart`, then re-run.

- [ ] **Step 2: Run the cheap, no-VM tiers first (fail fast)**

Run: `uv run python scripts/stability_campaign.py run --count 1 2>&1 | tee reports/junit/campaign/stage1.log` — or, to isolate, start with just the unit tier: `make nox COUNT=1`.
Expected: unit tier GREEN across all 5 Pythons. If the inner-pytest flake fires here, proceed to Phase 4 before continuing.

- [ ] **Step 3: Run the full-suite breadth pass (surfaces the leak)**

Run: `uv run nox -s tests_all -- --count=1 --repeat-scope=session` (background/overnight; ~22-24 min/Python).
Expected: per the spec, ~0 failures except possibly 1 async-leak `unraisable exception` on a random test/Python (Issue 2). Record which test/Python.

- [ ] **Step 4: Aggregate and classify Stage 1**

Run: `uv run python scripts/stability_campaign.py run --count 1 --breadth --dry-run` to confirm the tier set, then aggregate the produced JUnit: `uv run python -c "from pathlib import Path; from scripts.stability_campaign import summarize_stage; import glob; print(summarize_stage([Path(p) for p in glob.glob('reports/junit/*.xml')]).counts)"`
Expected: a bucket breakdown. The `leak` and `flake` buckets are the Phase 4/5 work; `wedge` should be **0** at this diluted distribution (if not, invoke the §6 decision rule with Chris); `real` must be **0**.

- [ ] **Step 5: Gate**

Stage 1 is "smoke-clean" when `real == 0` and `wedge == 0`. The `leak`/`flake` buckets are addressed in Phases 4–5. Do not escalate to COUNT=3 until `real == 0 && wedge == 0` and Phases 4–5 are done.

---

## Phase 4: Workstream D — fix the inner-pytest race flake (required)

Root-cause-first (per Chris's preference): confirm the race before patching. Leading hypothesis (from `todo/test_otto_suite_3_12_flake.md`): two xdist workers concurrently running inner-`pytest.main()` tests collide on plugin/logger state.

**Files:**
- Modify: [tests/unit/suite/test_otto_suite.py](../../../tests/unit/suite/test_otto_suite.py)
- Reference: [todo/test_otto_suite_3_12_flake.md](../../../todo/test_otto_suite_3_12_flake.md)

- [ ] **Step 1: Reproduce reliably (capture a failure)**

Run: `for i in $(seq 1 50); do uv run --python 3.12 --group dev pytest tests/unit -m "not integration and not hops" --no-cov -p no:cacheprovider -q || break; done`
Expected: at least one run fails on `test_test_dir_created_per_test`. Capture the assertion message and, if it reproduces, the inner capture-file contents (per the todo's investigation step 2). **Gate:** do not edit until you've seen the failure and confirmed it is the inner-pytest worker race (capture file empty / short / extra lines) vs. a logger-patch leak.

- [ ] **Step 2: Apply the minimal fix indicated by the repro**

If worker contention is confirmed, mark the inner-pytest-spawning tests to serialize on one worker. At the top of each inner-pytest test class in `test_otto_suite.py` (e.g. `TestOttoTestDir`, `TestSuiteOptionsFixture`, `TestTeardownMethod`, and the parametrized test), add:

```python
@pytest.mark.xdist_group("inner_pytest")
```

If logger-patch leakage is confirmed instead, narrow the `with patch.object(suite_module, "logger", mock_logger)` in `_run_inner_pytest` to wrap only the `pytest.main()` call rather than the helper body. Apply whichever the repro proves — not both speculatively.

- [ ] **Step 3: Verify the fix holds under the same stress**

Run: `for i in $(seq 1 50); do uv run --python 3.12 --group dev pytest tests/unit -m "not integration and not hops" --no-cov -p no:cacheprovider -q || break; done`
Expected: 50/50 pass.

- [ ] **Step 4: Remove the resolved flake todo**

Run: `git rm todo/test_otto_suite_3_12_flake.md`

- [ ] **Step 5: Commit**

```bash
git add tests/unit/suite/test_otto_suite.py
git rm todo/test_otto_suite_3_12_flake.md
git commit -m "fix(test): serialize inner-pytest tests to kill the test_otto_suite race

Confirmed xdist worker contention on inner pytest.main() plugin/logger
state; [serialize via xdist_group OR scope the logger patch]. 50/50 green
under the repeated 3.12 loop that previously flaked. Closes the
test_otto_suite_3_12_flake tracker."
```

---

## Phase 5: Workstream A — fix the async ResourceWarning leak (required)

The parked leak: an unclosed `_UnixSelectorEventLoop` + 2 `AF_UNIX` sockets, surfaced as an `ExceptionGroup: multiple unraisable exception warnings` on a random test under `filterwarnings=["error"]`. Connection-specific (per `project_async_resource_warning_leak`). Root-cause-first — confirm the allocation site before patching.

**Files:**
- Modify: `src/otto/host/[MODULE].py` (exact module gated on the repro — likely a telnet/SNMP/subprocess transport path that opens a loop or `AF_UNIX` socketpair without closing it)
- Test: `tests/unit/host/[TEST].py` (regression test, path gated on the site)

- [ ] **Step 1: Build a focused, repeatable repro**

Run: `OTTO_DETECT_ASYNCIO_LEAKS=1 PYTHONTRACEMALLOC=25 uv run pytest tests/integration/host/test_host_stability_contract.py tests/integration/host/test_snmp_integration.py -m integration -W error::ResourceWarning -p no:cacheprovider -n0 --count=5`
Expected: the `ResourceWarning: unclosed event loop` / `unclosed <socket.socket ... AF_UNIX ...>` fires. With `tracemalloc`, the warning includes the allocation traceback. **Gate:** record the allocation site (file:line that created the loop/socket) before any edit. If it does not reproduce here, widen to the full `nox-all` breadth and bisect by marker (`-m "integration and embedded"`, then `+snmp`).

- [ ] **Step 2: Write the failing regression test at the confirmed site**

Once the site is known, write a unit test that exercises that connection path and asserts no leak — e.g. open and close the transport inside the test and assert the loop/socket are closed (or run under `-W error::ResourceWarning` and assert the path produces none). Place it next to the owning module's tests. Run it; confirm it FAILS (leaks) before the fix.

Run: `uv run pytest [new test path] -W error::ResourceWarning -q --no-cov`
Expected: FAIL with the ResourceWarning promoted to error.

- [ ] **Step 3: Apply the minimal fix at the site**

Close the leaked resource deterministically (e.g. `loop.close()` / `transport.close()` / `sock.close()` in the owning teardown or a context manager). Make the smallest change that closes the allocation found in Step 1.

- [ ] **Step 4: Verify the regression test and the repro are clean**

Run: `uv run pytest [new test path] -W error::ResourceWarning -q --no-cov` → PASS.
Then re-run Step 1's repro at `--count=5` → expect **0** `unraisable`/`unclosed` warnings.

- [ ] **Step 5: Update the parked-leak memory + commit**

Update the `project_async_resource_warning_leak` memory note to "resolved — [site] closed in [commit]". Then:

```bash
git add src/otto/host/[MODULE].py tests/unit/host/[TEST].py
git commit -m "fix(host): close the leaked event loop/AF_UNIX sockets on [path]

Root-caused via tracemalloc under the embedded+snmp integration repro:
[site] opened a loop/socketpair without closing it, tripping
filterwarnings=error as a random-test 'unraisable exception'. Regression
test asserts the path is warning-clean. Resolves the parked async leak."
```

---

## Phase 6: Stage 2 (COUNT=3) then Stage 3 (COUNT=10) — gated runbook

**Operational.** Only start once Phase 3 is smoke-clean (`real==0 && wedge==0`) and Phases 4–5 have landed. Run heavy tiers in the background; never hard-kill.

- [ ] **Step 1: Health-check the lab**

Run: `make vm-health` (and `make qemu-restart` if any bed is wedged).

- [ ] **Step 2: Run Stage 2 (COUNT=3)**

Run: `uv run python scripts/stability_campaign.py run --count 3 2>&1 | tee reports/junit/campaign/stage2.log`
Expected final line: `== stage count=3: {...} => GREEN ==`. If DIRTY, triage by bucket: `real`/`leak`/`flake` ⇒ a fix regressed (return to Phase 4/5); `wedge` ⇒ invoke the §6 decision rule with Chris.

- [ ] **Step 3: Gate on Stage 2 GREEN**

Do not run Stage 3 unless Stage 2 is GREEN.

- [ ] **Step 4: Run Stage 3 (COUNT=10)**

Run: `uv run python scripts/stability_campaign.py run --count 10 2>&1 | tee reports/junit/campaign/stage3.log` (background/overnight; the deep tier is ~4 h on 3.10).
Expected final line: `== stage count=10: {'leak': 0, 'wedge': 0, 'flake': 0, 'real': 0} => GREEN ==`.

- [ ] **Step 5: Recover beds and re-confirm**

If any bed wedged mid-run from concentration, `make qemu-restart`, then re-run only the affected tier at COUNT=10 to confirm it was bed concentration, not a regression.

- [ ] **Step 6: Archive the evidence**

Run: `mkdir -p reports/junit/campaign/final && cp reports/junit/*.xml reports/junit/campaign/count*/*.xml reports/junit/campaign/final/ 2>/dev/null; uv run python scripts/junit_failures.py reports/junit/campaign/final/*.xml > reports/junit/campaign/final/summary.txt`
Expected: `summary.txt` shows 0 problems (or only documented buckets, if Chris accepted any).

---

## Phase 7: PR assembly

- [ ] **Step 1: Final full-suite confirmation pass**

Run: `make all` (validate + build) and confirm the CI-equivalent path is green: `make ci`.
Expected: both exit 0.

- [ ] **Step 2: Assemble the PR with the evidence appendix**

Summarize in the PR body: the three-phenomenon verdict, the two fixes (flake + leak), the graduated-campaign results (paste the Stage 3 `summary.txt` bucket counts and the per-stage GREEN lines), and the diluted-wedge finding for the x86 telnet beds. Link the spec and this plan.

- [ ] **Step 3: Hand off**

Invoke `superpowers:finishing-a-development-branch` to choose merge/PR/cleanup. Provide the paste-able PR-create command (no self-commit; Chris runs it).

---

## Self-review notes (author)

- **Spec coverage:** §5 B1 (marker-based selection) → Task 1 (subsumes the stale-`stability`-path bug found during planning); §5 B2 (COUNT knob) → Task 2; §5 B3 (campaign runner) → Tasks 3–6; §3 graduated×tiered → `build_tiers` + Phases 3/6; §4 Workstream A → Phase 5; §6 decision rule → Phases 3/6 gates; §7 Workstream D → Phase 4; §1 success criteria → Phase 6 Step 4 + Phase 7.
- **Known-deferred specifics:** the exact fix sites for the flake (Phase 4 Step 2) and the leak (Phase 5 Step 3) are intentionally gated on reproduction per root-cause-first; the leading hypothesis is stated but not pre-committed in code.
- **`repeat` target:** subsumed by the `full-deep`/`full-breadth` tiers (full `tests/unit`+integration under `--count`); not run separately to avoid redundancy.
