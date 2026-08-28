# Reservations — the gate

`otto reservation` has no pipeline; its value is *transparency*. The
reservation **gate** runs invisibly inside other commands' preambles
({doc}`../lifecycle`); this command exposes the same machinery so a user can ask
"who does otto think I am?" and "would my run pass the gate?" without
starting a run.

## What is unique about `reservation`

- **Lab-free by design.** Identity and backend come from repo settings and
  root options, never from the lab and never from a host. `whoami` runs with
  no `--lab` at all; `check` is the one subcommand that loads the lab — the
  lab defines the required-resource list — and it does so lazily, through
  the same loud path the preamble uses. Neither contacts a remote host,
  which is also why the group opts out of per-invocation output directories.
- **`check` is the preamble's gate, standalone.** The required set is the
  lab's *declared* `resources` — hosts carry none, and for `--lab a+b` it is
  the union of the components' declarations. It asks the backend what
  the effective user holds, and reports what is missing and *who holds it*
  (backends answer `who_reserved` with a list — resources can have multiple
  concurrent holders). A one-second pre-flight before a twenty-minute
  `otto test`.
- **Break-glass stays honest.** Under `-R` / `--skip-reservation-check` the
  backend is never even constructed — a hanging scheduler cannot block lab
  access — but a factory is kept so `reservation` subcommands can still
  build it on demand. Contention errors deliberately do *not* advertise
  `-R`; only backend-unreachable errors do ({doc}`../../guide/cli/reservation/index`).

- **The gate runs in completion too, on its own terms.** Remote-path tab
  completion for `otto host <id> get` / `put` ({doc}`../../guide/cli/index`)
  contacts a lab host, so the same required-resource check runs first — before
  the host is even constructed. Two deliberate differences from the command
  path: `-R` does *not* bypass it (the loud skip warning has nowhere to print
  mid-TAB, and a silent break-glass is not one), and a backend failure fails
  closed to *no suggestions* rather than to an error, because a completer that
  prints is a completer that corrupts the user's prompt.
- **Windows are an optional capability, and only completion consumes them.**
  A backend that can report booking start/end implements
  {class}`~otto.reservations.protocol.SupportsReservationWindows`
  (isinstance-detected, like `SupportsUsernameCompletion`), and completion uses
  the edges to invalidate its cached reservation answer the moment a boundary
  passes. Owner ruling: that cache is completion-only — command execution
  always queries the backend live, because stale reservation data is an
  acceptable trade for a deliberate TAB and never for a recalled command. A
  unit test enforces that boundary by AST-scanning the tree: nothing but
  `otto.cli.remote_completion` may import the cache, the
  `--clear-autocomplete-cache` handler aside.

Backends are a registry like everything else (`json`, `none` built in;
custom schedulers register by name — {doc}`registries`), and
{func}`otto.testing.assert_reservation_backend_conforms` verifies a custom
one against the contract. See {doc}`../../guide/cli/reservation/index` for the
built-in JSON backend's configuration, `--as-user`, `-R`, and the full
walkthrough for writing and registering a custom backend.

## Where the code lives

- {mod}`otto.reservations` — the gate (`ReservationGate`), identity
  resolution, and the backend registry
- {mod}`otto.reservations.protocol` — the `ReservationBackend` Protocol every
  backend implements
- {mod}`otto.reservations.json_backend` / {mod}`otto.reservations.null_backend`
  — the two built-in backends
- {func}`otto.testing.assert_reservation_backend_conforms` — the conformance
  helper for custom backends
