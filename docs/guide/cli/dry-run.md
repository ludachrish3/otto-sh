# Dry runs

**A dry run never runs a command on any device.** By default it validates and
stops at the CLI seam; a command may opt in to a deeper, configuration-only
preview; and `--probe` may permit a *connection* — never a command — so you can
see whether the hosts would answer.

That sentence is the whole contract. It is written so plainly because of what
the alternative looks like. When logic branches on a fact about a device there
are exactly three things a dry run can do: **fabricate the fact**, **decline
loudly**, or **never run the logic**. "Continue gracefully" is not a fourth
option — it is the first one wearing a kinder name. otto used to take it, and a
dry run would report every link clean, print the body of a file it had not
written, and accuse a host it had never contacted of missing `socat`.
Everything below is the consequence of removing that option.

## The default: validate, print, stop

Under `--dry-run` (`-n`), otto's dispatch layer — not the command — does this,
and exits **0 before the command body runs**:

- arguments parse and coerce, so a typo'd `--mode 789` still fails here
- the lab loads, and every host, link or tunnel the command names resolves
  against it
- the command's module imports; for `otto test`, the suite imports and its
  steps bind
- it prints what would run: the command, its target, and the arguments you gave

```console
$ otto --lab my_lab -n host dut1 run "systemctl restart nginx"
[DRY RUN] Commands and file transfers will be skipped. No device will be contacted.
dry run: no command body was run and no device was contacted
  would run: otto host dut1 run 'systemctl restart nginx'
  lab: my_lab (3 hosts); references resolve: host 'dut1'
```

No body executes, so nothing a body might do can happen. **A command author who
does nothing at all gets this, and cannot get it wrong** — which is the point.
The alternative, where every author has to keep dry runs in mind while writing
run-parse-branch logic, produces a feature that is subtly broken in a different
way in each command, and a consistently broken complex feature is worse than a
reliable simple one.

Resolution really happens, and its failures are still failures — the block is
printed *after* the references resolve, so a dry run never reports that a
command "would run" against a host that does not exist:

```console
$ otto --lab my_lab -n host nosuchbox run "uptime"
No host with ID 'nosuchbox'.
Available hosts:
  - router1
  - dut1
  - local
```

`otto test -n` is the same rule rather than a special case — the suite really
imports and its tests really bind, and then nothing runs:

```console
$ otto --lab my_lab -n test TestExample
dry run: no command body was run and no device was contacted
  would run: otto test TestExample
  suite: TestExample imported and bound; 1 test(s), no test body will run
    - test_logs_message
```

Making step bodies previewable is not a missing feature — steps not running
*is* the feature.

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
nothing. This is deliberate, not an oversight.

`lab_free` means **"this command drives its own lifecycle"**, not "this command
touches no device". `otto monitor --live` is registered lab-free and collects
metrics from every host in the lab — so a carve-out letting lab-free commands
run their bodies under `-n` would exempt precisely the command that can still
reach a device while you believed nothing would. One rule with no exceptions is
also the only version a third-party author can hold in their head.

If a lab-free command of your own should genuinely run under `-n`, that is what
the opt-in below is for.

## Reachability: `--dry-run --probe`

A plain `--dry-run` contacts **nothing**: it parses the arguments, loads the
lab, resolves every host/link/tunnel the command names, prints what would run,
and stops.  Adding `--probe` buys exactly one extra thing — otto opens a
connection to each host in that resolved set and prints whether it answered:

```console
$ otto --lab my_lab --dry-run --probe host router1 run "make install"
probe: a connection only -- no command was run
  router1: unreachable
dry run: no command body was run; --probe opened a connection only, and ran no command
  would run: otto host router1 run 'make install'
  lab: my_lab (3 hosts); references resolve: host 'router1'
```

A host that answers is reported `reachable (connect <N> ms)` instead, and the
dry run exits 0 either way — **reachability is information, not a gate.**

- **A connection, never a command.**  `--probe` opens **and authenticates** the
  connection(s) this invocation would use, and no command follows.
- **`--probe` requires `--dry-run`.**  On its own it is a usage error (exit 2):
  the only thing that makes dialing safe here is that no command can follow it.


`--probe` is opt-in on top of a dry run. It lets otto **open a connection** to
each host the command names and report whether it answered.

```{important}
**`--probe` permits a connection, never a command.** A connection attempt
produces no command result, so it feeds no `if result.is_ok:` and no parser —
the fabrication hazard is untouched. Reachability is information, not a gate:
an unreachable host is reported and the dry run still exits 0.
```

```console
$ otto --lab my_lab -n --probe host router1 run "make install"
[DRY RUN] Commands and file transfers will be skipped. --probe will open a
connection to each named host, and run no command.
probe: a connection only -- no command was run
  router1: unreachable
@router1   | [DRY RUN] Connection FAILED: [Errno 111] Connect call failed ('127.0.0.1', 23) — a real connection; no command was run
dry run: no command body was run; --probe opened a connection only, and ran no command
  would run: otto host router1 run 'make install'
  lab: my_lab (3 hosts); references resolve: host 'router1'
```

Note the headline: it changes the moment a socket is opened, because the
default one ends "and no device was contacted" and that would then be false.

**`--probe` requires `--dry-run`.** On its own it is a usage error (exit 2) —
the only thing that makes dialing safe here is that no command can follow it:

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

That is deliberate. The probe answers "would this run's connect phase succeed?",
and anything narrower — a bare TCP connect, or the terminal channel alone —
would report a reachability the real run is not going to get. A host that
accepts TCP and then refuses the login is not usable, and calling it
`reachable` would be this contract's own defect in a smaller font. It is also
exactly why the flag is opt-in rather than part of `--dry-run`.

`--term`, `--transfer` and `--hop` are honoured, so the probe dials the
transport the command would have dialed rather than the host's configured
default.

### Three states, not two

| state | meaning |
| ----- | ------- |
| `reachable` | a connection opened (and authenticated); the row carries `connect <N> ms` |
| `unreachable` | a connection was attempted and did not open — refused, timed out, or refused the login |
| `not probed` | no reachability question could be asked at all |

`not probed` is a separate state on purpose. A Docker container host is reached
through its parent's shell and has no transport of its own, so otto never asks;
reporting it `unreachable` would be a measurement nobody took. "We could not
ask" and "we asked and it said no" are different facts, and only one of them is
about the host.

The built-in `local` host is reachable without a socket — otto is already
running there — and says so:

```console
$ otto --lab my_lab -n --probe host local run "uptime"
probe: a connection only -- no command was run
  local: reachable -- no transport to open
dry run: no command body was run and no device was contacted
  would run: otto host local run uptime
  lab: my_lab (3 hosts); references resolve: host 'local'
```

The headline stayed at "no device was contacted", because none was: the block
counts **sockets, not rows**.

### The limit

**No link or tunnel command lends the probe a reference resolver today.** Only
`otto host` does. So `--probe` on a link or tunnel command dials nothing, and
says so rather than letting silence imply the hosts were found healthy:

```console
$ otto --lab my_lab -n --probe link impair core --delay 50ms
[DRY RUN] Commands and file transfers will be skipped. --probe will open a
connection to each named host, and run no command.
probe: this command names no host to dial
dry run core: no device was contacted — nothing was read and nothing was changed
  would: a->b on router1/eth1: tc qdisc replace dev eth1 root netem delay 50ms
  …
```

## The lab-level verbs answer the same way

`otto.project`'s verbs — what `otto run install` and the `ensure` marker's
steps call — compose the host verbs above, and inherit their answers. Two of
them have an answer of their own, and both are reached from a *library* caller:
the `otto run` group keeps the seam default, so `otto -n run cleanup` prints the
block and runs no body at all, while a suite marked `ensure("clean")` calls
the converge directly.

- `cleanup()` finishes with two lab-wide steps, and neither pretends to have
  run: `otto.link.manage.repair_all` reads no netdev and
  `otto.tunnel.manage.remove_all_tunnels` scans no host, so each is reported
  `Status.NotRun` ("dry run: no link was read and no impairment was reset").
  Their empty reports are exactly what a real sweep of an already-clean lab
  produces, which is why the status carries the difference.
- `is_clean()` returns a `bool` and so, like `host.is_clean()`, raises rather
  than answering as soon as something it needs was not measured — the
  toolchain probe, a link whose impairment state was declined
  ({class}`~otto.link.manage.LinkNotMeasuredError`), or a tunnel scan that
  asked nobody ({class}`~otto.tunnel.discovery.TunnelNotMeasuredError`). It is
  the same rule in the same words: reporting a lab clean on the strength of
  reads nobody took would send a converge into a cleanup on a fact nobody
  established.

## What a dry run still does

One exception, and it is deliberate: otto reads **its own** SUT checkout's git
HEAD under a dry run, to stamp the run's provenance. That is a local, read-only
query about the machine otto is already running on, not a command on a device,
so declining it would be a false positive of the contract rather than
enforcement of it. It is the only such exemption, it is declared at the one
call site that uses it, and it does not extend to anything else — including
`otto host local run`, which declines under `-n` exactly like every other host.
