# otto lab

`otto lab` shows what otto's lab sources make of their files. No verb touches
a host: every verb is read-only and creates no output directory.

```{raw} html
:file: ../../_static/generated/termynal/help-lab.html
```

## `otto lab render`

```text
otto lab render ENTRY [--source NAME]
```

`otto lab render` prints the JSON document a Jsonnet entry file renders to.
ENTRY is a `.jsonnet` file that a `backend = "jsonnet"` lab source names in its
`paths` ({doc}`../../configuration/lab/templating` covers writing one, with a
worked example). The
file renders exactly as that source renders it when a lab loads, with the
inventories the source declares, and the document prints as indented JSON on
standard output:

```bash
otto lab render lab_data/layouts/router.jsonnet
```

A refusal or an error prints on standard output too, so redirecting the output
to a file hides a failure in that file. To keep the document in a file and
still see a failure, show the file when the command fails:

```bash
otto lab render lab_data/layouts/router.jsonnet > /tmp/router-lab.json || cat /tmp/router-lab.json
```

On success the file holds the document. On a failure it holds the one-line
message instead, which the `cat` shows.

ENTRY is a file path, not a lab name, so `render` needs no `--lab`.

The document prints **before** otto validates it. A document that otto would
refuse to load still prints, so you can see what the template produced and
why the load failed.

When more than one `jsonnet` source names ENTRY, pass `--source` with the
label of the one to render with. A source's label is
`<project-name>/<name>` (see the `name` key under
[Declaring sources](../../configuration/host-sources.md#declaring-sources)).
Without `--source`, the error lists the labels of every source that reads the
file. A file that no source names fails with the labels of the `jsonnet`
sources there are.

````{important}
**A rendered document never prints a password.** When the document holds a
`password` or `passphrase` key at any depth whose value is anything but
`null` (a string, a number, a list or a table), `render` prints none of the
document and exits 1 with one line naming where the value is, never the
value:

```text
refusing to print lab_data/layouts/router.jsonnet: it holds a literal secret at elements[0].hosts[0].creds[0].password; move it to the credentials store
```

Passwords belong in {ref}`the credentials store <credentials-layered>`, which a
template never reads.
````

Every failure exits 1 and never shows the document. A Jsonnet error names the
file and line where evaluation stopped.

## `otto lab explain`

```text
otto -l NAME lab explain HOST_ID
```

`otto lab explain` shows where each of a host's values came from. otto builds
a host from several layers of configuration, and a value from a higher layer
replaces a lower one; {doc}`../../configuration/precedence` gives the layers
and their order. `explain` shows, for each value, the layer it came from:

```bash
otto -l router-lab.east-r7 lab explain east-r7-dut0
```

```text
╭────────────────┬──────────────────┬────────╮
│ field          │ layer            │ detail │
├────────────────┼──────────────────┼────────┤
│ creds.password │ inventory record │ admin  │
│ ip             │ inventory record │        │
│ os_type        │ template         │        │
│ rack           │ inventory record │        │
│ site           │ inventory record │        │
╰────────────────┴──────────────────┴────────╯
host: east-r7-dut0
source: sut/jsonnet#1
entry: /srv/sut/lab_data/layouts/router.jsonnet (element 'east-r7-dut0', hosts[0])
instance: {"rack": "r7", "site": "east"}
role: dut
host label: dut
inventory physical: live
```

The lab is the one the global `-l`/`--lab` option names, written before
`lab`, as for every other command. `explain` has no `--lab` option of its own,
and an unknown or missing lab fails exactly as it does for any other command.
The lab loads exactly as a run loads it, and the answer comes from that load:
nothing is loaded a second time.

Each row of the table is one value the host got:

- **field** is the name of the value. A host field is named as in a lab file
  (`ip`). One key of an option table is named with its table (`ssh_options.port`).
  A field of a credential is named `creds.<field>` (`creds.password`).
- **layer** is where the value came from, as one of these labels (their
  order is on {ref}`the precedence page <precedence>`):
  - `creds store`: the `[creds]` store.
  - `os profile`: a default of the host's `os_type` profile.
  - `inventory record`: the host's record, through its `inventory` reference.
  - `lab file`: the host's entry in a hand-written lab file.
  - `template`: the host's entry in the document a Jsonnet template rendered.
  - `host preferences`: a `[host_preferences]` value.
- **detail** says which part of the layer the value came from. For a
  credential it is the credential's login, with its `protocols` scope when
  it has one (`root [ssh]`). For a `[host_preferences]` value it is the host-id
  selector the value came from. For a profile default it is the profile's
  name.

Each credential is listed field by field, so one login can show a password
from the credentials store and a `proxy` from the inventory record. The same
login with two `protocols` scopes is two credentials, and each has its own
rows. **No value is ever shown**, so a password never prints: the table names
the field and the layer, and the detail names the login.

Below the table:

- `source` is the label of the lab source the host's entry came from.
- `entry` is the file the entry is in, with its element name and its position
  in the element's `hosts`.
- `instance`, `role` and `host label` say which instance and which role of the
  template's `layout` made the element. They show only for a rendered entry
  that has them.
- One `inventory` line per configured inventory says how old its records are.
  An age like `3h` or `2d 7h` is the age of the inventory's cached snapshot.
  `live` means the inventory has no cache and reads its source each time.
  `not read` means the cache was not read for this lab. `(served stale: the
  inventory was unreachable)` follows the age when otto used an old snapshot
  because the inventory could not be reached.

When two lab sources declare the same element, the later source's element
replaces the earlier one ({doc}`../../configuration/host-sources`), and
`explain` shows only the source whose element the host came from.

`explain` touches no host, so it never asks whether you hold the lab's
reservation, and it works on a lab someone else has reserved. It still sets
up the reservation backend, as every command that loads a lab does. If that
backend cannot be reached, `explain` fails the same way: pass
`--skip-reservation-check` (`-R`) to skip it.
