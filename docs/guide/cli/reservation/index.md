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

### Three reservable levels

A `lab.json` declares reservation identifiers at three levels.  Every level is
optional, they combine freely, and the identifiers themselves are opaque
strings the backend matches byte-for-byte:

| Level | Where it is declared | What it stands for |
|-------|----------------------|--------------------|
| lab | `resources` in the lab's [`labs` table](../../configuration/lab-config.md#the-labs-table) entry | Infrastructure the lab shares as a whole — a switch, a PDU, a bed. |
| element | `resources` on an [`elements`](../../configuration/lab-config.md#elements) entry | The element reserved as one unit — a chassis. |
| host | `resources` on a [host entry](../../configuration/lab-config.md#common-optional) | The slot. |

A lab whose every element carries a resource leaves nothing unguarded — every
reservable host in play then requires something (container hosts declare none,
ever, and the built-in `local` host is never in play at all) — but it is not
the same as a whole-lab
lock: distinct per-element identifiers can be held by different people at the
same time, which is usually the point.  A single lab-level identifier is what
*asks* the scheduler for one holder.  Otto enforces no exclusivity of its own:
it asks your backend who holds a resource, accepts a list, and will report
`held by: dana, sam` without complaint.  A chassis shared slot by slot needs
only host-level entries.  This `rig` has all three kinds of thing, so it
declares at all three levels:

```json
{
  "labs": {"rig": {"resources": ["rig-pdu"]}},
  "elements": [
    {"name": "chassis", "id": 1, "labs": ["rig"], "resources": ["chassis-1"],
     "hosts": [
       {"ip": "10.0.0.11", "board": "slot", "slot": 1,
        "resources": ["chassis-1-slot-1"], "creds": [{"login": "admin", "password": "…"}]},
       {"ip": "10.0.0.12", "board": "slot", "slot": 2,
        "resources": ["chassis-1-slot-2"], "creds": [{"login": "admin", "password": "…"}]}
     ]},
    {"name": "gw", "labs": ["rig"],
     "hosts": [{"ip": "10.0.0.1", "creds": [{"login": "admin", "password": "…"}]}]}
  ]
}
```

Taking the whole rig means holding `rig-pdu`; taking the chassis out of
service means holding `chassis-1`; one engineer per slot means one identifier
per host entry.  The same identifier may appear at more than one level, or on
two hosts that share a single physical lock — the required set is a set, so
that costs nothing.

(`board` and `slot` are what give the two chassis hosts distinct ids,
`chassis1_slot1` and `chassis1_slot2`; see {ref}`host-identity`.  A resource
identifier is unrelated to a host id — otto never derives one from the other.)

### The fleet the gate reserves for

The required set is computed over the **hosts in play** — every host the run's
project(s) declare an interest in via `[project] host_patterns`
({ref}`project-scope`), or the whole lab when no repo declares one.  The lab
level always counts; each host in play adds its element's identifiers and its
own.

The built-in `local` host is never among them.  Otto can always run on the
machine it is running on, so a reservation standing between you and
`otto host local <verb>` would cost you a run and buy nobody a slot.  A lab
that declares its *own* `local` host entry is a different thing — otto injects
no built-in host in that case, and that entry's `resources` are enforced like
any other's.

For a combined `--lab a+b` the lab level contributes the union of the
components' declared sets, and every lab-level row is attributed to the merged
name `a+b` rather than to the component that declared the identifier.

Naming a host explicitly — as the target or as the `--hop` — adds it to that
set, the built-in `local` host still excepted.  `otto host <id>` is not scoped
by any `[project]` declaration
({doc}`../host/index`), so a host outside the fleet has its OWN element- and
host-level identifiers checked before the command runs — otherwise holding the
fleet's slots would be permission to touch hardware nobody reserved, and
reaching a fleet host through an unreserved jump box is still using the jump
box.

Otto then asks the configured backend which of those identifiers the effective
user holds.  Anything missing raises an error that names each missing
identifier, the level and owner that required it, and who holds it today — and
the command does not run.

`--hosts` on `otto monitor` / `otto tunnel` narrows that verb's walk inside a
run the gate has already admitted; it does not narrow the gate.  To reserve
fewer slots, narrow `[project] host_patterns` in the project that runs them.

### Two labs over one element

One consequence is worth planning for: two labs that share an element contend
with each other only if something they both hold locks that element.  The
`otto init` doctor warns about a pair that shares an element when **both**
labs reserve at least one resource, their lab-level sets have nothing in
common, **and** the shared element is unprotected — it declares no `resources`
of its own and not every one of its host entries does either.  Protect it at
either lower level and the pair is fine: reserving one lab now contends with
the other through that identifier.  The other two remedies the warning offers
are a shared lab identifier, or making one lab a sub-lab of the other.  A lab
that declares `resources: []` reserves nothing at all, so it is never half of
such a pair — there is no reservation for the shared element to protect.

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
: Expected when the check is working correctly.  One line follows per
  missing identifier and the level and owner that required it — read it as
  "reserve *this* thing" rather than "reserve the lab".  Either reserve the
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
