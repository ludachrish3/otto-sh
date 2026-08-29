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
- **`check` is the preamble's gate, standalone.** It computes the same
  required set the preamble does, asks the backend what the effective user
  holds, and reports what is missing and *who holds it* (backends answer
  `who_reserved` with a list — resources can have multiple concurrent
  holders). A one-second pre-flight before a twenty-minute `otto test`.
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

## Three levels, one reader

A lab declares reservation identifiers at three levels — the lab as a whole,
an element, a host entry — and the gate has to answer one question from all
three: *what must this user hold before this run starts?*

The answer is a list of **origins**, not a bare set of strings.
{class}`~otto.reservations.check.ResourceOrigin` carries the identifier, the
level that declared it (`lab`/`element`/`host`), and the owner at that level —
the lab name, the element rendered as `('chassis', 1)`, or the host id.
{func}`~otto.reservations.check.required_resource_origins` builds them;
{func}`~otto.reservations.check.required_resources` is the projection down to
the identifiers, so there is one derivation and two views of it. Keeping the
origin is what lets a failure say *which slot* is missing rather than only
which string, and what lets `otto reservation check` explain a requirement it
would otherwise just assert.

Carriage is by the road each level's neighbour already travels: the lab set
stays on `host.lab_info.resources`, the element's is stamped onto every host of
the element as `host.element_resources` (beside `element_metadata`), and the
host entry's is `host.resources`. Both host-side fields are `frozenset[str]`,
so no caller can mutate one lab's declaration through a host it happens to
hold.

The set is computed over the **fleet of interest**, not the whole lab:
{meth}`~otto.context.OttoContext.admissible_ids` — the same set every fleet
walk starts from, public since this work precisely so the gate and the walks
cannot disagree about which hosts a run may touch. One definition, two
readers. A declared fleet that admits no host in the loaded lab is **zero
hosts in play**, not a refusal: the requirement narrows to the lab's own set
and every reservation reader passes `require_nonempty=False` to say so.

Reservation readers reach that set through
{func}`otto.config.fleet.get_hosts_in_play`, which applies the one adjustment
the gate makes and the walks do not: the built-in `local` host is subtracted.
Otto can always run on the machine it is running on, so requiring a slot to
reach it would be a footgun with no upside. The subtraction is by host
identity, not by the id
string (`otto.host.builtin_hosts.is_builtin_host`) — a lab that declares its
own `local` entry suppresses the built-in host altogether, and that entry's
`resources` are enforced like any other's.
All four reservation readers — the gate, `otto reservation check`, the
explicit-target check in `otto host`, and completion's cached gate — go
through that accessor, so none can drift from the others.

The
fleet-shaped `ProjectScopeError` stays with the WALK — a run that walks still
aborts on it, with the same message, exactly where it did before this gate
existed — so the gate never becomes a new abort surface. Completion's gate
builds a temporary context and passes that context's ids, so a TAB and a run
agree about what is needed.

## Where the code lives

- {mod}`otto.reservations` — the gate (`ReservationGate`), identity
  resolution, and the backend registry
- {mod}`otto.reservations.protocol` — the `ReservationBackend` Protocol every
  backend implements
- {mod}`otto.reservations.json_backend` / {mod}`otto.reservations.null_backend`
  — the two built-in backends
- {func}`otto.testing.assert_reservation_backend_conforms` — the conformance
  helper for custom backends
