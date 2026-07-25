# AppShell follow-ups after the echo-proof `_recover_session` fix

> The echo-proof root fix SHIPPED 2026-07-05 (`confirm_live` +
> `BashFrame.recover` exit-code probe on the shell-liveness-probe-unification
> branch): recovery from inside a mysql/python3 REPL now correctly reports the
> session dead instead of a false positive. Shipped detail pruned 2026-07-25;
> what remains is the relaxation below plus the deferred-minors backlog.

## Remaining: relax the `AppShell.attach()` caveat

`AppShell.attach()` still documents "discard the session after
`AppShellTimeoutError`" (`src/otto/host/app_shell.py`) — a caveat from the era
when `_recover_session` could report success while the session was still
parked inside a REPL. Now that recovery is echo-proof and trustworthy, revisit
it: the "discard the session" note can likely be relaxed, and the I-2
`_needs_recovery` mark on launch timeout becomes a genuine self-heal rather
than a best-effort. Confirm on the live bed (recovery from inside mysql /
python3 / a wedged bash) before changing the documented contract.

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
