# otto link check

```text
otto link check <link> [--live] [--feature <name>[,<name>...]] [--from <host>]
                        [--report <path>] [--verbose]
```

```bash
otto --lab unix link check edge --report check.json
otto --lab unix link check edge --from test1 --live
otto --lab unix link check edge --feature "delay,port range"
otto -n --lab unix link check edge --live
```

Nothing is written unless you pass `--report`, so pass it the first time:
the file is what you attach if a row turns out to need a report.

`otto link check` tells you which of otto's impairment features work on the
hosts that would impair one link. A link is a route between two lab hosts,
declared in `lab.json` (see {doc}`index`); `otto link list` shows every link
and its id. otto impairs a link with netem, the Linux kernel's network
emulator, which it configures with `tc`. How well that works depends on
things that vary from lab to lab: the kernel, the `tc` version, BusyBox
versus GNU tools, the installed probe tools. So instead of hoping otto works
on your hosts, run this once when you set up a lab, add a host, or see an
impairment misbehave.

On each host that would impair the link, otto builds a throwaway network
namespace, applies each feature (delay, loss, rate, and so on) inside it, and
measures whether the feature did what it should. Your interfaces are not
touched. `--live` then repeats the features a real interface can change on
the link itself.

It's a survey of one link, not a CI gate. It takes host time, runs privileged
commands, and a busy host can leave rows `unmeasured`: no evidence either
way (see {doc}`../check-verdicts`). The
sandbox pass spends about 20 seconds per host pinging, transferring and
waiting on the `expire` row, plus a round trip for every command otto sends.

| Option | Description |
| ------ | ----------- |
| `<link>` (argument) | Link id or name. Tab-completes from the loaded lab. |
| `--live` | After the sandbox, run a short real impair, measure, repair cycle on the link. See [Checking the real link](#checking-the-real-link---live). |
| `--feature` | Check only these features, comma-separated: `read-back`, `delay`, `jitter`, `loss`, `duplicate`, `reorder`, `corrupt`, `rate`, `port range`, `side`, `expire`. Quote the list if it contains `port range`. `read-back` always runs, because every other verdict depends on otto reading back what it wrote. An unknown name is a usage error (exit 2). |
| `--from` | Check only the direction that starts at this host, which must be one of the link's two endpoints. Both directions by default. |
| `--report` | Also write the full result as JSON to this path. See {doc}`../check-verdicts`. |
| `--verbose`, `-v` | Also print every probe's raw output. |

Each row of the result is a verdict: `pass`, `fail`, `unsupported`,
`unmeasured` or `skipped`. What each means, the exit codes, the report
file, and the proven-range labels are defined once, on
{doc}`../check-verdicts`. The versions otto has been proven on are listed on
{doc}`../known-good`.

## Which hosts it checks

otto checks the hosts that [`otto link impair`](impair.md) would change. Each
direction of a link is impaired on the host that direction starts from, or,
when the link names an in-path middlebox, on that middlebox (see
{doc}`in-path`). So an endpoint link checks both endpoint hosts (one, with
`--from`), and an in-path link checks only the middlebox. `--live` also runs
probes on the link's two endpoints.

If otto can't impair the link at all, the check stops there and reports the
refusal as its result (exit 1). The usual case is a link otto derived from a host's
`hop`, which has no named interface to impair; the hint says to declare the
link in `lab.json` with an interface on each endpoint (see {ref}`lab-links`
in {doc}`../../configuration/lab-config`).

## What each host needs

On every host otto checks:

- **A way for otto to become root.** otto logs in as the host's first
  applicable `creds` entry in the lab config (see
  [Per-host fields](../../configuration/lab-config.md#common-optional) and
  {ref}`cred-protocols`). If that user isn't root, otto elevates the way every
  privileged otto command does: through the host's `sudo`, or `su` where there
  is no sudo, answering a password prompt from that host's `creds`. So a sudo
  that asks for a password works, as long as the lab declares the password
  (for `su`, root's). otto checks this by running `id -u` through that
  elevation; it must print `0` within 8 seconds. If the host answers
  something else, every sandbox row is `skipped` with the hint `otto could
  not become root on <host>: …`, which names the command and what the host
  answered. If it doesn't answer at all, sudo or su is almost always waiting
  for a password the lab doesn't declare. otto interrupts it, and every
  sandbox row is `skipped` with the hint `` otto could not become root on
  <host>: `id -u` got no answer within 8 s — … ``, which points you to the
  host's `creds`. The rest of the check still runs. The heading shows
  `root` (the login user is root), `elevated` or `no privilege`. `--live`
  doesn't run this check: its steps elevate the way `otto link impair` does.
- **`ip` with network namespace support** (`ip netns`). Without it, every
  sandbox row is `skipped` with `needs ip netns support on <host>`.
- **iproute2's `tc`** and the kernel's **`sch_netem`** module. When the
  fingerprint can't find the module, the heading says `no sch_netem` (or
  `sch_netem ?` when the host can't tell), and any `unsupported` row's
  hint names it (see [When a row isn't pass](#when-a-row-isnt-pass)).
- **`ping`**, for every row that measures timing or loss. Without it, those
  rows are `unmeasured`, with the detail `missing-tool: needs ping on <host>`.
- **python3 or socat**, for the `rate`, `port range` and `side` rows, which
  time TCP connections through small echo listeners. otto uses python3 when
  both are installed. Without either, those rows are `unmeasured`, with the
  detail `missing-tool: needs socat or python3 on <host>`.
- **bash**, for three things: the echo listeners are started with `setsid
  bash`, whichever tool runs them; the socat probe times itself with bash's
  `$EPOCHREALTIME` (bash 5.0 or newer); and the `expire` row launches the
  same timer [`--expire`](impair.md#--expire-auto-clearing) does. Without
  bash, those rows are `unmeasured`, with the detail `missing-tool: needs bash
  on <host>`.

With `--live`, the link's two endpoints also need `ping`, bash, and python3 or
socat, because the live probes run between them. For an endpoint link they
are the hosts above. For an in-path link they are two more hosts.

otto never uses `nc` for these probes. Its variants (OpenBSD, traditional,
BusyBox) disagree on exactly the options a timed echo needs, such as how to
listen and whether to close on end of input. A probe built on `nc` would
become one more thing that works on one host and fails on the next.

## The sandbox

The sandbox pass always runs. On each host, otto:

1. Fingerprints the host with one read-only command: kernel, architecture,
   userland, `tc` version, whether the `sch_netem` module is there, and
   which tools are installed. Then it checks that it can become root (see
   [What each host needs](#what-each-host-needs)).
2. Removes any namespace an earlier check left behind longer ago than
   [the sweep's age bound](../check-verdicts.md#leftovers-and-concurrent-checks) (see [Cleanup](#cleanup-and-leftovers)).
3. Builds a namespace named `otto-check-<id>` (`<id>` is six random hex
   digits) and a veth pair between it and the host. `<id>` also picks the
   sandbox's own `/30` from `198.18.0.0/15`: the host end, called `ock<id>`,
   gets its first address, and the namespace end gets its second. For
   `<id>` `000000` that's `198.18.0.1/30` and `198.18.0.2`. otto draws
   `<id>` so that its `/30` misses every `otto-check-` namespace step 2
   found on the host. Once the sandbox is up, otto asks the host which
   device reaches the namespace's address, and it must be `ock<id>`. If
   another namespace there has the same `/30` (one made after step 2, by a
   check starting at the same moment), the host routes into whichever came
   first, so every row fails, saying so, rather than measure the other
   sandbox. Run the check again: it draws new addresses. `198.18.0.0/15` is
   reserved for network benchmarking, so it shouldn't clash with a real
   network. If your lab does route part of
   it, a sandbox whose `/30` falls there shadows that route on the host while
   the check runs.
4. Pings the namespace with nothing applied: 10 pings, 0.2 seconds apart. This
   baseline must lose nothing and vary by no more than 5 ms (standard
   deviation). If it doesn't, every measured row is `unmeasured` rather than
   a verdict otto can't trust, and its detail starts with `noisy-baseline:`
   followed by what the baseline measured.
5. Applies each feature to the host end of the veth, using the same `tc`
   commands `otto link impair` sends, measures it, and clears it before the
   next row.
6. Deletes the namespace and the veth pair, even when the check fails or is
   interrupted.

Only the veth pair is impaired. Your own interfaces carry none of the
sandbox's probe traffic and are never changed.

| Feature | What otto applies and measures | Passes when |
| --- | --- | --- |
| `read-back` | A 100 ms delay on the whole link, then the same delay on TCP destination ports 5200–5210 only. After each, otto reads the tree back with the same reader `otto link list` uses. | Both trees read back as written, allowing for the kernel's rounding. If `tc` rejects the port-scoped tree, the row keeps the whole-link tree's verdict with the detail `port-scoped tree not applied: tc rejected it (see port range)`, and `port range` and `side` report the rejection as `unsupported`. |
| `delay` | 100 ms delay; 10 pings. | The added round-trip time is 100 ms, give or take 10 ms + 3σ, where σ is the baseline's standard deviation. |
| `jitter` | 100 ms delay with 20 ms jitter; 20 pings. | The spread of the round-trip times grows by at least 5 ms over the baseline's. |
| `loss` | 30% loss; 200 pings, 10 ms apart. | Observed loss is within a band that allows for chance, about ±12 points at 200 pings. |
| `duplicate` | 50% duplication; 50 pings. | At least a quarter of the expected duplicate replies arrive. |
| `reorder` | 50 ms delay with 50% reordering; 100 pings. | At least one reply arrives out of order. |
| `corrupt` | 50% corruption; 100 pings. | At least 25% of pings are lost. A corrupted ping fails its checksum and is dropped, so corruption shows up as loss. |
| `rate` | A 1 mbit rate limit; 256 KiB sent through an echo listener on TCP port 5299 and timed. | Throughput is within 25% of 1000 kbit/s. |
| `port range` | 100 ms delay on TCP destination ports 5200–5210 only. otto times a connection to port 5211 first, then to 5205 (inside the range) and 5211 (outside it). | The connection to 5205 slows by two to three times the delay, since a connection crosses the delayed direction twice. The one to 5211 slows by less than 25 ms. |
| `side` | 100 ms delay on TCP *source* port 5205 (`--side src`). otto times a connection to destination port 5205 before and after. | The connection does not slow down (by less than 25 ms): a source-port selector must not match traffic to that destination port. |
| `expire` | 100 ms delay with a 3-second expire timer; otto waits 6 seconds. | The delay is gone. |

The `rate`, `port range` and `side` rows use three echo listeners inside the
namespace, on TCP ports 5205, 5211 and 5299. Before those rows run, otto
connects for about five seconds until one answers. A connect counts only
when otto's own echo sends back the byte it sent, so something else
listening on the port reads the same as no listener. If none answers, those
rows are `skipped` with `listener did not start`, and the evidence shows
what the connect got.

Every timed probe gives up on its own. A python3 probe stops after 15
seconds. A socat probe stops when its connect gets no answer within 5
seconds, or once 15 seconds pass with no data moving, so a stall ends
within about 20 seconds; a transfer that keeps trickling is bounded only by
that 15-second limit on inactivity. A probe that stalls is a `fail` row with
the detail `probe did not finish within 15 s` (`the timed probe did not
complete` for a socat connect nobody answered), the command and what it
printed. The check goes on with the next row.

## Checking the real link: `--live`

A veth pair is not a real interface. The real one may already have other
qdiscs, be a VLAN sub-interface or a bond, or have a NIC that offloads
segmentation, and otto may place an impairment on the wrong host, interface
or direction. `--live` checks what the sandbox can't: after the sandbox pass,
it impairs the link itself for a few seconds per step, measures, and repairs
it.

It re-checks six features:

| Feature | Why it's checked live |
| --- | --- |
| `read-back` | The real tree may hold other qdiscs, and real interfaces have real names (VLAN sub-interfaces, bonds). |
| `delay` | Proves placement: the right host, interface, direction and middlebox. |
| `port range`, `side` | The port match reads fixed offsets in the packet, which real encapsulation such as VLAN tags or tunnels can shift. |
| `rate` | NIC offloads and multi-queue interfaces can skew shaping on real hardware. |
| `loss` | Cheap, and it proves the drops land on the real direction. |

Every other feature shows `n/a` in the live column, with the legend line
`n/a: sandbox result applies; a real link doesn't change this feature`.

How the cycle runs:

- **It never overwrites your impairment.** If the link already carries one
  in either direction, every live row is `skipped` with
  `link <id> already carries an impairment — repair it first, or run without
  --live`. Either direction counts, because the cycle's repair clears the
  whole link. A qdisc otto didn't create is never otto's to remove, so the
  hint for one says `link <id> carries a qdisc otto did not create on
  <direction> — otto will not touch it, so --live cannot run` and points to
  {doc}`safety`. Remove it yourself, or run without `--live`.
- **One direction at a time, one step at a time.** Each step impairs the
  link through the same code as `otto link impair`, with `--expire 60`. If
  otto dies mid-step, the impairment clears itself within a minute. Each
  step is repaired as soon as it has been measured. `otto -n … --live` prints
  roughly how many seconds each direction is impaired (about 12 for all six
  features).
- **Every safety refusal applies.** A step that {doc}`safety` refuses, such
  as one on the interface otto reaches the host through, skips the rest of
  that direction's rows, with the refusal as the hint.
- **A feature that was `fail` or `unsupported` in the sandbox isn't
  retried.** Its live cell is `skipped` with `failed in sandbox`.
- **Probes run between the link's endpoints.** otto pings and connects from
  the direction's starting endpoint to the far endpoint's address on this
  link, and runs the echo listeners on the far endpoint, bound to that
  address only. The far endpoint must answer ping and accept TCP connections
  on ports 5205, 5211 and 5299 from the near one. Open those in any firewall
  between them. A far endpoint with no address in `lab.json` skips its rows.
- **The link must heal.** After the last step, otto reads the link back. It
  must be clean in every direction, or the `read-back` row fails with `link
  did not heal after repair`, with a hint to run `otto link repair <link>`.
  That is what a failed repair leads to. When a repair fails, no
  further step runs, and every row not yet measured, in either direction, is
  `skipped` with the hint `repair failed: <the error>`. The link is then left
  impaired, and the heal check fails `read-back`. Run `otto link repair
  <link>` yourself: a failed repair has already cancelled the expire timer,
  so nothing else will clear the link.

A host that stops answering during the cycle is an error (exit 1), never a
`skipped` row.

## Reading the output

This is `otto --lab unix link check edge --from test1 --live`, on a host
where the sandbox passed everything but the real link shaped the rate too
low (the likely cause is under
[When a row isn't pass](#when-a-row-isnt-pass)):

```text
test1  eth1.100 10.10.201.11  (a->b)   iproute2 6.1.0 · kernel 6.1.0-18-arm64 · aarch64 · gnu · elevated
proven range: iproute2 within · kernel older · aarch64 within · gnu within

feature     sandbox  live  detail
read-back   pass     pass
delay       pass     pass
jitter      pass     n/a
loss        pass     pass
duplicate   pass     n/a
reorder     pass     n/a
corrupt     pass     n/a
rate        pass     fail  measured 612 kbit/s, want 1000 kbit/s ±25%
                           live ran: impair_link edge from test1: rate 1mbit, expire 60s
                           live ran: bash -c 's=$EPOCHREALTIME; n=$(head -c 262144 /dev/zero | socat
                           -T 15 -t 15 - TCP:10.10.202.12:5299,connect-timeout=5 | wc -c);
                           e=$EPOCHREALTIME; n=$((n+0)); [ $n -eq 262144 ] || echo "echoed $n of
                           262144 bytes"; echo "$s $e"; [ $n -eq 262144 ]'
                           live ran: repair_link edge
port range  pass     pass
side        pass     pass
expire      pass     n/a
n/a: sandbox result applies; a real link doesn't change this feature
test1: 16 pass · 1 fail
```

Each host gets one block:

- **The heading** names the host, the interface and its address on this
  link, the direction, and what the fingerprint found: iproute2 version,
  kernel, architecture, userland (`gnu`, `busybox`, or `unknown` when none of
  the tools otto looks for was found) and privilege (`root`, `elevated` or
  `no privilege`). `no sch_netem` or `sch_netem ?` follows when the netem
  module wasn't found. A version otto couldn't read shows as `?`. A
  middlebox that impairs both directions lists both interfaces.
- **`proven range:`** compares each of those with the versions otto has been
  proven on ({doc}`../known-good`). The labels are defined on
  [the verdicts page](../check-verdicts.md#proven-range-labels). A label
  never changes a verdict: `kernel older` in the sample only means this
  host's kernel predates every kernel otto has been proven on.
- **Sweep lines**, if any, come next. Each names a leftover the sweep found
  before the check started, and says whether otto removed it (`swept …`)
  or left it because it may belong to a check running right now
  (`left …`). With `--live`, the first host's block also gets the listener
  sweep's lines, and a `reusing leftover listener …` line for each port
  where a leftover answered this run's probes (see
  [Cleanup](#cleanup-and-leftovers)).
- **A shared `hint:` line**, when every row of the block has the same hint
  (for example, every row `skipped` because otto couldn't become root), is
  printed once here instead of under each row; see [Reading the
  table](../check-verdicts.md#reading-the-table) for how otto decides what
  to collapse, on this line and under each row.
- **The table** has one row per feature and one column per pass: `sandbox`,
  plus `live` with `--live`. A row that passes everywhere is one line. The
  `detail` column, and the `ran:`/`said:`/`hint:` lines a non-`pass` cell
  gets beneath it, follow [Reading the
  table](../check-verdicts.md#reading-the-table). Live cells' `ran:` lines
  show the impair and repair steps as `impair_link …` and `repair_link …`,
  and with `--live` every evidence line starts with its pass's name
  (`sandbox ran:`, `live ran:`) so you can tell which one it belongs to.
- **The summary line** counts every cell, sandbox and live, by verdict.

`-v` adds an `output:` block after each cell that printed anything, passing
cells included: the raw ping replies, the tree `tc` read back, and each
timed probe's clock readings. In the sample above, `live output:` under
`rate` would show the two clock readings behind the 612 kbit/s.

## Previewing: `--dry-run`

The global `--dry-run` (`-n`) contacts no device and prints what a real run
would do:

```console
$ otto -n --lab unix link check edge --from test1 --live --feature delay,rate
dry run edge:
  placement a->b on test1/eth1.100
  would fingerprint test1 (one read-only command) and check it can become root
  would sweep leftover otto-check-* namespaces older than 45 min on test1; a younger one may be a running check's, and would be left
  would build netns otto-check-<id> on test1 (the /30 of 198.18.0.0/15 <id> picks: its first address on ock<id> ↔ its second inside) and test: read-back, delay, rate
  would run echo listeners inside it on tcp 5205, 5211, 5299
  would sweep leftover otto-check processes older than 45 min on test1 and test2; a younger one may be a running check's, and would be left
  would impair edge a->b on test1/eth1.100 for about 7s (read-back, delay, rate), each step with expire 60s
  would run echo listeners on test2 10.10.202.12 tcp 5205, 5211, 5299 for a->b
  no device was contacted — nothing was measured
```

The plan names every scratch resource the run would use: each host's
namespace and its addresses, and the TCP ports its echo listeners take,
inside the namespace and, with `--live`, on each far endpoint. Listener
lines appear only when a requested row needs them (`rate`, `port range`,
`side`).

For an in-path link the plan can't name the middlebox's interfaces, since
they're resolved from its live address table, and it says so
(`<resolved at run time>`). A refused link is still refused, because
refusal needs no device. `--report` writes nothing in a dry run, and the
output says so. See {doc}`../dry-run` for what every command's dry run
promises.

## Cleanup and leftovers

Everything the check creates carries a random six-hex-digit id. Each host's
sandbox has its own: the namespace `otto-check-<id>`, its veth pair
`ock<id>`, and the echo listeners inside it, whose command lines carry
`otto-check-<id>`. A `--live` cycle picks one more id for the listeners it
runs on the link's far endpoints. All of it is
removed when the check finishes, whether it passed, failed or was
interrupted. Each live impairment is repaired straight after its step, and
its 60-second expire clears it even if otto is killed.

A check that was killed outright can still leave something behind, so every
run starts with a sweep:

- **Namespaces.** Before building its sandbox, otto removes every namespace
  on the host whose name starts with `otto-check-` and that was created longer
  ago than [the sweep's age bound](../check-verdicts.md#leftovers-and-concurrent-checks), by the host's own clock. It says so under the heading:
  `swept leftover sandbox otto-check-1a2b3c on test1 (earlier or concurrent
  run, created 52 min ago)`.
- **Listeners (`--live` only).** Before the live cycle, otto kills every
  process on either endpoint whose command line contains `otto-check-`
  followed by six hex digits, once that tag's oldest process started longer
  ago than that bound. It prints one line per tag: `swept otto-check process
  otto-check-1a2b3c on test2 (earlier or concurrent run, started 52 min
  ago)`. An endpoint whose `ps` can't list processes that way (a busybox
  `ps` built without `-o` support) isn't swept, and the run says `could not
  list processes on test2; sweep skipped`.

A younger namespace or tag may belong to a check running right now, so both
sweeps leave it and print a `left …` line instead, for example `left
otto-check-3fa9c1 namespace on test1 (created 40 s ago — may be a running
check)`. A young leftover `--live` listener holds a port this run's own
listener needs, so its probes reach the leftover instead, and the run says
`reusing leftover listener on …`. How the age rule works, why the reused
listener's answers still count, and how to clear a young leftover by hand,
is under
[Leftovers and concurrent checks](../check-verdicts.md#leftovers-and-concurrent-checks).

Two sandbox passes on the same host at once don't disturb each other: each
has its own namespace and addresses, and neither sweep takes the other's.
Don't run two `--live` checks of the same link at once, though: both would
impair it.

## When a row isn't `pass`

Start with the `detail` column and the `hint:` line. Common causes:

**`unmeasured` with `noisy-baseline: measured loss …, σ … ms` on every
measured row.** The baseline ping
lost packets or varied by more than 5 ms. In the sandbox the path never
leaves the host, so the host itself is too busy: another workload, or a VM
short of CPU. Run again when it's quieter. On the live pass, the path
between the endpoints is lossy or congested, or something rate-limits
ping.

**A BusyBox host.** The heading shows `busybox`, and the iproute2 version
is `?`, because BusyBox's `tc` is a much smaller program than iproute2's.
BusyBox's `ip` usually has no `netns`, so every sandbox row is `skipped`
with `needs ip netns support`. Where the sandbox does build, a feature
BusyBox's `tc` can't apply is `unsupported`, and its complaint is on the
`said:` line. An older BusyBox `ping` may reject the fractional interval
otto pings with (`-i 0.01`), which reads as `loss` `fail: no ping replies`;
the usage error it printed shows under `-v`. Install iproute2 on that host
to check it properly.

**Every applying row `unsupported`.** The netem kernel module is probably
missing. When the fingerprint found no module, the heading says `no
sch_netem` and each row's hint names it; the `said:` line is `tc`'s
complaint, usually that it doesn't know the qdisc kind. Load the module
with `modprobe sch_netem`. Some distributions ship it in a separate
package, such as Ubuntu's `linux-modules-extra-$(uname -r)`.

**Every sandbox row `skipped`.** The hint names the missing prerequisite:
a way to become root, `ip netns`, or `tc` (see
[What each host needs](#what-each-host-needs)). With
`could not build the sandbox netns`, a setup command failed; `-v` shows what
it printed. On an in-path link, `could not resolve <middlebox>'s interfaces
from its address table` means `ip -o addr show` failed there, so otto
couldn't tell which interfaces to name; the row shows what it printed.

**`read-back` or `expire` `fail` with `reading the tree back failed`.**
`tc` accepted the impairment, but reading it back failed. The `ran:` line is
the read, and `-v` shows what it printed.

**The link is refused.** otto can't impair a link without a named interface
on the endpoint each direction starts from, and the links otto derives from
`hop` never have one. Declare the link in `lab.json`'s `links` section with
an interface on each endpoint (see {ref}`lab-links` in
{doc}`../../configuration/lab-config`), then check it by its new name.

**`rate` passes in the sandbox but fails live.** Real NICs segment traffic
in hardware (TSO and GSO), so netem sees fewer, larger packets than the
wire carries, and a multi-queue interface can spread traffic in ways a veth
never does. Both can move the shaped rate outside the ±25% band. Try again
with segmentation offload off on that interface
(`ethtool -K <interface> tso off gso off`), and include `--report` when you
tell otto's developers about it.

(link-check-mtu-black-hole)=
**`rate` fails live with `probe did not finish within 15 s` while every ping
row passes.** This can be a path-MTU black hole: something on the path
silently drops full-size packets, and the rate row is the only one that
sends them (pings, connects and the one-byte echoes are all small). The
transfer stalls until the probe gives up, and its output says how many
bytes came back (`echoed … of 262144 bytes`). otto's own bed hit
this: its VLAN sub-interfaces kept the default 1500-byte MTU on a network
that drops a tagged frame carrying a full 1500-byte packet. To test, ping the far endpoint's address on the link with
fragmentation forbidden, from the near one:
`ping -M do -s 1472 <far ip>` (a full 1500-byte packet) is lost while
`ping -M do -s 1468 <far ip>` answers. The fix is to lower that interface's
MTU to what the path carries (`ip link set <interface> mtu 1496` on both
ends, in the bed's case) or to fix the network so it carries full-size
tagged frames.

**`fail` with nothing above explaining it.** That is what otto's developers
most want to hear about: your host accepted the command, and the result was
wrong. [Send a report](#sending-a-report).

## Sending a report

When to send one, and how, is on the known-good page:
[Sending a report](../known-good.md#sending-a-report).

## From Python

`otto.link.check_link(lab, "edge", live=False, features=None,
from_host=None)` runs the same check and returns the `LinkCheckReport` the
CLI renders and `--report` writes. To run several at once, give each its
own `Lab` (see [Leftovers and concurrent
checks](../check-verdicts.md#leftovers-and-concurrent-checks)). See the
{doc}`API reference <../../api/link>`.
