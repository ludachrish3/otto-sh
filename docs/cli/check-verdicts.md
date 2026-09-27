# Check verdicts and reports

otto's setup checks survey your lab and tell you which of otto's features
work there. There are two: {doc}`otto link check <link/check>` for one link's
impairments, and {doc}`otto tunnel check <tunnel/check>` for one tunnel
path. They all answer in the same words, which this page defines: the verdict on each
row, the reason an `unmeasured` row gives, the exit code, the `--report`
file, and the labels that say how your hosts compare with the versions otto
has been proven on.

## Verdicts

Every row of a check's table carries exactly one verdict:

| Verdict | What happened | What it means for you | Fails the run |
| --- | --- | --- | --- |
| `pass` | otto applied the feature and measured it working as configured. | You can rely on this feature on this host. | no |
| `fail` | The host accepted the command, but the measurement came out wrong. | Either otto or the host misbehaves. This is the row otto's developers most want a report on. | yes |
| `unsupported` | The host's tool or kernel rejected the command. | The host lacks a capability, such as a kernel module or a tc feature. The rejected command and the tool's complaint are printed beneath the row. | yes |
| `unmeasured` | otto could not get evidence either way. | Nothing is known about this feature on this host yet. The row always gives a [reason](#why-a-row-is-unmeasured). | no |
| `skipped` | Something this row depends on didn't happen, so the row never ran. | The row's detail, or the hint beneath it, says what was missing. | no |

The split between `fail` and `unsupported` matters. `unsupported` means "your
host cannot do this". `fail` means "your host said yes, and the result was
wrong", which usually means otto has a bug on your kind of host.

## Why a row is unmeasured

An `unmeasured` row always carries one of these reason codes. The row's
detail column starts with the code, followed by whatever otto measured or
knows, for example `noisy-baseline: measured loss 20%, σ 9.8 ms` or
`missing-tool: needs socat or python3 on test1`. A code with nothing to add
is printed alone.

| Code | Why | What to do |
| --- | --- | --- |
| `missing-tool` | A tool the measurement needs isn't installed. The detail names it, and the host. | Install it and run again. |
| `noisy-baseline` | Before measuring, otto takes a baseline with nothing impaired. That baseline lost packets or varied too much to compare against. The detail shows the loss and spread otto saw. | Run again when the host or path is quieter. If it keeps happening, look for what is loading the host or dropping packets on the path. |
| `no-reply-oracle` | The target runs nothing of otto's that could answer, so a UDP payload cannot be confirmed. `otto tunnel check` reports it for UDP to a `--dest` that gets the [split proof](tunnel/check.md#checking-a---dest). | Nothing on your side. This is a known gap in what otto can prove ([#440](https://github.com/ludachrish3/otto-sh/issues/440)). `otto link check` never reports it. |
| `no-clock` | A timed probe printed no elapsed time. For a socat probe that means the host's bash has no `$EPOCHREALTIME` (bash older than 5.0). In `otto tunnel check` only the `rtt` row needs the clock, so the payload verdicts still stand. | `otto link check`: install python3 on the host (otto prefers it), or a newer bash. `otto tunnel check`: bash 5.0 or newer on the path's first hop. |

`unmeasured` and `skipped` never fail a run. They mean "no evidence", not
"broken".

## Reading the table

`otto link check` and `otto tunnel check` print their tables through the
same renderer, so both read the same way:

- **The `detail` column** says what a row measured (`measured …`, and
  `want …` too when the row isn't a pass), then, after ` — `, anything else
  the row says about it: a caveat a pass still carries, or the cause a
  `fail` or `unsupported` names. A detail that only repeats the measurement
  isn't said twice. An `unmeasured` row leads with its [reason
  code](#why-a-row-is-unmeasured) instead, whatever else it carries. When a
  multi-column row's columns disagree (e.g. differing tcp/udp byte counts),
  each cell's own text prints on its own line, starting with its column's
  name; the first stays on the row, the rest print beneath it, before any
  `ran:`/`said:`/`hint:` evidence.
- **`ran:` lines**, under any cell that isn't `pass` and ran something, are
  the exact commands otto sent, one per line. A probe script can run to
  several hundred characters, so on screen a `ran:` line over 300 characters
  is cut and ends `… (+<n> chars; full command with -v or in --report)`:
  `-v` prints the line whole, and the report file always has it.
- **`said:`** follows, for an `unsupported` cell: the last line the host's
  tool printed when it rejected the command.
- **`hint:`** follows when otto knows what to do. A hint every row of a
  block shares is printed once, under the heading, instead of on each row.
  A hint a row shares only with the row right before it — a cascade of
  skipped rows downstream of one failure — is likewise said once, under the
  first of them, whether that row carries a single hint or, with more than
  one column, several at once.
- With more than one column (`sandbox`/`live`, or a protocol each), an
  evidence line not shared by every column starts with its column's name
  (`tcp ran:`, `live hint:`, …).

## Exit codes

| Code | When |
| --- | --- |
| `0` | Nothing failed. `pass`, `unmeasured` and `skipped` rows only. |
| `1` | Any row is `fail` or `unsupported`. Also when the check could not produce a table: the target was refused (for example, a link otto cannot impair, or a tunnel path `otto tunnel add` would refuse), a host did not answer at all or stopped answering during the check, or the link or host you named does not exist. A host that answers but whose probe stalls is not this case: that probe's row is `fail`, and the rest of the table is still produced. |
| `2` | A usage error, such as an unknown `--feature` or `--protocol` name. Nothing was contacted. |

A dry run (`otto -n …`) exits `0` after printing its plan. It still exits
`1` for a target that is refused or doesn't exist, since it can tell that
without contacting anything.

## Leftovers and concurrent checks

Everything a check creates is tagged `otto-check` and removed when the
check ends, whether it passed, failed or you pressed Ctrl-C once. That
Ctrl-C's teardown has a deadline (`OTTO_TEARDOWN_DEADLINE`, 10 seconds by
default; see {doc}`../architecture/lifecycle`), so on a slow host it can be
cut off, leaving what it hadn't removed yet as young leftovers. A check
killed outright (a second Ctrl-C, `kill -9`, a dropped connection) can leave
some of it behind too: an echo or listener process, a throwaway tunnel, or
a sandbox namespace. So each run starts by sweeping the hosts it's about to
use for leftovers.

A leftover doesn't say whether the run that made it is still going, so the
sweep can't tell a killed run's from one that a check running right now
still needs. It goes by age instead, and judges each run's leftovers
together: a run is as old as its oldest leftover, so a killed run's last
echo, started seconds before it died, goes with the rest. A run older than
45 minutes is removed. That's longer than either check can run, even with
every probe on a slow host waiting out its timeout, for a tunnel path of up
to five hosts that run echoes and a link check on its two placement hosts.
A longer path, or a host slow on every command, can run past it, and
another check's sweep may then take that run's leftovers while it is still
using them; `otto tunnel check` notices, and says so in its `teardown` row.
A younger run is left alone, and the run says so under the host's heading:

```text
left otto-check echo fwd-echo on test2 (started 40 s ago — may be a running check)
```

A leftover whose age the host can't report is removed, so the cleanup never
silently stops.

A young leftover sits idle until a check on that host finds it past that
age and sweeps it. Later runs step around it meanwhile: a tunnel
check's echo holds a scratch port that later runs see in use, and each
sandbox has its own addresses. A `otto link check --live` listener is the
exception: every `--live` run listens on the same ports of the link's
address, so the next one's own listener on that port can't bind, and the
leftover answers its probes instead. That still measures the link, since the
leftover echoes the same way, and the run says so under the first host's
heading: `reusing leftover listener on test2:5299 (otto-check-1a2b3c,
started 40 s ago)`.

If you know no other check is using those hosts, you can clear a young
leftover yourself instead of waiting:

- **A throwaway tunnel** shows in `otto tunnel list` like any other tunnel.
  Remove it with `otto tunnel remove <id>`.
- **An echo or a listener** is a process with `otto-check` in its command
  line. On the host, `ps -eo pid,etime,args | grep otto-check` lists each
  with its pid and age; stop it with `kill <pid>`. A `otto link check
  --live` listener runs as root, so it needs `sudo kill <pid>`.
- **A sandbox namespace** is `otto-check-<id>` in `ip netns list`, with its
  veth `ock<id>` in the host's `ip link`. Remove both with `sudo ip link del
  ock<id>` and `sudo ip netns del otto-check-<id>`. A listener still running
  inside it shows in the `ps` listing above; kill it first.

What running two checks against the same hosts at once does differs by
check: see {doc}`otto link check <link/check>` and
{doc}`otto tunnel check <tunnel/check>`, under *Cleanup and leftovers*. From
Python, `check_tunnel` calls may share a `Lab`, even run concurrently with
`asyncio.gather`; give each concurrent `check_link` its own `Lab`.

## The report file

Nothing is written unless you ask. `--report PATH` writes everything the
check found as JSON, and the command prints `report: PATH` when it's done. A
dry run writes no report and says so.

Attach this file when you [open an issue](https://github.com/ludachrish3/otto-sh/issues).
It carries the evidence otto's developers need, so they don't have to ask you
for it. It is also how otto learns about environments it hasn't been proven
on (see {doc}`known-good`).

The file is one JSON object:

- `schema` is always `"otto-check/1"`. It changes only if the layout does.
- `kind` names the check: `"link"` or `"tunnel"`.
- `otto_version` is the otto that ran the check.
- `proven_revision` is the revision of the proven-range list your hosts were
  labeled against.
- `result` is the check's whole result. It holds each host's fingerprint
  (kernel, architecture, userland, privilege, which tools were found and
  their versions, and the raw probe output) and, for each row, the verdict
  and reason code, the measured and wanted values, the exact commands otto
  ran, and what they printed. A tunnel check's result also carries the
  scratch port it built on, which proof a `--dest` got, and the sentence its
  `proven:` line printed. A refused target has an empty host list and the
  refusal and its hint instead.

The report contains addresses: each host's management address, the
addresses on the interfaces checked, and target addresses inside the
commands. It also contains host ids and the login user. Nothing in it is a
secret, and otto has no option to leave any of it out. If you'd rather not
post them, edit them out before you attach the file. Keep the structure
intact, and replace each address consistently so the report still makes
sense. Addresses sit inside the recorded commands as well as in the
fingerprint fields. A timed probe's command, for example, which the
terminal shows as a `ran:` line, names the far endpoint:

```text
live ran: bash -c 's=$EPOCHREALTIME; echo x | socat -T 15 -t 15 - TCP:10.10.202.12:5205,connect-timeout=5 | grep -qx x; …'
```

Here `10.10.202.12` is the address to replace, in this command and in every
other place it appears.

## Proven-range labels

Every host a check fingerprints gets a `proven range:` line under its
heading, with one label per component (for example, its iproute2 version or
its kernel). The label compares your version with the versions otto has been
proven on, listed on {doc}`known-good`.

| Label | Meaning |
| --- | --- |
| `within` | otto has been proven on this version, or, for a component with a version order such as iproute2, on versions both older and newer than it. |
| `older` | Older than every proven version. |
| `newer` | Newer than every proven version. |
| `outside` | For a component without a version order (the architecture, the userland), a value otto hasn't been proven on. |
| `unknown` | otto couldn't read or order your version, or nothing has been proven for this component yet. |

A label never changes a verdict. It tells you how much otto's own testing
covers your host. Any report is welcome, and one from a host that reads
`older`, `newer` or `outside` is especially useful, even when every row
passed, because that is how the proven range grows. `unknown` alone is
common: it often means otto has nothing recorded for that component yet.
