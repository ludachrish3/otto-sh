# Live mode

`--live` is the explicit opt-in that touches hardware; it is never the
default. By default it polls every real host in the lab:

```bash
otto --lab my_lab monitor --live
```

Docker container hosts are excluded — they aren't operated on as part of
the host fleet. Embedded targets without an `snmp` block are also
excluded: the monitored set is Unix hosts (shell metrics) plus any host
that declares `snmp` (polled over SNMP — see
[SNMP monitoring](metrics.md#snmp-monitoring)).

## Selecting hosts

Pass a regex to `--hosts` to narrow the live host set. It is a FULL match
against each host ID (`re.fullmatch`), never a substring search: `router`
selects the host whose ID is exactly `router`, and nothing else. To match by
prefix, append a wildcard — wrapping any alternation first:

```bash
otto --lab my_lab monitor --live --hosts '(router|switch).*'
otto --lab my_lab monitor --live --hosts router1
```

A pattern that matches none of the hosts the run may walk is an error, not an
empty run: `otto monitor` prints the pattern, how many hosts it was matched
against, and the wildcard to add.

Omit the option to monitor every real host in the lab (Docker containers
excluded).

"The hosts the run may walk" is the run's **fleet of interest**, not
necessarily every host in the lab: when a repo declares a `[project]` table in
its `.otto/settings.toml`, `--hosts` selects a subset of what that declaration
admits. See {ref}`project-scope`.

If the **driving** project's own declaration admits no host here — no loaded lab
applies to it, or none of their hosts match its `host_patterns` — `--live`
refuses before it builds a fleet at all, naming the repo and the file to edit.
A dependency's declaration never refuses: a lab that another project narrowed
out is still this one's to watch.

### When nothing gets monitored

Four different emptinesses, four different messages — because the next edit is
different in each case:

- **The pattern matched nothing.** You get the pattern, the size of the set it
  was matched against, and the wildcard form to try. Fix the regex.
- **The pattern matched, but every match is held out of fleet sweeps** by
  `include_containers` / `include_local`. The message opens by saying the regex
  is *not* the problem and names the flag, because widening an already-matching
  regex is the natural first guess and changes nothing. To reach one of those
  hosts, use `otto host <id> <verb>`.
- **Nothing was selected at all.** `No hosts available in the active lab.` —
  deliberately silent about `--hosts`, because with nothing to select from the
  pattern is innocent.
- **Hosts were selected, but none can be sampled.** You get the count and the
  ids (up to five, then a summary), and a reminder that otto samples over a
  shell or over SNMP. Give the host an `snmp` block, or point `--hosts` at a
  Unix host — widening the selection will not help.

All four exit 1 with the message on stderr. The first two come from the
selection layer and are caught and framed by `otto monitor` itself, so an empty
`--hosts` is one line rather than a traceback.

## Collection interval

Control how often metrics are collected with `--interval` (default: 5
seconds, minimum: 1 second):

```bash
otto --lab my_lab monitor --live --interval 2.0
```

The 1-second floor is deliberate: a host needs time to answer every query in
the interval without being taxed by the polling itself. It's enforced at
every human-facing boundary that names an interval — `otto monitor
--interval` above, `otto test --monitor-interval` (see [Monitoring during a
test run](during-tests.md#monitoring-during-a-test-run)), and
`OttoSuite.start_monitor()` (see [Monitoring from test
suites](../../../library/custom-parsers.md#monitoring-from-test-suites)) all
reject anything lower.
`MetricCollector` itself is deliberately exempt — it's the mechanism, not a
knob a human sets, and otto's own tests drive it as fast as 0.01s against
fake hosts.

## Persisting data — sessions

Add `--db` to persist the run as a **session** — this run's lab snapshot,
chart/tab layout, and every collected point — into a SQLite archive:

```bash
otto --lab my_lab monitor --live --db metrics.db
```

Reusing the same `--db` path on a later run doesn't overwrite it: each
`--live --db metrics.db` invocation appends one more session, so a single
archive can accumulate a whole day's worth of separately-labeled runs. Tag a
session for later review with `--label` (short, shown in the dashboard's
session picker) and `--note` (free-form, shown as that picker entry's
tooltip):

```bash
otto --lab my_lab monitor --live --db metrics.db \
    --label "fan fix" --note "post-repair burn-in, rack 3"
```

Review a captured archive later with `otto monitor metrics.db` — see
[Reviewing a capture](review.md#reviewing-a-capture).

## Running otto on shared/NFS storage

otto is safe to run with its log/artifact root (`OTTO_XDIR`) on a shared mount
(NFS, CIFS/SMB, sshfs, …):

- **Monitor database.** SQLite's WAL journaling is not supported over a network
  filesystem, so when the `--db` path is on one otto automatically uses the
  `DELETE` journal mode instead (logged at debug level). This is transparent and
  lossless for monitoring's write pattern.
- **Multi-machine, one shared database.** The "another instance is already
  writing" guard relies on `flock`, whose semantics on network filesystems are
  same-host only. If several machines may write to the *same* database file,
  put that database on **local disk** (or give each machine its own `--db`
  path).
- **Logs and artifacts.** Per-run log directories are fine on shared storage.
  Old-log rotation is wall-clock budgeted, so even a very large log tree cannot
  stall a run — any backlog is pruned across subsequent runs.
- **Lab data and settings** (`lab.json`, `.otto/settings.toml`) are read once
  per run and are unaffected.

If otto cannot determine the filesystem type, it assumes local disk and keeps
its default behaviour.

