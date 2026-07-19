# Local browser sharding once the dev-VM RAM bump takes effect

The Vagrantfile change (dev VM 4096 → 8192 MB) removes the reason the
browser suites are pinned serial locally — the pin is a RAM policy, not a
correctness constraint (see the policy block in `tests/e2e/conftest.py`).
The sharding mechanism already exists and is CI-proven: `OTTO_BROWSER_SHARD=1`
switches the audited suites (dashboard, covreport) to per-file xdist groups.
Nothing new to build; this is validation + a default-flip decision.

After `vagrant reload` with the new allocation:

1. Bounded probe (watch `free -h` alongside):
   `OTTO_BROWSER_SHARD=1 uv run pytest tests/e2e/monitor/dashboard
   tests/e2e/cov/report_browser -m "browser and not soak" --browser chromium
   -n 2 --no-cov -p no:cacheprovider`
   Two chromium instances peak ~1.2–1.5 GB; headroom should be ample at 8 GB.
2. If comfortable: decide where the dev VM uses it —
   - a `DASHBOARD_WORKERS ?= 1` knob on `make dashboard` (opt-in, smallest
     change), or
   - export the flag in the `dashboard`/`coverage-python` recipes (default-on
     locally; the serial fallback stays one env var away).
   Expected win: each engine's dashboard lane roughly halves (~70s → ~40s
   chromium; `nox -s dashboard`'s three serial engines ~210s → ~120s).
3. Update the two comment sites that cite the RAM policy when the default
   changes: the `tests/e2e/conftest.py` policy block ("never parallel
   browsers on the 3GB dev VM") and the Makefile `-n 1` note above
   `coverage-python`.

Explicitly NOT unlocked by more RAM: cross-version `make nox` parallelism
and any concurrent bed-touching runs — the lab testbed is machine-global
and xdist group pins are process-local; that needs the cross-process bed
lock (separate follow-up), not memory.
