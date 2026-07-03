# otto reservation — the gate, made inspectable

`otto reservation` has no pipeline; its value is *transparency*. The
reservation **gate** runs invisibly inside other commands' preambles
({doc}`index`); this pillar exposes the same machinery so a user can ask
"who does otto think I am?" and "would my run pass the gate?" without
starting a run.

## What is unique about `reservation`

- **Lab-free by design.** Identity and backend come from repo settings and
  root options, never from the lab and never from a host. `whoami` runs with
  no `--lab` at all; `check` is the one subcommand that loads the lab — the
  lab defines the required-resource list — and it does so lazily, through
  the same loud path the preamble uses. Neither contacts a remote host,
  which is also why the group opts out of per-invocation output directories.
- **`check` is the preamble's gate, standalone.** It computes the union of
  the lab's `resources` and every host's `resources`, asks the backend what
  the effective user holds, and reports what is missing and *who holds it*
  (backends answer `who_reserved` with a list — resources can have multiple
  concurrent holders). A one-second pre-flight before a twenty-minute
  `otto test`.
- **Break-glass stays honest.** Under `-R` / `--skip-reservation-check` the
  backend is never even constructed — a hanging scheduler cannot block lab
  access — but a factory is kept so `reservation` subcommands can still
  build it on demand. Contention errors deliberately do *not* advertise
  `-R`; only backend-unreachable errors do ({doc}`../../guide/reservations`).

Backends are a registry like everything else (`json`, `none` built in;
custom schedulers register by name — {doc}`../subsystems/registries`), and
{func}`otto.testing.assert_reservation_backend_conforms` verifies a custom
one against the contract.
