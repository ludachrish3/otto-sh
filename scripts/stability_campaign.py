#!/usr/bin/env python3
"""Graduated x tiered stability campaign runner for feature/embedded-host.

Drives every kind of test suite under pytest-repeat --count, classifies the
JUnit output, and gates escalation (COUNT 1 -> 3 -> 10) on a clean stage.

See docs/superpowers/specs/2026-06-06-merge-readiness-stability-verification-design.md
The tier commands mirror the Makefile stability/nox targets; keep them in sync.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # importable both as `scripts.stability_campaign` and as a script
    from scripts.junit_failures import iter_problems
except ImportError:  # pragma: no cover - script-relative fallback
    from junit_failures import iter_problems

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


@dataclass
class StageReport:
    counts: dict[str, int] = field(
        default_factory=lambda: {"leak": 0, "wedge": 0, "flake": 0, "real": 0}
    )
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def green(self) -> bool:
        # Genuinely clean: zero problems AND every expected JUnit report present
        # (a tier that crashed before writing its report must not look GREEN).
        return self.total == 0 and not self.missing


def summarize_stage(xml_paths: list[Path]) -> StageReport:
    report = StageReport()
    for xml_path in xml_paths:
        path = Path(xml_path)
        if not path.exists():
            report.missing.append(str(path))
            continue
        for _kind, name, message, text in iter_problems(path):
            report.counts[classify_problem(name, message, text)] += 1
    return report


PYTHONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
DEEP_PYTHON = "3.10"  # pinned deep-escalation version (oldest supported floor)


@dataclass
class Tier:
    name: str
    argv: list[str]
    junit: list[str]  # JUnit path(s) this tier produces
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
            argv=["uv", "run", "nox", "-s", "tests_unit", "--", *repeat],
            junit=[f"reports/junit/nox-unit/tests_unit-{py}.xml" for py in PYTHONS],
        ),
        # T2 full lab — deep, pinned Python.
        Tier(
            name="full-deep",
            argv=["uv", "run", "nox", "-s", f"tests_all-{DEEP_PYTHON}", "--", *repeat],
            junit=[f"reports/junit/nox/tests_all-{DEEP_PYTHON}.xml"],
        ),
        # T3a concurrency soak — direct pytest, marker-selected, controlled JUnit.
        Tier(
            name="concurrency",
            argv=[
                "uv",
                "run",
                "pytest",
                "-m",
                "concurrency",
                f"--count={count}",
                "-p",
                "no:cacheprovider",
                f"--junitxml={cdir}/concurrency.xml",
            ],
            junit=[f"{cdir}/concurrency.xml"],
            env=leak_env,
        ),
        # T3b unix stability (real telnet/SSH) — direct pytest, marker.
        # No -n0: the suite self-serializes (docker via xdist_group, etc.),
        # mirroring `make stability-unix`.
        Tier(
            name="integration-stability",
            argv=[
                "uv",
                "run",
                "pytest",
                "-m",
                "stability and integration and not embedded",
                f"--count={count}",
                "-p",
                "no:cacheprovider",
                f"--junitxml={cdir}/integration-stability.xml",
            ],
            junit=[f"{cdir}/integration-stability.xml"],
            env=leak_env,
        ),
        # T3c embedded contract — via make (COUNT knob from Phase 1).
        Tier(
            name="embedded-contract",
            argv=["make", "stability-embedded", f"COUNT={count}"],
            junit=["reports/junit/stability-embedded/stability-embedded.xml"],
        ),
    ]
    if breadth:
        tiers.insert(
            2,
            Tier(
                name="full-breadth",
                argv=[
                    "uv",
                    "run",
                    "nox",
                    "-s",
                    "tests_all",
                    "--",
                    "--count=1",
                    "--repeat-scope=session",
                ],
                junit=[f"reports/junit/nox/tests_all-{py}.xml" for py in PYTHONS],
            ),
        )
    return tiers


def _run_tier(tier: Tier) -> None:
    Path("reports/junit/campaign").mkdir(parents=True, exist_ok=True)
    for j in tier.junit:
        Path(j).parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **tier.env}
    # Never hard-kill: a SIGKILL'd embedded run wedges single-client consoles.
    subprocess.run(tier.argv, env=env, check=False)  # noqa: S603 — trusted args


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
    if report.missing:
        print(
            f"  WARNING: {len(report.missing)} expected JUnit report(s) missing "
            f"(tier crashed before writing?): {', '.join(report.missing)}"
        )
    print(f"\n== stage count={count}: {report.counts} => {'GREEN' if report.green else 'DIRTY'} ==")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one stage (all tiers at --count)")
    run.add_argument("--count", type=int, required=True)
    run.add_argument(
        "--breadth", action="store_true", help="add the all-Pythons full-suite pass (Stage 1)"
    )
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
