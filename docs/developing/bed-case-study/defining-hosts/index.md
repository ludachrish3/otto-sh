# Defining hosts

Defining hosts is the one thing otto cannot run without. The workflow is
short: write down what you know, let otto find out the rest, pin what it
found.

Before the first entry, know where the two things this section grows into
come from. Host *facts* — addresses, credentials, rack positions — can be
written in the lab file, as this page does, or referenced from an **inventory**: a JSON
file, NetBox, or a backend you write ({doc}`../../../extend/inventory-backends`).
And the lab can be gated by a **reservation scheduler** ({doc}`../reservations`)
— the JSON file otto ships, or a backend you write
({doc}`../../../extend/reservation-backends`).
Both seams are pluggable; the pages that follow use the shipped backends and
link to the contract when you outgrow them.

## What you write first

The example is the project `docs/examples/getting-started/`
({ref}`where to get it <bed-example-projects>`). A
[project root](../../../overview.md#projects-project-roots-and-ottopath)
is the directory holding `.otto/settings.toml`. The example's identity and
where its lab data lives:

```{literalinclude} ../../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin identity"
:end-before: "# doc: end identity"
```

```{literalinclude} ../../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin lab-sources"
:end-before: "# doc: end lab-sources"
```

`backend = "jsonnet"` says the lab file is a Jsonnet template,
`lab_data/lab.jsonnet`, rather than a hand-written `lab.json`; a section
below shows why. `paths` lists directories (each one's `lab.jsonnet`),
`.jsonnet` files or globs, so lab data can be split across many files: see
{ref}`The source's settings <the-sources-settings>`.

And where the project's own code lives: `libs` puts it on the import path,
and `init` lists every module otto imports as it loads. The first is the
`libs/gs_example/` package itself, where the later pages register a command
frame, parsers, a login proxy and a reservation backend; the three beside it
are the modules {doc}`../../../setup/defining-products-and-tools` and
{doc}`../../../setup/customizing-project-instructions` add — the project's products, its
dev tools, and its `ProjectActions`:

```{literalinclude} ../../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin libs"
:end-before: "# doc: end libs"
```

The lab file declares the labs and the elements that join them
({doc}`../../../configuration/lab-config` is the field reference). The
four labs the bed declares:

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin labs"
:end-before: "// doc: end labs"
```

## One function for hosts that share a shape

The bed's four Ubuntu VMs have the same fields, and their addresses follow
their number: `test<n>` is `10.10.200.(10+n)`. Written out by hand, that is
four copies of one entry to keep in step. So the lab file is a template, in
[Jsonnet](https://jsonnet.org): a function writes the shared entry once, and
each VM states only what makes it different
({doc}`../../../configuration/lab/templating` is the reference, and its
{ref}`simple example <lab-templating-simple>` makes whole labs this way).

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin ubuntu-vm"
:end-before: "// doc: end ubuntu-vm"
```

`ubuntuVm(n, labs, host, element)` returns one element named `test<n>`, a
member of each lab in `labs`, with one host. `host` holds what this VM adds
to the shared host entry, or changes in it, and `element` the same for the
element: each is merged over the shared fields with `+`, so a field it
states replaces the shared one. `racked(shelf)` is what the three
docker-capable VMs share: their rack position, and `docker_capable: true`,
which lets otto run containers on them. The BusyBox guests and the Zephyr targets on
the next pages have a function each, the same way.

The first element is `test1`, a member of two labs. `ubuntuVm(1, ...)`
gives it its name, its address and the two shared logins; the call adds what
only `test1` has: how its sessions are set up, a third login, its rack
position, one more interface, and the element's resources and metadata.
All of it you can write down before ever connecting. `local test1 = ...`
names the element, so the document at the end of the file can list it.
`racked(3) { ... }` is Jsonnet's short form of `racked(3) + { ... }`. And
`creds+:` and `interfaces+:` add to the shared list and table instead of
replacing them:

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin test1"
:end-before: "// doc: end test1"
```

(bed-sudo-root-warning)=

The third credential is not a password: it names a login *proxy*, code that
{doc}`../customizations` registers from the `init` module above. Leave that
line out until you reach that page — a lab that names an unregistered proxy
refuses to load.

`os_type: 'unix'`, in `ubuntuVm`, selects the host class
({doc}`../../../configuration/os-profiles`). `hop`, absent from `test1`,
names the host otto must reach this one through.

(bed-lab-document)=

The file ends with the lab document itself, the value otto reads. It lists
every element in order: the ones the case study's pages show by name, like
`test1`, and the rest written in place:

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin document"
:end-before: "// doc: end document"
```

To see the hosts the template made, with their addresses, show the lab:
`otto --lab unix --show-lab`
([Inspecting what a lab holds](../../../cli/index.md#inspecting-what-a-lab-holds)).
`otto --lab unix --list-hosts` lists their ids.

`otto lab render lab_data/lab.jsonnet` refuses this lab. Its hosts carry the
bed's well-known passwords as literals, and `lab render` refuses to print a
document that holds a password
({ref}`What otto checks in a rendered document <lab-templating-checks>`).
For a lab whose hosts are written inline, use `--show-lab` and `--list-hosts`
instead. [#1269](https://github.com/ludachrish3/otto-sh/issues/1269) tracks
a way to render such a lab.

## What otto finds out

Everything a Unix host's userland does differently — which `stat` it has,
whether `base64` takes `-d` or `--decode`, how it elevates — is a
`userland_options` field, and every one of them defaults to *unset*, which
means "probe it at connect". `otto host <id> probe` runs those probes once
and prints the result as a pin; `--lab unix` names the lab to load, and a
host that belongs to several labs is reached through any one of them:

```{literalinclude} ../../../examples/captures/probe-test1.txt
:language: text
```

The `fleet of interest` line reports the project's `[project]` scope
({doc}`../boards-of-interest`); an explicitly named host like `test1` is
never scoped by it.

Paste the pin into the host's entry and the next connection skips every
probe. In this template a VM's host entry is its `host` argument, so the pin
goes there, merged in beside the rest:
`host=racked(3) { userland_options: { ... }, ... }`. Every `userland_options` field is optional: the seven `applet_*`
fields answer `"present"` or `"absent"`, the rest name a tool or a flag
(`elevation: sudo`, `base64_flag: -d`, …), and any of them may be left
unset, so a fresh entry works before anyone has pinned it. The per-field
values are listed at {ref}`userland-capabilities`.

## The pages that follow

```{toctree}
:maxdepth: 1

inventory
unix
busybox
zephyr
```
