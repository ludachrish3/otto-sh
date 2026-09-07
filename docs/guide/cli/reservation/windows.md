# Reservation times

Every reservation otto reads carries the times of the booking that holds it.
There is no opt-in and no capability to check: a backend returns
[`Reservation`](../../../api/reservations.rst) records, and each one has a
`start` and an `end`.

```python
Reservation(user="alice", resource="rack3-psu", start=..., end=...)
```

That is what lets otto tell you a booking is about to lapse (below) and lets
tab-completion notice when one begins or ends. Backends whose scheduler
records no times say so honestly, with `None`, rather than being a second,
dimmer class of backend.

## Where the rules live

The contract those times must satisfy — timezone-awareness, and the rest —
belongs to the implementer, and it is stated once, in
[Contract rules for implementers](../../../library/reservation-backends.md#contract-rules-for-implementers);
the window predicate and what each `None` bound means have their own section,
[The query window](../../../library/reservation-backends.md#the-query-window).
This page is the other half: what otto *does* with the times once a backend
reports them.

Two consequences are worth knowing as a user, whichever backend your team
runs. A booking your scheduler cannot put an end on is reported as
open-ended, so nothing below ever warns about it or treats it as stale. And a
backend whose format records only an expiry — the built-in JSON one, for
instance — reports no start at all, which costs you nothing here.

## The expiry warning

Because every backend reports `end`, otto can warn — once per booking per run
— when a reservation the current command actually needs is about to run out:

```text
⚠  Reservation for 'rack3-psu' expires in 3 minute(s) (at 15:30).
```

The rules, deliberately narrow:

- The window is **five minutes**, a module constant rather than a setting: it
  is a nudge, and a configurable nudge is a support question with no right
  answer.
- Only resources **this run requires** are mentioned. Racks you hold but are
  not touching are noise on every gated command.
- A booking with `end is None` never warns. One whose `end` has already passed
  does — that is the most urgent case there is.
- It is a warning, never a refusal. An expiring reservation is still a valid
  one, and the command proceeds.
- `-R` / `--skip-reservation-check` suppresses it wherever it suppresses the
  gate itself. It does **not** suppress it in `otto reservation check`
  ({doc}`check`): `-R` means "do not block me", and the command whose whole
  job is reporting reservation status should still say the booking is lapsing.

## What tab-completion does with them

Remote-path tab completion ({doc}`../index`) caches its reservation answer for
up to two minutes, and a booking edge cuts that short. Cross the start or end
of one of your bookings and the very next TAB re-asks the backend instead of
trusting a cached answer that a boundary just made wrong. A `None` bound
contributes no edge — an absent boundary is one that never arrives — so an
open-ended booking simply runs the full two minutes.

```{important}
This cache is read by tab completion and by nothing else. Every real command
invocation checks reservations live, straight against the backend. Accepting a
two-minute-old answer is a fair trade for a key you just pressed deliberately;
it is not a fair trade for a command recalled from history.
```

## What the conformance helper checks

[`assert_reservation_backend_conforms`](../../../library/reservation-backends.md#verify-your-backend)
enforces those contract rules for every backend, not just the ones that opt in
— including two checks of the window predicate, and what they can and cannot
see. That is implementer's ground, and it is written up where
implementers are:
[How the window predicate is checked](../../../library/reservation-backends.md#how-the-window-predicate-is-checked).
