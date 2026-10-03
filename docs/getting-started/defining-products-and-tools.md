# Defining products and tools

The pages before this one defined *hosts*: machines otto can reach. This page
defines what goes **on** them.

Two things do, and they are the same shape:

- a **product** — a unit of the software under test. Whether the lab is
  installed is a question about products.
- a **dev tool** — the repo's own tooling that helps test the product: a trace
  probe, a scratch helper. It is placed and removed on its own schedule and is
  never part of the installed-or-not answer.

Neither is ever named in lab data: lab data describes machines, and what goes
on them lives in the project. Most products need no code at all.

## A product

A product is one artifact, where it stages on the host, and three command
strings — install, uninstall, and a check that answers "is it installed right
now". Declare it in `.otto/settings.toml`. `kind` says which built-in
behaviour drives the verbs: `shell` runs the command strings, and the
{doc}`configuration page <../configuration/declared-products-tools>` lists the
others.

```toml
[[products]]
name = "agent"
kind = "shell"
artifact = "build/agent"
stage_dir = "/opt/agent"
install = "chmod +x /opt/agent/agent && /opt/agent/agent --install"
uninstall = "/opt/agent/agent --uninstall; rm -rf /opt/agent"
check = "test -x /opt/agent/agent"
match = { os_type = "unix" }
```

`match` picks the hosts; `install`/`uninstall`/`check` run on each. Leave a
string out and otto takes the honest default: no `install` means staging was
the install, no `check` means otto assumes the product is not installed and
stages again. Every key is in {doc}`../configuration/declared-products-tools`.

Declaring a product makes the `[project]` table required — the repo has to say
which labs and hosts it is speaking for. The worked example declares it as
`lab_patterns = ["busybox"]` and `host_patterns = ["bb.*-qemu"]`, both
fullmatched regexes, defined in {ref}`project-scope` in
{doc}`../configuration/lab-config`.

### When a command string is not enough

A command string can do a lot: `{cov_dir}` expands in it, so an install can set
`GCOV_PREFIX` from the product's own coverage directory ({ref}`the placeholders
<declared-placeholders>`), and the shell can chain checks. What it cannot carry
is a value that differs per host — the placeholders are only `{cov_dir}` and
`{name}`, and a `match` table can pick one of a few known values but not carry a
free-form one. The worked example's install must pass each element's `role` from lab
metadata to the agent, so that one step is code. The entry names a class, and
the class overrides the one method:

```{literalinclude} ../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin product"
:end-before: "# doc: end product"
```

```{literalinclude} ../examples/getting-started/libs/gs_example/products.py
:language: python
:start-after: "# doc: begin product-class"
:end-before: "# doc: end product-class"
```

`DeclaredProduct` is the class every `kind = "shell"` entry builds; a subclass
inherits staging, the declared strings and the honest defaults, and replaces
only what it overrides (`stage`, `install`, `uninstall`, `is_installed`,
`get_logs`). The entry's other keys are the class's fields — `cov_dir` is the
one coverage-shaped thing a product declares: the host-side directory its
instrumented build writes `.gcda` counters into ({doc}`coverage` is the whole
coverage walkthrough). The class is imported from the repo's `libs` when the
entry is built; it is not listed in `init`. {ref}`class-entries` has the full
rules, including fields a subclass adds.

One rule follows: a product name is defined in data **or** in code
({ref}`one-definition`). A **provider** is a function that computes a host's
products; the registry refuses a lab in which a `[[products]]` entry and a
provider both define one name, naming both — the entry's `class` key is how
data gets custom behaviour. Products that must be computed — one per version,
one per run variant — are providers, in
{doc}`../cookbook/extending/product-providers`.

## A dev tool

A dev tool has the product's shape and a different lifecycle. Declare it the
same way, in `[[dev_tools]]`:

```{literalinclude} ../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin dev-tool"
:end-before: "# doc: end dev-tool"
```

A dev tool that needs code names a `DeclaredProduct` subclass the same way —
the class serves both products and dev tools.

Dev tools go on with `otto run install-tools` and come off with `otto run
cleanup`, never with `otto run uninstall`, and `otto run status` never counts
them: a host carrying nothing but a debug probe does not read as installed.

## What you get for free

With products declared and nothing else written, six commands already work
against the lab. Each walks every configured repo in dependency order:

| Command | What it does |
| ------- | ------------ |
| `otto run install` | Installs every repo's products, dependencies first, stopping at the first failure. |
| `otto run uninstall` | Takes the products off, dependents first, best-effort, hauling logs off first. |
| `otto run cleanup` | Uninstall, plus each repo's dev tools, each host's shared toolchain tools, and the lab's own leftovers (network impairments, otto's tunnels). |
| `otto run get-logs` | Gathers every repo's product logs, then sweeps each host's debug logs once. |
| `otto run install-tools` | Installs each repo's dev tools (and, when asked, each host's shared toolchain tools). |
| `otto run status` | Reports each repo's install state and the lab's, and exits on it. |

Two of them are worth trying first.

`otto run status` reads and changes nothing. It exits `0` when every
[counted](../cli/run/defaults.md#reading-status) repo is installed, `1`
when every counted repo is uninstalled, and `2` for anything in
between — a *partial* lab, which is what a half-finished install leaves behind.

`otto run install --ensure` converges rather than installs blindly: it reads
the lab's current state and does only the work that is missing, recovering a
partial lab instead of installing on top of remnants. That is exactly what a
test marked `@pytest.mark.ensure("installed")` runs before its body.

{doc}`../cli/run/defaults` is the full treatment: every flag, the walk
order across repos, what `cleanup` does and does not take off the lab, and how
to read `status`.

## Variants

A product can carry a `variant = "debug"` or `"field"` entry beside a generic
one; `otto --field run install` picks the field one. See {ref}`product-variants`.

## Next

The six commands above are otto's own bodies for six **project instructions**.
A repo can change what any of them do, and add flags of its own — that is
{doc}`customizing-project-instructions`, after the host customizations on the
next page.
