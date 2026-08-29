# The JSON backend

The built-in JSON backend is the fastest way to experiment and, for
small teams, a perfectly adequate production setup.

Add a `[reservations]` section to your repo's `.otto/settings.toml`:

```toml
[reservations]
backend = "json"

[reservations.json]
path = ".otto/reservations.json"
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

## File format

The top-level object has two required fields:

`version`
: Integer schema version.  Currently only `1` is supported.  Bumping
  this value will be reserved for breaking changes.

`reservations`
: List of reservation records.  Each record has:

  * `user` *(string, required)* — the reservation-system username.
  * `resources` *(list of strings, required)* — resource identifiers
    the user holds.  Must match byte-for-byte the identifiers otto computes
    for a run, which a `lab.json` may declare at any of three levels — the
    `labs` table entry, an element, or a host entry
    ({doc}`index`).  Write the identifier exactly as the lab file spells it;
    otto normalizes nothing.
  * `expires` *(string, optional)* — ISO-8601 timestamp.  Past-dated
    entries are silently ignored.  Omit for "no expiry".

A user may appear in multiple records — the effective set is the union.
This is intentional: if your booking source has multiple entries for the
same person, you don't need to merge them before writing the file.

## Choosing a location

Two common layouts work well:

- **Checked-in** — put `reservations.json` under `.otto/` in the repo
  and commit it.  Reservation changes land as normal PRs; the full git
  history shows who had what and when.  Good when churn is low and a
  PR-level review is desirable.
- **Shared volume** — point `path` at a file on a networked volume
  (`/mnt/team/reservations.json`) or an absolute path that's synced by
  some other tool.  Good when reservations change frequently throughout
  the day and PR overhead would feel absurd.

Relative paths resolve against the repo root — see
{doc}`../../configuration/settings`.
