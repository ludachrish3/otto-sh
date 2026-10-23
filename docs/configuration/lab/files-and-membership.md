# Lab files and membership

(lab-files)=

## Lab files

Each directory a json `[[lab.sources]]` entry lists in `paths` (in
`.otto/settings.toml`) may contain a `lab.json` file — and a `paths` entry
may name a `.json` file directly, or a glob matching several, `**` included
({ref}`Splitting lab data across files and directories <lab-data-across-files>`).
Otto checks `paths` when it prepares the source, after the project's `init`
modules have run, not at settings parse.
The file is a JSON **object** with three sections:

```json
{
    "$schema": "../.otto/schemas/lab.schema.json",
    "labs": {
        "unix": {
            "resources": ["unix-bed"],
            "metadata": {"description": "unix regression bed"}
        },
        "embedded": {"resources": ["embedded-bed"]}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix"],
            "metadata": {"rack": "B4"},
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "os_type": "unix",
                    "creds": [{"login": "vagrant", "password": "vagrant"}]
                }
            ]
        },
        {
            "name": "zephyr37_fat",
            "labs": ["embedded"],
            "hosts": [
                {"ip": "192.0.2.1", "os_type": "zephyr", "os_version": "3.7"}
            ]
        }
    ],
    "links": []
}
```

`labs` is the table of declared labs, keyed by name ([The labs
table](#the-labs-table)).  `elements` is the array of element entries, each
grouping the host entries that address it ([Elements](elements.md#elements)).  `links`
declares data-plane routes and is covered in {ref}`lab-links`.  All
three are optional: omitting a section is the same as writing it empty.

A top-level `$schema` is comment space, and so is any key beginning with `_`
at **every** level: the document, the `labs` table, a `labs` entry, an
element, a host entry, a link.  Otto strips them before validation, so a file
can carry a note inline anywhere and still be wired to the generated schema
in an editor.  Every other unknown key fails the load naming itself, and a
top-level `hosts` key is the pre-v2 shape: it fails with a migration message
pointing at [Migrating from the hosts
array](migrating.md#migrating-from-the-hosts-array).

Pass `--lab unix` (or set `OTTO_LAB=unix`) and otto loads every host of every
element whose `labs` patterns match `unix`.  The lab has to be declared
first — elements alone never conjure one.

The host **id** used by `get_host()`, `--list-hosts`, and the rest of the CLI
is composed from the element's name and the host's `board` and `slot` —
{ref}`host-identity` gives the rules and a worked example.

### Splitting a lab across files

Hundreds of elements do not belong in one file.  One source can read many
files, from one directory or from a whole tree, and an element in one file joins
a lab declared in another.  {ref}`Splitting lab data across files and
directories <lab-data-across-files>` shows how to name the files, and
{ref}`how the files combine <one-source-several-files>` follows it.

## The labs table

`labs` is keyed by lab name; each value declares what belongs to that lab as
a whole.

| Field | Type | Description |
|-------|------|-------------|
| `resources` | array of strings | Reservation identifiers shared by the lab as a whole — a switch, a PDU, a bed.  Matched byte-for-byte by the reservation backend.  Elements and hosts may declare their own alongside; see {doc}`../../cli/reservation/index`.  Defaults to empty — a lab that reserves nothing is a perfectly good declaration. |
| `metadata` | object | Opaque lab-level user data; otto never reads it.  Surfaces as `lab.metadata["<lab name>"]`, and on every host of the lab as `host.lab_info.metadata`. |

**A lab exists if and only if some source declares it here.**  `otto
--list-labs` reads the declared names, and loading a name nothing declares
fails naming every configured source — even when elements' patterns would
have matched it.  The converse is a mistake too: a declared lab that no
element anywhere matches fails the load rather than yielding an empty lab.
What *is* fine is a `labs` entry in a file that holds none of that lab's
elements — another file, or another source, supplies them.

Resources are **declared**, never derived: this entry is what the lab holds *as
a whole*.  An element or a host may declare its own alongside — three levels
that combine freely; see [Elements](elements.md#elements) and [Per-host
fields](host-fields.md#per-host-fields), and {doc}`../../cli/reservation/index` for what a
run actually has to hold.  Two labs that share an element contend with each
other only if something they both hold locks that element.  `otto init` warns
about any pair that shares an element, reserves nothing in common, **and**
leaves that element unprotected — no element- or host-level `resources` on it.
The warning names the labs, the unprotected elements, and three remedies: a
shared lab identifier, a `resources` entry on the element (or on each of its
hosts), or making one lab a sub-lab of the other.  (A lab that declares no
resources reserves nothing at all, so it is never half of such a pair.)

A **sub-lab** is just another declared lab whose name follows a scheme —
`unix.rack-b4` alongside `unix` — with its own `resources` and `metadata`.
Elements opt into it through their membership patterns: `"unix(\\..*)?"` joins
`unix` and every dotted sub-lab of it.  Reserving a portion of a lab means
declaring that portion as a lab; elements stay the smallest divisible unit, and
there is no host-level lab addressing.
