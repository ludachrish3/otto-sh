# otto inventory

Read-only helpers over the configured host inventory — the tool-agnostic layer
beneath `lab.json`, described in
{doc}`../../configuration/inventory`. `lookup` and `list` debug the join
without editing a lab file; `export` and `diff` are the transition tools
between a JSON inventory and NetBox; `refresh` forces a fetch of a cached
remote inventory.

Every verb reads the `[inventory]` table from your user settings file
(`~/.otto/settings.toml`, or wherever `OTTO_HOME` puts it) or from a project's
`.otto/settings.toml`, and exits 1 with the inventory's own error text when it
cannot answer. No verb needs a lab, touches a host, or writes to the
inventory — and none of them ever prints a password, whether the credentials
came from a record or from a `creds_file`.

```{important}
**A stale answer always says so.** When a cached remote inventory is
unreachable, otto serves the snapshot rather than failing — a lab that loaded
yesterday should load today — and `lookup`, `list`, `export` and `diff` each
print the snapshot's age and a pointer to `otto inventory refresh` before their
own output. An `export` taken during an outage is a copy of an old snapshot,
and a `diff` run during one compares against an old left side; both still
answer, and both tell you.
```

```{raw} html
:file: ../../../_static/generated/termynal/help-inventory.html
```

## Synopsis

```text
otto inventory lookup KEY
otto inventory list
otto inventory export PATH [--force]
otto inventory diff PATH [OTHER]
otto inventory refresh
```

## Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `lookup KEY` | The resolved record as a table (creds as login names only), plus the key, the backend label and `supplies` |
| `list` | Every key with its address, the count and the label, and what the backend skipped |
| `export PATH` | Write the inventory as a stage-1 JSON file — sorted keys, no `creds`; refuses to overwrite without `--force` |
| `diff PATH [OTHER]` | Compare the inventory with a stage-1 file, or two files with each other; exit 1 when anything differs |
| `refresh` | Force a fetch of a cached remote inventory and rewrite the snapshot |

## Exit codes

| Outcome | Exit code |
| --- | --- |
| The verb answered | 0 |
| No inventory is declared anywhere, the declaration is broken, the backend could not answer, the key does not exist, `export` refused to overwrite or could not write, `refresh` had nothing to refresh | 1 |
| `diff` found at least one difference | 1 |
| `diff` could not compare at all | 2 |

`diff` follows `diff(1)`: it is meant to be used as a gate in a script, where
"the two sides disagree" is the answer you are testing for, so that outcome has
to be distinguishable from "I never managed to look". A missing or unreadable
file, a malformed document, no configured inventory, an inventory backend that
is down — all of those exit **2**, and only a real difference exits 1. Without
the split a typo'd filename reads to your script exactly like a drifted
inventory.

## lookup

```bash
otto inventory lookup dut1
```

Prints the provenance a referencing host carries as `host.inventory_ref` — the
key and the backend's label — then the record itself, then the inventory's
`supplies` declaration. Use it when a host built from an inventory reference
has a field you did not expect: `supplies` tells you which fields the inventory
owns, and the table tells you what it said about them.

Credentials appear as login names only. The record holds passwords; this
command prints to a terminal, and to whatever captured it.

## list

```bash
otto inventory list
```

Every key the inventory holds, with its address, then the count and the label.

A backend that had to pass over part of what it selected says so afterwards.
The NetBox backend reports two kinds: devices with no address at the
configured `ip_source`, and devices with no name (NetBox allows both, and the
default filter is "everything"). Those lines appear only when the command
actually fetched — a `list` served from a fresh snapshot selected nothing this
run, so it has nothing to report.

## export

```bash
otto inventory export inventory.json
```

Writes the inventory as a stage-1 JSON document: sorted keys, only the fields
each record actually states, and never `creds` — those live in `creds_file`,
so an export is shareable, diffable and committable without leaking one.

The file is written whole or not at all (write-then-rename), at mode `0600`,
and an existing path is refused until you pass `--force`. This is the same
document shape the `json` backend reads, so an export from NetBox is directly
usable as a `json` inventory — which is what makes it a bridge rather than a
report.

## diff

```bash
otto inventory export today.json
otto inventory diff yesterday.json
```

Compares record by record and field by field, `creds` excluded, and exits 1
when anything differs — or 2 when it could not compare at all (see
[Exit codes](#exit-codes)). With a second path both sides are files, and the
configured inventory is never read at all.

Two blank cells would otherwise mean two different things, so the table spells
them out: `absent` means the key is not on that side at all, `not stated`
means the record is there and says nothing about that field. A field neither
side mentions is not a difference; a field one side states at its default and
the other omits is.

## refresh

```bash
otto inventory refresh
```

Fetches unconditionally and rewrites the snapshot, whatever the TTL says. Use
it when you know better than `cache_ttl` — you just added a device, or the
stale-snapshot warning told you the inventory had been unreachable. It reports
the replaced snapshot's timestamp in your local time and its age, the number of
records fetched, and where the snapshot lives.

An inventory with no snapshot cache exits 1 saying so: a `json` inventory is
read from its file on every command, and a remote one is cached only when
`cache_ttl` is greater than `0` and the backend reports no fingerprint of its
own. The verb fails rather than shrugging, so `otto inventory refresh && ...`
cannot proceed as though a fetch had happened.
