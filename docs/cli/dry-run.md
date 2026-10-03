# Dry runs

**A dry run never runs a command on any device.** By default it validates and
stops at the CLI seam, and a command may opt in to a deeper,
configuration-only preview. Neither contacts a device. The one thing that does
is `--probe`: it opens, and logs in over, a connection to each host the
command names, and runs no command, so you can see whether the hosts would
answer. Read {ref}`dry-run-probe` before using it.

## The default: validate, print, stop

Under `--dry-run` (`-n`), otto's dispatch layer — not the command — does this,
and exits **0 before the command body runs** (the command body is the work
the command exists to do: an instruction, the tests, a host command):

- arguments parse and coerce, so a typo'd `--mode 789` still fails here
- the lab loads, and every host, link or tunnel the command names resolves
  against it
- the command's module imports
- for `otto run`, and for any other `@cli_command(options=)` leaf, the
  command's own options build and validate exactly as a real run would; for
  `otto run` and `otto test`, every option class registered for the verb
  builds and validates too (see below)
- it prints what would run: the command, its target, and the arguments you gave

```console
$ otto --lab my_lab -n host dut1 exec "systemctl restart nginx"
[DRY RUN] Commands and file transfers will be skipped. No device will be contacted.
dry run: no command body was run and no device was contacted
  would run: otto host dut1 exec 'systemctl restart nginx'
  lab: my_lab (3 hosts); references resolve: host 'dut1'
```

No body executes, so nothing a body might do can happen. A command gets this
behavior without doing anything itself.

Resolution really happens, and its failures are still failures — the block is
printed *after* the references resolve, so a dry run never reports that a
command "would run" against a host that does not exist:

```console
$ otto --lab my_lab -n host nosuchbox exec "uptime"
No host with ID 'nosuchbox'.
Available hosts:
  - router1
  - dut1
  - local
```

`otto -n test` is the same rule, plus one listing: after the block it prints
the tests the run would run, from the run's own pytest collection with
`--collect-only`. Parametrizations are expanded and a `-m` expression is
evaluated. Collecting imports the test files and conftests it reaches, so
their module-level code runs, as it does for `--list-tests`; no test,
fixture or host is touched:

```console
$ otto --lab my_lab -n test TestExample
dry run: no command body was run and no device was contacted
  would run: otto test TestExample
  options:
    RepoOptions: message='hello from acme'
  lab: my_lab (2 hosts)
dry run: pytest collected these tests; nothing ran
acme 0.1.0
└── test_example.py
    └── TestExample
        ├── test_logs_message
        └── test_expect_and_artifacts
```

## `otto run` and `otto test` build and show their options

`run` and `test` are the verbs that take *registered* options
(`@options(verbs=[...])` or `register_options`, see
{doc}`../cookbook/authoring/options-classes`). Under `run` they sit alongside
each command's own options: a standalone `@instruction`, and every one of
otto's six project instructions (`install`, `uninstall`, `cleanup`,
`get-logs`, `install-tools`, `status`) alike. `otto test` has no options class
of its own, only the registered ones, as the `otto -n test` example above
shows. A dry run pays for them exactly as a real run would: the command's own
options class and every class registered for the verb build and validate from
the flags you gave, so a bad value fails here with the identical exit-2 error
a real run would give — and, once everything validates, the block shows the
*resolved* value of every option that applies to the command:

```console
$ otto --lab my_lab -n run install --ensure
dry run: no command body was run and no device was contacted
  would run: otto run install --ensure
  options:
    InstallOptions: ensure=True, recover_partial=True
  lab: my_lab (3 hosts)
```

`install` here is the real project instruction, and it also prints its plan
above this block ({ref}`the lab-level verbs <dry-run-lab-verbs>` shows it; the
plan is left out of this example to show the options). A repo overriding it
with its own options class (inheriting `InstallOptions`, see
{doc}`../getting-started/customizing-project-instructions`) shows the same
way, its own fields included.

The command's own options class is listed first, then any classes registered
for the verb, in registration order. One rule decides what a field prints:
a field masks to `name=<hidden>` — never the raw value — when its type IS or
CONTAINS `pydantic.SecretStr`/`SecretBytes` anywhere (including inside
`Optional[...]`, a union, a container such as `list[...]`, or an `Annotated`
wrapper), or when the field is declared `repr=False`. Every other field
prints its real value. The `would run:` echo above masks that same flag's
value too, so no line in the block ever carries it. A command with no
options at all (its own or the verb's) prints no `options:` line.

A leaf built with `@cli_command(options=...)` on any OTHER top-level verb
gets this same treatment — its own options class builds, validates and shows
under `-n` exactly as above — even though that verb never takes registered
options.

## The stop is uniform, and that will surprise you once

The seam applies to **lab-free** commands too. These print the block and exit 0
**without doing their work**:

```console
$ otto -n schema export
dry run: no command body was run and no device was contacted
  would run: otto schema export
  lab: not loaded (lab-free command)

$ otto -n init
dry run: no command body was run and no device was contacted
  would run: otto init
  lab: not loaded (lab-free command)

$ otto -n reservation whoami
dry run: no command body was run and no device was contacted
  would run: otto reservation whoami
  lab: not loaded (lab-free command)

$ otto --lab my_lab -n monitor --live
dry run: no command body was run and no device was contacted
  would run: otto monitor --live
  lab: not loaded (lab-free command)
```

So `otto schema export -n` writes no schemas and `otto init -n` scaffolds
nothing.

One thing is still written. A command that keeps a run directory, such as
`otto test`, creates it before the stop, as it does on a real run: the dry
run ends with its `Output directory:` line, and the directory stays, holding
that run's log.

`lab_free` means **"this command drives its own lifecycle"**, not "this command
touches no device" — `otto monitor --live` is registered lab-free and collects
metrics from every host in the lab.

If a command of your own should genuinely do work under `-n`, register it
with `dry_run_preview=True` — see
{doc}`../cookbook/dry-run-contract`.

(dry-run-probe)=

## Reachability: `--dry-run --probe`

A plain `--dry-run` contacts **nothing**: it parses the arguments, loads the
lab, resolves every host/link/tunnel the command names, prints what would run,
and stops.  Adding `--probe` buys exactly one extra thing — otto opens a
connection to each host in that resolved set and prints whether it answered:

```console
$ otto --lab my_lab --dry-run --probe host router1 exec "make install"
probe: a connection only -- no command was run
  router1: unreachable
dry run: no command body was run; --probe opened a connection only, and ran no command
  would run: otto host router1 exec 'make install'
  lab: my_lab (3 hosts); references resolve: host 'router1'
```

A host that answers is reported `reachable (connect <N> ms)` instead, and the
dry run exits 0 either way — **reachability is information, not a gate.**

- **A connection, never a command.**  `--probe` opens **and authenticates** the
  connection(s) this invocation would use, and no command follows.
- **`--probe` requires `--dry-run`.**  On its own it is a usage error (exit 2).

With the full dry-run banner, the same probe reads:

```console
$ otto --lab my_lab -n --probe host router1 exec "make install"
[DRY RUN] Commands and file transfers will be skipped. --probe will open a
connection to each named host, and run no command.
probe: a connection only -- no command was run
  router1: unreachable
@router1   | [DRY RUN] Connection FAILED: [Errno 111] Connect call failed ('127.0.0.1', 23) — a real connection; no command was run
dry run: no command body was run; --probe opened a connection only, and ran no command
  would run: otto host router1 exec 'make install'
  lab: my_lab (3 hosts); references resolve: host 'router1'
```

Note the headline: once a socket is opened it no longer ends "and no device
was contacted".

On its own:

```console
$ otto --lab my_lab --probe link list
╭─ Error ──────────────────────────────────────────────────────────────────────╮
│ Invalid value for --probe: --probe requires --dry-run/-n: it opens a         │
│ connection to each host the command names, which is only safe because a      │
│ dry run runs no command afterwards.                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### It opens *and authenticates*

The probe opens the connections the invocation would have used, and opening
includes logging in:

- the terminal channel (ssh or telnet), and
- the FTP control channel as well, when the host's transfer backend is `ftp` —
  so one probe of an FTP-configured host opens **two** sockets, not one.

```{warning}
**For telnet and FTP, authenticating puts the login credentials on the wire.**
```

The probe answers "would this run's connect phase succeed?", so a host that
accepts TCP and then refuses the login is reported `unreachable`.

`--term`, `--transfer` and `--hop` are honoured, so the probe dials the
transport the command would have dialed rather than the host's configured
default.

### Three states, not two

| state | meaning |
| ----- | ------- |
| `reachable` | a connection opened (and authenticated); the row carries `connect <N> ms` |
| `unreachable` | a connection was attempted and did not open — refused, timed out, or refused the login |
| `not probed` | no reachability question could be asked at all |

A Docker container host is `not probed`: it is reached through its parent's
shell and has no transport of its own, so otto never asks.

The built-in `local` host is reachable without a socket — otto is already
running there — and says so:

```console
$ otto --lab my_lab -n --probe host local exec "uptime"
probe: a connection only -- no command was run
  local: reachable -- no transport to open
dry run: no command body was run and no device was contacted
  would run: otto host local exec uptime
  lab: my_lab (3 hosts); references resolve: host 'local'
```

The headline stayed at "no device was contacted", because none was: the block
counts **sockets, not rows**.

### The limit

**No link or tunnel command lends the probe a reference resolver today.** Only
`otto host` does. So `--probe` on a link or tunnel command dials nothing, and
says so:

```console
$ otto --lab my_lab -n --probe link impair core --delay 50ms
[DRY RUN] Commands and file transfers will be skipped. --probe will open a
connection to each named host, and run no command.
probe: this command names no host to dial
dry run core: no device was contacted — nothing was read and nothing was changed
  would: a->b on router1/eth1: tc qdisc replace dev eth1 root netem delay 50ms
  …
```

(dry-run-lab-verbs)=

## The lab-level verbs answer the same way

`otto -n run install`, `uninstall` and `install-tools` print the plan first:
every step the real run would take, host by host, derived from configuration
alone, and then the standard block. A real run is unchanged.

```text
[DRY RUN] Commands and file transfers will be skipped. No device will be contacted.
repo1
  test1
    stage    agent  PUT /work/repo1/build/agent.tar.gz -> /opt/stage
    install  agent  tar -xzf /opt/stage/agent.tar.gz -C /opt/agent && /opt/agent/install.sh
    install  kcov   PUT /work/repo1/build/kcov.ko -> <login home>
                    sudo insmod '<login home>/kcov.ko'
                    rm -f '<login home>/kcov.ko'
  not checked:
    test1: kcov: the login home — product 'kcov' declares no stage_dir and test1 no default_dest_dir
    test1: kcov: whether kcov is resident on test1 (cat /proc/modules): uninstall runs rmmod only then
    test1: kcov: sudo is assumed because the login user is not root; a real run measures how test1 elevates
dry run: no command body was run and no device was contacted
  would run: otto run install
  options:
    InstallOptions: ensure=False, recover_partial=True
  lab: bench (3 hosts)
```

`install` lists every product's `stage` lines before any product's `install`
lines, the order a host really uses; `install-tools` keeps each tool's two
phases together. A host with nothing to do prints only its own line (the
example leaves out `bench`'s other two hosts), and a local path is anchored to
the repo that declared it. The lines' vocabulary, `<login home>` included, is
{ref}`the product-kind contract's <product-kind-plan>`. Everything under `not checked:` is a fact a real run reads off the host (or a
decision it takes from a flag) that a dry run cannot: `--ensure`'s "is the lab
already installed" check, a repo or host class that replaces the default verb,
`--toolchain`, the log hauls, and each product's own unchecked reads, prefixed
with its name. A kernel-module load shows `sudo` only for a host whose
configured login user is not root, the same test the real `load` applies. A gap that holds for the whole lab prints
once, under the first repo it applies to. `status`, the other first-party
instructions and every user-defined instruction keep the seam default.

`otto.project`'s verbs — what `otto run install` and the `ensure` marker's
steps call — compose the host verbs above, and inherit their answers. Two of
them have an answer of their own, and both are reached from a *library* caller:
the `otto run` group keeps the seam default, so `otto -n run cleanup` prints the
block and runs no body at all, while a test marked `ensure("clean")` calls
the converge directly.

- `cleanup()` finishes with two lab-wide steps, and neither pretends to have
  run: `otto.link.manage.repair_all` reads no netdev and
  `otto.tunnel.manage.remove_all_tunnels` scans no host, so each is reported
  `Status.NotRun` ("dry run: no link was read and no impairment was reset").
  Their empty reports look exactly like a real sweep of an already-clean lab,
  so check the status to tell them apart.
- `is_clean()` returns a `bool` and so, like `host.is_clean()`, raises rather
  than answering as soon as something it needs was not measured — the
  toolchain probe, a link whose impairment state was declined
  ({class}`~otto.link.manage.LinkNotMeasuredError`), or a tunnel scan that
  asked nobody ({class}`~otto.tunnel.discovery.TunnelNotMeasuredError`).

## `otto docker` previews the exact command

`otto --dry-run docker compose up <usecase>` is a place a preview is *more* than
a description. Selection, placement, env assembly and a repo's compose adapter
are all pure — they contact no device — so otto runs the whole resolution and
declines at the first real touch, printing the resolved plan **and the exact
per-host `docker compose` command it would have issued**, env prefix included.
For `up` that is the exact `up` command, carrying `--force-recreate` and
`--pull` when given; the build `--build` adds is not part of it — that is
`compose build`'s preview. `down` declines the same way with its resolved plan. See
{doc}`docker/use-cases` for what the plan's parts mean. `build` and
`compose build` preview the exact `docker build` per image, as
{doc}`docker/build` describes under its dry run.

`otto docker use-cases` opts out of the seam's generic stop as well, because it
is already a configuration-only inventory: under `--dry-run` it prints the same
tables, displacement lines and placement problems as without, and exits with
the same code. `otto docker ps` keeps the seam default.
