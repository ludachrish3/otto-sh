# Elements

An `elements` entry is one piece of equipment and the host entries that
address it.  It carries identity, lab membership, and metadata — and nothing
operational.  There is no element-to-host inheritance: credentials, hops, and
option tables stay on the host entries.

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Element name — the human-readable name of the equipment, and the source of every child host's id (`slug(name)`; see {ref}`host-identity`).  Must slug to a non-empty token.  Required. |
| `id` | integer | Data the author assigns to the element; reached as `host.element.id`, never part of any id.  Must be `>= 0`. |
| `labs` | array of strings | Membership patterns, `re.fullmatch`-ed against a lab name (below).  Required and non-empty: an element that joins nothing is a mistake, and "every lab" is spelled `[".*"]`. |
| `metadata` | object | Opaque element-level user data; otto never reads it.  Surfaces as `host.element.metadata` on every host of the element.  The element's `resources` below surface the same way, as `host.element.resources` (a frozenset). |
| `resources` | array of strings | Reservation identifiers for the element as one unit — the chassis, where the lab is too coarse and a slot too fine.  Combined with the lab's and each host's; see {doc}`../../cli/reservation/index`.  Defaults to empty. |
| `hosts` | array of objects | The element's host entries — the [per-host fields](host-fields.md#per-host-fields).  Required and non-empty. |

**Membership is by pattern.**  Each `labs` entry is a Python regular
expression matched with `re.fullmatch` against a lab name — the same rule
`[project] lab_patterns` uses ({ref}`project-scope`), so there is one
matching rule to learn:

- A bare name is a pattern that matches itself: `"unix"` joins `unix`, and
  does *not* join `unix2`.
- `".*"` joins every declared lab.
- `"unix(\\..*)?"` joins `unix` and every dotted sub-lab of it.
- `.` is a metacharacter.  A lab actually named `lab.1` is matched by
  `"lab\\.1"`; the unescaped `"lab.1"` matches `lab-1` as well.

An invalid regex fails at parse, naming the element and the pattern.  A
pattern that fullmatches no declared lab anywhere is *dead*: `otto init`
reports it as a warning rather than a load error, because a shared file may
legitimately serve projects that declare different labs.

**The element is the smallest unit assignable to a lab.**  A chassis whose
boards must belong to *different* labs is modelled as separate elements with
distinct names.  `os_type` stays per host either way, so a Zephyr board and
its Unix management host can share one element when they share membership.

**The element is also the unit of multi-source override.**  When two
`[[lab.sources]]` entries carry an element with the same name (compared by
slug), the later source's element replaces the earlier one **wholesale** —
hosts, metadata, and the membership the later element states — with a warning
naming both sources.  Overriding one board of a four-board chassis means
restating the whole element entry; a hybrid element (this source's hosts with
that source's metadata) never results.  `labs` entries replace the same way,
resources and metadata together.  See {doc}`../host-sources`.

Replacement happens per lab load, so it covers exactly the labs *both*
elements match.  An override that **drops** a membership pattern therefore
does not take the element out of a lab the earlier source's element still
matches — that lab keeps loading the earlier element; to remove an element
from a lab, change it at the source that declares it.
