# Reservation times

Every reservation otto reads carries the times of the booking that holds it.
There is no opt-in and no capability to check: a backend returns
[`Reservation`](../../api/reservations.rst) records, and each one has a
`start` and an `end`.

```python
Reservation(user="alice", resource="rack3-psu", start=..., end=...)
```

That is what lets otto tell you a booking is about to lapse (below) and lets
tab-completion notice when one begins or ends. Backends whose scheduler
records no times report `None`.

## Where the rules live

Backend implementers: the timing contract is in
[Contract rules for implementers](../../library/reservation-backends.md#contract-rules-for-implementers)
and [The query window](../../library/reservation-backends.md#the-query-window).

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

The rules:

- The window is **five minutes**, and it is not configurable.
- Only resources **this run requires** are mentioned, not every rack you
  hold.
- A booking with `end is None` never warns. One whose `end` has already passed
  does.
- It is a warning, never a refusal. An expiring reservation is still a valid
  one, and the command proceeds.
- `-R` / `--skip-reservation-check` suppresses it wherever it suppresses the
  gate itself. It does **not** suppress it in `otto reservation check`
  ({doc}`check`).

## What tab-completion does with them

Remote-path tab completion ({doc}`../index`) caches its reservation answer for
up to two minutes, and a booking edge cuts that short. Cross the start or end
of one of your bookings and the very next TAB re-asks the backend instead of
trusting a cached answer that a boundary just made wrong. A `None` bound
contributes no edge, so an open-ended booking simply runs the full two
minutes.

```{important}
This cache is read by tab completion and by nothing else. Every real command
invocation checks reservations live, straight against the backend.
```

## What the conformance helper checks

[`assert_reservation_backend_conforms`](../../library/reservation-backends.md#verify-your-backend)
enforces those contract rules for every backend, not just the ones that opt in
— including two checks of the window predicate, and what they can and cannot
see — see
[How the window predicate is checked](../../library/reservation-backends.md#how-the-window-predicate-is-checked).
