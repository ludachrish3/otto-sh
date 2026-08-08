# CI 3.11 flake: `test_surplus_time_donated_to_later_commands` — a wall-clock proxy that is also blind

**Date:** 2026-08-08
**Origin:** CI run [31252345542](https://github.com/ludachrish3/otto-sh/actions/runs/31252345542) (dependabot JS bump `@internationalized/date-3.12.3`), job `tests_hostless-3.11`
**Status:** analysis independently re-verified; the recommended fix is implemented **in this commit**, on its own
branch off `main` (not the `worktree-gate-fresh` copy §7 described). See §7 for the landed shape, the second
mutation added during that pass, and the review condition that reshaped the assertions.
**For:** the test-infra audit workstream (paused after W17c, `58db5dfc` at the time of writing; waves 17d–g specced)

> SHAs in this doc are point-in-time. The unpushed wave series gets rebased (W17c was `b4268478`, then
> `58db5dfc` after a pull ~30 min later), so prefer the commit *subjects* over the hashes, and re-derive with
> the SHA-independent commands in §8.

---

## 1. TL;DR

`tests/unit/host/test_run_timeout.py::TestRunTimeoutIntegration::test_surplus_time_donated_to_later_commands`
failed only on the 3.11 hostless lane. It has **two independent defects**, and the CI flake only exposed the first:

1. **It fails on load.** It races a real `sleep 0.1` in a real shell against a 0.5s budget.
2. **It cannot fail on the regression it claims to guard.** It stays green under the exact
   even-split allocation its own docstring says it rules out.

Neither landed nor queued audit work would have caught it: **no wave has ever touched the file**, and the
audit never inventoried it. This is a **5th sighting** of the wall-clock-under-loaded-xdist root behind
`serial_timing` — but a *different shape*, and marking it `serial_timing` would be the wrong fix (§5).

The recommended remedy is already implemented and proved: **assert the granted value, not the race outcome.**

---

## 2. What failed, and what the failure proves

The test runs three commands under a single 0.5s cumulative budget:

```python
result = await host.run(
    ["echo fast1", "echo fast2", "sleep 0.1 && echo done"],
    timeout=0.5,
)
assert result.status == Status.Success
```

CI failure:

```text
Command timed out after 0.495853722999982s
```

**Read that number.** The third command was granted **0.4958s of the 0.5s budget — 99.2%**. Budget donation
worked exactly as designed; the two `echo`s (shell builtins, no fork) consumed ~4ms combined. The product is
correct. What failed is the test's *incidental* requirement that the machine execute a real fork+exec
`sleep 0.1` inside ~0.4s.

Environment: 4/4 xdist workers on a 4-vCPU runner (`created: 4/4 workers`), under `--cov`. Chris: *"The GitHub
host is much slower than this host."*

Measured locally (15 runs, warm session): the third command costs **~103ms** (min 101.4 / median 103.3 / max
107.9). CI needed a **~4.8x** inflation to fail.

---

## 3. The second, worse defect: the assertion is blind

The docstring claims the test rules out an even split:

> *"The point is that the later command gets nearly the full 0.5s budget (donation), not just an even-split 0.17s slice."*

But an even split grants the third command 0.5/3 = **0.167s**, and `sleep 0.1` **finishes inside 0.167s**. So a
success-only assertion is green against the very allocation it claims to reject.

**Proved by mutation.** In `src/otto/host/host.py::_run_cmds_with_budget`:

```python
# MUTATION: no donation
_even_split = timeout / max(1, len(cmds))
effective = _even_split if sc.timeout is None else min(sc.timeout, _even_split)
```

Result — the two *mocked* unit tests correctly went red, the integration test that names donation stayed green:

```text
FAILED TestRunTimeout::test_timeout_passes_remaining_to_run_one
FAILED TestRunTimeout::test_fast_commands_donate_surplus
        TestRunTimeoutIntegration::test_surplus_time_donated_to_later_commands   <-- PASSED
2 failed, 37 passed
```

The test is inverted: **insensitive to its own regression, sensitive to machine speed.** This is the
guards-that-cannot-fail pattern, in a file no gate covers.

---

## 4. Why the audit missed it

| Check | Command | Result |
|---|---|---|
| Any wave touched the file? | `git log --oneline -1 main -- tests/unit/host/test_run_timeout.py` | `d72ca272 refactor(host): RemoteHost absorbs the duplicated session-delegation methods` — **predates the audit entirely** |
| Audit review names it? | grep `run_timeout\|surplus\|donat` in `todo/test-infra-review-2026-08-06.md` | **0 hits** |
| Remediation plan names it? | same grep in `todo/test-infra-remediation-plan-2026-08-06.md` | **0 hits** |
| Main's copy drifted? | `git diff main HEAD -- <file>` | **empty** (identical) |

**The near-miss is Wave 16.** W16 was *"Timing hardening, unit tier"* and rewrote 8 files in
`tests/unit/host/` — including 371 lines of `test_session.py`, one directory over. It missed this because its
census targeted the **sleep-then-feed family**: tests that sleep to synchronize a *test double*. This test
races a **real** command in a **real** shell against a real budget. That shape does not match the
sleep-then-feed AST census, so the file was never in scope.

> **Lesson for future censuses:** a timing census keyed on *how the test synchronizes its doubles* is blind to
> tests that race *real work* against a *real budget*. The two shapes need separate instruments.

---

## 5. Relationship to `serial_timing` (W17c) — and why marking it is the wrong fix

W17c's `serial_timing` marker documents *four* loaded-gate sightings (lifecycle force discriminators ×3, docker
exec-concurrency probe ×1). **This is a fifth** — same environmental root (wall-clock bounds under saturated
xdist), different assertion shape:

| | `serial_timing` shape | This test |
|---|---|---|
| Assertion | `elapsed < X` — **rejects** the slow (deadline/inert) path | operation **completed** within a budget |
| Under load | false RED (load counterfeits the slow path) | false RED |
| Blind to its regression? | **No** — the slow path always costs ≥X | **Yes** — an even split also completes |
| Is elapsed time the only observable? | Yes | **No** — the granted budget is observable at a seam |

Marking this test `serial_timing` would be wrong twice over:

1. It does not match the marker's documented contract (*"its assert REJECTS the slow path by elapsed time"*).
   Enrolling a "demand the fast path" assert under that marker dilutes a precise contract into "tests that are
   timing-flaky", which is how markers rot.
2. **It would preserve the blindness.** Serializing removes the noise and leaves a test that still passes
   against the regression it exists to catch — arguably worse, because the flake was the only signal that
   anyone was looking at this test at all.

`serial_timing` remains right for the four members it has. The rule that distinguishes them is in §6.

---

## 6. Recommendations for the workstream

**R1 — Adopt the discriminating rule (the important one).**

> **Assert the granted value, not the race outcome.** When a timing test's assertion is *"it finished in
> time"*, ask what the observable form of the property is. If a value is computed and passed across a seam,
> assert **that value**. Reserve elapsed-time bounds — and `serial_timing` — for **reject-asserts**, where the
> only observable is that the slow path was *not* taken.

Widening a tolerance only trades one flake rate for another and leaves the blindness untouched. Serializing
does the same. Both are the right tool only when there is genuinely nothing but the clock to look at.

**R2 — This class has no standing gate, so it can regrow.** W16 was a fix wave, not a gate wave; W17c gates
the *lane wiring* (`test_lane_invariants.py`) but nothing detects a newly written wall-clock-proxy assertion.
If 17d–g has room for a gate, the tractable signal is: *a test that awaits real work under a sub-N-second
budget and asserts only on success/status*. Worth a feasibility read before committing to it — a naive AST
rule will drown in false positives, and per the house rule, a gate that cannot be proved red does not land.

**R3 — Scope check: this is a one-off, not a sweep.** After the fix, no other test races a real command
against a tight budget. Census: `sleep 0.1`-under-tight-timeout is unique to this file; the other four files
with sub-2s timeouts (`test_power.py`, `test_reboot_recovery.py`, `test_session.py`,
`tests/unit/suite/test_plugin.py`) drive mocked sessions. **Do not budget a migration wave for this.**

> **Narrowed during review (see §7).** The "no sweep" verdict holds, but the sentence above is too strong as
> written: review found **one real exception in this same file** — `test_reap_is_bounded_when_process_ignores_sigterm`
> asserted `elapsed < 2.0` around a real spawn + SIGTERM-ignored reap + SIGKILL escalation measuring ~0.3 s
> (a 6.5x margin, *thinner* than the one that flaked). Its bound has a genuine job, so it was widened to 4.0,
> not rewritten. The correct rule is the discriminator below, not "everything else wants the timeout to fire".

---

## 7. The fix

> **Implementation pass (2026-08-08).** Every load-bearing claim above was re-derived with an independent
> instrument before any code changed — §3's blindness reproduced exactly (2 failed / 37 passed, the
> donation-named test green), and §4/§R3's scope claim re-checked from a different angle: the
> discriminator is not "short sleep" but *whether the test needs real work to **complete** inside the
> budget*. Most tight timeouts here want the timeout to **fire** (`sleep 10` vs `0.1s`, `sleep 999` vs
> `0.1s`), and load makes those **more** reliable, not less; the two lab-lane sleeps run under 10s
> budgets. **§R3's "one-off, do not budget a sweep" verdict holds, with the one exception noted there.**
>
> Three deltas from the draft:
>
> 1. **A second mutation was added**, because the draft's single mutation only exercised one of the two
>    assertions. `effective = timeout` (grants ignore elapsed time → `[10.0, 10.0, 10.0]`) leaves the
>    donation assert **green** and is caught only by the strict-ordering assert (`assert 10.0 > 10.0`).
>    So the two asserts guard genuinely distinct properties — donation, and the deadline still being
>    real — and each is now proved to fail independently.
> 2. **The ordering assert is documented as load-safe**, which is the subtle part: it requires *time to
>    advance* between grants, not work to finish inside a bound. Load makes it more true. That is what
>    keeps this fix from quietly reintroducing the very class of defect it removes.
> 3. **The donation assert was replaced, because the draft's version was the old defect in miniature.**
>    Review caught that `granted[2] > budget - 1.0` fails iff the first two commands burn a whole second
>    — a wall-clock bound with roughly a 7x margin on a cold worker whose first subprocess spawn lands
>    here, against the **4.8x** inflation that produced the original flake. It is now load-INVARIANT:
>    each grant must equal the *remaining* budget, `grant == approx(budget - elapsed)`, so a slower
>    machine moves the observed clock and the expected grant together and the margin cannot erode.
>    The reviewer proposed this replace **both** asserts; that was measured and rejected — under M2 the
>    grants are `[10.0, 10.0, 10.0]` against expectations `10.0 / 9.998 / 9.997`, i.e. 12–50x *inside*
>    the tolerance on a warm session, so an invariant-only form drops the M2 kill (or makes it
>    contingent on the machine being cold — the same sin in a new place). Both asserts landed, and the
>    reviewer re-proved and accepted that.
>
> Verification on the landed tree: mutation M1 (even split) → red on the **invariant** check
> (`3.3333333333333335 == 10.0 ± 0.1`); mutation M2 (deadline ignored) → red on the **ordering** check
> (`assert 10.0 > 10.0`), with the invariant loop shown executing and passing; 20/20 repeats green;
> `tests/unit/host/` 1425 passed; ruff clean; full gates green.

### The landed implementation

`tests/unit/host/test_run_timeout.py`, one file — +33/−8 as drafted, **+81/−10 as landed** (delta 3 above
reshaped both assertions and added the docstring rationale). Spy the seam that receives the budget while
still running the real `LocalHost` session; raise the budget so the sleep is never a race. **This is the
form that landed — do not lift the draft's `granted[2] > budget - 1.0`, which is the shape delta 3
removed:**

```python
budget = 10.0
host = LocalHost()
loop = asyncio.get_running_loop()
granted: list[tuple[float, float]] = []
run_one = host._run_one

async def spy(cmd, **kwargs):
    granted.append((loop.time(), kwargs["timeout"]))
    return await run_one(cmd, **kwargs)

with patch.object(host, "_run_one", new=spy):
    result = await host.run(
        ["echo fast1", "echo fast2", "sleep 0.1 && echo done"], timeout=budget,
    )

assert len(granted) == 3
# Donation, load-INVARIANT: each command is granted whatever is LEFT of the
# shared budget. An even split grants budget/3 and is red here.
first_clock, _ = granted[0]
for clock, grant in granted:
    expected = budget - (clock - first_clock)
    assert grant == pytest.approx(expected, abs=0.1), (
        f"grant {grant} is not the remaining budget {expected} "
        f"— donation regressed (an even split would grant {budget / 3})"
    )
# ...and the deadline is real, so grants strictly SHRINK. Load-SAFE, and the
# only one of the two that catches grants ignoring elapsed time entirely.
assert granted[0][1] > granted[1][1] > granted[2][1]

assert result.status == Status.Success
assert all(r.status == Status.Success for r in result)
assert "done" in result[2].value
```

Why this is not just a wider tolerance: it asserts the **allocation** (the property), keeps the real
end-to-end path (real shell, real fork+exec, real output), and the 10s budget makes `sleep 0.1` a 100x margin
instead of a 4.8x race.

**Verification record:**

| Check | Result |
|---|---|
| Red against the even-split mutation | **yes** — now fails with the other two (3 failed, was 2) |
| Green, repeated | 20/20 |
| `tests/unit/host/` full | 1417 passed (draft tree); **1425 passed** on the landed tree |
| `ruff check` + `ruff format --check` | clean |
| `src/` after mutation experiments | reverted; `git diff` on `src/` empty |

**Where it lives:** this commit, squashed onto `main` from branch `worktree-flaky-donation`, cut
fresh off `main` — not lifted from `worktree-gate-fresh`, whose copy was never used. (**As of 2026-08-08**,
that worktree carried its own uncommitted copy of an equivalent edit; whoever owns it can drop that hunk,
since main now has this one. Unverifiable from here and expected to go stale — treat as a note, not a fact.)

---

## 8. Re-derivation

```bash
# The failure
gh api repos/ludachrish3/otto-sh/actions/jobs/93090529980/logs | grep -A20 "=== FAILURES"

# The blindness (mutate src/otto/host/host.py::_run_cmds_with_budget to an even split, then)
.venv/bin/python -m pytest tests/unit/host/test_run_timeout.py -o addopts="" -q
#   pre-fix:  2 failed  (the integration test passes -> blind)
#   post-fix: 3 failed  (it now catches its own regression)

# The audit's blind spot — last touch predates the audit's first commit, so no wave ever saw it
git log --oneline -1 main -- tests/unit/host/test_run_timeout.py
grep -ri "run_timeout\|surplus\|donat" todo/test-infra-review-2026-08-06.md   # 0 hits
```
