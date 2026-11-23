(lab-templating)=

# Templated labs

A lab file states each element by hand. When several labs share one shape,
and only their names, addresses and ports change from lab to lab, a
**templated lab** writes the shape once, in [Jsonnet](https://jsonnet.org),
and otto renders the lab documents from it each time it needs them. The
labs may also be a rule over machines that already live in an inventory,
such as NetBox: "one router lab per site, with that site's router and its
traffic generator".

The Jsonnet language is taught by its
[tutorial](https://jsonnet.org/learning/tutorial.html), and its functions are
listed in the [standard library](https://jsonnet.org/ref/stdlib.html). This
page starts with a {ref}`simple example <lab-templating-simple>`, then an
{ref}`advanced one <lab-templating-advanced>` that reads inventories, and
then the reference.

A `backend = "jsonnet"` lab source renders Jsonnet **entry files** into lab
documents, in memory, and reads each one exactly as it reads a `lab.json`
({doc}`files-and-membership`). Nothing rendered is written to disk. The
inventories keep the machine facts; the template decides which labs exist and
which records join each one. How a lab source differs from an inventory is
stated once, on {doc}`../host-sources`.

Rendering needs a Jsonnet binding. otto installs one on most platforms; the
platforms without one are listed in {ref}`jsonnet-known-gaps`. Everything that
renders no template works without one.

(lab-templating-simple)=

## A simple example: one bench lab per number

Each bench is a lab of two hosts, a DUT and a traffic generator. Its number
`n` decides everything else: the lab is `bench<n>`, its hosts sit on
`10.20.<n>.0/24`, and host `k` of the bench has ssh port `2200 + 100 * n + k`.
The example needs no inventory.

Make an empty directory named `benches` and create these two files in it.
The paths below are relative to `benches/`.

```{literalinclude} ../../examples/templating/simple/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
```

The settings declare one `jsonnet` lab source
({ref}`The source's settings <the-sources-settings>`). It reads the
`lab.jsonnet` of each directory its `paths` list.

```{literalinclude} ../../examples/templating/simple/lab_data/lab.jsonnet
:language: jsonnet
:caption: lab_data/lab.jsonnet
```

The file is plain Jsonnet:

- `benches` lists the bench numbers.
- `subnet(n)` and `sshPortBase(n)` hold the address and port scheme.
- `bench(n)` takes one bench number and returns that bench's two elements,
  `bench<n>-dut` and `bench<n>-tgen`. Each joins the lab `bench<n>` and holds
  one host. Host `k` (1 for the DUT, 2 for the traffic generator) gets the
  `ip` `10.20.<n>.<k>` and the `ssh_options.port` `2200 + 100 * n + k`
  ({doc}`../host-options`).
- The object at the end is the lab document ({doc}`files-and-membership`): a
  `labs` table with one lab per number, and the elements of every bench.

To add a bench, add its number to `benches`: `[1, 2, 3, 4]` adds `bench4`,
on `10.20.4.0/24` with ports 2601 and 2602. To change the address or port
scheme, edit `subnet` or `sshPortBase`.

A real lab's hosts need credentials, either inline `creds` in the lab file or
an inventory's creds store ({ref}`credentials-layered`), which the advanced
example uses. An inline literal password makes `otto lab render` refuse the
document ({ref}`What otto checks in a rendered document <lab-templating-checks>`), and
[#1269](https://github.com/ludachrish3/otto-sh/issues/1269) tracks that.

Point otto at the project, from inside `benches/`. otto finds projects
through `OTTOPATH`, never through the current directory
([Environment variables](../../cli/index.md#environment-variables)):

```bash
export OTTOPATH=$PWD
otto --list-labs
```

```{literalinclude} ../../examples/templating/output/simple/list-labs.txt
:language: text
```

`otto lab render` prints the document the file renders to, so each host's
name, address and port is in view ({doc}`../../cli/lab/index`):

```bash
otto lab render lab_data/lab.jsonnet
```

```{literalinclude} ../../examples/templating/output/simple/render.json
:language: json
```

One lab, as otto loads it:

```bash
otto -l bench2 --show-lab
```

```{literalinclude} ../../examples/templating/output/simple/show-lab.txt
:language: text
```

(lab-templating-advanced)=

## Advanced: labs generated from inventory records

Reach for this when the machines already live in an inventory, such as
NetBox or an inventory file, and a lab is a rule over their records. The
template then holds no addresses at all: it selects records from the
inventories, pairs them into labs by role, and marks a lab whose roles are
incomplete as unavailable, while the other labs still load. The credentials
stay in the inventory's creds store. That is what `otto.libsonnet`'s
`layout()` and the `function(otto)` entry below buy
({ref}`otto.libsonnet <otto-libsonnet>`).

This example builds a project from an empty directory. Its two inventories
are `.jsonnet` files standing in for NetBox, so it runs without one:
`physical` holds one router per site, and `virtual` holds the traffic
generator VMs. The template selects records by their `groups`, which a NetBox
record does not carry yet, so pointing `physical` at NetBox would need another
selector ([A Jsonnet inventory file](../inventory.md#a-jsonnet-inventory-file)
says what the switch needs). The template makes one lab per site, `router-lab.<site>`,
holding that site's router (the DUT) and its traffic generator.

Make an empty directory named `routers` and create these six files in it.
The paths below are relative to `routers/`.

```{literalinclude} ../../examples/templating/routers/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
```

The settings declare the two inventories ({ref}`named-inventories`), each a
`backend = "json"` inventory whose `path` names a `.jsonnet` file that renders
its records
([A Jsonnet inventory file](../inventory.md#a-jsonnet-inventory-file)), the
`jsonnet` lab source ({ref}`The source's settings <the-sources-settings>`), and two
layers the {ref}`explanation below <lab-templating-explain>` shows: an `os_type`
profile for the routers ({doc}`../os-profiles`) and a `[host_preferences]`
value for every DUT ({doc}`host-preferences`).

```{literalinclude} ../../examples/templating/routers/inventories/physical.jsonnet
:language: jsonnet
:caption: inventories/physical.jsonnet
```

```{literalinclude} ../../examples/templating/routers/inventories/creds.json
:language: json
:caption: inventories/creds.json
```

```{literalinclude} ../../examples/templating/routers/inventories/virtual.jsonnet
:language: jsonnet
:caption: inventories/virtual.jsonnet
```

Each inventory file renders to an inventory document, the same flat
key-to-record map a `.json` inventory holds
([A Jsonnet inventory file](../inventory.md#a-jsonnet-inventory-file)).

```{literalinclude} ../../examples/templating/routers/lab_data/lib/hosts.libsonnet
:language: jsonnet
:caption: lab_data/lib/hosts.libsonnet
```

```{literalinclude} ../../examples/templating/routers/lab_data/layouts/router-lab.jsonnet
:language: jsonnet
:caption: lab_data/layouts/router-lab.jsonnet
```

The entry file is a function of one argument, `otto`, which holds the
records of the inventories the source declares
({ref}`What a template reads <what-a-template-reads>`). It imports two files.
`otto.libsonnet` is the scaffolding otto ships ({ref}`otto.libsonnet <otto-libsonnet>`).
`hosts.libsonnet` resolves under `lab_data/lib`, the source's default import
root ({ref}`Imports <lab-templating-imports>`). `o.groupBy` makes one instance per
`site`, and `o.layout` turns each instance into one lab, with one element per
role.

Point otto at the project, from inside `routers/`. otto finds projects through
`OTTOPATH`, never through the current directory
([Environment variables](../../cli/index.md#environment-variables)):

```bash
export OTTOPATH=$PWD
```

The outputs below show the project at `/home/me/routers`; yours show your own
directory, and a long line may wrap at your terminal's width.

### List the labs

```bash
otto --list-labs
```

```{literalinclude} ../../examples/templating/output/list-labs.txt
:language: text
```

East has a DUT and a traffic generator. West has a router but no traffic
generator yet, so its lab is listed **unavailable**, with the reason the
layout gave, and the other lab still loads
({ref}`Instances, roles and counts <instances-roles-and-counts>`). The listing is
described in {ref}`listing-labs`.

```bash
otto -l router-lab.east --list-hosts
```

```{literalinclude} ../../examples/templating/output/list-hosts.txt
:language: text
```

```bash
otto -l router-lab.west --list-hosts
```

```{literalinclude} ../../examples/templating/output/west.txt
:language: text
```

### See the rendered document

`otto lab render` prints the document an entry file renders to, before otto
validates it ({doc}`../../cli/lab/index`):

```bash
otto lab render lab_data/layouts/router-lab.jsonnet
```

```{literalinclude} ../../examples/templating/output/render.json
:language: json
```

It is an ordinary lab document: a `labs` table, `elements` whose hosts
reference their inventory records by name and key, and membership patterns
that match exactly one lab (`router\-lab\.east` is `router-lab.east`,
escaped). The `_otto` key is comment space to the loader, as every
`_`-prefixed key is; `otto lab explain` reads the provenance in it.

(lab-templating-explain)=

### Explain a host

`otto lab explain` shows which layer each of a host's values came from
({doc}`../../cli/lab/index`):

```bash
otto -l router-lab.east lab explain east-dut0
```

```{literalinclude} ../../examples/templating/output/explain.txt
:language: text
```

The `ip` and `site` come from the `physical` record, the password from
`physical`'s creds store, `os_type` from the template, `valid_terms` from the
`router` profile, and the connect timeout from `[host_preferences]`. Which layer wins when two of them state a
value is on {doc}`../precedence`.

Explain names the layer, never the value. To see values, show the lab:
`otto -l router-lab.east --show-lab` prints each host's address, transports
and products.

(the-sources-settings)=

## The source's settings

A `jsonnet` source is a `[[lab.sources]]` entry ({doc}`../host-sources`):

```toml
[[lab.sources]]
backend = "jsonnet"
paths = ["lab_data/layouts/router-lab.jsonnet"]
inventories = ["physical", "virtual"]
scope = { prefixes = ["router-lab."] }
```

| Key | Default | Meaning |
|-----|---------|---------|
| `paths` | required | The entry files to render. Each entry is a directory (its `lab.jsonnet`), a `.jsonnet` file, or a glob, anchored at the project root, as a json source's `paths` are ({ref}`lab-data-across-files`). A source whose paths name no entry file fails, listing the paths it searched. |
| `inventories` | `[]` | The inventories the templates read, by name. They are the only inventories a rendered document may reference. |
| `scope` | none | The lab names the source may declare: `{ names = [...], prefixes = [...] }`, exact names and literal prefixes. At least one of the two lists must be non-empty. See {ref}`Scope <lab-templating-scope>`. |
| `overrides` | `[]` | Element names whose replacement of another source's element with different machines is intended. See {ref}`Generated elements that replace others <lab-templating-overrides>`. |
| `import_paths` | `["lab_data/lib"]` | The directories imports resolve under, in order, anchored at the project root. A directory that does not exist is allowed; nothing resolves there. See {ref}`Imports <lab-templating-imports>`. |

```{note}
`scope` is a table, never a list: an exact name and a prefix are spelled
apart, so `router-lab.east` in `names` is never read as a prefix. Early design
sketches wrote it as a plain list.
```

Every entry file is evaluated with one top-level argument, `otto`. Each one
renders once per otto operation (one run, one `--list-labs`, one doctor pass),
and listing, loading, host summaries and the doctor share that one rendering.

(what-a-template-reads)=

## What a template reads

The `otto` argument holds three fields:

- `project`: the name of the project that declares the source. An entry
  loaded from Python with `search_paths=[...]` has no declaring project
  ({func}`otto.load_lab <otto.lab.load_lab>` says how it reads one), so
  there `project` is the empty string `""`.
- `inventories`: for each inventory the source declares, the list of its
  records, sorted by key.
- `instances`: for each of those inventories, the instances its records
  declare (`{<instance>: {members, roles}}`), or `null` when its backend has
  no notion of instances ([Declaring instances](../inventory.md#declaring-instances-the-ottoinventoryv2-document)).

A record holds `inventory` (the inventory's name), `key`, and the public
fields its inventory states: `ip`, `interfaces`, `hw_version`, `sw_version`,
`os_name`, `os_version`, `board`, `site`, `rack`, `shelf`, `slot`,
`is_virtual`, `element_id`, `groups` and `labels`
([Record fields](../inventory.md#record-fields)). When the inventory declares
instances, a record also holds `instances`, the instances its key belongs to
and the role it plays in each. **A template never reads a credential**: a
record's `creds` and `extra` are never in it, nor is anything from a creds
store. No environment variable, external variable or native function reaches
a template either.

A field the inventory does not state for a record is **absent**, not `null`.
Read an optional field with a default, `std.get(record, 'rack', null)`.

(otto-libsonnet)=

## otto.libsonnet

`import 'otto.libsonnet'` always resolves to the library otto ships, whatever
the import roots hold. It is plain Jsonnet: a template may read it, wrap it or
ignore it. It provides:

- **Predicates** over a record, each a function returning true or false:
  `inGroup(name)`, `label(name, value)`, `fact(name, value)` and
  `inInventory(name)`, combined with `allOf([...])` and `anyOf([...])`, or
  with any predicate of your own.
- **Instances**, the sets of records that become one lab each:
  `instances(otto, inventory)` reads the instances an inventory declares, and
  `groupBy(records, scope)` derives them by grouping records on fields such as
  `['site', 'rack']`. Records from several inventories may share an instance.
- **`layout(spec)`**, which turns instances into a lab document: one lab per
  instance and one element per role member.
- **`element(name, record, host, labs)`**, one element with a qualified
  inventory reference, for a template that builds its document by hand.
- **`escapeName(s)`**, a membership pattern that matches only the lab `s`.
  A name that holds a NUL character is an error.

The library's comments are its reference. The instance shape:

```{literalinclude} ../../../src/otto/templating/otto.libsonnet
:language: jsonnet
:start-at: "  // Both instance providers return a list of instances of one shape:"
:end-before: "  // instances(otto, inventory):"
:dedent: 2
```

And `layout`'s spec:

```{literalinclude} ../../../src/otto/templating/otto.libsonnet
:language: jsonnet
:start-at: "  // layout(spec): a lab document with one lab per instance, and its elements."
:end-before: "  layout(spec)::"
:dedent: 2
```

(instances-roles-and-counts)=

### Instances, roles and counts

Each role states how many members it needs (`count`). An instance with a role
whose member count differs from `count`, with a `missing` member, or with an
`error` emits no element, and its lab is declared unavailable with the reason
(`tgen: expected 1, found 0 (none)` in the example). The other instances' labs
load. Records are never paired by the order an inventory lists them: members
are taken in (inventory, key) order.

Lab and element names are yours to build from the instance's `id` and the
role. A name emitted twice fails the render.

(lab-templating-authoring-notes)=

### Authoring notes

- **`fact()` matches stated fields only.** `fact('is_virtual', false)` does
  not match a record that never states `is_virtual`, because an unstated
  field is absent from the record ({ref}`What a template reads <what-a-template-reads>`).
  The same holds for `label()`, which also compares types: `'7'` is not `7`.
- **`error` is a Jsonnet keyword.** Read an instance's error as `i['error']`,
  never `i.error`.
- **`missing` holds qualified keys:** `[{inventory, key}]`, one per member the
  inventory has no record for.
- **An error instance's `id` holds `null`.** A record that lacks a `groupBy`
  scope value gets an instance of its own, whose `id` holds `null` for that
  field and whose `error` says which field is missing. `lab(i)` is called for
  it too, so write `lab(i)` to tolerate `null`: `'router-lab.' + i.id.site`
  spells it `router-lab.null`, and that lab is listed unavailable with the
  error while every other lab loads. A `lab(i)` that does not tolerate it
  fails the whole file, loudly, because Jsonnet cannot catch an error: one
  that returns `i.id.site` itself fails naming the instance, and one that
  calls a std function on the `null`, such as `std.asciiLower(i.id.site)`,
  fails at that call.
- **Composing two layouts needs `+:`.** `o.layout(a) + o.layout(b)` keeps
  only `b`'s `labs`, `elements` and `_otto`, because `+` replaces a field.
  Merge each field instead, `_otto`'s `provenance` included:

  ```jsonnet
  local routers = o.layout(routerSpec);
  local switches = o.layout(switchSpec);
  routers + {
    labs+: switches.labs,
    elements+: switches.elements,
    _otto+: { provenance+: switches._otto.provenance },
  }
  ```

  Each `layout` checks its own names for duplicates; across the two, a lab
  named twice keeps the second entry, and two elements that compose one host
  id fail the source ({ref}`What otto checks <lab-templating-checks>`).
- **Instance order.** `groupBy` orders instances by their scope values, which
  is deterministic and independent of the records' order. Within one field it
  is the order of the values written as JSON, not their plain string order,
  so sites `'a b'`, `'a!'` and `'a'` come in that order. Name your labs from
  the values, never from an instance's position.

(lab-templating-imports)=

## Imports

One import rule serves `import`, `importstr` and `importbin`, in a lab
template and in a `.jsonnet` inventory alike:

- `otto.libsonnet` is always served, from otto itself.
- Every other import resolves under the source's `import_paths`, in order. A
  file imported from inside a root may also import relative to its own
  directory. The **entry file's own directory is not a root**, so a file
  beside an entry is importable only when a root holds it.
- Symlinks are collapsed before the check, and an import that resolves
  outside every root is refused. An absolute import path is refused.
- **A protected file is never served, even inside a root:** every settings
  file of the active projects and of `~/.otto/settings.toml`, and every file
  their inventory and credentials tables name. A template cannot read an
  inventory file, a creds store or a settings file. Every otto command builds
  that list from the settings; a library caller's hand-built
  `otto.inventory.InventoryView`, or an `otto.inventory.JsonInventory` built
  with no `env`, has none, and renders no template at all.
- **Lab files are not protected.** The list holds settings, inventory and
  credentials files, not the lab files of your file-backed lab sources, and
  a hand-written lab file may hold a literal password (`otto init
  --inline-hosts` writes one). Keep every lab file outside your import
  roots: an import root must not hold one.
- **No list, no render.** While `~/.otto/settings.toml` cannot be read or is
  invalid, the files it names are unknown, so no template renders: each one
  is refused with a message naming that file, and a hand-written lab file
  still loads. Fix the file and the templates render again.
- `JSONNET_PATH` and every other ambient search path are ignored.

(lab-templating-checks)=

## What otto checks in a rendered document

A rendered document is read by the same reader as a `lab.json`: the same
sections, the same `_`-prefixed comment space, the same host-entry checks.
Errors name the entry file, and a Jsonnet error names the file and line where
evaluation stopped. A generated document is held to three more rules, each
failing the source and naming the entry file:

- **Every inventory reference is qualified**, `{"name": <inventory>, "key":
  <key>}`, and names an inventory the source declares. A bare
  `"inventory": "<key>"` is refused ({doc}`inventory-references`).
- **Every host id is unique within the source**: two entries that compose one
  host id are refused, naming both.
- **In a scoped source**, everything stays inside the scope
  ({ref}`Scope <lab-templating-scope>`).

A document may carry a literal password, as a lab file may, but keep
passwords in the credentials store ({ref}`credentials-layered`):
`otto lab render` refuses to print a document that holds one.

(lab-templating-scope)=

## Scope: what fails when a template fails

A source without a `scope` might contribute to any lab, so when it fails
(an inventory is down, the template has an error, its `paths` match no entry
file), every lab load fails,
naming the source. A source with a `scope` fails only the labs inside it, and
otto asks it for no lab outside it ({ref}`source-failure-scope`).

otto holds a scoped source to its scope on what it renders:

- every lab the document declares is inside the scope, and
- every element membership pattern names exactly one lab, and that lab is
  inside the scope. `layout` and `element` write such patterns. Shared
  equipment that joins labs by an open pattern, a console server for every
  `router-lab\..*`, belongs in a hand-written source.

A breach fails the source, naming the entry file and what was outside. Its
declared links are not checked against the scope: they reach only labs inside
it because otto never asks the source for another lab
({ref}`lab-links-across-sources`).

**The last labs a scoped source declared.** A template's labs are known only
by rendering it, so a source that fails cannot say which labs it would have
declared. A scoped source therefore records the lab names of its last
complete read, under `lab-names/` in otto's home
([otto cache](../../cli/cache/index.md)). Only a scoped source records them,
and only when they change. They are diagnostic: `--list-labs` shows them,
unavailable with the source's reason, only while the source is failing, and
they never let a lab load. `otto cache clear --all` and `otto cache prune`
remove them; the source writes them again on its next complete read.

(lab-templating-overrides)=

## Generated elements that replace others

Across sources, a later source's element replaces an earlier one's in the same
lab ([Ordering and overrides](../host-sources.md#ordering-and-overrides)).
When the replacing element is one a `jsonnet` source generated, otto first
compares the two elements' machines: the address of each host and the
inventory record it references. The same machines override
as any element does, with the override warning. Different machines are an
error, because a template that reuses an element name by accident would
otherwise replace a lab's real equipment.

For example, give the {ref}`router example <lab-templating-advanced>` a hand-written source, read before the
template's, that holds an element named `east-dut0` at another address. Its
lab file is `bench/lab.json`:

```{literalinclude} ../../examples/templating/override/bench/lab.json
:language: json
:caption: bench/lab.json
```

and the two sources replace the example's one `[[lab.sources]]` table:

```{literalinclude} ../../examples/templating/override/sources.toml
:language: toml
```

The template's source is now the second, `routers/jsonnet#2`, and its
`east-dut0` would replace `bench`'s, so loading the lab fails:

```bash
otto -l router-lab.east --list-hosts
```

```{literalinclude} ../../examples/templating/output/override.txt
:language: text
```

List the element in the source's `overrides` to mean it: with
`overrides = ["east-dut0"]` on the template's source, its `east-dut0`
replaces `bench`'s, with the override warning. An entry is read as
the element's name, compared by its slug as elements are. An `overrides` entry
that names no element the source generates is a warning of `otto init`
({ref}`doctor-templated-labs`).

(lab-templating-completion)=

## Completion

Templated labs and hosts complete on TAB like hand-written ones, a cold cache
included. A TAB renders a source only when its inputs changed: its entry
files, every file under its import roots, `otto.libsonnet`, and each
declared inventory. A warm TAB only checks those files' metadata, as it does for a
`lab.json`. A render that takes too long is given up on, and reported
({ref}`Limits <lab-templating-limits>`).

(lab-templating-limits)=

## Limits

Every evaluation runs under these limits:

| Limit | Value | What it bounds |
|-------|-------|----------------|
| Stack | 500 frames | Deeper recursion fails the render with `max stack frames exceeded`. |
| Output | 25 MiB | A larger rendered document is refused before otto parses or keeps any of it. |
| Error trace | 20 frames | The most stack frames a Jsonnet error message lists. |

What is **not** bounded:

- **Memory and CPU.** Neither binding can bound an evaluation's memory or be
  interrupted, and otto runs no evaluator in a separate process. A template
  that allocates without bound, or loops without recursing, runs until it
  ends.
- **A run's wait.** `--lab` and every other command wait for the template
  to finish, as they wait for a slow hand-written backend: a command has no
  time limit on a render.
- **TAB's wait** is bounded: completion gives up on a project's
  enumeration at its deadline, 2 seconds unless
  `OTTO_COMPLETION_HOST_TIMEOUT` says otherwise
  ({doc}`../../extend/lab-source-backends`). The deadline bounds the whole
  enumeration of the project, every source in it together, not each source:
  one slow source can use the budget, and then completion offers none of
  that project's labs or hosts, its healthy sources' included, and reports
  the project as dropped in `otto cache info` ({doc}`../../cli/cache/index`).
  Lab names have a deadline of their own, so a slow host summary never costs
  `--lab` its names; when the names themselves time out, the host summaries
  are given up on with them, so one stalled source costs one deadline. A render that had not started by then never
  starts ({func}`~otto.templating.memo.current_cancellation`), and the
  cancelled read surfaces as the source's timeout, raised from a
  {class}`~otto.templating.errors.RenderCancelled`. An evaluation already
  running cannot be stopped: it keeps running until the TAB's process exits.

**`std.trace` prints to standard error, as Jsonnet writes it**, during TAB
too, where it lands in your terminal. A trace can still print after
completion gave up, because the abandoned evaluation keeps running. otto
cannot capture or silence it: neither binding offers a trace sink per
evaluation (gojsonnet's C library has one per VM, out of Python's reach), and
redirecting standard error would capture unrelated output too. **Never trace
a secret.** A template's inputs hold none, but anything a template traces is
printed as it is.

(lab-templating-performance)=

## Performance notes for authors

A template renders on every operation and on every TAB whose inputs changed,
so its cost is felt in the shell. Measured with gojsonnet 0.22 on an aarch64
Linux VM:

- **`%` and `std.format` cost about 0.3 ms a call** under gojsonnet, because
  `std.format` is itself written in Jsonnet. Build per-record and per-element
  strings with `+` and `std.toString`, as `otto.libsonnet` does: 2,000 calls
  alone cost about 0.6 s.
- **Object lookups are slower under gojsonnet.** `std.objectHas`, `std.get`
  and `in` each take time proportional to the object's number of fields, and
  `std.setMember` far more. Index a table directly (`table[key]`), building
  one that holds every key you will ask for.
- **The inventory projection costs about 23 µs per record**, about 116 ms at
  5,000 records, before the template itself runs.

With completion's default 2-second deadline, a fleet of a few thousand
records leaves the template most of the budget: `layout` over 500
instances of 4 roles each takes under half a second. A template that formats every element
with `%`, or probes large objects with `std.objectHas`, can spend the rest and
be dropped from completion. Raise `OTTO_COMPLETION_HOST_TIMEOUT` only for a
source that is legitimately slow.

## Known limitations

- **Two elements without machine identity replace each other unchecked.** The
  comparison above reads each host's address and inventory record. An element
  whose hosts carry none, such as one that holds only containers, replaces an
  earlier element of the same name that also carries none with the override
  warning alone, even when `overrides` does not list it.
- **Only `otto init` reports an `overrides` entry that matches no element.** A
  load and `otto lab explain` do not ({ref}`doctor-templated-labs`).
- The doctor's own checks never render a template ({doc}`../../cli/init`).
