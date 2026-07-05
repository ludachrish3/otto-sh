# Shell-liveness probe unification

**Status:** design — approved (pending spec review)
**Date:** 2026-07-05
**Author:** Chris Collins (with Claude)
**Supersedes (interim):** the `_RESYNC_SETTLE` + `_RESYNC_TIMEOUT` hardening shipped in `otto.host.login_proxy` for the `make release` 3.13 flake.

## 1. Motivation

otto confirms "a real shell is back at its prompt" in **three** places, each hand-rolled and each brittle in its own way:

1. **Session handshake** — `ShellSession._ensure_initialized` (session.py). Sends `frame.handshake()`, waits for the `READY` marker, **resends on a short interval bounded by an overall deadline**. This one is *robust* — it is the template.
2. **Post-timeout recovery** — `ShellSession._recover_session` (session.py). Sends Ctrl+C then `frame.recover()`, then confirms on a **bare substring** match of the `RECOVER` marker, **single-shot** (no resend).
3. **Post-transition resync** — `login_proxy._resync_shell` (login_proxy.py). After a su/sudo/exit hop, sends `echo <uuid-marker>` and confirms with a **negative-lookbehind** (`(?<!echo )<marker>`), retried a **fixed 5×2 s**.

Two confirmed defects flow from (2) and (3):

- **3.13 flake (this cycle) — `_resync_shell`.** Live-bed evidence (40/40 roundtrips) shows the first probe after a transition is **deterministically eaten** by the su/sudo/exit tty-flush (the transition command and the `echo <marker>` are written back-to-back, so the marker lands in the flush window). Recovery then depends on the remaining fixed-2 s attempts each completing a round-trip in time; under `make release` load (nox ×3 + subprocess-coverage saturating the client VM) they don't, and all 5 are exhausted → `LoginProxyError`. A pre-probe settle eliminates the eaten attempt (live-bed: attempt 1 lands in ~7 ms with a 0.3 s settle).
- **Foreseen I-3 — `_recover_session`.** A bare echoed marker cannot *prove* the shell ran: parked inside a REPL (mysql/python3), Ctrl+C does not exit, the marker echoes back inside the REPL's error text, the substring matches, and recovery **falsely reports success**. Documented today via an `AppShell.attach()` caveat; see `todo/app-shell-recover-session-echo-proof.md`.

Both are the same class of brittleness: **an echoed marker is not proof of execution, and a fixed per-attempt retry budget starves under load.** This design unifies the confirmation into one primitive that is *echo-proof* (where the dialect supports it) and *flush/load-robust*, and routes (2) and (3) through it. (1) already embodies the robust half and is left in place, but is refactored to share the retry loop.

## 2. Goals / non-goals

**Goals**
- One shared, dialect-agnostic **resend-until-deadline** liveness loop, reused by `_recover_session` and `_resync_shell` (and the handshake, if it falls out cleanly).
- **Echo-proof confirmation on POSIX/bash**: a match proves genuine shell execution, so a REPL/echo cannot fake it — fixes I-3 and removes the `(?<!echo )` heuristic.
- **Flush/load robustness**: a pre-probe settle + a generous overall deadline with fast resends — fixes 3.13 at the root.
- **Works on both host families**: bash (SSH/telnet/local unix, docker) *and* Zephyr embedded — via the `CommandFrame` dialect abstraction. No regression to Zephyr recovery.

**Non-goals**
- No change to the *command* path (`run_cmd`) framing or parsing.
- No new lab-data / host-schema fields.
- The interim `_resync_shell` hardening already shipped; this design replaces it but does not depend on it.

## 3. Design

### 3.1 The dialect owns the probe; a shared helper owns the timing

Two responsibilities, cleanly split:

- **`CommandFrame` (dialect knowledge)** renders the probe payload and supplies its confirmation pattern. The echo-proofness is a *property the dialect provides*, not something the loop knows about.
- **A new `otto/host/shell_liveness.py` (I/O orchestration)** owns the resend-until-deadline loop, working over plain `send` / `expect` callables so it is agnostic to whether the caller is a `ShellSession`, a `ProxyIO`, or the interact bridge.

```python
# otto/host/shell_liveness.py  (sketch — final API in the plan)
async def confirm_live(
    send: Callable[[str], Awaitable[None]],
    expect: Callable[[re.Pattern[str], float], Awaitable[str]],
    frame: CommandFrame,
    *,
    settle: float,
    probe_timeout: float,
    deadline: float,
) -> bool:
    """Prove a real shell is at its prompt. Resend a fresh framed probe on a
    short interval until confirmed or the deadline passes."""
    await asyncio.sleep(settle)                      # absorb the transition flush
    loop = asyncio.get_running_loop()
    stop = loop.time() + deadline
    while loop.time() < stop:
        m = SessionMarkers.for_session(uuid.uuid4().hex[:12])   # fresh per probe
        await send(frame.recover(m))                            # dialect probe payload
        remaining = stop - loop.time()
        if remaining <= 0:
            break
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await expect(frame.recover_pattern(m), min(probe_timeout, remaining))
            return True
    return False
```

Key properties:
- **Fresh markers per probe** — a slow probe's late output can never satisfy a later probe, and the recover probe never collides with the session's *command* markers (so it can't match a stale command END sentinel left in the buffer).
- **Settle then resend** — the settle makes the common case land on probe 1; the resend loop covers the tail (a settle that under-absorbs under heavy load, or a genuinely slow round-trip).
- **`probe_timeout` decoupled from `deadline`** — the exact fix for 3.13: how long to wait for *one* probe is independent of how long to *keep trying*. The old code fused them into 5×2 s.

### 3.2 Frame API additions

`CommandFrame` already has `recover(m)` (render) and `end_pattern(m)`. We formalize the confirmation:

- Add `recover_pattern(self, m) -> re.Pattern[str]` — the pattern that *proves execution* for this dialect.
- **`BashFrame`**: change `recover(m)` to an **exit-code probe** — `echo "{end_prefix}$?__"\n` — and return `end_pattern(m)` (`{end_prefix}(\d+)__`) from `recover_pattern`. The `(\d+)` is `$?` **expanded by a real shell**; an echo of the probe carries the literal `$?`, and a REPL (mysql/python3) errors on the line, so neither can produce the digit form. Echo-proof in echo-on, echo-off, and REPL-parked states — no lookbehind, no line-anchor needed.
- **`ZephyrFrame` / `ZephyrSerialFrame`**: `recover(m)` stays the bare `{recover}\n` token (the shell rejects it and its error handler echoes it back — shell *output*, not input echo); `recover_pattern(m)` returns `re.compile(re.escape(m.recover))`. Zephyr has no `$?`, but also none of the failure modes echo-proofing defends against: an embedded shell is never parked in a nested REPL and does no su/sudo user-switching. Zephyr therefore keeps its current confirmation and only **gains the resend-until-deadline robustness** — a strict improvement over today's single-shot recover, with no behavior change to its probe shape.

This is the crux of "works on both families": the loop is identical; the dialect supplies a probe whose match means *live shell* in terms appropriate to that shell.

### 3.3 Call-site integration

- **`_recover_session`** (all dialects): keep the Ctrl+C + 0.1 s pause, then call `confirm_live` with `self._write` and an `expect` adapter (`lambda p, t: asyncio.wait_for(self._read_until_pattern(p), t)`) and `self._frame`. On `False`, mark the session dead (as today). On bash this now correctly *fails* when parked in a REPL (fixes I-3); on Zephyr it behaves as before but with retries.
- **`_resync_shell`** (bash only — login proxies are a `PosixPrivilege` path; Zephyr embedded hosts have no user-switching): call `confirm_live` with `io.send` / `io.expect` and a `BashFrame()`. This works over all three `ProxyIO` implementations — `_HostProxyIO`, `_SessionProxyIO`, and the echo-ON interact `_BridgeProxyIO` — because the digit-form match is echo-proof over each. Raise `LoginProxyError` on `False`, preserving the current wrapping.
- **`_ensure_initialized`** (handshake): optional. It already has the robust loop; refactoring it onto `confirm_live` is a nice-to-have consolidation, not required. Proposed: refactor if it lands cleanly without perturbing the failed-login `ConnectionError` semantics; otherwise leave and note the shared lineage.

### 3.4 Retired

- `login_proxy._RESYNC_ATTEMPTS`, `_RESYNC_TIMEOUT`, `_RESYNC_SETTLE` (interim) and the `(?<!echo )` lookbehind probe.
- `_recover_session`'s bare-substring confirmation.
- Once recovery is trustworthy from inside a REPL, revisit the `AppShell.attach()` "discard the session" caveat (per the follow-up doc) — tracked, not necessarily done here.

## 4. Testing & validation

**Unit**
- `shell_liveness.confirm_live`: settles before the first probe; resends past N timeouts then confirms; returns `False` at the deadline; uses fresh markers per probe (injected tiny settle/timeout/deadline; a fake `send`/`expect`).
- `BashFrame.recover`/`recover_pattern`: the probe is the exit-code form; the pattern matches `_END__0__` / `_END__130__` but **not** the echoed literal-`$?` probe line.
- `ZephyrFrame.recover_pattern`: matches the bare recover token.

**Live-bed matrix (why this is its own change — it touches shared Part-1 recovery machinery)**
- Bash su/sudo/exit `as_user` roundtrip (the 3.13 test) — loop it; assert fast + stable.
- `interact --as-user` bridge (echo-ON) — resync still confirms.
- `_recover_session` driven from **inside mysql** and **inside python3** — must report *failure* (session dead), not a false positive (I-3).
- `_recover_session` on a **wedged bash** — must recover.
- **Zephyr** `_recover_session` — drive a command timeout on the Zephyr bed (ssh hop → telnet Zephyr shell) and confirm recovery still works with the new retry loop; no regression to the `retval`/bare-token path.
- Regression: existing SSH/telnet/local recovery + handshake tests stay green; full `make coverage` + login-proxy/app-shell e2e.

## 5. Rollout & risk

- **Interim already shipped** (staged): settle + larger `_RESYNC_TIMEOUT` in `_resync_shell` — unblocks `make release` now. This redesign lands after, as the durable fix, and removes the interim knobs.
- **Blast radius**: shared session recovery (Part-1) + login-proxy resync. Mitigated by the frame-delegated design (dialect-specific probe, one loop) and the live-bed matrix above.
- **Open questions for the plan**: (a) exact `settle` / `probe_timeout` / `deadline` defaults (start from the handshake's 0.5 s interval / a ~15 s deadline; tune on the bed); (b) whether to fold `_ensure_initialized` onto `confirm_live` now or note it; (c) home of the helper (`shell_liveness.py` vs a `command_frame` free function) — leaning to a dedicated module for isolation.
