(host-identity)=

# Host identity & naming

There is no `id` field on a host entry.  The **element's `name`** is the id
source.  Every portion is **slugged** — lower-cased, every run of characters
outside `[a-z0-9]` (spaces, punctuation, `_`) collapsed to one `-`, ends
trimmed — and the portions are joined by `-`: the element, then the board
when set, then the slot when set.  `slot` never appears without a board, and
a slot of `0` is a slot.

| Host | Identity fields | Id |
| --- | --- | --- |
| element only | `name="test1"` | `test1` |
| element + board | `name="test1"`, `board="bb"` | `test1-bb` |
| element + board + slot | `name="test1"`, `board="bb"`, `slot=0` | `test1-bb-0` |
| spaced or punctuated names | `name="Edge Router"`, `board="line.card"`, `slot=3` | `edge-router-line-card-3` |

Two different declarations can compose one id — element `a-b` (no board), or
element `a` with board `b`: both compose `a-b`.  otto refuses that at load and
names both, with their ip:

```text
host id 'a-b' in lab 'bench': element 'a-b' (10.0.0.1) collides with element 'a' board 'b' (10.0.0.2). Give the elements distinct names, or set board/slot.
```

Rename one; nothing is renamed for you.  The same refusal across lab sources
is in {ref}`the host-sources troubleshooting entry <host-id-collision>`.  A container host's id is
`<host>.<project>.<service>`, docker's own names — see
{doc}`../../cli/docker/index`.

The element's `id` is **data** — it is carried as
`host.element.id` and never enters a host id, a display name, or an ordering.
**Renaming an element changes its hosts' ids** — and, transitively, the id of
any declared {ref}`link <lab-links>` whose `endpoints[].host` names one.

An `os_profile` can supply `board` or `slot` as a default, so a host record
that names no `board` may still get one, and a different id than the record
alone suggests.  When authoring a link endpoint, take the id from `otto host
<TAB>` (or `otto --show-lab`) rather than composing it by eye: an endpoint
naming an id no host answers to fails the lab load.  The profile is resolved
with every selected project's `[os_profiles]` tables, so a lab in one project may
name a table another project declares; a custom lab backend receives them as
`env.profiles` ({doc}`../../extend/lab-source-backends`).

Element names are unique per source, compared **by slug**: `Server` and
`server` are the same element and the load fails naming both entries.  Several
elements of one type are named distinctly — `server1` / `server2`, or whatever
your site's scheme is; the number is yours, written once, and the same in every
lab combination.  Two hosts of one element that resolve to the same id (two
board-less hosts, say) fail the load with a clear error instead of one silently
overwriting the other.

The **display name** (`host.name`) is the element `name`, then `board`, then
`slot`, space-joined with parts omitted when absent — **exactly as written**
in the lab entry, with no case change and no number added.  Setting an
explicit `name` on the host entry replaces this label entirely.

On the CLI, wherever a *host* is named — the `otto host <id>` positional,
`--hop`, and docker's `--parent` — you type the id; tab completion offers it.

```json
{
  "elements": [
    { "name": "Lab X Server", "labs": ["unix"],
      "hosts": [{ "ip": "10.0.0.2", "creds": [{"login": "root", "password": "pw"}] }] },
    { "name": "server1", "id": 47, "labs": ["unix"],
      "hosts": [{ "ip": "10.0.0.3", "creds": [{"login": "root", "password": "pw"}] }] },
    { "name": "Edge Router", "labs": ["unix"],
      "hosts": [{ "ip": "10.0.0.4", "board": "LineCard", "slot": 3,
                  "creds": [{"login": "root", "password": "pw"}] }] },
    { "name": "chassis", "labs": ["unix"],
      "hosts": [
        { "ip": "10.0.0.5", "board": "cpu", "slot": 1, "creds": [{"login": "root", "password": "pw"}] },
        { "ip": "10.0.0.6", "board": "cpu", "slot": 2, "creds": [{"login": "root", "password": "pw"}] },
        { "ip": "10.0.0.7", "board": "io", "slot": 7, "creds": [{"login": "root", "password": "pw"}] }
      ] }
  ]
}
```

| Element `name` / `id` | Host id | Display name | `host.element.id` |
| --- | --- | --- | --- |
| `"Lab X Server"` | `lab-x-server` | `Lab X Server` | `None` |
| `"server1"` / `47` | `server1` | `server1` | `47` |
| `"Edge Router"`, board `LineCard`, slot `3` | `edge-router-linecard-3` | `Edge Router LineCard 3` | `None` |
| `"chassis"`, board `cpu`, slot `1` | `chassis-cpu-1` | `chassis cpu 1` | `None` |
| `"chassis"`, board `cpu`, slot `2` | `chassis-cpu-2` | `chassis cpu 2` | `None` |
| `"chassis"`, board `io`, slot `7` | `chassis-io-7` | `chassis io 7` | `None` |

A single element with several hosts — a chassis's boards — is the common
case a repeated *name* is not: one element, several host entries, one id and
one display name per host, all sharing the element's `metadata` and `id`.
