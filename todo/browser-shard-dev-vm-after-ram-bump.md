# Local browser sharding once the dev-VM RAM bump takes effect

The Vagrantfile change (dev VM 4096 → 8192 MB) removes the reason the
browser suites are pinned serial locally — the pin is a RAM policy, not a
correctness constraint (see the policy block in `tests/e2e/conftest.py`).
The sharding mechanism already exists and is CI-proven: `OTTO_BROWSER_SHARD=1`
switches the audited suites (dashboard, covreport) to per-file xdist groups.
Nothing new to build; this is validation + a default-flip decision.

After `vagrant reload` with the new allocation:

1. ~~Bounded probe~~ **DONE 2026-07-19 on the reprovisioned 8 GB VM**:
   chromium `-n 2` over dashboard + covreport = **75 passed in 28.9s vs
   51.5s serial (1.8×)**, same-day same-selection baseline. Mid-run system
   RAM 2.2 GiB used / 5.4 GiB available, swap 0 — ample headroom (two
   chromium process trees live). Remaining items are the decisions below.
   **`-n 3` measured too: 29.6s — no gain over `-n 2`.** The wall-clock is
   pinned by the single-file critical path: `test_review_shell` is 52.5s of
   the 95.4s summed serial workload (~28s under --no-cov), and per-file
   groups can't split a module. Use `-n 2` for the local default; more
   workers only pay off if `test_review_shell` is ever split into two
   modules (optional, low value — would shave ~10s off the sharded lane).
2. ~~Default-flip decision~~ **DONE 2026-07-19 (Chris's call: default 2)**:
   `BROWSER_WORKERS` in the Makefile — 2 when the host has ≥2 CPUs AND
   ≥6GiB physical RAM (cores alone was NOT the gating factor; the pin was
   always a RAM policy), else the serial fallback; `OTTO_BROWSER_SHARD=1`
   accompanies >1 worker automatically. Override with `BROWSER_WORKERS=N`.
   Verified end-to-end: `make dashboard` = 75 passed in 53.9s vs 75.3s
   serial-with-coverage same day; lint + policy unit tests green.
3. ~~Comment sites~~ **DONE 2026-07-19**: both updated (Makefile `-n` note
   above `coverage-python`, `tests/e2e/conftest.py` policy block).

This note is now fully resolved — kept as the record of the measurements.

Explicitly NOT unlocked by more RAM: cross-version `make nox` parallelism
and any concurrent bed-touching runs — the lab testbed is machine-global
and xdist group pins are process-local; that needs the cross-process bed
lock (separate follow-up), not memory.
