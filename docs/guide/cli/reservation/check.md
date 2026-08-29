# otto reservation check

```bash
otto --lab tech1 reservation check
```

Runs the check standalone and reports what the lab requires of you before
anything touches hardware.  Useful as a pre-flight before kicking off a long
`otto test` run — you find out in one second instead of twenty minutes.
`check` is the one reservation subcommand that needs `--lab`: the lab
defines the required-resource list.  It reads lab *data* only — no host
is contacted.

`check` runs the same gate every hardware-touching command runs in its
preamble — see {doc}`index` for what that gate covers and
{doc}`skipping` for the break-glass override.

## What it prints

First the requirement, as a table with one row per (resource, origin) pair:

| Column | What it holds |
|--------|---------------|
| `resource` | The identifier, exactly as declared.  Opaque to otto. |
| `level` | `lab`, `element` or `host` — the level that required it. |
| `owner` | The lab name, the element as `('chassis', 1)`, or the host id. |
| `held` | `yes` or `no` for the effective user.  `n/a` under `backend = "none"`, which is never queried. |

Rows are sorted by resource, then level, then owner, so the same lab always
prints in the same order.  Level sorts widest-first — `lab`, then `element`,
then `host` — not alphabetically, so one identifier declared at two levels
reads top-down from the coarsest thing that required it.  Each such declaration
gets its own row: the table explains *why* something is required, not just
*that* it is.  The title names the lab, the identity being checked, and how
many hosts are in play; that count is the fleet of interest ({doc}`index`), not
the whole lab when a project narrows it.  It counts the built-in `local` host on
the whole-lab fallback, and also whenever a declared scope's `lab_patterns` and
`host_patterns` both fullmatch it — `host_patterns = [".*"]` is enough.
Scoping admits by pattern, not by id: nothing filters `local` out of the fleet
itself.  It never adds a row, though, because `local` declares no resources.

Then the verdict — `OK — all required resources are reserved.`, or the same
error a gated command would fail with, one line per missing identifier and
origin.  For the three-level `rig` on {doc}`index`, checked as a user who
holds everything but the second slot:

```text
reservations required by lab rig for chris (4 host(s)
                       in play)
╭──────────────────┬─────────┬────────────────┬──────╮
│ resource         │ level   │ owner          │ held │
├──────────────────┼─────────┼────────────────┼──────┤
│ chassis-1        │ element │ ('chassis', 1) │ yes  │
│ chassis-1-slot-1 │ host    │ chassis1_slot1 │ yes  │
│ chassis-1-slot-2 │ host    │ chassis1_slot2 │ no   │
│ rig-pdu          │ lab     │ rig            │ yes  │
╰──────────────────┴─────────┴────────────────┴──────╯
User 'chris' does not hold all resources required by lab 'rig'. Missing:
  chassis-1-slot-2  host chassis1_slot2  (held by: dana)
```

```{note}
That rendering is illustrative.  No documentation harness captures `check`, so
nothing keeps the block above in step with the code the way the captured help
output elsewhere in this guide stays in step — read the columns and their
values as the contract, and the box art as a sketch of the shape.
```

A lab that requires nothing of the hosts in play prints one line instead of an
empty box — `(this lab requires no reservation for the hosts in play)` — and
the backend is never queried, so a scheduler that is up but unhappy cannot turn
an empty requirement into a failure.  The backend is still *constructed* first,
though, so a constructor that raises fails the command as usual (see
{doc}`index`'s fail-closed section).
