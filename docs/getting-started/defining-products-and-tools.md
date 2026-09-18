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
on them lives in the project's code. The project registers a **provider**: a
function otto runs once per host as it is ingested, which returns the products
(or dev tools) that host should carry.

## A product

A product knows how to get onto a host, how to install, how to come off,
whether it is installed right now — and, for a coverage build, whether its
artifact carries the compiler's instrumentation.

```{literalinclude} ../examples/getting-started/libs/gs_example/products.py
:language: python
:start-after: "# doc: begin product"
:end-before: "# doc: end product"
```

`cov_dir` is the one coverage-shaped thing a product declares: the host-side
directory its instrumented build writes `.gcda` counters into; `install`
hands it to `GCOV_PREFIX`. Leave it out and it defaults to
`/tmp/<name>`; either way `self.cov_dir` is concrete by the time a host
carries the product, so composing a command out of it always works.
{doc}`coverage` is the whole coverage walkthrough.

The provider is keyed on `os_type`, an attribute of the host that knows
nothing about products — which is the rule for providers generally: key on the
host's product-agnostic attributes, and source versions and artifact paths from
the project's own configuration.

Registering a provider makes the `[project]` table in `.otto/settings.toml`
required — the repo has to say which labs and hosts it is speaking for, and
otto will not call the provider for a host outside that declaration. The
worked example declares it as `lab_patterns = ["busybox"]` and
`host_patterns = ["bb.*_qemu"]` — both fullmatched regexes, defined in
{ref}`project-scope` in {doc}`../configuration/lab-config`.

## A dev tool

The same four methods, a separate registry, a different lifecycle:

```{literalinclude} ../examples/getting-started/libs/gs_example/dev_tools.py
:language: python
:start-after: "# doc: begin dev-tool"
:end-before: "# doc: end dev-tool"
```

Dev tools go on with `otto run install-tools` and come off with `otto run
cleanup`, never with `otto run uninstall`, and `otto run status` never counts
them: a host carrying nothing but a debug probe does not read as installed.

## What you get for free

With products registered and nothing else written, six commands already work
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
test suite marked `@pytest.mark.ensure("installed")` runs before it starts.

{doc}`../cli/run/defaults` is the full treatment: every flag, the walk
order across repos, what `cleanup` does and does not take off the lab, and how
to read `status`.

## Declaring instead of registering

A provider is code, and the common cases do not need any. A `[[products]]` or
`[[dev_tools]]` entry in `.otto/settings.toml` attaches a product to the hosts
a `match` table picks out, with no Python at all; a provider stays the fallback
for what a match table cannot express. See
{doc}`../configuration/declared-products-tools`.

## Next

The six commands above are otto's own bodies for six **project instructions**.
A repo can change what any of them do, and add flags of its own — that is
{doc}`customizing-project-instructions`, after the host customizations on the
next page.
