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
union of the selected lab's `resources` plus every `RemoteHost.resources`
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
path = "${sutDir}/.otto/reservations.json"
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
    `RemoteHost.resources` and `Lab.resources`.
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

Relative paths are resolved against the repo root.  `${sutDir}` expands
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
otto --lab tech1 reservation whoami
```

Prints the resolved identity, its source (`--as-user` or `$USER`), the
configured backend name, and the selected lab.  Uses zero lab state
beyond what the top-level callback already loaded.

```bash
otto --lab tech1 reservation check
```

Runs the check standalone and prints a human-readable report: required
resources, whether everything is covered, and if not, what's missing
and who holds it.  Useful as a pre-flight before kicking off a long
`otto test` run — you find out in one second instead of twenty minutes.

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
`[reservations]` section per repo — if you need to vary by lab, that
lives on the roadmap.

## Writing a custom backend

When your team already has a scheduler (Jira, a web API, a database),
write a backend that talks to it instead of using the JSON file.

Implementers implement the [`ReservationBackend`](../api/reservations.rst)
Protocol — three read-only methods.  Otto never calls a write method;
the scheduler remains authoritative.

### Minimal skeleton

```python
# my_team_backend.py
from typing import Optional

from otto.reservations import ReservationBackendError


class MyTeamBackend:
    """Reservation backend backed by the team's internal Jira project."""

    def __init__(self, *, url: str, api_key_env: str = "JIRA_API_KEY") -> None:
        import os
        self._url = url
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise ReservationBackendError(
                f"Environment variable {api_key_env} is not set"
            )

    def get_reserved_resources(self, username: str) -> set[str]:
        try:
            records = self._fetch(f"/reservations?user={username}&active=true")
        except OSError as e:
            raise ReservationBackendError(f"Scheduler unreachable: {e}") from e
        return {r["resource_id"] for r in records}

    def who_reserved(self, resource: str) -> Optional[str]:
        try:
            records = self._fetch(f"/reservations?resource={resource}&active=true")
        except OSError as e:
            raise ReservationBackendError(f"Scheduler unreachable: {e}") from e
        return records[0]["user"] if records else None

    def backend_name(self) -> str:
        return "my-team-jira"

    def _fetch(self, suffix: str) -> list[dict]:
        ...
```

### Selecting it in settings

```toml
[reservations]
backend = "my_team_backend:MyTeamBackend"
url = "https://jira.example.com"

[reservations.MyTeamBackend]
api_key_env = "JIRA_API_KEY"
```

The factory resolves `"pkg.module:ClassName"` via `importlib`, then
instantiates the class as `Class(url=url, **kwargs_from_settings)` —
where `kwargs_from_settings` is the nested
`[reservations.<ClassName>]` sub-table.  The full dotted name (via
quoted keys, e.g. `["reservations"."pkg.mod:Class"]`) also works, but
the unquoted class-name form is easier to read.

The module must be importable from otto's process — add the directory
to `[libs]` in `.otto/settings.toml`, install the module as a package,
or use any other `sys.path` mechanism.

### Contract rules for implementers

- **Never mutate.**  Otto only reads from the scheduler.  Writes,
  releases, extensions — all stay in the scheduler's own UI/API.
- **Return the user's full reserved set** from `get_reserved_resources`.
  Don't pre-filter against what otto "might need" — otto does that
  filtering itself, and doing it twice loses information for the error
  message.
- **Raise [`ReservationBackendError`](../api/reservations.rst)** for
  *every* failure mode that prevents a definitive answer: network
  errors, timeouts, credential failures, malformed responses, missing
  data files.  Do not swallow, do not return empty.  The CLI surfaces
  this specific exception as a fail-closed startup error with an `-R`
  hint — swallowing it means otto proceeds as if the user has nothing
  reserved, which is the opposite of fail-closed.
- **String-match byte-for-byte.**  The strings you return from
  `get_reserved_resources` must match `RemoteHost.resources` and
  `Lab.resources` exactly.  If the upstream scheduler uses different
  identifiers, normalize inside your backend — not in otto.
- **`backend_name()` should be stable.**  The name shows up in
  diagnostic output and skip warnings; changing it between versions
  breaks search queries over log history.
- **`url` is optional on both sides.**  You can either accept `url: str
  | None = None` and consume it, or hardcode your scheduler's endpoint
  and omit the parameter.  The factory only passes `url=` when the
  setting is present.

### Third-party package layout

If you distribute the backend as a Python package:

```text
my_team_backend/
├── pyproject.toml
└── src/
    └── my_team_backend/
        ├── __init__.py         # exports MyTeamBackend
        └── backend.py
```

```toml
[reservations]
backend = "my_team_backend:MyTeamBackend"
```

Users `pip install my_team_backend` into the same environment as otto,
and the factory picks it up by dotted path.  No otto-side code changes
are needed to add a new backend.

## Fail-closed behavior

If backend construction raises, or an early health check fails, otto
exits before running the requested command.  The error message does
mention `-R` in this case — the user otherwise has no way to proceed
if their scheduler is down.

All other failures (user genuinely doesn't hold the resource) exit via
the normal `MissingReservationError` path, which does not mention `-R`.

## Troubleshooting

`"User $USER does not hold all resources required by lab ..."`
: Expected when the check is working correctly.  Either reserve the
  listed resources in your scheduler, pass `--as-user` if the booking
  is in someone else's name, or (if you're certain the data is wrong)
  use `-R` for one command.

`"Could not import reservation backend module ..."`
: The dotted path in `[reservations] backend = "pkg.mod:Cls"` pointed
  at a module otto can't find.  Either add the containing directory to
  `libs = [...]` in `.otto/settings.toml`, or install the backend as a
  package in the same environment.

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
