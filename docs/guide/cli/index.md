# The otto CLI

`otto` is one command with thirteen subcommand groups. This page covers what is
true of all of them — how an invocation is shaped, the options the top-level
command owns, the environment variables that back them, and where each run
writes its output. Each group then has its own page, and every subcommand of
that group has a page under it.

```{raw} html
:file: ../../_static/generated/termynal/help-otto.html
```

## Invocation

```text
otto [GLOBAL OPTIONS] COMMAND [SUBCOMMAND] [ARGS...]
```

## Global options

These options are available on every `otto` command:

| Option | Env var | Default | Description |
| ------ | ------- | ------- | ----------- |
| `--lab, -l` | `OTTO_LAB` | *(required)* | Lab name(s); combine several with `+` (e.g. `tech1+overlay`) |
| `--xdir, -x` | `OTTO_XDIR` | current dir | Output directory for logs and artifacts |
| `--include-projects, -I` | | | Force these projects active for this invocation, overriding lab inference — see {doc}`projects` |
| `--exclude-projects, -E` | | | Switch these projects off for this invocation, overriding lab inference — see {doc}`projects` |
| `--field / --debug` | `OTTO_FIELD_PRODUCTS` | `--debug` | Use field or debug products |
| `--log-days` | `OTTO_LOG_DAYS` | `30` | Number of days to retain logs |
| `--log-level` | `OTTO_LOG_LEVEL` | `INFO` | Logging level |
| `--rich-log-file / --no-rich-log-file` | `OTTO_LOG_RICH` | `--no-rich-log-file` | Rich formatting in log files |
| `--show-time` | | `False` | Show per-line timestamps on the live console (log files are always timestamped) |
| `--dry-run, -n` | | `False` | Validate, print what would run, and exit 0 **before the command body runs**. Never runs a command on any device — see {doc}`dry-run` |
| `--probe` | | `False` | With `--dry-run`: open a connection to each host the command names and report reachability. A connection only — never a command |
| `--as-user USERNAME` | | current user | Check reservations as USERNAME instead of the current user |
| `--skip-reservation-check, -R` | | `False` | Bypass the reservation check entirely (emergency use only) |
| `--list-labs` | | | List available lab names and exit |
| `--list-hosts` | | | List host IDs in the loaded lab and exit |
| `--show-lab` | | | Print full lab details and exit |
| `--lab-depth` | | `3` | Nesting depth for `--show-lab` output — how deep the lab's host details are expanded (0 = unlimited) |
| `--clear-autocomplete-cache` | | | Delete the shell-completion cache files and exit |
| `--version` | | | Show version and exit |
| `--install-completion` | | | Install shell completion and exit |
| `--show-completion` | | | Print shell completion script and exit |
| `-h, --help` | | | Show help and exit |

```{important}
**Option placement matters.**  Global options (including `--lab`/`-l`) must
appear **before** the subcommand — they are parsed by the top-level `otto`
command, not the subcommand.  For example:

- ✅ `otto --lab my_lab run deploy --debug`
- ❌ `otto run deploy --debug --lab my_lab`

The same rule applies to `--dry-run`, `--xdir`, `--log-level`, and every
other option listed above.  Subcommand-specific options (like `--firmware`
for a suite, or `--interval` for `monitor`) go **after** the subcommand.
```

## Selecting a lab

Almost every command needs a lab. Name it with `--lab`/`-l`, or set `OTTO_LAB`
once and leave it out of the command line.

### Combining labs with `+`

Combine lab names with `+` to merge them:

```bash
otto --lab lab_a+lab_b test TestDevice
```

Hosts from all labs are merged into a single lab named `lab_a+lab_b`.  If two
labs define the same host ID, the later lab's definition wins.  `+` is the same
operator the `Lab` objects themselves use to merge.

### Inspecting what a lab holds

```bash
otto --lab my_lab --list-labs      # list all available lab names
otto --lab my_lab --list-hosts     # list host IDs in the loaded lab
otto --lab my_lab --show-lab       # full lab details (use -v for expanded output)
```

## Environment variables

Most environment variables below back a global option; where one does, the
flag always wins when both are present.  `OTTO_SUT_DIRS`, `OTTO_HOME` and
`OTTO_TEARDOWN_DEADLINE` have no flag.

| Variable | Backs | Notes |
| --- | --- | --- |
| `OTTO_LAB` | `--lab` / `-l` | Combine several with `+` |
| `OTTO_XDIR` | `--xdir` / `-x` | Defaults to the current directory |
| `OTTO_FIELD_PRODUCTS` | `--field` / `--debug` | |
| `OTTO_LOG_DAYS` | `--log-days` | |
| `OTTO_LOG_LEVEL` | `--log-level` | |
| `OTTO_LOG_RICH` | `--rich-log-file` | |
| `OTTO_SUT_DIRS` | *(no flag)* | Paths to the repo roots under test, separated by `,` or the OS path separator (`:` on Linux/macOS, `;` on Windows). Required when a development repo is under test |
| `OTTO_HOME` | *(no flag)* | otto's user-level home; defaults to `~/.otto`.  Holds one workspace home per `OTTO_SUT_DIRS` set — see [The workspace home](#the-workspace-home) |
| `OTTO_TEARDOWN_DEADLINE` | *(no flag)* | Seconds an interrupted command's graceful cleanup may run before it is abandoned; defaults to `10` — see {doc}`../../architecture/lifecycle` |

## Shell completion

After `otto --install-completion`, tab completion covers the dynamic,
otto-specific values a static shell script couldn't know: suite and
instruction names, host ids and their per-class verbs, transfer/term
backends, reservation usernames, and — multi-value lists included — `--lab`
names (`+`-combined) and `--tests` names (comma-separated).  It is served
from a cache in [the workspace home](#the-workspace-home) so the process
answering the keystroke never runs your init modules or test code.
`--tests` completes by base name and layers a static source scan (the instant
floor) with a pytest-collected set that also includes dynamically-generated
tests; that set warms itself from any real `otto test --list-tests` run, or
from a one-time bounded collection on the first `--tests` TAB (see
{doc}`test/index`).  `--clear-autocomplete-cache` drops the cache if it ever goes
stale.

### Remote path completion

`otto host <HOST_ID> get` completes its source paths **on the remote host**,
and `otto host <HOST_ID> put` completes its destination the same way —
directories only, because that is the only thing a destination can be.  Every
other path on those commands is local, and your shell completes it as usual.

```console
$ otto --lab my_lab host dut1 get /var/log/<TAB>
/var/log/dmesg  /var/log/journal/  /var/log/syslog
```

Directories come back with a trailing `/`, so the next TAB descends into them.
Dotfiles appear only once the fragment you typed itself starts with `.` (the
usual shell convention), and a TAB with nothing typed lists the remote home
directory.  Completion resolves the same host the command would, honouring
`--hop` and `--term`, so it needs a lab selected (`--lab` or `OTTO_LAB`) just
like the command does.

Remote completion is deliberately narrow, and where it can't answer it stays
**silent** — a TAB never prints an error onto your prompt.  It offers nothing
when:

- **the host isn't reached over SSH.**  Serial and telnet hosts complete no
  remote paths in this release.
- **you don't hold the lab's reservations.**  The same required-resource set
  the command itself checks is verified *before* any host is contacted, and
  `-R` / `--skip-reservation-check` does **not** bypass it — that flag's loud
  warning has nowhere to print in the middle of a TAB, and a silent
  break-glass is not one.  See {doc}`reservation/index`.
- **the listing didn't come back in time.**  The remote `ls` runs under a hard
  two-second budget; a slow or wedged host costs you the suggestions, not your
  prompt.
- **the directory can't be listed** — it doesn't exist, or you can't read it.
  A failed listing is never remembered, so fixing the cause and pressing TAB
  again asks the host afresh.

Two short-lived caches keep repeated TABs quick: a directory listing is reused
for 45 seconds per host and directory, and the reservation answer for up to
two minutes — less when a booking of yours starts or ends sooner, since
crossing a window edge invalidates it immediately (see
[Reservation windows](reservation/windows.md)).
Both live in `remote_completion_cache.json` in [the workspace
home](#the-workspace-home), beside the main completion cache, and
`--clear-autocomplete-cache` deletes both files.

The cached reservation answer is read by tab completion and by nothing else —
see {doc}`reservation/windows` for why, and for what a window-aware backend
buys.

## Output directories

Most commands create a per-invocation output directory under `--xdir`
before the command body runs; the run's log files and artifacts are
written there, and the path is printed at the end of the run
(`Output directory: ...`):

```text
<xdir>/<command>/<timestamp>_<subcommand>/
```

- `<command>` is the top-level subcommand (`run`, `test`, `host`, ...)
  and `<subcommand>` is the leaf — the instruction name, suite name, or
  host verb.  Commands with no distinct leaf (`monitor`) omit the
  suffix: `monitor/<timestamp>/`.
- `<timestamp>` is UTC with millisecond precision
  (`YYYYMMDD_HHMMSS_mmm`), so directories sort chronologically.
- Hyphens in command names become underscores (`write-file` →
  `write_file`).

Read-only commands create no directory: `otto cov`, `otto reservation`,
`otto inventory`, `otto schema`, and `otto init` opt out entirely, as do
read-only host verbs such as `ls`, `exists`, `read-file`, `is-installed`, and
`is-uninstalled`.  Third-party commands control this with the `output_dir=`
flag at registration — see {doc}`../../library/extending-cli`.

## The workspace home

Three directories carry the `.otto` name, and each holds exactly one kind of
thing:

- a repo's `.otto/` holds **source config** — `settings.toml`, coverage
  overrides, the schemas `otto init` scaffolds.  Committed, shared by the team.
- the xdir holds **run outputs** — the per-invocation directories above.
- the **workspace home** holds everything otto derived and can rebuild.

The third is otto's user-level home (`~/.otto`, relocatable wholesale with
`OTTO_HOME`), sharded by *workspace* — the normalized `OTTO_SUT_DIRS` set — so
one directory serves every invocation against the same repos, from wherever
you run them:

```text
~/.otto/
  <hash8>-<slug>/                   # the workspace home
    completion_cache.json
    remote_completion_cache.json
  tls/                              # a convention, not derived state — see below
```

The `<hash8>` half of the key makes the name correct: two different workspaces
cannot collide, even when their directories share basenames.  The `<slug>`
half, built from those basenames, makes `ls ~/.otto` readable.

Everything under a workspace home is derived, so the whole directory is
disposable — delete it and otto rebuilds what it needs on the next run.
`OTTO_HOME` moves all of it at once.

`tls/` is the exception on both counts.  otto never creates it and never
rebuilds it: it is the conventional per-user location a committed
`[monitor]` table points its `tls_cert` / `tls_key` at (see
{doc}`monitor/serving`).  Those settings spell the path `~/.otto/tls/`
literally, so they follow `$HOME` — setting `OTTO_HOME` relocates otto's
derived state and leaves them where they were.

```{note}
The completion caches used to live under `$OTTO_XDIR/.otto/`, one copy per
directory you invoked otto from.  There is no migration, because a cache is
rebuilt on demand: stale `$OTTO_XDIR/.otto/completion_cache.json` and
`remote_completion_cache.json` files have no remaining reader and may be
deleted by hand.
```

## Exit codes

Every command derives its exit code from the {class}`~otto.result.Result` it
returns:

| Outcome | Exit code |
| --- | --- |
| Success (including `Status.Skipped`) | 0 |
| `Status.Failed` | 1 |
| `Status.Error` | 2 (Click also uses 2 for usage errors) |
| `Status.Unstable` | 3 |

Commands that run something on a host are ssh-like instead: they exit with the
remote command's own return code, or 255 when the command never ran because the
connection failed.

## Commands

| Command | What it does |
| --- | --- |
| [`otto init`](init.md) | Scaffold a new otto repo, or validate an existing one |
| [`otto env`](env/index.md) | Build and maintain this workspace's orchestration environment |
| [`otto host`](host/index.md) | Run commands and transfer files on lab hosts |
| [`otto run`](run/index.md) | Run a registered instruction on the lab |
| [`otto test`](test/index.md) | Run a registered `OttoSuite` test suite |
| [`otto docker`](docker/index.md) | Build images and orchestrate compose stacks on docker-capable hosts |
| [`otto link`](link/index.md) | Inspect and impair the lab's **static** links — the edges that already exist |
| [`otto tunnel`](tunnel/index.md) | Create, list and remove host-resident tunnels — paths that do not exist until you build them |
| [`otto monitor`](monitor/index.md) | Launch an interactive performance dashboard |
| [`otto cov`](cov/index.md) | Collect coverage from the lab and render reports |
| [`otto reservation`](reservation/index.md) | Inspect and verify lab reservations |
| [`otto inventory`](inventory/index.md) | Inspect, export and diff the configured host inventory |
| [`otto schema`](schema/index.md) | Export JSON Schema for `lab.json`, `settings.toml`, reservations and the inventory file |

Reach for `otto link` to shape a path that already exists, and `otto tunnel`
when you need one that does not.

```{toctree}
:caption: Topics
:hidden:

dry-run
projects
```

```{toctree}
:caption: Commands
:hidden:

init
env/index
host/index
run/index
test/index
docker/index
link/index
tunnel/index
monitor/index
cov/index
reservation/index
inventory/index
schema/index
```
