# Chaos hardening Plan 5 — deliberate scoping leftovers

Recorded during the final-review fix wave for
`docs/superpowers/plans/2026-08-02-chaos-plan5-docker-extended-surfaces.md`.
Both items are known gaps, deliberately left out of Plan 5's scope rather
than missed — flagged here so a later plan (or a standalone fix) picks them
up instead of rediscovering them.

## 1. `BedHygiene`'s shared probe helper never checks exec status

`tests/_fixtures/bed_hygiene.py`'s `_out()` runs every remote probe with a
trailing `|| true`, and no caller asserts the exec's own status. The `|| true`
itself is load-bearing and correct: the hygiene bracket must tolerate a
dockerless or tool-less host without erroring, and it snapshots hosts whose
exact toolset varies. What is *not* intended is the second-order effect — a
probe that fails for an unrelated reason (host unreachable mid-scenario, a
transport that died between the before- and after-snapshot) collapses to empty
output, which the NEW-ONLY diff then reads as "nothing new appeared," i.e. as
a clean bed.

Plan 5's final review found and fixed exactly this blindness in the docker
pile-up test's own in-test oracle (it now asserts `.is_ok` on its probe execs
and on every `compose_down`), but deliberately did **not** extend the checks
into the shared `_out()` helper: making it assertive needs per-probe policy
(which probes may legitimately come back empty on which host families?), and
touching the shared bracket would have required re-certifying every chaos
module on the bed inside a scoped fix wave.

So today, every chaos module using the autouse hygiene bracket shares the same
residual blindness: a probe-side failure is indistinguishable from a clean
result. A fix should give `_out()` a per-probe "may be absent" policy —
tolerate a *tool* that isn't installed, but fail loud when the probe could not
be executed at all.

## 2. Force-exit hook leakage is guarded per-file, not process-globally

`interact.py`'s `_force_restore_guard` deliberately leaves its force-exit hook
registered when the body unwinds exceptionally (unconditional unregistering was
the original Plan 1 bug — the hook must survive to run after `asyncio.run`
finalization). That is correct production behavior: the process exits moments
later. In tests it means any test driving a cancelled login bridge leaks an
inert hook into `lifecycle._force_exit_hooks`, which is process-global state
shared across an xdist worker.

Two test files now carry the same manual snapshot/truncate cleanup for this
reason (`tests/unit/host/test_interact_force_restore.py` and, since Plan 5,
`tests/unit/host/test_interact.py`). Per this repo's guard-scope rule — a
guard lives where the state lives, and process-global state belongs to the
root conftest — a third occurrence should trigger hoisting this into a root
`conftest.py` watermark fixture that fails any test leaking a hook, rather
than a fourth copy of the manual cleanup.
