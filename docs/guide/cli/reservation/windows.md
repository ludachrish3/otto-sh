# Reservation windows

Some schedulers know only "alice holds rack3 right now"; others know the whole
booking — that it runs from 09:00 to 17:00. A backend that knows the second
kind opts in by implementing the optional
[`SupportsReservationWindows`](../../../api/reservations.rst) capability, a single
method:

```python
def get_reservation_windows(self, username: str) -> list[ReservationWindow]: ...
```

Each [`ReservationWindow`](../../../api/reservations.rst) is a resource plus the
`start` and `end` of the booking that holds it. The semantics otto relies on:

- **Both timestamps are timezone-aware.** A naive `datetime` is a contract
  violation, not a UTC guess.
- **`start` at the Unix epoch means "start unknown".** Use it when your
  scheduler records only an expiry — the booking is treated as having been
  held all along. The built-in JSON backend does exactly this, because its file
  format records `expires` and nothing else.
- **A far-future `end` means "open-ended".** A booking with no expiry never
  goes stale on its own.
- **Return only live bookings** — expired ones are omitted. The set of
  resources whose window covers *now* must equal what
  `get_reserved_resources()` returns for the same user — the two views are
  answers to the same question, so they must not disagree.

Otto detects the capability structurally (no inheritance, no registration) and
uses it in exactly one place: **remote-path tab completion**
({doc}`../index`) caches its reservation answer for up to two minutes, and
a window edge cuts that short. Cross the start or end of one of your bookings
and the very next TAB re-asks the backend instead of trusting a cached answer
that a boundary just made wrong. A backend that omits the capability loses
nothing but that precision — its cached answer simply runs the full two
minutes.

```{important}
This cache is read by tab completion and by nothing else. Every real command
invocation checks reservations live, straight against the backend. Accepting a
two-minute-old answer is a fair trade for a key you just pressed deliberately;
it is not a fair trade for a command recalled from history.
```

[`assert_reservation_backend_conforms`](../../../library/reservation-backends.md#verify-your-backend) checks these
rules whenever the backend it is given implements the capability — timezone
awareness, `start <= end`, and agreement with the flat
`get_reserved_resources()` view. Backends that don't implement it are not
penalized; the rules simply don't run.
