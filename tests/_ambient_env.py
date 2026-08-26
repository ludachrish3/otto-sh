"""The one declaration of which ``OTTO_*`` variables survive into the suite.

``tests/conftest.py`` strips every ``OTTO_``-prefixed variable from the
process environment at import time so ambient otto *product* configuration
can never leak into a test run (see its comment for the failure that guard
exists to prevent). A handful of variables are not product configuration at
all, though — they are **harness opt-ins**: knobs a Makefile target, a CI
job, or a developer sets to steer the harness itself. Those have to survive
the strip to reach their reader.

Getting that wrong fails *silently*, which is what makes it worth a module.
A stripped opt-in does not raise; its reader simply sees its own default and
the run continues, green, doing something other than what was asked:

- issue #192 — nightly's ``chaos-docker`` job exports
  ``OTTO_CHAOS_DOCKER=loopback``, the variable was undeclared and therefore
  stripped, ``docker_venue()`` returned its ``"test3"`` default, and the
  job spent four minutes trying to SSH to the bed host ``10.10.200.13`` from
  a GitHub runner. On the dev VM the same bug is invisible: test3 *is*
  reachable there, so the "loopback venue certified 9/9" run had in fact
  certified test3.
- ``OTTO_CHAOS_SEED`` is printed on every chaos run as the documented way to
  reproduce a failure; while stripped, re-running with the pinned seed drew a
  fresh random one instead.
- ``OTTO_CHAOS_BED_HOST`` is the opt-in that points tier-2 chaos at a leased
  bed host; while stripped, it silently kept using the loopback sshd.
- ``make stability-tunnel CYCLES=N`` passes ``OTTO_TUNNEL_SOAK_CYCLES``;
  while stripped, every soak ran at the default depth of 5 regardless.

So: declare the variable in :data:`AMBIENT_OPT_INS` and read it through
:func:`ambient`, which rejects anything undeclared. Reading an undeclared
``OTTO_*`` variable straight from ``os.environ`` still fails quietly — the
registry cannot prevent that, it can only make the supported path the easy
one and keep the strip and its allowlist from drifting apart.

Pinned by ``tests/unit/test_env_hermeticity.py``.
"""

import os
from typing import overload

# name -> what it drives, and who sets it. Keep the reader's location in the
# note: the next person to touch the strip needs to know what breaks.
AMBIENT_OPT_INS: "dict[str, str]" = {
    "OTTO_DETECT_ASYNCIO_LEAKS": (
        "arms the asyncio leak detector (tests/conftest.py); set by the "
        "stability/repeat Makefile targets"
    ),
    "OTTO_TS_COVERAGE": (
        "arms the browser suites' CDP coverage collection "
        "(tests/_fixtures/_ts_coverage.py); set by `make dashboard`. Stripped, "
        "`make coverage-ts` fails far downstream on empty coverage"
    ),
    "OTTO_BROWSER_SHARD": (
        "relaxes the browser suites' single-worker pin to per-file xdist "
        "groups (tests/e2e/conftest.py, read at collection); set by ci.yml "
        "and nightly.yml's dashboard jobs"
    ),
    "OTTO_CHAOS_DOCKER": (
        "selects the docker chaos venue, `test3` (bed) or `loopback` "
        "(tests/e2e/chaos/_docker.py); set by nightly.yml's chaos-docker job"
    ),
    "OTTO_CHAOS_SEED": (
        "pins the chaos lane's injection offsets for reproduction "
        "(tests/e2e/chaos/_seed.py); set by hand from the seed a failing run "
        "printed"
    ),
    "OTTO_CHAOS_BED_HOST": (
        "opts tier-2 chaos onto a leased bed host instead of the loopback "
        "sshd (tests/integration/chaos/_target.py); set by hand on the lab"
    ),
    "OTTO_CONFORMANCE_BED": (
        "selects the bed venue for the host-contract conformance suite "
        "instead of the default hermetic one (tests/conformance/_venue.py); "
        "set by hand on the dev VM, which is the only place the bed exists "
        "(spec item 4) -- `make conformance` runs the HERMETIC venue and does "
        "not set this"
    ),
    "OTTO_CONFORMANCE_CELLS": (
        "how many cells the conformance suite samples, an integer or `all` "
        "(tests/conformance/_venue.py); set by hand -- nightly deliberately "
        "runs at the default sample size (spec s4), so nothing in CI sets it"
    ),
    "OTTO_CONFORMANCE_OBSERVATIONS": (
        "redirects where the conformance suite writes its per-cell observation "
        "records (tests/conformance/_observation.py); default "
        "reports/conformance-observations/. Set by a CI job that uploads them "
        "as an artifact, and by the unit guards that read them back. Stripped, "
        "a redirected run writes into the repo's real directory instead and the "
        "collate step folds records nobody asked it to"
    ),
    "OTTO_BUSYBOX_CACHE": (
        "redirects the BusyBox artifact cache off ~/.cache/otto/busybox "
        "(tests/_fixtures/busybox.py); set by `make busybox-cache` and by an "
        "air-gapped lab priming the cache by hand. Stripped, every run "
        "silently writes and reads the real cache instead"
    ),
    "OTTO_BUSYBOX_SOURCE": (
        "which host each BusyBox artifact fetch attempt asks, `mirror-first` "
        "(default: the ci-assets-busybox-1 release assets, then busybox.net) or "
        "`upstream` (tests/_fixtures/busybox.py); set ONLY by ci.yml's `busybox` "
        "job, which exists to notice upstream rebuilding an artifact in place "
        "and would read yesterday's bytes off the mirror. Stripped, that job "
        "silently fetches mirror-first and verifies nothing about upstream"
    ),
    "OTTO_TUNNEL_SOAK_CYCLES": (
        "internal soak depth per tunnel stability test "
        "(tests/e2e/tunnel_stability/_harness.py); set by `make "
        "stability-tunnel CYCLES=N`"
    ),
}


def ambient_opt_ins() -> "frozenset[str]":
    """The declared harness opt-ins: every ``OTTO_*`` name the strip spares."""
    return frozenset(AMBIENT_OPT_INS)


@overload
def ambient(name: str, default: str) -> str: ...


@overload
def ambient(name: str, default: None = None) -> "str | None": ...


def ambient(name: str, default: "str | None" = None) -> "str | None":
    """Read a declared harness opt-in from the ambient environment.

    Raises on an undeclared name rather than returning ``None``: an opt-in
    that is read but not declared is stripped before its reader ever runs,
    and every symptom of that is silent (see the module docstring). Failing
    at the read is the only loud moment available.
    """
    if name not in AMBIENT_OPT_INS:
        raise KeyError(
            f"{name} is not a declared ambient harness opt-in, so "
            "tests/conftest.py strips it before this read — it would always "
            f"return {default!r}. Add it to AMBIENT_OPT_INS in "
            "tests/_ambient_env.py with a note on what it drives."
        )
    return os.environ.get(name, default)
