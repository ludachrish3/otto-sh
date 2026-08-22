# otto reservation

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
{ref}`team-setup-checklist` in {doc}`../../configuration/settings` for the full onboarding map.
```

See {doc}`../../../architecture/subsystems/reservations` for what makes
`reservation` unique among the first-party commands — lab-free identity,
`check` as the preamble's gate standalone, and the break-glass design.

```{raw} html
:file: ../../../_static/generated/termynal/help-reservation.html
```

## Synopsis

Inspect and verify lab reservations.

```text
otto reservation whoami
otto reservation check
```

## Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `whoami` | Show the resolved reservation identity and backend |
| `check` | Verify the current reservation for the loaded lab |

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

```{toctree}
:caption: Subcommands
:hidden:

whoami
check
```

```{toctree}
:caption: Topics
:hidden:

json-backend
identity
windows
skipping
```
