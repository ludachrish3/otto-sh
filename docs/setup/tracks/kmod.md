# Kernel-module track: a loadable module

This track builds a small kernel module from end to end: a word counter you
write lines to and read counts back from, compiled with coverage. You build
otto's coverage library and the module for one Linux host, load them there,
test the module there and its logic on the build machine, and end with one
coverage report that combines three tiers — the end-to-end tests, the unit
tests and a session by hand — plus a custom metric charted while the module
runs.

## You need

- A Linux host you can reach over SSH, where your login can use `sudo`:
  otto loads and unloads modules with `sudo insmod` and `sudo rmmod` unless
  the login is root, and answers `sudo`'s prompt with the login's password
  from `lab.json` ({doc}`../../cli/host/capabilities/privilege`).
- On the build machine, the kernel headers for the release the host runs
  (`uname -r` on the host): `linux-headers-<release>` on Debian and Ubuntu.
  A module loads only into the release it was built for. This track builds
  with the build machine's own toolchain, so the build machine and the host
  must also share an architecture; a cross build is on [Another kernel, ISA or
  compiler](../../cli/cov/instrumenting/kernel-modules.md#another-kernel-isa-or-compiler).
- `make`, gcc and `modinfo` (the `kmod` package on Debian and Ubuntu) for the
  module, and CMake 3.24 or newer, g++, git and lcov for the unit tests and
  the report. [Coverage's
  prerequisites](../../cli/cov/index.md#prerequisites) list what the coverage
  commands need there.
- GoogleTest, installed (`libgtest-dev` on Debian and Ubuntu) or downloaded by
  the build. Offline, install it first; {doc}`../../installation` covers
  installing otto itself offline.
- The shared setup done: {doc}`../shared-setup`.

## Get the example

Download {download}`quickstart-kmod.tar.gz </_static/generated/examples/quickstart-kmod.tar.gz>`
and unpack it:

```bash
tar xzf quickstart-kmod.tar.gz && cd quickstart-kmod
```

`lab_data/lab.json` holds a placeholder host. Edit its address and
credentials to your host's, the same edit as [one host, written
inline](../shared-setup.md#one-host-written-inline).

The module links otto's coverage library, `otto_kmodcov`, which the example
does not carry. Scaffold it into the project with
`otto init --kmodcov --path .`, then copy the starter's two sentinel files
beside the module; the module's `Kbuild` links them first and last, so the
library can find the module's coverage constructors between them
({doc}`../../cli/cov/instrumenting/kernel-modules`):

```bash
otto init --kmodcov --path .
cp third_party/otto_kmodcov-consumer/kmodcov_begin.c third_party/otto_kmodcov-consumer/kmodcov_end.c module/
```

```{literalinclude} ../../examples/captures/qs-kmod-init.txt
:language: text
```

The captures' paths are the docs build's, under `/tmp/otto-gs`; yours are
your own. The example already declares the library in `.otto/settings.toml`,
so `init` adds nothing there. When to use `otto cov kmodcov export` instead,
and how otto checks that the library matches it, is on
[Getting the library](../../cli/cov/instrumenting/kernel-modules.md#getting-the-library).

Make the project a git repository and tell your shell where it is. If git
asks who you are, set `git config user.name` and `git config user.email`
first:

```bash
git init && git add -A && git commit -m "quickstart-kmod"
export OTTOPATH=$PWD
export OTTO_XDIR=~/otto-runs
```

The project is a git repository because coverage records the commit a run
measured. Check that otto reaches your host, as in {ref}`the shared setup's
first success <shared-setup-first-success>`.

## Build

Build the library, then the module against it. These two commands build for
the kernel the build machine runs; when the host runs another release, use the
override below instead:

```bash
third_party/otto_kmodcov/build.sh build/kmodcov
make -C module
```

If your host runs another release, install that release's headers and name
it: `third_party/otto_kmodcov/build.sh build/kmodcov <release>`, then
`make -C module KDIR=/lib/modules/<release>/build`.

The module is one file of kernel code and one of plain C:

```{literalinclude} ../../examples/quickstart-kmod/module/qs_main.c
:language: c
:caption: module/qs_main.c
```

A line written to the `line` parameter is counted by `qs_count_words`, and
the count is read back from `words`. {doc}`../../cli/cov/instrumenting/kernel-modules`
explains the instrumentation: the `Kbuild` and `Makefile` beside these files,
the two sentinel objects, and the three `KMODCOV_` macros. kbuild builds the
module in place with absolute paths, which is all {ref}`the one build
requirement <coverage-build-requirement>` asks.

The build writes only files the example's `.gitignore` lists, so the git
repository stays clean. A project you scaffold with `otto init` ignores only
`otto-output/`, so add the build's lines to its `.gitignore` yourself:

```{literalinclude} ../../examples/quickstart-kmod/.gitignore
:language: text
:caption: .gitignore
```

## Declare the library and the module

The library is a dev tool and the module is a product:

```{literalinclude} ../../examples/quickstart-kmod/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin declare"
:end-before: "# doc: end declare"
```

`match` puts both on the host whose id is `dut`
([Matching](../../configuration/declared-products-tools.md#matching)).

Preview the install. The dry run contacts no host:

```bash
otto --lab example_lab -n run install
```

```{literalinclude} ../../examples/captures/qs-kmod-install-dry.txt
:language: text
```

The lab-wide commands log `fleet of interest: 1 of 2 lab hosts`, though the
lab declares only `dut`: the second host is otto's {ref}`built-in local host
<overview-local-host>`, which lab-wide commands leave out.

Install:

```bash
otto --lab example_lab run install
```

```{literalinclude} ../../examples/captures/qs-kmod-install.txt
:language: text
```

{doc}`../../configuration/declared-products-tools` documents every field of
both entries, and [Declaring the module and its library](../../cli/cov/instrumenting/kernel-modules.md#declaring-the-module-and-its-library)
how the two bind.

## An end-to-end test

The tests write to the module on `dut` through one helper:

```{literalinclude} ../../examples/quickstart-kmod/pylib/quickstart_kmod/wordcount.py
:language: python
:caption: pylib/quickstart_kmod/wordcount.py
```

```{literalinclude} ../../examples/quickstart-kmod/tests/test_wordcount.py
:language: python
:caption: tests/test_wordcount.py
```

[`ensure("installed")`](../writing-tests.md#declaring-lab-state-the-ensure-marker)
loads the module before the first test when it is not loaded. The class's
teardown unloads it, so the counters of its exit routine reach the run; that
leaves the module uninstalled, and the coverage section below puts it back
({ref}`kmod-teardown-ensure`).

Both act on more than this module. `get_host("dut").uninstall()` takes every
product on `dut` off, and `ensure("installed")` on a partially installed lab
uninstalls every product in the lab before it installs ({ref}`--recover-partial <recover-partial>`).
Here the module is the only product, so the two are the same. In a project
with other products, see {ref}`several-products`.

## A unit test

`wordcount.c` includes no kernel header and no C library header, so the
same file builds into the module and into a GoogleTest binary on the build
machine. Keeping a module's logic in files like it is what lets it be unit
tested:

```{literalinclude} ../../examples/quickstart-kmod/module/wordcount.c
:language: c
:caption: module/wordcount.c
```

```{literalinclude} ../../examples/quickstart-kmod/tests/unit/test_wordcount.cpp
:language: cpp
:caption: tests/unit/test_wordcount.cpp
```

```{literalinclude} ../../examples/quickstart-kmod/tests/unit/CMakeLists.txt
:language: cmake
:caption: tests/unit/CMakeLists.txt
```

Build and run them:

```bash
cmake -S . -B build && cmake --build build
./build/tests/unit/unit_tests
```

```{literalinclude} ../../examples/captures/qs-kmod-unit.txt
:language: text
```

The first line names the GoogleTest the build used, so its path can differ
from yours.

## Coverage

The project declares three tiers:

```{literalinclude} ../../examples/quickstart-kmod/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin coverage"
:end-before: "# doc: end coverage"
```

`hosts` selects the lab hosts coverage walks ({ref}`[coverage] hosts
<coverage-configuration>`), here `dut`.

Collect them in this order. {doc}`../../cli/cov/tiers` explains the order,
and {doc}`../../cli/cov/report` the rules the report applies.

First, zero the module's counters on the host ({doc}`../../cli/cov/clean`):

```bash
otto --lab example_lab cov clean
```

Write two lines to the module by hand. The write needs root, so `--sudo`
runs it, inside `sh -c` so the redirection is root's too:

```bash
otto --lab example_lab host dut exec --sudo 'sh -c "echo by hand > /sys/module/qs_wordcount/parameters/line; echo once more > /sys/module/qs_wordcount/parameters/line"'
```

```{literalinclude} ../../examples/captures/qs-kmod-manual-run.txt
:language: text
```

Retrieve that session as the manual tier, under a ticket. otto asks the
loaded module to dump its counters, then fetches them:

```bash
otto --lab example_lab cov get --tier manual --ticket QS-1 --tester-name docs --tester-email docs@example.invalid
```

```{literalinclude} ../../examples/captures/qs-kmod-cov-get-manual.txt
:language: text
```

The `skipped` line counts a kernel header the module inlined, which is not the
project's source; only `qs_main.c` and `wordcount.c` are measured ([what the
report shows](../../cli/cov/instrumenting/kernel-modules.md#what-the-report-shows)).

`--tester-name` and `--tester-email` record who did the testing. Leave them
out to record your own login and git email ({doc}`../../cli/cov/get`).

The module was loaded before `otto cov clean` zeroed it, so this tier shows
its init routine as not run ({ref}`kmod-init-coverage`). `otto cov get`
wrote the capture under `.otto/coverage/manual/`. Commit it:

```bash
git add .otto/coverage/manual && git commit -m "manual coverage QS-1"
```

This commit comes before the e2e run because a report only takes an e2e
capture taken at `HEAD` ({ref}`the report's commit rule <coverage-capture-commit>`).
Commit any other change you want, settings included, before the e2e run too: a
commit after it makes `cov report` refuse that run's capture.

The e2e run zeroes every counter before its first test, the same way, so a
module still loaded from the install would lose its init routine there too.
Unload it first with [`otto run uninstall`](../../cli/run/defaults.md), which
takes off every product in the lab, here only the module. The test's
`ensure("installed")` then loads it after the zeroing. So the module comes
off twice, for its two routines: before the run, here, so its init routine
counts, and after the last test, in the test class's teardown, so its exit
routine counts ({ref}`kmod-init-coverage`):

```bash
otto --lab example_lab run uninstall
```

```{literalinclude} ../../examples/captures/qs-kmod-uninstall.txt
:language: text
```

Run the end-to-end tests with coverage, into a directory of your choosing.
`--no-random` keeps the test order stable, so your log reads like this one:

```bash
otto --lab example_lab test --cov --cov-dir ~/otto-runs/e2e/cov --no-random TestWordcount
```

```{literalinclude} ../../examples/captures/qs-kmod-test-cov.txt
:language: text
```

`--cov-dir` refuses a directory that is not empty, so a second run adds
`--overwrite-cov-dir` or names a new directory ({ref}`coverage-cov-dir`).

The teardown unloaded the module, so the lab is no longer installed
([Reading `status`](../../cli/run/defaults.md#reading-status)):

```bash
otto --lab example_lab run status
```

```{literalinclude} ../../examples/captures/qs-kmod-status.txt
:language: text
```

Put it back, so the lab reads installed for whatever you do next; the report
below reads the module's local build and does not need it loaded. The lab is
uninstalled, so `--ensure` installs it. On a partially installed lab it would
first uninstall every product in the lab
({ref}`--recover-partial <recover-partial>`):

```bash
otto --lab example_lab run install --ensure
```

```{literalinclude} ../../examples/captures/qs-kmod-install-ensure.txt
:language: text
```

Your unit run's counters are already in `build/`, from the unit test
section. Report all three tiers, naming the directory that holds `cov/`,
not `cov/` itself ({doc}`../../cli/cov/report`):

```bash
otto --lab example_lab cov report ~/otto-runs/e2e --dir ~/otto-runs/report
```

```{literalinclude} ../../examples/captures/qs-kmod-report.txt
:language: text
```

The unit tier keeps only the module's own sources, `wordcount.c` here
({ref}`what the unit report counts <coverage-unit-filter>`). Open
`~/otto-runs/report/index.html` to see all three tiers, and which one covered
each line of `qs_main.c` and `wordcount.c` — the init and exit routines
included, from the e2e run.

## Monitoring

The project's own code adds one metric to otto's monitor, the module's size
as `/proc/modules` reports it. `libs` puts `pylib` on the import path, `init`
imports the package at startup, and `tests` is where `otto test` finds this
page's tests ({doc}`../../configuration/settings`):

```{literalinclude} ../../examples/quickstart-kmod/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin libs"
:end-before: "# doc: end libs"
```

```{literalinclude} ../../examples/quickstart-kmod/pylib/quickstart_kmod/__init__.py
:language: python
:caption: pylib/quickstart_kmod/\_\_init\_\_.py
```

```{literalinclude} ../../examples/quickstart-kmod/tests/test_monitoring.py
:language: python
:caption: tests/test_monitoring.py
```

Run it. `--no-cov` keeps coverage out of this run, because plain `otto test`
turns it on by itself when it finds instrumented products
({ref}`coverage-tristate`):

```bash
otto --lab example_lab test --no-cov --no-random TestMonitoring
```

```{literalinclude} ../../examples/captures/qs-kmod-monitor.txt
:language: text
```

{doc}`../../extend/custom-parsers` covers project-level parsers, and
{doc}`../../cli/monitor/live` covers watching a lab live.

## Clean up

```bash
otto --lab example_lab run cleanup
```

This project has no docker stack, so this one command is the whole
{ref}`teardown-recipe`: it unloads the module, then `otto_kmodcov`. What comes
off, and in which order, is in [Declaring the module and its
library](../../cli/cov/instrumenting/kernel-modules.md#declaring-the-module-and-its-library).
`cleanup` acts on the whole lab, not only this project: it also resets every
impairment and removes every otto tunnel in it. On a lab you share, add the
flags the recipe gives.

## Next

- {doc}`../../cli/cov/instrumenting/kernel-modules` — instrumenting a module
  of your own, and building for another kernel or compiler.
- {doc}`shell` — the shell track, for command-line tools.
- {doc}`../writing-tests` — writing tests of your own.
