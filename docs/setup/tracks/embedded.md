# Embedded track: a Zephyr extension

This track builds a small loadable extension for a Zephyr board from end to
end: a clamp-and-divide library, compiled with coverage. You build it, load it
onto the board over its console, test it there and its logic on the build
machine, and end with one coverage report that combines three tiers — the
end-to-end tests, the unit tests and a call made by hand.

## You need

- A Zephyr 3.7 west workspace and the Zephyr SDK 0.16.8, which carries GCC
  12.2: this track's captures were recorded on that pair. On another Zephyr
  or SDK version, the report's `gcov` must come from the same GCC as the
  build ({ref}`Toolchain for embedded coverage <coverage-embedded-toolchain>`).
- A base image you can build and flash, with LLEXT and its shell loader
  enabled, and with the shell buffer and heap sized for the extension
  ({ref}`Operational
  notes <coverage-embedded-operational-notes>`). The captures' board has
  `CONFIG_SHELL_CMD_BUFF_SIZE=81920`, `CONFIG_LLEXT_SHELL_MAX_SIZE=65536` and
  `CONFIG_LLEXT_HEAP_SIZE=128` (that last one in KB): room for a stripped
  extension of about 32 KB, whose `load_hex` line is twice that.
- In that image, the kernel symbols the extension links against. This
  extension's only undefined symbol is `printk`.
- A console otto can reach: a telnet server in front of the board's UART,
  such as `ser2net`, a console appliance or QEMU's `-serial telnet:`. The
  example's lab is written for that. A board that serves its own shell over
  telnet is reached at its `ip` with the `telnet` term instead
  ({doc}`../../configuration/host-options`).
- On the build machine, git, and CMake 3.24 or newer, g++ and lcov for the
  unit tests and the report. [Coverage's
  prerequisites](../../cli/cov/index.md#prerequisites) list what the coverage
  commands need there.
- GoogleTest, installed (`libgtest-dev` on Debian and Ubuntu) or downloaded by
  the build. Offline, install it first; {doc}`../../installation` covers
  installing otto itself offline.
- The shared setup done: {doc}`../shared-setup`.

Building the toolchain, the workspace and the base image is out of scope for
this track; it starts from a board that already runs one.

## Get the example

Download {download}`quickstart-embedded.tar.gz </_static/generated/examples/quickstart-embedded.tar.gz>`
and unpack it:

```bash
tar xzf quickstart-embedded.tar.gz && cd quickstart-embedded
```

Make the project a git repository and tell your shell where it is. If git
asks who you are, set `git config user.name` and `git config user.email`
first:

```bash
git init && git add -A && git commit -m "quickstart-embedded"
export OTTOPATH=$PWD
export OTTO_XDIR=~/otto-runs
```

The project is a git repository because coverage records the commit a run
measured.

## Point the example at your board

`lab_data/lab.json` holds two placeholder hosts: the board, `dut`, and the
console server in front of its UART, `console`:

```{literalinclude} ../../examples/quickstart-embedded/lab_data/lab.json
:language: json
:caption: lab_data/lab.json
```

`filesystem` and `loader` are the {doc}`embedded-only fields
<../../configuration/lab/hops-and-embedded>`. `console_options` belongs to the
{ref}`console term <console-term>`, and `toolchain` to the
{doc}`coverage toolchain <../../configuration/lab/coverage-toolchain>`. The
others are {doc}`host fields <../../configuration/lab/host-fields>`. Replace
these fields with your own:

- on `console`, `ip` and `creds`: the server's address, and a login otto can
  open SSH with. otto reaches the console port from inside the server, so a
  port that only listens on the server's loopback works. For a console
  server with no SSH (a console appliance, or ser2net or QEMU on a machine
  without sshd), drop the `creds` and set the board's
  `console_options.dial` to `"direct"`, and otto connects to the port over TCP
  ({ref}`console-term`);
- on `dut`, `console_options.server` names that server's host, `console`, and
  `console_options.port` is the board's telnet port on it. The example
  declares no login, which suits a shell with none; a console that asks for
  one has the `login` options on the same page;
- on `dut`, `ip`: the board's own address. Over a console, otto does not use
  it to reach the board, but it still addresses the board for anything that
  reaches it over its own network ({doc}`../../configuration/lab/host-fields`);
- on `dut`, `toolchain.sysroot`, `toolchain.gcov` and `toolchain.lcov`: the
  SDK's cross-`gcov` that the report decodes counters with, and the build
  machine's `lcov` ({doc}`../../configuration/lab/coverage-toolchain` lists the
  fields; {ref}`Toolchain for embedded coverage <coverage-embedded-toolchain>`
  shows them on a board).

`write_chunk_size` and `write_chunk_delay` pace a long line for the UART;
they are board tuning, so leave them until a load overruns the console
([Host options](../../configuration/host-options.md)).

The captures on this page were made through a hop, so their log lines show a
connection to a local address. Your lab may need none; a console otto
reaches directly is declared without one, and
{doc}`../../configuration/lab/hops-and-embedded` covers the case that does.
In the captures' lab, `test4` is both the hop and the console server, so it is
the second host the dry run below lists, where yours lists `console`.

Before the long build, check that otto reaches your board's shell. The
shared setup's {ref}`first success <shared-setup-first-success>` does this for
a Unix host; on a Zephyr console, ask the LLEXT shell for its loaded
extensions instead. On a freshly booted board the answer is the list's
`| Name | Size |` header with no rows under it:

```bash
otto --lab example_lab host dut exec 'llext list'
```

## The coverage runtime

The extension carries its own coverage runtime: NASA's embedded-gcov, which
the extension's source includes. Clone it at the commit otto's patch applies
to:

```bash
git clone https://github.com/nasa-jpl/embedded-gcov ~/embedded-gcov
git -C ~/embedded-gcov checkout 7cc670697d0b9fa23f7d22ed5f6776164d7732e2
```

Scaffold otto's patch and the entry-point starter into the project:

```bash
otto init --embedded-gcov --path .
```

```{literalinclude} ../../examples/captures/qs-embedded-init.txt
:language: text
```

The captures' paths are the docs build's, under `/tmp/otto-gs`; yours are
your own. The example already declares `[coverage.embedded]` in
`.otto/settings.toml`, so `init` adds nothing there. To write the patch alone,
or to refresh it from a newer otto, use `otto cov embedded-gcov export DIR`.

Apply it to your clone:

```bash
git -C ~/embedded-gcov apply "$PWD/third_party/embedded-gcov-patch/embedded-gcov-zephyr-gcc12plus.patch"
```

The stock runtime was written for GCC 11 and earlier and misbehaves on
GCC 12 and later, which is what Zephyr's SDK ships; why, and what the patch
changes, is in {ref}`Vendoring embedded-gcov, and the modern-GCC
patch <coverage-embedded-vendoring>`.

The example's `.gitignore` lists `third_party/`, because `init` writes it
there, and the build's `build/`. In a project of your own, the `.gitignore`
that `otto init` writes lists only `otto-output/`, so add the example's other
lines:

```{literalinclude} ../../examples/quickstart-embedded/.gitignore
:language: text
:caption: .gitignore
```

The starter that `init` wrote under `third_party/` is a pristine copy to
restore from; the one you edit is the committed `ext/src/cov_entry.c`, which
holds the three entry points otto calls.

## Build

The extension is built by one script, from the environment:

```{literalinclude} ../../examples/quickstart-embedded/ext/build.sh
:language: bash
:caption: ext/build.sh
```

Set its inputs and run it. Set `BOARD` to the board your base image runs on:
the extension is built for it. It defaults to `mps2_an385`, the board the
captures were made on. If `west` lives in a Python virtual environment, as
Zephyr's getting-started guide sets it up, set `ZEPHYR_VENV` to it and the
script activates it. `OTTO` is optional and defaults to the `otto` on your
`PATH`:

```bash
export ZEPHYR_WORKSPACE=~/zephyrproject
export ZEPHYR_SDK_INSTALL_DIR=~/zephyr-sdk-0.16.8
export EMBEDDED_GCOV_DIR=~/embedded-gcov
export ZEPHYR_VENV=~/zephyrproject/.venv
export BOARD=mps2_an385
ext/build.sh
```

The extension is one translation unit: `ext.c` includes the entry-point
starter and the logic, and adds the wrappers that call it:

```{literalinclude} ../../examples/quickstart-embedded/ext/src/cov_entry.c
:language: c
:caption: ext/src/cov_entry.c
```

```{literalinclude} ../../examples/quickstart-embedded/ext/src/ext.c
:language: c
:caption: ext/src/ext.c
```

```{literalinclude} ../../examples/quickstart-embedded/ext/CMakeLists.txt
:language: cmake
:caption: ext/CMakeLists.txt
```

The build instruments only the extension, and the paths it records are
absolute, which is all {ref}`the one build requirement
<coverage-build-requirement>` asks. Zephyr's loader refuses the instrumented
object as built, and a stale extension would report 0% minutes later, so the
script's last step, `otto cov embedded-gcov finish`, strips the extension and
checks that its gcov stamp matches the `.gcno` files under `--gcno-dir`, the
extension's own object directory (`build/ext/CMakeFiles/mathops_llext_lib.dir`,
named after the extension's CMake target). Why, and what to point
`--gcno-dir` at for an extension of your own, is in
{ref}`Post-build: strip, and guard the .gcno stamp <coverage-embedded-stamp-guard>`.

The build is incremental. West records the project's absolute path in
`build/ext`, so if you move the project directory, delete `build/ext` and
build again. The build writes only under `build/`, which the `.gitignore`
above lists, so the git repository stays clean.

## Declare the extension

The extension is a product, of kind `embedded`:

```{literalinclude} ../../examples/quickstart-embedded/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin declare"
:end-before: "# doc: end declare"
```

`match` puts the product on the host whose id is `dut`
([Matching](../../configuration/declared-products-tools.md#matching)).

Preview the install. The dry run contacts no board:

```bash
otto --lab example_lab -n run install
```

```{literalinclude} ../../examples/captures/qs-embedded-install-dry.txt
:language: text
```

The `fleet of interest` line counts the hosts of the lab the captures were
made on; yours counts your own, and leaves out otto's {ref}`built-in local
host <overview-local-host>`.

Install:

```bash
otto --lab example_lab run install
```

```{literalinclude} ../../examples/captures/qs-embedded-install.txt
:language: text
```

Install loads the extension over the console, then runs `cov_init`, which
registers the extension's counters with the runtime.
{doc}`../../configuration/declared-products-tools` documents every field of
the entry.

## An end-to-end test

The tests call the extension's exported functions through one helper:

```{literalinclude} ../../examples/quickstart-embedded/pylib/quickstart_embedded/__init__.py
:language: python
:caption: pylib/quickstart_embedded/\_\_init\_\_.py
```

```{literalinclude} ../../examples/quickstart-embedded/tests/test_mathops.py
:language: python
:caption: tests/test_mathops.py
```

`ensure("installed")` loads the extension before the first test when it is
not loaded ([the `ensure`
marker](../writing-tests.md#declaring-lab-state-the-ensure-marker)). Each
exported `op_*` function in `ext/src/ext.c` runs one path of the logic and
prints its result for the test to read.

## A unit test

`logic/mathops.c` includes no C library or Zephyr header, so the same file
builds into the extension and into a GoogleTest binary on the build machine.
Keeping the logic in files like it is what lets it be unit tested:

```{literalinclude} ../../examples/quickstart-embedded/logic/mathops.c
:language: c
:caption: logic/mathops.c
```

```{literalinclude} ../../examples/quickstart-embedded/tests/unit/test_mathops.cpp
:language: cpp
:caption: tests/unit/test_mathops.cpp
```

```{literalinclude} ../../examples/quickstart-embedded/tests/unit/CMakeLists.txt
:language: cmake
:caption: tests/unit/CMakeLists.txt
```

Build and run them:

```bash
cmake -S . -B build && cmake --build build
./build/tests/unit/unit_tests
```

```{literalinclude} ../../examples/captures/qs-embedded-unit.txt
:language: text
```

The first line names the GoogleTest the build used, so its path can differ
from yours.

The unit binary compiles `logic/mathops.c` only. The `op_*` wrappers in
`ext/src/ext.c` run on the board alone, so only the end-to-end and manual
tiers can reach them, and the logic is the part all three tiers share.

## Coverage

The project declares three tiers:

```{literalinclude} ../../examples/quickstart-embedded/.otto/settings.toml
:language: toml
:caption: .otto/settings.toml
:start-after: "# doc: begin coverage"
:end-before: "# doc: end coverage"
```

`hosts` selects the lab hosts coverage walks ({ref}`[coverage] hosts
<coverage-configuration>`), here the board.

Collect them in this order. {doc}`../../cli/cov/tiers` explains the order,
and {doc}`../../cli/cov/report` the rules the report applies.

First, zero the extension's counters on the board ({doc}`../../cli/cov/clean`):

```bash
otto --lab example_lab cov clean
```

Call one function by hand, as someone testing manually would:

```bash
otto --lab example_lab host dut exec 'llext call_fn mathops op_clamp_in'
```

```{literalinclude} ../../examples/captures/qs-embedded-manual-run.txt
:language: text
```

Retrieve that session as the manual tier, under a ticket. otto asks the
loaded extension to dump its counters over the console, then decodes them:

```bash
otto --lab example_lab cov get --tier manual --ticket QS-1 --tester-name docs --tester-email docs@example.invalid
```

```{literalinclude} ../../examples/captures/qs-embedded-cov-get-manual.txt
:language: text
```

The `skipped` line counts the three embedded-gcov runtime sources the
extension includes, which are not the project's own.

`--tester-name` and `--tester-email` record who did the testing. Leave them
out to record your own login and git email ({doc}`../../cli/cov/get`).

The `capture.json` the output names is the run's own copy. A manual-tier
capture is also stored in the project, under `.otto/coverage/manual/`, which
the output does not print and `git status` now shows. That copy is the one the
report reads, so commit it:

```bash
git add .otto/coverage/manual && git commit -m "manual coverage QS-1"
```

This commit comes before the e2e run because a report only takes an e2e
capture taken at `HEAD` ({ref}`the report's commit rule <coverage-capture-commit>`).

The e2e run zeroes the counters before its first test, so an extension still
loaded from the install would lose its `cov_init` line there; unload it
first; the test's `ensure("installed")` then loads it after the zeroing:

```bash
otto --lab example_lab run uninstall
```

```{literalinclude} ../../examples/captures/qs-embedded-uninstall.txt
:language: text
```

`uninstall` ({doc}`../../cli/run/defaults`) unloads until the board answers
"No such extension": it is safe to run when nothing is loaded, which is why
the second `llext unload` line is there.

Commit any other change first, your edits to `lab_data/lab.json` and
`.otto/settings.toml` included: a commit after the e2e run moves `HEAD`, and
the report then refuses its capture ({ref}`the report's commit rule
<coverage-capture-commit>`).

Run the end-to-end tests with coverage, into a directory of your choosing.
`--no-random` keeps the test order stable, so your log reads like this one:

```bash
otto --lab example_lab test --cov --cov-dir ~/otto-runs/e2e/cov --no-random TestMathops
```

```{literalinclude} ../../examples/captures/qs-embedded-test-cov.txt
:language: text
```

The third line, `No such extension mathops`, is the pre-run reset
(`llext call_fn mathops cov_reset`) finding nothing loaded. An extension that
is not loaded has nothing to clear, so otto counts that answer as a successful
reset ({ref}`coverage-embedded-reset`).

`--cov-dir` refuses a directory that is not empty, so a second run adds
`--overwrite-cov-dir` or names a new directory ({ref}`coverage-cov-dir`).

Your unit run's counters are already in `build/`, from the unit test
section. Report all three tiers, naming the directory that holds `cov/`,
not `cov/` itself ({doc}`../../cli/cov/report`):

```bash
otto --lab example_lab cov report ~/otto-runs/e2e --dir ~/otto-runs/report
```

```{literalinclude} ../../examples/captures/qs-embedded-report.txt
:language: text
```

The log names only the unit tier, the one the report captures itself from the
counters in `build/`. The e2e and manual captures are already made, and the
report loads them without a line each; the report's tier columns show all
three.

The report reads below 100% because it includes `ext/src/cov_entry.c`, the
committed starter compiled into the extension, along with `logic/mathops.c`
and `ext/src/ext.c`. The unit tier keeps only the product's own sources, `mathops.c`
here ({ref}`what the unit report counts <coverage-unit-filter>`). Open
`~/otto-runs/report/index.html` to see all three tiers, and which one covered
each line of `logic/mathops.c` and `ext/src/ext.c`.

## Monitoring

This track has no monitoring step: the example's board declares no SNMP
agent and has no Unix shell, so `otto monitor` has nothing to read. For a
board that does run an agent, SNMP is how otto reads its metrics: {doc}`../../configuration/lab/snmp` configures it and
{doc}`../../cli/monitor/metrics` covers what the monitor does with the
readings.

## Clean up

The end-to-end run left the extension loaded. `cleanup` unloads it and takes
the rest of what otto put on the lab off with it ([what `cleanup` takes off
the lab](../../cli/run/defaults.md#what-cleanup-takes-off-the-lab)):

```bash
otto --lab example_lab run cleanup
```

`cleanup` acts on the whole lab, not only this project: it also resets every
impairment and removes every otto tunnel in it. On a lab you share, add the
flags {ref}`the teardown recipe <teardown-recipe>` gives.

## Next

- {doc}`../../cli/cov/instrumenting/embedded` — instrumenting an extension of
  your own, and the board and toolchain details behind this track.
- {doc}`kmod` — the kernel-module track.
- {doc}`../writing-tests` — writing tests of your own.
