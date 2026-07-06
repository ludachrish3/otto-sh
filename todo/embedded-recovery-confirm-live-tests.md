# Embedded (Zephyr) recovery through `confirm_live` — add durable tests (deferred)

Goal: prove, with durable automated tests, that the unified shell-liveness
recovery mechanism (`otto.host.shell_liveness.confirm_live`) works on
**embedded / Zephyr** hosts, not just bash. Owner-requested 2026-07-05 during
the shell-liveness-probe-unification branch; queued as a follow-up so it does
not block that branch.

## Why embedded already works by construction (no code change expected)

Embedded hosts (`EmbeddedHost`, `src/otto/host/embedded_host.py`) declare a
`ZephyrFrame` and connect over telnet, i.e. they run on `TelnetSession`
(`src/otto/host/session.py`). `TelnetSession` does **not** override
`_recover_session` — it inherits the base `ShellSession._recover_session`, which
after this branch sends the interrupt then delegates to `_confirm_recovered`,
driving `confirm_live` with `self._frame.recover` / `self._frame.recover_pattern`.
For a Zephyr host `self._frame` is a `ZephyrFrame`, so recovery uses the Zephyr
dialect's probe:

- `ZephyrFrame.recover(m)` → the bare `{recover}\n` token (NOT the bash
  `echo "{recover}$?__"` exit-code form).
- `recover_pattern(m)` → the inherited base default `re.compile(re.escape(m.recover))`
  (bare-token match).

The Zephyr shell rejects the unknown `{recover}` command and its error handler
prints the token back — shell **output**, not input echo — so the bare-token
match is a sound liveness signal for that dialect. Zephyr deliberately does NOT
get the echo-proof `$?` treatment (design spec §3.2): an embedded shell is never
parked in a nested REPL and does no su/sudo user-switching, so none of the
failure modes echo-proofing defends against apply. Embedded recovery therefore
**gains** the resend-until-deadline robustness of `confirm_live` (a strict
improvement over the old single-shot recover) with no behavior change to its
probe shape.

Net: the mechanism is expected to work on embedded today. The gap is that
nothing asserts it — coverage is frame-level unit tests (`ZephyrFrame.recover` /
`recover_pattern`) plus a manual Task 6 live-bed check, not a durable recovery
test through `confirm_live` with a Zephyr frame.

## What to add

1. ~~**Hostless unit test — `confirm_live` + `_recover_session` with a `ZephyrFrame`.**~~
   **✅ DONE (on the shell-liveness-probe-unification branch):**
   `tests/unit/host/test_session.py::TestZephyrRecovery` — a `MockSession` built with
   `command_frame=ZephyrFrame()`/`ZephyrSerialFrame()` drives the real base
   `ShellSession._recover_session` → `confirm_live` path: `test_recovery_confirms_on_bare_token`
   (a hung, interrupted command answered with the bare `{recover}` token recovers,
   session stays alive) and `test_recovery_marks_dead_when_unanswered` (an
   unanswered probe exhausts the deadline → session dead, no false positive), both
   parametrized over the two Zephyr frames. Generic resend-until-deadline is already
   covered by `test_shell_liveness.py::test_resends_past_timeouts_then_confirms`, so
   these focus on the Zephyr probe/pattern through the real recovery method. No bed
   required. This closes the durable-coverage gap; only the e2e harness below remains.

2. **(Optional) Live-bed embedded recovery test.** If a Zephyr bed is routinely
   available in CI, add an `@pytest.mark.embedded` recovery test that drives a
   real command timeout on a Zephyr host (ssh hop `10.10.200.14` → telnet Zephyr
   shell) and confirms `_recover_session` recovers via the `retval`/bare-token
   path with no regression. Otherwise leave the live check to the Task 6 manual
   matrix and rely on (1) for durable coverage.

## E2e embedded harness — a trivial REPL shell as a repo product (owner-requested 2026-07-05)

The larger motivation: **otto has no e2e embedded testing today.** The embedded
recovery path is exercised only by unit/frame-level tests and by manual live-bed
checks — there is no automated, real-target proof that the recovery mechanism
(or command framing generally) actually works against a Zephyr shell. The bash
and Zephyr dialects are unified behind one `confirm_live` loop *today*, but if
they ever need to diverge, we want the validation harness already in place
rather than discovering the gap under pressure. It does not need to be elaborate
— it just needs to **exist** so embedded behavior can be verified in good faith.

Proposal: build a **minimal REPL/shell as a repo test product** and stand up one
real e2e embedded test around it.

- **The product can be trivial.** A tiny Zephyr application whose only job is to
  present an interactive prompt otto can drive: either the stock Zephyr shell
  subsystem (`CONFIG_SHELL=y`) with one custom command that can be made to hang
  (so a command timeout → `_recover_session` cycle is reachable), or an even
  simpler bespoke read-eval-print loop over the console. Place it under the
  existing test-product convention (see `tests/repo*/product/` and its
  `build.sh`), so it builds and deploys through the same path real embedded
  products use. Keep it as small as the Zephyr build system allows.
- **The e2e test** (`@pytest.mark.embedded`): deploy the product to the Zephyr
  bed (ssh hop `10.10.200.14` → telnet Zephyr shell), then drive at least:
  (a) a normal framed command round-trip (proves `ZephyrFrame` framing e2e);
  (b) a command timeout followed by `_recover_session`, asserting the session
  recovers via the bare-token `retval` path with no regression. This is the
  first real embedded e2e in the repo — it establishes the harness; more cases
  can follow once it exists.
- **Value:** turns "embedded recovery works by construction" into "embedded
  recovery is verified against a real target," and gives any future bash/Zephyr
  divergence a place to be caught automatically.
- **Cost / why deferred:** a Zephyr product build + bed deploy + CI wiring is
  substantial and touches the real embedded bed (never power/reboot VMs without
  the owner's say-so). This is a follow-up effort in its own right, not part of
  the shell-liveness-probe-unification branch. The hostless unit test (item 1
  above) remains the cheap durable coverage to land first; this harness is the
  higher-investment, higher-fidelity complement.

## Notes

- No product change is anticipated — this is test coverage. If (1) surfaces a
  real defect in the Zephyr recovery path, that becomes a bug fix on top.
- `ZephyrSerialFrame` (serial embedded) inherits the same default
  `recover_pattern`; a parametrized version of (1) over
  `[ZephyrFrame(), ZephyrSerialFrame()]` covers both with one test body.
- Related: the echo-proof root fix that motivated this whole area is now
  implemented (see `todo/app-shell-recover-session-echo-proof.md`).
