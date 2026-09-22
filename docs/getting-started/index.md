# Getting Started

This page installs otto and maps into a multi-page worked example that
defines otto's own test bed, host by host, from scratch.

## Installation

Otto requires **Python 3.10** or later. Install the latest release from PyPI into a
virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install otto-sh
```

The distribution is named `otto-sh`; the command it installs is `otto`.

Setting up a team, working on an air-gapped network, or managing otto alongside your
project's other Python dependencies? {doc}`../installation` covers the recommended
`pyproject.toml`/uv setup, downloading wheels, internal package indexes, and reading
these docs offline.

### Verifying the installation

```bash
otto --version
```

### Enabling tab completion

Otto ships with a Typer-generated shell completion script.  Install it once
with `--install-completion` and then source the generated script in your
shell:

```bash
otto --install-completion
source ~/.bash_completions/otto.sh
```

To make tab completion available in every new shell, add those two lines to
your `~/.bashrc` (or `~/.profile`) so they run automatically at login. Type
`otto ru<Tab>` to check.

## Project setup

Otto discovers your project through a `.otto/settings.toml` file. `otto init
--all --name acme --path /tmp/otto-gs/acme` scaffolds a runnable one (settings,
an example lab host, an example suite, an example instruction) and prints the
next steps, completion included. The directory has to exist first
(`mkdir -p /tmp/otto-gs/acme`) — `otto init` never creates one. Substitute
your own name and a path of your own.

```{literalinclude} ../examples/getting-started/captures/init-all.txt
:language: text
```

Steps 4 and 6–8 name the lab explicitly (`--lab example_lab`), as every
example on this page does; {doc}`../cli/index` covers `--lab`, the
`OTTO_LAB` environment variable that replaces it, and the rest of the global
options.

{doc}`../cli/init` is the flag reference; {doc}`../configuration/settings`
explains every key `settings.toml` accepts and the one-time
{ref}`team-setup-checklist`. Point otto at the project with the
`export OTTO_SUT_DIRS=…` line it printed — nothing is discovered from the
working directory.

The scaffolded `example-device` is a placeholder: its `lab_data/lab.json`
entry names the inventory key `device-01.lab.example`, so replace that key's
placeholder `ip` in `lab_data/inventory.json` and placeholder `creds` in
`lab_data/creds.json` (the scaffolded `lab_data/README.md` explains every
field; {doc}`../configuration/inventory` is the home for how the three
files compose) before anything connects to it. Every lab also carries a built-in
`local` host — the machine otto runs on — which needs no lab edit at all:
`otto --lab example_lab host local exec "uname -a"`.

Every command that contacts a host writes a run directory under `--xdir` —
your current directory unless you say otherwise — and prints its path as
`Output directory:`; see {doc}`../cli/index` for the layout.

## Worked Example

The pages below define otto's own test bed — four Ubuntu VMs, five BusyBox
guests, seven Zephyr targets — as the checked-in project
`docs/examples/getting-started/`. The last two pages return to the project you
scaffolded in [Project setup](#project-setup) to write an instruction and a
test suite of your own.

```{toctree}
:maxdepth: 1

defining-hosts/index
defining-products-and-tools
coverage
customizations
customizing-project-instructions
boards-of-interest
reservations
running-instructions
running-test-suites
```

## Where to go next

- {ref}`team-setup-checklist` -- One-time setup when adopting otto for a team
- {doc}`../cli/host/index` -- `otto host`: what every host answers, and what each host family supports
- {doc}`../cli/index` -- Every `otto` command, one page per verb
- {doc}`../configuration/index` -- The project and lab files every command reads
- {doc}`../cookbook/index` -- Using otto from Python: authoring, extending, and short recipes
- {doc}`../cli/docker/use-cases` -- Docker compose services as lab hosts
- {doc}`../cookbook/python-library` -- Using otto as a Python library
- {doc}`../api/index` -- Full API reference
