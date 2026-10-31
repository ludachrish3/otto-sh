# Joining an existing project

Someone handed you an otto project and a lab. These five commands tell you
what you have and whether it is ready:

```bash
export OTTOPATH=~/src/acme            # the project's root directory
export OTTO_XDIR=~/otto-runs          # where run output goes
otto --list-labs
otto --lab my_lab --list-hosts
otto --lab my_lab run status
```

`OTTOPATH` and `OTTO_XDIR` are described in [Environment variables](../cli/index.md#environment-variables). The two list commands are in
[Inspecting what a lab holds](../cli/index.md#inspecting-what-a-lab-holds),
and `run status` is in [Reading status](../cli/run/defaults.md#reading-status).
The sections below take each step in turn.

## Get otto and the project's environment

Install otto first: {doc}`../installation`.

A project's instructions and tests can import Python packages of their own.
`otto env` builds one environment that satisfies every active project — see
{doc}`../cli/env/index`. If the project's README names an `otto env` step, run
it now.

## Point otto at the project

otto finds projects through `OTTOPATH`, not through your current directory, so
export it before anything else. Set `OTTO_XDIR` as well: without it, each run
writes its output under `otto-output/` in the directory you ran it from.

```bash
export OTTOPATH=~/src/acme
export OTTO_XDIR=~/otto-runs
```

Global options go **before** the subcommand: `otto --lab my_lab test …`, not
`otto test --lab my_lab …`. The [global options table](../cli/index.md#global-options) lists them.

## What's in it

```bash
otto --list-labs                         # the labs the project can use
otto --lab my_lab --list-hosts           # the hosts in one lab
otto --lab my_lab --list-products        # what the project installs, and where
```

`--list-hosts` needs `--lab`. Every lab also holds `local`, the machine otto
runs on. The product table shows each product's kind, its hosts, and the
directory it is staged to; neither list command contacts a host. See
[Inspecting what a lab holds](../cli/index.md#inspecting-what-a-lab-holds)
and {ref}`list-products-flag`.

Output that is piped or captured renders at 80 columns, so wide tables
truncate. Set `COLUMNS=200` when you capture or script otto
([Environment variables](../cli/index.md#environment-variables)).

## Is it installed?

```bash
otto --lab my_lab run status             # exit 0 installed, 1 uninstalled, 2 partial
otto --lab my_lab -n run install         # preview: what install would copy and run
otto --lab my_lab run install --ensure   # converge: installed is a no-op, else install
```

`run status` checks every product on every host and exits on the lab as a whole.
`-n` previews a run without contacting a device. `--ensure` turns `install`
into a converge: an installed lab does no work, and an uninstalled one is
installed. A partial lab is not topped up: by default otto uninstalls every
product in the lab, then installs them all fresh ({ref}`--recover-partial <recover-partial>`).
A coverage run of a kernel-module project can unload its
modules at teardown and leave the lab partial, or uninstalled when they are
its only products; `run install --ensure` restores it.
{doc}`../cli/run/defaults` covers all of this.

`run install` stages what the project has already built; it never builds it.
If a product's artifact is missing, build it with the project's own build
steps (its README). A `docker_image` product's image, when the project declares it
under `[[docker.images]]`, is built on the product's host with
`otto --lab my_lab docker build --parent <host> <image>` ({doc}`../cli/docker/build`).

## Run its tests

```bash
otto test --list-tests                   # every pytest test the project collects
otto --lab my_lab test TestDevice       # a class or a test, by name — not a file
otto --lab my_lab test -m smoke          # every test with a marker
otto --lab my_lab test -m "otto_all or not otto_all"   # every test
```

Names are the class and test names `--list-tests` prints, not file paths.
`otto test` needs a name or a marker expression; the always-true expression
in the last line runs every test ({ref}`run-every-test`).

Unit tests (GoogleTest and the like) are the project's own build binaries:
otto neither lists nor runs them, and only harvests their coverage counters
({ref}`coverage-tier-kinds`).

A project's coverage tests may stop a `docker_image` product's container to
flush its counters, which leaves the lab partial or uninstalled afterwards.
`run status` shows it. If its image is named by reference, start the
container again before `otto cov report`
({ref}`why <coverage-container-stop-to-flush>`). A container left stopped
blocks the next install, because its name is taken: remove it first with
`otto --lab my_lab run uninstall`. On a partial lab,
`otto --lab my_lab run install --ensure` does that itself
({ref}`what --ensure does <recover-partial>`).

## Where results go

Every invocation ends with an `Output directory:` line. That directory holds
the run's log, its per-test results and any coverage it captured; its layout is
in {ref}`invocation-output-directory`. `otto cov report` writes its HTML report
under its own invocation's output directory unless you give `--dir` — see
{doc}`../cli/cov/report`. To watch the lab's hosts while tests run, see
{doc}`../cli/monitor/index`.

## When a reservation refuses you

If the team wired up reservations, otto refuses to start a command that touches
hardware you do not hold. `otto reservation whoami` shows who otto thinks you
are, and `otto --lab my_lab reservation check` shows what you hold. Both, and
the break-glass override, are in {doc}`../cli/reservation/index`.
