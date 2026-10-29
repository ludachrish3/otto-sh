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

## Is it installed?

```bash
otto --lab my_lab run status             # exit 0 installed, 1 uninstalled, 2 partial
otto --lab my_lab -n run install         # preview: what install would copy and run
otto --lab my_lab run install --ensure   # converge: do only the missing work
```

`run status` checks every product on every host and exits on the lab as a whole.
`-n` previews a run without contacting a device. `--ensure` turns `install`
into a converge: an installed lab does no work, and a partial one is brought
back to installed. A coverage run of a kernel-module project can unload its
modules at teardown and leave the lab partial, or uninstalled when they are
its only products; `run install --ensure` restores it.
{doc}`../cli/run/defaults` covers all of this.

## Run its tests

```bash
otto test --list-tests                   # every test the project collects
otto --lab my_lab test TestDevice       # a class or a test, by name — not a file
otto --lab my_lab test -m smoke          # every test with a marker
```

Names are the class and test names `--list-tests` prints, not file paths.
`otto test` needs a name or a marker expression. To run every test, see
{doc}`../cli/test/index`.

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
