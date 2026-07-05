# Echo-proof `_recover_session` for REPL-parked sessions (deferred)

Goal: make `ShellSession._recover_session()` reliably confirm the POSIX shell
is back even when the session was parked *inside* an application REPL (mysql,
python3) — so an `AppShell.attach()` caller can keep using the session after an
`AppShellTimeoutError` instead of having to discard it.

Deferred out of the AppShell (Part 2) branch by decision (2026-07-04): the
AppShell branch ships with a documented caveat on `AppShell.attach()` ("discard
the session after `AppShellTimeoutError`"), and this root-cause fix lands as its
own focused, live-bed-validated change because it touches the shared Part-1
recovery machinery, not just AppShell.

## The problem (from the Part 2 whole-branch review, finding I-3)

`_recover_session()` writes Ctrl+C, then `echo __OTTO_<id>_RECOVER__`, and
declares success on a bare **substring** match of the marker in the output.
That confirmation is unsound when the session is sitting inside a REPL:

- Ctrl+C does **not** exit mysql or python3 — both trap SIGINT and stay at
  their prompt.
- The `echo __OTTO_<id>_RECOVER__` payload then lands *inside the REPL*, and
  both REPLs quote the offending input back in their error output — mysql:
  `ERROR 1064 ... near 'echo __OTTO_..._RECOVER__'`; python3: the `SyntaxError`
  traceback echoes the source line. So the marker appears in output, the
  substring matches, and recovery reports success **while the session is still
  parked inside the app**.

Impact (today, mitigated by the caveat): on the caller-owned
`AppShell.attach()` path, a `cmd()` timeout → `_exit` skips quit (correct),
"recovers" (false positive), unlocks — and the caller's session is
poisoned-but-green: every later `run()` types a sentinel frame into the REPL,
times out at its own budget, and "recovers" falsely again. `BaseHost.app_shell`
is unaffected — it owns and closes the session, so a poisoned session is simply
discarded.

This is pre-existing `_recover_session` machinery; Part 2 only made
"sitting inside a REPL" a first-class, reachable state, which turns the latent
weakness into a reachable failure of the documented "still recovers" guarantee.

## Proposed fix: an echo-proof framed confirmation

A plain echoed marker can be *reproduced* by a REPL, so it can never prove the
shell actually ran. Follow the recovery marker with a **framed probe** whose
end sentinel embeds `$?` (the same mechanism the command frame already uses —
see `src/otto/host/command_frame.py`, the `END_<prefix>__<N>__` form):

- A real POSIX shell expands `$?` and emits `...__0__` (or the real exit code).
- An echoing REPL can only reproduce the literal `$?` text; it cannot fabricate
  the `__<digits>__` exit-code form. A match on the digit-form end sentinel is
  therefore proof of genuine shell execution, not an echo.

Confirm recovery on the framed-probe match, not the bare marker substring. This
strengthens `_recover_session()` for **all** of otto (any path that can leave a
transport mid-interaction), not just AppShell.

## Validation required (why this is its own change)

- Live-bed: drive `_recover_session` from *inside* mysql and python3 (and a
  wedged bash) and confirm the framed probe reports failure/true-state, not a
  false positive.
- Confirm no regression on the clean recovery path (SSH/telnet/local) already
  covered by the recovery tests.
- Once landed, revisit the `AppShell.attach()` caveat: if recovery becomes
  trustworthy from inside a REPL, the "discard the session" note can be relaxed,
  and the I-2 `_needs_recovery` mark on launch timeout becomes a genuine
  self-heal rather than a best-effort.

## Related deferred minors from the Part 2 review (low priority backlog)

Non-blocking; triaged "defer" in the whole-branch review. Pick up opportunistically:

- **M12-3** — add unit tests for two already-correct nested edges: a nested
  region that doesn't match its sub-pattern (clean `ParseMismatch` propagation),
  and a `Sub | None` *Parsed*-typed optional field (the current optional test
  only covers scalar `str | None`). Missing tests, not missing behavior.
- **M12-4** — `parse_one`/`parse_all` could return `TypeVar("P", bound=Parsed)`
  instead of `Parsed`/`list[Parsed]` so callers keep the concrete static type.
  Ergonomics only; tightening breaks no caller.
- **N-1** — `apply_parse(Parsed, ...)` on the abstract base itself dies with a
  bare `AttributeError` (no `pattern`); a `spec is not Parsed` guard would give
  a crisp `TypeError`. Authoring-error polish.
- **N-2** — after an `AppShellTimeoutError` from `cmd()`, if user code swallows
  it and calls `cmd()` again, the broken shell sends another line (`_broken`
  only gates `_exit`). A `if self._broken: raise` precondition in `cmd` fails
  faster/clearer. Edge-case hardening.
- **N-3 / M15** — an un-parsed `cmd()` always returns `Status.Success`
  regardless of the app's own error text (a parse mismatch is the intended
  in-band failure signal). Sound by design; add one sentence to the sessions
  cookbook / `cmd` docstring so users find it where they look. Consider a future
  opt-in app-level error hook if demand appears.
- **M16-1** — `docs/guide/host-database.md` quotes the legacy-dict `ValueError`
  hard-wrapped across two lines vs the real single-line string. Cosmetic.
