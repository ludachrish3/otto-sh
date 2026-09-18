# Collecting coverage

The previous page put a product on a host. This one makes that product report
which of its lines the lab actually executed.

Coverage in otto is **per product**. A product says where its counters land,
otto notices that the product is an instrumented build, and everything
downstream — the fetch, the run tree, the report — is keyed by
`(host, product)`. Nothing else has to be configured for a second product, or
a second host, to be kept apart from the first.

## Instrument the build

Coverage data does not exist unless the compiler put it there. Build *and
link* the product with `--coverage`, and keep the build tree: the compiler
writes one `.gcno` notes file per object at build time, and the report step
needs them to decode the `.gcda` counters the product writes at run time.

```make
CFLAGS  += --coverage -O0
LDFLAGS += --coverage
```

{doc}`../guide/cli/cov/instrumenting/gcc` is the full GCC treatment — version
matching, cross toolchains, the stale-build stamp guard — and its siblings
cover clang and embedded targets.

## Tell otto where counters land

An instrumented binary writes its `.gcda` files to the absolute paths baked in
at compile time, which are the build machine's, not the target's. `GCOV_PREFIX`
redirects them at run time, and `cov_dir` is where a product declares the
directory it redirects them to — the one place `otto cov` looks:

```{code-block} toml
:caption: .otto/settings.toml

[[products]]
name = "agent"
kind = "shell"
artifact = "build/agent"
dest_dir = "/opt/agent"
cov_dir = "/var/cov/agent"
check = "test -x /opt/agent/agent"
install = "chmod +x /opt/agent/agent && GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 /opt/agent/agent --install"
```

`check` is how otto asks the host whether this product is already there: a
command that exits zero means installed, and staging and installing are
skipped. Declare one. Without it the answer is always "no", so every run
re-stages and re-installs the product.

`install` runs under the host's default command timeout, 30 seconds, and must
return inside it. Anything that stays running — a daemon, an agent like this
one — is backgrounded with a trailing `&`, or the install fails on the timeout
rather than on anything being wrong.

`{cov_dir}` and `{name}` are the two placeholders `install`, `uninstall`, and
`check` accept; anything else is refused when settings load. A literal brace —
`awk '{{print $1}}'`, a shell `${{VAR}}` — is written doubled. See
{doc}`../guide/configuration/declared-products-tools` for the whole `shell`
kind.

A product written in Python says exactly the same thing. The previous page's
`AgentBinary` ({doc}`defining-products-and-tools`) declares
`cov_dir = "/var/cov/agent"` as a class attribute, and reads it back as
`self.cov_dir` when it composes the command:

```{literalinclude} ../examples/getting-started/libs/gs_example/products.py
:language: python
:pyobject: AgentBinary.install
:dedent: 4
```

One thing does change, and it is the one a code-defined product has to do for
itself: say whether its build is instrumented. A TOML `[[products]]` entry
names an `artifact`, so otto scans that file and answers on its own; a Python
`Product` is asked by calling `instrumented()`, and the base implementation
answers "cannot tell" — which is the `unknown` verdict below, and counts as
not instrumented. Override it with the same scan:

```{literalinclude} ../examples/getting-started/libs/gs_example/products.py
:language: python
:pyobject: AgentBinary.instrumented
:dedent: 4
```

`cov_dir` is optional. Left out, it defaults to `/tmp/<name>`, and either way
`self.cov_dir` is concrete by the time the product is attached to a host, so
building a command out of it always works.

`GCOV_PREFIX_STRIP` is the compiler's own knob, not otto's: it drops that many
leading components from the path baked in at compile time, so the tree under
`cov_dir` stays shallow instead of replicating the build machine's layout.
Pick it by counting the leading components of the build directory's absolute
path *on the machine that compiled the product* — `/home/dev/agent/build` is
four. Nothing in otto reads the value: the fetch pulls every `.gcda` it finds
under `cov_dir` into one flat directory per product, and the merge pairs each
one with its `.gcno` by file name, whatever depth it was written at.

## Switch coverage on

Retrieval is automatic, but it is not unconditional: otto collects only for a
repo that declares a `[coverage]` table, and a table counts as declared only
when it carries `hosts`, `tiers` or `embedded` — an empty `[coverage]` is the
same as none. The smallest one that works names the hosts to collect from:

```{code-block} toml
:caption: .otto/settings.toml

[coverage]
hosts = ".*"
```

`hosts` is a regular expression, full-matched against each host's otto id:
`".*"` is every host in the lab, `"test[12]"` is those two. A tier is optional
— with no `[coverage.tiers]` table otto assumes a single implicit tier named
`system`, of the `e2e` kind, which is the one a lab run collects into.
{doc}`../guide/cli/cov/tiers` is the tier model, and the unit views at the
bottom of this page are the second tier most projects add.

## Run tests

Nothing else is needed beyond the `[coverage]` table above. `otto test` looks
at every product on every coverage host, decides for itself whether any of
them is an instrumented build, and turns retrieval on when one is:

```bash
otto test TestMyDevice
```

It says so in the log, naming what it found:

```text
otto test: coverage retrieval on — instrumented products detected:
  test1: agent — yes
  test2: agent — yes
```

The verdict is read locally, off the artifact otto is about to stage: a build
compiled with `--coverage` carries its own `.gcda` path strings, and otto
looks for those. That has a consequence worth knowing before it bites you — a
missing or unreadable artifact is not "no", it is "cannot tell", and an
unknown counts as not instrumented. **A product whose artifact the test suite
builds during the run must be built before `otto test`,** or declare
`instrumented = true` and skip the scan.

Three modes, because auto is not always what you want:

| Flag | Meaning |
|---|---|
| *(none)* | Auto: on when some product is instrumented and `[coverage]` is configured |
| `--cov` | Insist. An error, before a single test runs, when nothing is instrumented |
| `--no-cov` | Off, whatever the lab looks like |

`--cov` failing early is the point — a run that was worth the lab time only
because it was going to produce coverage should not finish and then tell you.
The refusal is one line plus a table of every product it looked at and what it
concluded (an excerpt, at a narrow terminal):

```text
        coverage instrumentation
╭───────┬─────────┬──────────────╮
│ host  │ product │ instrumented │
├───────┼─────────┼──────────────┤
│ test1 │ agent   │ no           │
│ test2 │ agent   │ unknown      │
╰───────┴─────────┴──────────────╯
 unknown = the product cannot tell: override Product.instrumented(),
        or set `instrumented = true` on the [[products]] entry

error: otto test --cov: no instrumented product — coverage cannot be collected.
```

The caption is the remedy, and it appears only when something came back
`unknown` — an artifact the scan cannot see inside, such as an archive. A run
with *some* products instrumented proceeds and warns about the rest, so a
product that quietly stopped being a coverage build is visible rather than
silently absent from the report.

`--cov` also refuses when the repo has no `[coverage]` table at all: there is
nowhere to collect into. In auto mode the same situation is one warning and no
coverage, because a plain `otto test` asked for a test run, not for coverage,
and must not die of a coverage misconfiguration.

{doc}`../guide/cli/cov/during-tests` has the rest of the flags — an explicit
destination, the pre-run counter cleanup, the inline report.

## Where it lands

`otto test` writes its run into a fresh per-invocation directory under
`--xdir` — the directory you ran it from, unless you said otherwise — named
`test/<timestamp>_<suite>/`, and prints the path when the run ends. Counters
are fetched into that directory, one directory per host per product, mirroring
the `logs/` layout. {ref}`The run tree <run-tree>` is the shape, and it is the
same tree the logs pipeline writes into.

Two products on one host never collide, because they never share a directory —
not in the tree, and not on the host either, where the `/tmp/<name>` default
already separates them. `otto cov get` and `otto cov clean` walk each
product's own `cov_dir` in turn.

## Report

The report is rendered from those captures. Point `otto cov report` at the
run directory from the last step and tell it where to write:

```bash
otto cov report path/to/run_output/ --dir ./cov_report
```

That writes a self-contained HTML report into `./cov_report`. Open
`cov_report/index.html` in a browser — there is no server to start, and the
whole directory can be copied or published as it is.

Every run in the report carries its product: the runs page lists them as
`host · product`, so a lab with two products says which one each number came
from, and one of them can be pinned to narrow every page to its runs.
{doc}`../guide/cli/cov/report` is the full report tour, pinning included.

## A kernel module

A Linux kernel module is a product too, declared with `kind = "kmod"`
instead of `kind = "shell"` — but a module has no process to write
`GCOV_PREFIX` counters on exit, so a companion runtime, `otto_kgcov`
(`docs/examples/kgcov/`), does that job instead. A product links against it
and declares `coverage = "module"`:

```{literalinclude} ../../tests/repo5/.otto/settings.toml
:language: toml
:start-at: "[[products]]"
:end-before: "# The container-image products"
```

The library, `otto_kgcov`, is declared first — products install in
declaration order, so the module below it always finds it already loaded —
and with `instrumented = false` overriding the artifact scan, for the
reason its own entry comment gives. The consumer module, `otto_kmod_demo`,
sets `coverage = "module"` and a `cov_dir`: on install, otto appends
`gcov_dir=<cov_dir>` to *its own* `insmod` line — the parameter
`KGCOV_DECLARE()` declares on the module — and the module hands that value
to the `otto_kgcov` runtime at `KGCOV_INIT()`. A debugfs write then dumps
its counters there as ordinary `.gcda` files, fetched exactly like any
other product's.

```bash
otto test --cov TestKmodDemo
```

Coverage from a module's exit routine only reaches the report if the
module is uninstalled before the post-run fetch — a suite's teardown does
this by unloading the product, and the exit dump lands before `otto test
--cov`'s own post-run fetch runs (not `otto cov get`, a separate,
later command). {doc}`../guide/cli/cov/instrumenting/kernel-modules` has the
rest: why a runtime is needed at all, instrumenting a module of your own,
and the alternative `coverage = "kernel"` method.

## A container image

A container image run by a docker daemon is a product too, declared with
`kind = "docker_image"` instead of `kind = "shell"`. `install` loads it
(from a tarball, here) and runs it with `docker run -d`, the product's
`cov_dir` bind-mounted at the same path inside the container:

```{literalinclude} ../../tests/repo5/.otto/settings.toml
:language: toml
:start-after: "# needs no pull. An archive scans unknown, hence `instrumented = true`."
:end-at: "match = { id = \"test3\" }"
```

`instrumented = true` because the scan cannot see inside a tarball, so it
answers *unknown* on its own. The bind mount is the whole coverage story:
an instrumented binary inside the container writing its counters under
`GCOV_PREFIX=<cov_dir>` (set in `run_args` above) is, as far as the daemon
host is concerned, just writing ordinary files under `<cov_dir>` — the
same fetcher and the same default hooks pick them up, no different from a
product that was never containerized.

```bash
otto test --cov TestCovContainer
```

`image` can also be a `registry/name:tag` reference instead of a tarball
path, with `pull = true` to fetch it on every install rather than requiring
it already present. {doc}`../guide/cli/cov/instrumenting/containers` has
the rest: the tarball/reference split, what `uninstall` removes, and how
this differs from running a service *inside* a container as the thing
under test.

## Several products, one container

A compose-built container host ingests products exactly like any other host,
so a container running two products reports two products. That is a
service under test *inside* a compose-built container; for an image you
run *as* a product, see {doc}`../guide/cli/cov/instrumenting/containers`.

## Unit test views

Unit coverage is collected at report time by sweeping build directories, and
it can be split per product the same way, so one product filter shows a
product's end-to-end and unit evidence together:

```toml
[coverage.tiers.unit]
kind = "unit"
precedence = 2

[coverage.tiers.unit.products]
agent = ["build/agent-tests"]
```

`kind = "unit"` is what makes a tier a build-directory sweep at report time
instead of a lab collection; `precedence` orders the tiers when two of them
cover the same line, lower winning. The paths are repo-relative — resolved
against the repo that declares `[coverage]`, never the directory you happen to
run from. Without the `products` sub-table the tier still sweeps whatever
`harvest_dirs` it declares, but as one unnamed view: the lines count, and no
product owns them.

Each view needs its **own** build directory — `.gcno` files sit beside the
objects they describe, so two views built from one tree cannot be told apart.
{doc}`../guide/cli/cov/tiers` has the rule and the rest of the tier model.

## Next

{doc}`../guide/cli/cov/index` is the command reference for the whole workflow:
`otto cov get`, `otto cov clean`, `otto cov report`, tiers, exclusions,
thresholds, and per-ticket attribution.
