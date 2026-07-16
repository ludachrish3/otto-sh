# Lab Reservations

Otto runs commands against real lab hardware.  If two people kick off
work against the same rack at the same time, the results of both runs
are compromised.  The reservation subsystem is a gate that refuses to
start commands which touch hardware the current user does not hold —
pulling the "who has what reserved" answer from whatever scheduler your
team already uses (Jira, a homegrown booking tool, a shared JSON file).

Otto is strictly a consumer of reservation data.  It never creates,
edits, or releases a reservation — the external scheduler remains
authoritative.

```{note}
Wiring up reservations is a one-time, team-level decision. See the
{ref}`team-setup-checklist` in {doc}`setup/repo-setup` for the full onboarding map.
```

See {doc}`../architecture/subsystems/reservations` for what makes
`reservation` unique among the first-party commands — lab-free identity,
`check` as the preamble's gate standalone, and the break-glass design.

## `otto reservation --help`

```{raw} html
:file: ../_static/generated/termynal/help-reservation.html
```

## What gets checked, and where

The gate runs at the top of every live-lab subcommand:

| Command               | Gated? | Notes                                              |
|-----------------------|--------|----------------------------------------------------|
| `otto run ...`        | yes    |                                                    |
| `otto test ...`       | yes    |                                                    |
| `otto host ...`       | yes    |                                                    |
| `otto monitor ...`    | yes    | Only in live collection mode; file replay is free. |
| `otto cov report ...` | no     | Offline; never touches hardware.                   |
| `otto reservation ...`| no     | The whoami/check helpers only *report* on state.   |

For each gated invocation, otto computes the **required set** as the
union of the selected lab's `resources` plus every `UnixHost.resources`
in that lab, then asks the configured backend which of those the
effective user holds.  Anything missing raises an error and the command
does not run.

## Quick start: JSON backend

The built-in JSON backend is the fastest way to experiment and, for
small teams, a perfectly adequate production setup.

Add a `[reservations]` section to your repo's `.otto/settings.toml`:

```toml
[reservations]
backend = "json"

[reservations.json]
path = "${sut_dir}/.otto/reservations.json"
```

Create the file the `path` setting points at:

```json
{
  "version": 1,
  "reservations": [
    {"user": "alice", "resources": ["rack3-psu", "smartbits-07"]},
    {"user": "bob",   "resources": ["rack4-psu"], "expires": "2026-05-01T00:00:00Z"}
  ]
}
```

That is the complete setup.  `otto run`, `otto test`, `otto host`, and
`otto monitor` now refuse to start on any lab whose required resources
alice does not hold — the existing error path in Typer renders the
failure cleanly with missing resource names and their current holders.

### File format

The top-level object has two required fields:

`version`
: Integer schema version.  Currently only `1` is supported.  Bumping
  this value will be reserved for breaking changes.

`reservations`
: List of reservation records.  Each record has:

  * `user` *(string, required)* — the reservation-system username.
  * `resources` *(list of strings, required)* — resource identifiers
    the user holds.  Must match byte-for-byte the strings in
    `UnixHost.resources` and `Lab.resources`.
  * `expires` *(string, optional)* — ISO-8601 timestamp.  Past-dated
    entries are silently ignored.  Omit for "no expiry".

A user may appear in multiple records — the effective set is the union.
This is intentional: if your booking source has multiple entries for the
same person, you don't need to merge them before writing the file.

### Choosing a location

Two common layouts work well:

- **Checked-in** — put `reservations.json` under `.otto/` in the repo
  and commit it.  Reservation changes land as normal PRs; the full git
  history shows who had what and when.  Good when churn is low and a
  PR-level review is desirable.
- **Shared volume** — point `path` at a file on a networked volume
  (`/mnt/team/reservations.json`) or an absolute path that's synced by
  some other tool.  Good when reservations change frequently throughout
  the day and PR overhead would feel absurd.

Relative paths are resolved against the repo root.  `${sut_dir}` expands
to the repo root too, so either works.

## Overriding the default user

By default otto queries the backend using `getpass.getuser()` — i.e.
your shell login.  Pass `--as-user` on the top-level callback to query
as someone else:

```bash
otto --as-user alice test TestSmoke
otto --as-user alice host router1 run "uname -a"
```

When `--as-user` is on the command line, otto prints a bold-magenta
banner before the command runs:

```text
[reservations] acting as alice (--as-user)
```

The banner fires only in that one case.  On a normal run (no
`--as-user`) there is no banner — you already know who you are.

### Username tab-completion

If your backend can enumerate its users, otto offers them as `--as-user`
tab-completion values. A backend opts in by implementing the optional
[`SupportsUsernameCompletion`](../api/reservations.rst) capability — a single
`list_usernames() -> list[str]` method. Otto detects it structurally; backends
that can't list users simply omit it and `--as-user` still accepts free-form
input.

The values are cached with the same policy as host ids (otto's completion cache,
invalidated by the settings fingerprint and `--clear-autocomplete-cache`), because
enumerating users can be slow and the list changes rarely. A cold cache yields
no suggestions and refreshes on the next normal run — completion never blocks on
the backend.

Real situations where `--as-user` is the right tool:

- A teammate has a shared rack booked under their name; you need to run
  a one-off `otto host` command against it without rebooking.
- Oncall takes over from someone else mid-incident; the booking is in
  the original person's name.
- A CI job needs to run against a rack booked under a service account.

If your process never hits these, you can just leave the flag alone —
otto will always operate as `$USER`.

## Emergency skip: `-R`

```bash
otto -R run some_instruction
otto --skip-reservation-check test TestSmoke
```

`-R` / `--skip-reservation-check` bypasses the check entirely.  It is
intentionally aggressive:

- A bold-red WARNING is printed naming the user, lab, and required
  resources.  This is deliberate friction — the option should feel
  scary to reach for.
- A WARNING-level log line records the same details, so after-the-fact
  log review can find the runs that skipped.

`-R` exists for two realistic situations:

1. **Reservation-system outage** — scheduler is down, you need to keep
   working.
2. **Data mistakes** — your name is spelled wrong in the booking tool,
   or the entry got dropped, and you can't wait for it to be fixed.

It is *not* a normal path.  If your team runs with `-R` routinely, the
check is miscalibrated — fix the data instead.

### Why error messages don't mention `-R`

When the reservation check fails because you don't hold something, the
error message lists the missing resources and their current holders and
stops there.  It *deliberately* doesn't advertise `--skip-reservation-check`,
even though a suggestion would be friendly — the flag gets abused the
moment a user assumes it's a normal workaround.

The one exception is backend-unreachable errors (network down, file
corrupt).  There, `-R` is shown as a suggestion because the user
otherwise has no way to proceed.

## Inspecting state

```bash
otto reservation whoami
```

Prints the resolved identity, its source (`--as-user` or `$USER`), the
configured backend name, and the lab named on the command line (if any).
Needs no lab at all — identity and backend come from repo settings —
and never contacts a host.

```bash
otto --lab tech1 reservation check
```

Runs the check standalone and prints a human-readable report: required
resources, whether everything is covered, and if not, what's missing
and who holds it.  Useful as a pre-flight before kicking off a long
`otto test` run — you find out in one second instead of twenty minutes.
`check` is the one reservation subcommand that needs `--lab`: the lab
defines the required-resource list.  It reads lab *data* only — no host
is contacted.

## Disabling the check team-wide

Teams that don't have a scheduler yet, or who run against isolated
sandbox labs, can disable the check entirely:

```toml
[reservations]
backend = "none"
```

This is the default when no `[reservations]` section exists, so a repo
with no reservations config behaves the same as `backend = "none"`.
The `NullReservationBackend` short-circuits the check to a no-op — no
banner, no warning, no error.

Omit `[reservations]` or set `backend = "none"` for labs that nobody
else is using, while keeping `backend = "json"` (or your custom
backend) on the production labs.  There is currently one
`[reservations]` section per repo, so the backend cannot be varied by
lab.

## Writing a custom backend

When your team already has a scheduler (Jira, a web API, a database), write a
backend that talks to it instead of using the JSON file. A backend implements
the [`ReservationBackend`](../api/reservations.rst) Protocol — three read-only
methods (`get_reserved_resources`, `who_reserved`, `backend_name`). Otto never
calls a write method; the scheduler stays authoritative.

Otto ships a small, dependency-free reference implementation —
[`otto.examples.reservations.ExampleReservationBackend`](../api/examples.rst) —
that you can copy from `src/otto/examples/reservations.py` as a starting point.
It demonstrates a multi-holder `who_reserved`, a stable `backend_name`, and the
optional `list_usernames` completion capability:

```{doctest}
>>> from otto.examples.reservations import ExampleReservationBackend
>>> backend = ExampleReservationBackend()
>>> backend.backend_name()
'example'
>>> sorted(backend.get_reserved_resources("alice"))
['lab-a', 'shared']
>>> backend.who_reserved("shared")
['alice', 'bob']
>>> backend.list_usernames()
['alice', 'bob']
```

### Selecting it in settings

Register the backend under a bare name from an `init` module (one of the modules
in `init = [...]`), then select it by that name:

```python
# my_team_backend.py  (listed in init = [...])
from otto.reservations import register_reservation_backend
from my_company.jira_backend import MyTeamBackend

register_reservation_backend("my-team-jira", MyTeamBackend)
```

```toml
[reservations]
backend = "my-team-jira"
url = "https://jira.example.com"

[reservations.my-team-jira]
api_key_env = "JIRA_API_KEY"
```

Otto constructs the backend as
`MyTeamBackend(url="https://jira.example.com", api_key_env="JIRA_API_KEY")` —
the `[reservations.<name>]` sub-table becomes keyword arguments, and `url` is
passed when present. Selecting an unregistered name raises an error listing the
registered backends. This is the same named-registry mechanism otto uses for
host sources, term/transfer backends, and host classes; an `init` module always
imports before the reservation check runs, so the name is registered in time.
See {doc}`Extension points <../architecture/subsystems/extension-points>` for
the registry machinery behind this and every other seam otto can be extended
at.

### Verify your backend

Otto ships a conformance helper that checks a backend against the full contract
and reports every violation at once (a single `AssertionError` listing each
failed rule). The shipped sample conforms:

```{doctest}
>>> from otto.testing import assert_reservation_backend_conforms
>>> from otto.examples.reservations import ExampleReservationBackend
>>> assert_reservation_backend_conforms(
...     ExampleReservationBackend(),
...     known_user="alice",
...     known_resources=["lab-a", "shared"],
... )
```

Call it from your own suite. Passing `known_user` / `known_resources` (resources
that user is known to hold) enables the round-trip consistency rules against your
own fixtures:

```python
from otto.testing import assert_reservation_backend_conforms
from my_team_backend import MyTeamBackend

def test_my_backend_conforms():
    assert_reservation_backend_conforms(
        MyTeamBackend(url="https://jira.example.com"),
        known_user="alice",
        known_resources=["rack3-psu"],
    )
```

### Contract rules for implementers

- **Never mutate.** Otto only reads from the scheduler. Writes, releases,
  extensions — all stay in the scheduler's own UI/API.
- **Return the user's full reserved set** from `get_reserved_resources`. Don't
  pre-filter against what otto "might need" — otto does that filtering itself,
  and doing it twice loses information for the error message.
- **`who_reserved` returns a `list[str]`.** Return every username currently
  holding the resource, in a deterministic order with duplicates removed. An
  **empty list** means no one holds it — there is no `None` sentinel, and a
  resource can have any number of concurrent holders.
- **Raise [`ReservationBackendError`](../api/reservations.rst)** for *every*
  failure mode that prevents a definitive answer: network errors, timeouts,
  credential failures, malformed responses, missing data files. Do not swallow,
  do not return empty. The CLI surfaces this specific exception as a fail-closed
  startup error with an `-R` hint — swallowing it means otto proceeds as if the
  user holds nothing, the opposite of fail-closed.
- **String-match byte-for-byte.** The strings you return must match
  `UnixHost.resources` and `Lab.resources` exactly. Normalize inside your
  backend, not in otto.
- **`backend_name()` should be stable.** It shows up in diagnostics and skip
  warnings; changing it between versions breaks log-history searches.
- **`url` is optional on both sides.** Accept `url: str | None = None` and use
  it, or hardcode your endpoint and omit it — otto passes `url=` only when the
  setting is present.
- **Optionally implement `list_usernames()`** to power cached `--as-user`
  completion (see [Username tab-completion](#username-tab-completion)).

## Fail-closed behavior

If backend construction raises (scheduler unreachable, bad credentials), otto
exits before running the requested command — and the error message *does* mention
`-R`, because the user otherwise has no way to proceed.

Passing `-R` / `--skip-reservation-check` goes further: otto does **not construct
the backend at all**. A scheduler that fails or even hangs in its constructor can
never block lab access — that is the strongest form of break-glass. (The
introspection subcommands `otto reservation whoami` / `check` still build the
backend on demand when you ask them to.)

All other failures (the user genuinely doesn't hold the resource) exit via the
normal `MissingReservationError` path, which does not mention `-R`.

## Using the reservation library in your own CLI

Everything above walks through `otto`'s own subcommands. The library itself —
`otto.reservations` — has no dependency on Typer, rich, or any other part of
otto's CLI, so a completely separate tool (a deploy script, a CI gate, your
own CLI) can run the exact same check without going through `otto` at all.
Four steps:

1. **Build** a backend from your tool's own settings with
   [`build_backend`](../api/reservations.rst). An unconfigured (or `"none"`)
   backend setting resolves to
   [`NullReservationBackend`](../api/reservations.rst) — a no-op, so this step
   needs no live scheduler to exercise in a test.
2. **Resolve** the effective identity with
   [`resolve_username`](../api/reservations.rst).
3. **Construct** a [`ReservationGate`](../api/reservations.rst) from the
   backend and identity and call `.evaluate()`.
4. **Present** the result yourself. `evaluate()` returns a
   `ReservationGateResult` whose `warning` is plain text — the library never
   touches your terminal. `MissingReservationError` and
   `ReservationBackendError` (the same two exceptions from
   [Fail-closed behavior](#fail-closed-behavior) above) are what you catch;
   exit codes, logging, and styling are entirely your call — `otto`'s own CLI
   wraps `warning` in rich markup, nothing here requires you to do the same.

| Exception                 | Raised by                                                      | Means                                                                       |
|---------------------------|-----------------------------------------------------------------|------------------------------------------------------------------------------|
| `MissingReservationError` | `evaluate()` / `check_reservations()`                          | The identity doesn't hold every required resource.                         |
| `ReservationBackendError` | `build_backend()` (construction) or `evaluate()` (query time)  | The backend itself couldn't answer — network, credentials, malformed data. |

A complete, runnable example ships as
[`otto.examples.reservations_cli`](../api/examples.rst)
(`src/otto/examples/reservations_cli.py`) — copy it as a starting point. Its
`run_check()` is steps 3-4, kept separate from the Typer command so it is
directly testable against the Null backend or the
[`ExampleReservationBackend`](../api/examples.rst) sample, no real scheduler
or CLI invocation required:

```{doctest}
>>> from otto.config.lab import Lab
>>> from otto.examples.reservations import ExampleReservationBackend
>>> from otto.reservations import resolve_username
>>> from otto.examples.reservations_cli import run_check
>>> demo = Lab(name="demo", resources={"lab-a"})
>>> run_check(demo, backend=ExampleReservationBackend(), identity=resolve_username("alice"))
alice: OK
0
>>> run_check(demo, backend=ExampleReservationBackend(), identity=resolve_username("carol"))
carol: User 'carol' does not hold all resources required by lab 'demo'. Missing:
  - lab-a (held by alice)
1
```

Run the full example as a standalone CLI — with no `--backend` flag it falls
back to the Null backend, so this needs no scheduler either:

```bash
python -m otto.examples.reservations_cli --resource rack1
```

If you're also writing a custom backend for your tool (rather than reusing
`json` or `none`), see [Verify your backend](#verify-your-backend) above —
`otto.testing.assert_reservation_backend_conforms` checks the same contract
whether the backend ends up wired into `otto`, your own CLI, or both.

## Troubleshooting

`"User $USER does not hold all resources required by lab ..."`
: Expected when the check is working correctly.  Either reserve the
  listed resources in your scheduler, pass `--as-user` if the booking
  is in someone else's name, or (if you're certain the data is wrong)
  use `-R` for one command.

`"Unknown reservation backend '...'"`
: `[reservations] backend` names a backend that was never registered. Check the
  name, and confirm the `init` module that calls
  `register_reservation_backend(...)` is listed in `init = [...]`.

`"Failed to read reservation file ..."`
: The JSON backend can't open the file.  Check `path` in
  `[reservations.json]`, file permissions, and that the path is correct
  relative to the repo root (or absolute).

`"Reservation file ... has unsupported version ..."`
: The JSON file's `version` field is something other than `1`.  This
  is reserved for future breaking schema changes; keep `"version": 1`.

Check passes locally but fails in CI
: CI runs under a different username (often a service account).  Either
  reserve resources under that service account, or pass
  `--as-user SERVICE_USER` in the CI invocation so otto queries the
  scheduler under the right identity.
