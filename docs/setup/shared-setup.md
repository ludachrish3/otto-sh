# First lab and first command

Every project starts here. By the end of this page otto knows your project,
your lab has one host in it, and otto has run a command on that host.

## Scaffold

`otto init` fills in a directory that already exists; it never creates one.
Make the project a git repository too: coverage records which commit a run
measured.

```bash
mkdir acme && cd acme && git init
otto init --all --name acme --path .
```

`otto init` scaffolds a runnable project — settings, an
example lab host, example tests, an example instruction — and prints the
next steps:

```{literalinclude} ../examples/captures/init-all.txt
:language: text
```

The capture's paths are the docs build's; yours are your own directory. Its
**Try it** commands work once `example_lab` has a real host, which the next
sections give it.

{doc}`../cli/init` explains every scaffolded file and flag;
{doc}`../configuration/settings` explains every key `settings.toml` accepts.

## Tell your shell where the project is

```bash
export OTTOPATH=$PWD
export OTTO_XDIR=~/otto-runs
```

otto finds projects through `OTTOPATH`, never through the current
directory. `OTTO_XDIR` is where otto writes each run's logs and artifacts.
Without it, runs land under the current directory — inside your project, if
that is where you are. The {ref}`invocation-output-directory` section explains the run
layout under `OTTO_XDIR`, and {ref}`enabling-tab-completion` makes both
exports permanent.

## One host, written inline

Replace `lab_data/lab.json` with one host in the scaffolded `example_lab`.
Use the address and credentials of a machine you can reach over SSH:

```json
{
  "labs": { "example_lab": {} },
  "elements": [
    {
      "name": "dut",
      "labs": ["example_lab"],
      "hosts": [
        { "ip": "192.0.2.10", "os_type": "unix",
          "creds": [{ "login": "admin", "password": "changeme" }] }
      ]
    }
  ]
}
```

The host's id is its element's `name`, `dut`. The scaffold also wrote an
`[inventory]` and a `[creds]` table in `.otto/settings.toml`, plus
`lab_data/inventory.json` and `lab_data/creds.json`; they serve hosts that
*reference* an inventory instead of carrying their own address. When every
host is inline, delete both tables and both files.
{doc}`../configuration/lab-config` documents every host field and the
referenced form.

(shared-setup-first-success)=
## First success

```bash
otto --list-labs
otto --lab example_lab --list-hosts
otto --lab example_lab host dut exec 'uname -r'
```

`--list-labs` shows `example_lab`. `--list-hosts` needs `--lab`; it shows `dut`
and `local`, the built-in host for the machine otto runs on. `exec` prints
the host's kernel version:

```text
INFO     @dut   | uname -r
INFO     @dut > | 6.8.0-86-generic
```

## Before you declare products

Once `settings.toml` declares any `[[products]]` or `[[dev_tools]]`, it also
needs a `[project]` table saying which labs the project speaks for:

```toml
[project]
lab_patterns = ["example_lab"]
```

`lab_patterns` is required; `host_patterns` narrows the hosts and defaults to
every host in those labs. {ref}`project-scope` explains both, and
{ref}`project-scope-required` explains when the table is required.

## Next

- {doc}`tracks/shell` — a complete project: two command-line tools, tests, three-tier coverage and a custom metric.
- {doc}`defining-products-and-tools` — put your products on the host.
- {doc}`running-tests` — write and run a test.
- {doc}`coverage` — measure what the tests exercised.
