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

1. **Hostless unit test — `confirm_live` + `_recover_session` with a `ZephyrFrame`.**
   Mirror the bash MockSession recovery tests in `tests/unit/host/test_session.py`
   (`TestTimeout`), but construct the mock session with a `ZephyrFrame` (the
   `ShellSession.__init__` `command_frame=` param already supports this). Assert:
   - a hung command that is interrupted and then answers with the bare
     `{recover}` token → `_recover_session` confirms and the session stays alive
     (the resend loop tolerates a first dropped probe — feed the token only on
     the 2nd probe to prove resend works on the Zephyr dialect too);
   - a command that never answers → recovery times out and marks the session
     dead (`_alive is False`), no false positive.
   This is the durable core: it proves the dialect-agnostic loop drives the
   Zephyr probe/pattern correctly. No bed required.

2. **(Optional) Live-bed embedded recovery test.** If a Zephyr bed is routinely
   available in CI, add an `@pytest.mark.embedded` recovery test that drives a
   real command timeout on a Zephyr host (ssh hop `10.10.200.14` → telnet Zephyr
   shell) and confirms `_recover_session` recovers via the `retval`/bare-token
   path with no regression. Otherwise leave the live check to the Task 6 manual
   matrix and rely on (1) for durable coverage.

## Notes

- No product change is anticipated — this is test coverage. If (1) surfaces a
  real defect in the Zephyr recovery path, that becomes a bug fix on top.
- `ZephyrSerialFrame` (serial embedded) inherits the same default
  `recover_pattern`; a parametrized version of (1) over
  `[ZephyrFrame(), ZephyrSerialFrame()]` covers both with one test body.
- Related: the echo-proof root fix that motivated this whole area is now
  implemented (see `todo/app-shell-recover-session-echo-proof.md`).
