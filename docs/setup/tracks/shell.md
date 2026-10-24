# Shell track: command-line tools

This track builds a small project from end to end: a text library and two
command-line tools that use it, all compiled with coverage. You install the
tools on one Linux host, test them there and on the build machine, and end
with one coverage report that combines three tiers — the end-to-end tests,
the unit tests and a session by hand — plus a custom metric charted while the
tools run.

## You need

- A Linux host you can reach over SSH, with the same CPU architecture and C
  library as the machine you build on. The tools are built here and copied
  there.
- CMake 3.24 or newer, g++, git and lcov on the build machine. [Coverage's
  prerequisites](../../cli/cov/index.md#prerequisites) list what the coverage
  commands need there.
- GoogleTest, installed (`libgtest-dev` on Debian and Ubuntu) or downloaded by
  the build. Offline, install it first; {doc}`../../installation` covers
  installing otto itself offline.
- The shared setup done: {doc}`../shared-setup`.

## Get the example

Download {download}`quickstart-shell.tar.gz </_static/generated/examples/quickstart-shell.tar.gz>`
and unpack it:

```bash
tar xzf quickstart-shell.tar.gz && cd quickstart-shell
```

`lab_data/lab.json` holds a placeholder host. Edit its address and
credentials to your host's, the same edit as [one host, written
inline](../shared-setup.md#one-host-written-inline). Then make the project a
git repository and tell your shell where it is. If git asks who you are, set
`git config user.name` and `git config user.email` first:

```bash
git init && git add -A && git commit -m "quickstart-shell"
export OTTOPATH=$PWD
export OTTO_XDIR=~/otto-runs
```

The project is a git repository because coverage records the commit a run
measured. Check that otto reaches your host, as in {ref}`the shared setup's
first success <shared-setup-first-success>`.

## Build

```bash
cmake -S . -B build && cmake --build build
```

```{literalinclude} ../../examples/quickstart-shell/CMakeLists.txt
:language: cmake
:caption: CMakeLists.txt
```

The build has four parts:

- `src/textlib` — the text library, built in its own object directory.
- `src/wordcount` — the first tool. Its source is `main.cpp`, and it links
  the library.
- `src/shout` — the second tool. Its source is also `main.cpp`.
- `tests/unit` — the library's GoogleTest unit tests.

CMake compiles with absolute source and object paths, so the two `main.cpp`
files stay apart. That is all {ref}`the one build requirement <coverage-build-requirement>`
asks.

## Declare the products

Each tool is a product: otto copies it to `dut` and checks that it is there.

```{literalinclude} ../../examples/quickstart-shell/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin products"
:end-before: "# doc: end products"
```

Preview the install. The dry run contacts no host:

```bash
otto --lab example_lab -n run install
```

```{literalinclude} ../../examples/captures/qs-shell-install-dry.txt
:language: text
```

The captures' paths are the docs build's, under `/tmp/otto-gs`; yours are
your own. Each command that walks the lab logs
`fleet of interest: 1 of 2 lab hosts`, though the lab declares only `dut`:
the second host is otto's {ref}`built-in local host <overview-local-host>`,
which lab-wide commands leave out.

Install the tools:

```bash
otto --lab example_lab run install
```

```{literalinclude} ../../examples/captures/qs-shell-install.txt
:language: text
```

{doc}`../../configuration/declared-products-tools` documents every field of a
product entry.

## An end-to-end test

The tests run each tool on `dut` through one helper:

```{literalinclude} ../../examples/quickstart-shell/pylib/quickstart_shell/tools.py
:language: python
:caption: pylib/quickstart_shell/tools.py
```

```{literalinclude} ../../examples/quickstart-shell/tests/test_tools.py
:language: python
:caption: tests/test_tools.py
```

Each test runs its tool once, with `GCOV_PREFIX` and `GCOV_PREFIX_STRIP` set
on that one invocation, so the tool writes its counters under its product's
`cov_dir` as it exits ({ref}`a tool run once per test <coverage-run-per-test-case>`).
`STRIP` counts the project root's own path components, so it holds wherever
the project sits.

A product that runs as a daemon sets them in its `install` command instead:
{ref}`a daemon's counters <coverage-daemon-case>`.

The coverage section below runs these tests, with coverage on.

## A unit test

The unit tests exercise the library on the build machine:

```{literalinclude} ../../examples/quickstart-shell/tests/unit/test_text.cpp
:language: cpp
:caption: tests/unit/test_text.cpp
```

```{literalinclude} ../../examples/quickstart-shell/tests/unit/CMakeLists.txt
:language: cmake
:caption: tests/unit/CMakeLists.txt
```

Run them:

```bash
./build/tests/unit/unit_tests
```

```{literalinclude} ../../examples/captures/qs-shell-unit.txt
:language: text
```

The first line names the GoogleTest the build used, so its path can differ
from yours.

## Coverage

The project declares three tiers:

```{literalinclude} ../../examples/quickstart-shell/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin coverage"
:end-before: "# doc: end coverage"
```

Collect them in this order. {doc}`../../cli/cov/tiers` explains the order,
and {doc}`../../cli/cov/report` the rules the report applies.

First, zero the counters on the host:

```bash
otto --lab example_lab cov clean
```

Exercise `shout` by hand, twice, with the same two variables the tests set.
`GCOV_PREFIX_STRIP` is the number of components in the project root's absolute
path: `3` below, for the docs build's root, `/tmp/otto-gs/quickstart-shell`.
The tests compute theirs (`STRIP`); for a command typed by hand, this prints
yours when run from your project root:

```bash
python3 -c 'import pathlib; print(len(pathlib.Path.cwd().resolve().parts) - 1)'
```

Then run `shout`, with your number in place of `3`:

```bash
otto --lab example_lab host dut exec 'GCOV_PREFIX=/tmp/shout GCOV_PREFIX_STRIP=3 /tmp/quickstart/shout by hand; GCOV_PREFIX=/tmp/shout GCOV_PREFIX_STRIP=3 /tmp/quickstart/shout again'
```

```{literalinclude} ../../examples/captures/qs-shell-manual-run.txt
:language: text
```

Retrieve that session as the manual tier, under a ticket:

```bash
otto --lab example_lab cov get --tier manual --ticket QS-1 --tester-name docs --tester-email docs@example.invalid
```

```{literalinclude} ../../examples/captures/qs-shell-cov-get-manual.txt
:language: text
```

`otto-no-cov-dir` means `wordcount` has no counters on the host: nobody ran it
by hand. The `skipped` line counts the system headers the tool compiled in,
which are not the project's sources.

`--tester-name` and `--tester-email` record who did the testing. Leave them
out to record your own login and git email ({doc}`../../cli/cov/get`).

`otto cov get` wrote the capture under `.otto/coverage/manual/`. Commit it:

```bash
git add .otto/coverage/manual && git commit -m "manual coverage QS-1"
```

The commit you made with the example gave every capture a commit to name.
This one adds the manual store, which is part of the project's history, and it
comes before the e2e run because a report only takes an e2e capture taken at
`HEAD` ({ref}`the report's commit rule <coverage-capture-commit>`).

Run the end-to-end tests with coverage, into a directory of your choosing.
`--no-random` keeps the test order stable, so your log reads like this one:

```bash
otto --lab example_lab test --cov --cov-dir ~/otto-runs/e2e/cov --no-random TestTools
```

```{literalinclude} ../../examples/captures/qs-shell-test-cov.txt
:language: text
```

`--cov-dir` refuses a directory that is not empty, so a second run adds
`--overwrite-cov-dir` or names a new directory ({ref}`coverage-cov-dir`).

Your unit run's counters are already in `build/`, from the unit test
section: the unit run can come any time after the build, because
`otto cov clean` zeroes only the host and the report's unit tier reads the
build tree. Report all three tiers, naming the directory that holds `cov/`,
not `cov/` itself ({doc}`../../cli/cov/report`):

```bash
otto --lab example_lab cov report ~/otto-runs/e2e --dir ~/otto-runs/report
```

```{literalinclude} ../../examples/captures/qs-shell-report.txt
:language: text
```

The `unit unit:` line is the unit tier, named `unit`, keeping only the
products' own sources ({ref}`what the unit report counts <coverage-unit-filter>`).
The log says nothing of the other two tiers, but the report loads both: the
e2e capture from the directory you named, and the manual capture from the
store you committed. Open `~/otto-runs/report/index.html` to see all three,
and which tier covered each line.

## Monitoring

The project's own code adds one metric to otto's monitor. `libs` puts `pylib`
on the import path, `init` imports the package at startup, and `tests` is
where `otto test` finds this page's tests ({doc}`../../configuration/settings`):

```{literalinclude} ../../examples/quickstart-shell/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin libs"
:end-before: "# doc: end libs"
```

```{literalinclude} ../../examples/quickstart-shell/pylib/quickstart_shell/__init__.py
:language: python
:caption: pylib/quickstart_shell/\_\_init\_\_.py
```

A test monitors `dut` while `shout` runs, then checks that the metric was
charted:

```{literalinclude} ../../examples/quickstart-shell/tests/test_monitoring.py
:language: python
:caption: tests/test_monitoring.py
```

Run it. `--no-cov` keeps coverage out of this run, because plain `otto test`
turns it on by itself when it finds instrumented products
({ref}`coverage-tristate`):

```bash
otto --lab example_lab test --no-cov --no-random TestMonitoring
```

```{literalinclude} ../../examples/captures/qs-shell-monitor.txt
:language: text
```

{doc}`../../extend/custom-parsers` covers project-level parsers, and
{doc}`../../cli/monitor/live` covers watching a lab live.

## Clean up

```bash
otto --lab example_lab run cleanup
```

This removes the tools and their counters from the host.

## Next

- {doc}`../coverage` — coverage for your own products.
- {doc}`../writing-tests` — writing tests of your own.
- {doc}`../defining-products-and-tools` — products and tools of your own.
