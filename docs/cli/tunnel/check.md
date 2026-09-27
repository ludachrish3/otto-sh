# otto tunnel check

```text
otto tunnel check --hosts <h0[@if],h1[@if],...,hn-1[@if]> --port <P> [--protocol tcp|udp|both]
                  [--dest <host[@if]>] [--carrier <name>] [--report <path>] [--verbose]
```

```bash
otto --lab unix tunnel check --hosts test1@eth2,test2@eth2,test3@eth2 --port 15300 --report check.json
otto --lab unix tunnel check --hosts test1@eth2,test2@eth2 --port 15300 --dest test3 --protocol tcp
otto -n --lab unix tunnel check --hosts test1@eth2,test2@eth2,test3@eth2 --port 15300
```

Nothing is written unless you pass `--report`, so pass it the first time:
the file is what you attach if a row turns out to need a report.

`otto tunnel check` tells you whether a tunnel can be built on one path in
your lab, and whether TCP and UDP payloads get through it intact, in both
directions. It takes the same arguments as [`otto tunnel add`](add.md), so a
path that checks clean can then be built for real with the same command line.
A tunnel depends on things that vary from lab to lab: which tools each hop
has, their versions, the firewalls and MTUs between the hops. So instead of
hoping a tunnel will work, run this once when you plan a new path, add a hop,
or see a tunnel misbehave.

On this page every host in `--hosts` is a **hop**, and the first and last
hops are the tunnel's two endpoints. otto builds a throwaway tunnel on the
path, on a spare port of its own,
delivering to small echo listeners it starts for the purpose. It pushes
payloads through it and checks every byte that comes back. Then it removes
the tunnel and the echoes. Your service port is looked at, never used, so
nothing already running on it is disturbed.

It's a setup-time proof of one path, not a CI gate. Probes run one at a time,
and every command otto sends costs a round trip to the host. The payloads
themselves are quick: a round trip through the tunnel takes about a
millisecond on otto's own bed, and the TCP bulk transfer takes milliseconds.
Each UDP bulk row waits about 3 seconds by design, because UDP has no end of
stream to say the reply is complete, so a check of both protocols spends
about 6 seconds there. On a quiet path the whole check takes little more than
that: on otto's bed, checking both protocols over three hops takes about 7.5
seconds, and the whole command, with otto starting up and connecting to the
hosts, about 9.

| Option | Description |
| ------ | ----------- |
| `--hosts` | Ordered, comma-separated `host[@iface]` path, two or more hosts, exactly as for [`otto tunnel add`](add.md). The first and last are the tunnel's endpoints. `@iface` picks which of a host's interfaces the tunnel uses. |
| `--port` | The service port you intend to tunnel. It is checked on the first and last hop, and never bound. See [The service port and the scratch port](#the-service-port-and-the-scratch-port). |
| `--protocol` | `tcp`, `udp`, or `both` (the default, unlike `tunnel add`'s `tcp`). `both` checks TCP, then UDP. Anything else is a usage error (exit 2). |
| `--dest` | Deliver the far end's traffic to this host instead, as with `tunnel add`. How much of that last leg can be proven depends on the host: see [Checking a `--dest`](#checking-a---dest). |
| `--carrier` | The carrier the throwaway tunnel is built with; default `socat`. See [Custom carriers](../../cookbook/network-api.md#custom-tunnel-carriers). |
| `--report` | Also write the full result as JSON to this path. See {doc}`../check-verdicts`. |
| `--verbose`, `-v` | Also print every probe's raw output. |

Each row of the result is a verdict: `pass`, `fail`, `unsupported`,
`unmeasured` or `skipped`. What each means, the exit codes, the report file,
and the proven-range labels are defined once, on {doc}`../check-verdicts`.
The versions otto has been proven on are listed on {doc}`../known-good`.

## What each hop needs

Everything [`otto tunnel add` needs](endpoints.md#host-requirements): `bash`
and `socat` on every hop. The probes also use `cksum`, `mktemp`, `head`, `tr`
and `wc`, standard tools most userlands include, and otto finds its own
echoes with `ps -eo pid= -eo etime= -eo args=`, which a busybox `ps` built
without `-o` support can't run (see [Cleanup and
leftovers](#cleanup-and-leftovers)). otto runs everything as the login user
from the host's `creds`; nothing in this check needs root.

If any hop lacks one of these tools, otto builds nothing. Every row but
`service port` is `unmeasured`, `teardown` included, with a detail such as
`missing-tool: socat not found on test2`. The hint says what needs the tool:
for `socat` or `bash`, that `otto tunnel add` would refuse this path too; for
the others, that the check's probes need it and `tunnel add` itself does not.
(A split `--dest`'s `last segment` row is decided on its own; see
[Checking a `--dest`](#checking-a---dest).) That run exits 0, since `unmeasured` is not a failure, so read the
`proven:` line as well as the exit code: it says `not measured`.

For timing, bash 5.0 or newer on every hop that sends a probe. otto times
each trip with bash's `$EPOCHREALTIME`, on the hop that sent it, and older
bash doesn't have it (CentOS 7 ships bash 4.2). Without it on the first hop,
the `rtt` row is `unmeasured` with the reason `no-clock`. On any hop without
it, the segment row sent from that hop says `5/5 echoed` with no time, and
that leaves the `rtt` row's segment sum unmeasured. Every payload verdict
still stands.

## What it builds, and where

### The service port and the scratch port

`--port` is the port you will give `tunnel add` later. The real tunnel would
listen on it on the first and last hop, so that is the only place the check
looks: it lists the protocol's listening sockets there (`ss`, or `netstat`
where there is no `ss`), and the `service port` row fails if anything holds
`--port`. It never binds `--port`. A `--dest` is only connected to, so a
listener on the dest's `--port` is your service there, not a conflict.

The throwaway tunnel is built on a **scratch port** instead. otto picks it
at random from the free ports above every hop's ephemeral port range (the
range the kernel hands out for outgoing connections,
`net.ipv4.ip_local_port_range` on Linux) — the same floor, and the same
random-not-lowest draw, that `tunnel add` uses for its own carrier ports
(see **Previewing: `--dry-run`** in {doc}`index`). Drawing at random, rather
than always the lowest, is what lets two checks on the same hosts run at
once without landing on the same port (see [Cleanup and
leftovers](#cleanup-and-leftovers)). It still skips any port a hop already
listens on and `--port` itself. The heading of the output shows it, as
`(scratch <port>)`.

### The echoes

The echoes are socat listeners that send back every byte they receive. For
each protocol, otto starts:

- one **segment echo** per pair of neighboring hops, on the second hop of the
  pair, at the address the tunnel will use there, for a single trip, and
  killed straight after;
- the **fwd echo** on the last hop at `127.0.0.1:<scratch>`, where the
  throwaway tunnel delivers (or, for a `--dest` that gets the full proof, at
  the dest's own address);
- the **rev echo** on the first hop at `127.0.0.1:<scratch>`, where the
  reverse direction delivers.

Each echo starts detached, the way a tunnel process does (`systemd-run
--user`, or `setsid` where that isn't available), with its process name
replaced by a tag. `ps` on the host shows it:

```text
otto-check:v1:<run>:<tunnel id>:<protocol>:<role>:<host>
```

`<run>` is six hex digits, new for every run. `<tunnel id>` is the throwaway
tunnel the echo serves, or `-` for a segment echo. `<role>` is `fwd-echo`,
`rev-echo` or `segment`. The echoes never exit on their own: the check's
teardown kills them, and the tag is how it, and any later run's sweep, finds
them (see [Cleanup and leftovers](#cleanup-and-leftovers)).

The throwaway tunnel itself is built by the same code as `otto tunnel add`,
so its processes carry ordinary tunnel tags, and `otto tunnel list` shows it
while the check runs.

## What each row proves

The check runs one protocol at a time. `fwd` is traffic that enters the
tunnel on the first hop and leaves it on the last; `rev` is the reverse
direction. Every tunnel carries both. For each protocol, the rows are:

| Row | What otto does | Passes when |
| --- | --- | --- |
| `service port` | Lists the protocol's listening sockets on the first and last hop. | Nothing listens on `--port` on either. |
| `segment <a> → <b>` | Starts a segment echo on `<b>`, and sends it 5 round trips of 1 fresh random byte from `<a>`, on one connection or flow, over the protocol being checked: TCP for a TCP tunnel, UDP for a UDP tunnel. One row per pair of neighboring hops. | All 5 come back. The detail shows the median round-trip time. |
| `build` | Starts the fwd and rev echoes, then builds the throwaway tunnel on the scratch port with `otto tunnel add`'s code, including its check that every process came up. | The tunnel is built. The detail shows its carrier ports (the ports its hops pass traffic on, fwd then rev) and its id. |
| `fwd 1 B`, `fwd 1400 B` | Sends 5 round trips of fresh random bytes into the tunnel on the first hop, all on one TCP connection or one UDP flow. The fwd echo sends each one back through the tunnel. It stops at the first trip that doesn't come back. | All 5 come back byte for byte. |
| `fwd bulk` | Sends one random payload of 64 KiB (TCP) or 65000 B (UDP) the same way. A UDP payload leaves as one datagram, which the network splits into IP fragments. | Every byte comes back and its checksum matches the one sent. |
| `rev 1 B`, `rev 1400 B`, `rev bulk` | The same three, sent into the tunnel on the last hop, echoed by the rev echo on the first hop. | As for `fwd`. |
| `rtt` | Takes the median of the `fwd 1 B` round trips and shows it next to the sum of the segments' median round trips. Both are 5 trips of 1 byte on one flow, so you can see what the tunnel adds to the bare path. | It measured anything. The row is informational. |
| `list` | Runs the same discovery as `otto tunnel list`. | The throwaway tunnel is listed as `ok`. That proves its process tags describe it, which `tunnel list` and `tunnel remove` both rely on. |
| `teardown` | Removes the tunnel with `otto tunnel remove`'s code, kills this run's echoes, and scans every hop again. | Nothing this run started is still running. |
| `last segment → <dest>` | Only for a `--dest` that gets the split proof. See [Checking a `--dest`](#checking-a---dest). | TCP: a handshake completes. |

If a segment fails, no tunnel is built across it, so every row from `build`
to `list` is `skipped`. `teardown` still runs.

Every probe gives up on its own. A TCP connect that gets no answer stops after 5
seconds, and a trip or transfer stops after 15 seconds with no reply, so a
path that drops everything fails its row rather than hanging.

## Checking a `--dest`

With `--dest`, the tunnel's far end delivers to another host instead of its
own loopback, as described in [Relaying with `--dest`](add.md#relaying-with---dest).
What otto can prove about that last leg depends on what the dest runs.

**Full proof: the dest runs socat and bash.** otto fingerprints the dest
(one probe command, the same as for every hop) to find out. If both are there, the dest is treated
like one more hop: it gets a `segment <last hop> → <dest>` row, the fwd echo
runs at the dest's own address, the tunnel is built with `--dest`, and the
dest is swept and cleaned up like every hop. The `proven:` line says, for
example, `whole path payload-verified to test3 (tcp)`.

**Split proof: the dest runs nothing of otto's.** That is either a dest the
lab declares with
[`has_bash`](../../configuration/lab-config.md#common-optional) set to
`false` (an embedded target, say), which otto never contacts at all, or one
whose fingerprint shows no socat or no bash. Nothing is started on it and
nothing is swept there. The throwaway tunnel
delivers to the fwd echo on the last hop instead, which proves every leg otto
manages. The last leg gets its own row, `last segment → <dest>`:

- **TCP:** from the last hop, otto opens a TCP connection to `--port` on the
  dest and closes it without sending anything. This is the only time the
  check touches `--port`. otto closes its sending side at once, then reads and
  discards whatever the device says, until the device closes or has been
  quiet for 2 seconds. So the device sees a clean close, never a reset. That
  matters for a console that greets its clients: a reset in the middle of its
  greeting can wedge it. A device that never stops talking is cut off after 7
  seconds. A completed handshake is `pass`, but it proves only
  that the last hop reaches your service on the device, not that a payload
  gets there intact. If nothing answers, the row fails, and the hint asks
  whether the device's own service is listening on `--port`.
- **UDP:** `unmeasured`, with the reason `no-reply-oracle`: nothing on the
  device would answer, so there is no reply to check.

The `proven:` line then says exactly what was shown, for example
`hop chain: payload-verified; last segment to zephyr: TCP handshake only; UDP
not measured`. Payload proof on that last leg is
[issue #440](https://github.com/ludachrish3/otto-sh/issues/440).

## Reading the output

This is `otto --lab unix tunnel check --hosts test1@eth2,test2@eth2,test3@eth2
--port 15300`, on a path where everything works:

% Captured from a real bed run on 2026-09-26: check_tunnel over the unix bed's
% eth2 data plane (test1, test2, test3), rendered by tunnel_sections +
% render_sections at width 120 with no colour, trailing spaces trimmed, never
% typed by hand, with proven.json at revision 2026-09-26.

```text
test1 192.168.1.11 → test2 192.168.1.12 → test3 192.168.1.13  :15300 (scratch 65335)
test1  socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · kernel 6.8.0-86-generic · aarch64 · gnu
       proven range: kernel within · aarch64 within · gnu within · socat within · bash within · launcher within
test2  socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · kernel 6.8.0-86-generic · aarch64 · gnu
       proven range: kernel within · aarch64 within · gnu within · socat within · bash within · launcher within
test3  socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · kernel 6.8.0-86-generic · aarch64 · gnu
       proven range: kernel within · aarch64 within · gnu within · socat within · bash within · launcher within
bulk: tcp 64 KiB · udp 65000 B
proven: hop chain: payload-verified (tcp, udp)

feature                tcp   udp   detail
service port           pass  pass  free
segment test1 → test2  pass  pass  tcp 5/5 echoed, median 0.3 ms round trip
                                   udp 5/5 echoed, median 0.5 ms round trip
segment test2 → test3  pass  pass  tcp 5/5 echoed, median 0.3 ms round trip
                                   udp 5/5 echoed, median 0.4 ms round trip
build                  pass  pass  tcp carriers 65472/61685 · tun-7c33380c105d-65335
                                   udp carriers 62290/65506 · tun-587605b11d0d-65335
fwd 1 B                pass  pass  5/5 echoed byte for byte
fwd 1400 B             pass  pass  5/5 echoed byte for byte
fwd bulk               pass  pass  tcp got 65536 of 65536 B
                                   udp got 65000 of 65000 B
rev 1 B                pass  pass  5/5 echoed byte for byte
rev 1400 B             pass  pass  5/5 echoed byte for byte
rev bulk               pass  pass  tcp got 65536 of 65536 B
                                   udp got 65000 of 65000 B
rtt                    pass  pass  tcp through tunnel 0.8 ms; segments sum 0.6 ms
                                   udp through tunnel 0.9 ms; segments sum 0.9 ms
list                   pass  pass  ok
teardown               pass  pass  nothing tagged survived
path: 26 pass
```

The output has four parts:

- **The heading** names each hop and its address on this path, then
  `→ dest <host> <address>` with a `--dest`, then your `--port` and the
  scratch port the check built on.
- **Each hop's lines.** The first gives what the fingerprint found: socat and
  bash versions, `clock yes` or `clock no` (whether bash has
  `$EPOCHREALTIME`), the launcher (`systemd-run` or `setsid`), kernel,
  architecture and userland (`gnu`, `busybox`, or `unknown`). A version otto
  couldn't read shows as `?`. The fingerprint is one command per host. It
  only reads, except that to learn the launcher it runs `true` in a
  short-lived `systemd-run --user` unit, as `tunnel add` would. The indented `proven range:` line under it
  compares those with the versions otto has been proven on
  ({doc}`../known-good`); the labels are defined on
  [the verdicts page](../check-verdicts.md#proven-range-labels). A label
  never changes a verdict. A dest that was fingerprinted gets the same two
  lines. One the lab says has no shell gets a single line:
  `<dest>: runs nothing of otto's — not fingerprinted`.
- **The `bulk:`, sweep and `proven:` lines.** The `bulk:` line gives each
  protocol's bulk size, since the table calls both rows `fwd bulk` and
  `rev bulk`. Sweep lines, if any, name each leftover echo the sweep found
  before the check started, and say whether otto removed it (`swept …`,
  or `kept …` when its tunnel's removal couldn't be verified) or left it
  because it may belong to a check running right now (`left …`); see
  [Cleanup and leftovers](#cleanup-and-leftovers). The `proven:` line says in one sentence what the run proved.
- **The table** has one column per protocol. The `detail` column, the
  `ran:`/`said:`/`hint:` lines beneath a non-`pass` cell, and how a row
  whose protocols disagree or a repeated hint collapses, all follow [Reading
  the table](../check-verdicts.md#reading-the-table) — the same renderer
  `otto link check` uses. The summary line counts every cell by verdict.

Here is a path that fails: `otto --lab unix tunnel check --hosts
test3@eth2,test2@eth2,test1@bbeth-1350 --port 15300 --protocol tcp`. test1's
`bbeth-1350` address is on a link test2 isn't part of, so test2 has no route
to it and nothing answers:

% Captured from a real bed run on 2026-09-26, rendered the same way as the
% sample above, never typed by hand. The dead segment comes from the arguments
% alone: bbeth-1350 is a real test1 interface that test2 cannot reach.

```text
test3 192.168.1.13 → test2 192.168.1.12 → test1 198.51.100.18  :15300 (scratch 63650)
test3  socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · kernel 6.8.0-86-generic · aarch64 · gnu
       proven range: kernel within · aarch64 within · gnu within · socat within · bash within · launcher within
test2  socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · kernel 6.8.0-86-generic · aarch64 · gnu
       proven range: kernel within · aarch64 within · gnu within · socat within · bash within · launcher within
test1  socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · kernel 6.8.0-86-generic · aarch64 · gnu
       proven range: kernel within · aarch64 within · gnu within · socat within · bash within · launcher within
bulk: tcp 64 KiB
proven: hop chain: not proven — see the failing rows

feature                tcp      detail
service port           pass     free
segment test3 → test2  pass     5/5 echoed, median 0.3 ms round trip
segment test2 → test1  fail     test2 → test1: no echo from 198.51.100.18:63650
                                ran: bash -c 'export LC_ALL=C; trap '"'"''"'"' PIPE; coproc S { exec socat -b 65535 -T
                                15 -t 15 - TCP4:198.51.100.18:63650,connect-timeout=5 2>&1; }; m=0; for ((i=1; i<=5;
                                i++)); do p=$(tr -dc a-z0-9 </dev/urandom 2>/dev/null | head -c 1); if [ ${#p} -ne 1 ];
                                then echo "trip $i nonce got=${#p}"; bre … (+459 chars; full command with -v or in
                                --report)
build                  skipped
                                hint: segment test2 → test1 failed, so no tunnel was built across it
fwd 1 B                skipped
fwd 1400 B             skipped
fwd bulk               skipped
rev 1 B                skipped
rev 1400 B             skipped
rev bulk               skipped
rtt                    skipped
list                   skipped
teardown               pass     nothing tagged survived
path: 3 pass · 1 fail · 9 skipped
```

The failing row names the hop pair and the address and port that didn't
answer. That port is the scratch port the check built on (63650 here), not
your `--port`. Its `ran:` line is the probe, sent from test2. Check the
address first: `@iface` picked it, and here it's the wrong one. Every row
that needed a tunnel across that segment is `skipped`, the hint under the
first of them says why, and the run exits 1. What to try is under
[When a row isn't `pass`](#when-a-row-isnt-pass).

`-v` adds an `output:` block after each cell that printed anything, passing
cells included: each trip's transcript with its clock readings, the bulk
transfer's byte counts and checksum verdict, and the socket listings.

## Previewing: `--dry-run`

The global `--dry-run` (`-n`) contacts no device and prints what a real run
would do:

```console
$ otto -n --lab unix tunnel check --hosts test1@eth2,test2@eth2,test3@eth2 --port 15300
dry run test1@eth2 → test2@eth2 → test3@eth2:
  would fingerprint test1, test2, test3 (one probe command each)
  would sweep leftover otto-check echoes older than 45 min on test1, test2, test3, and remove the throwaway tunnels they name, scanning the lab for them the way `otto tunnel remove` does; a younger one may be a running check's, and would be left
  would check --port 15300 is free for tcp, udp on test1 and test3 (looked at, never bound), and allocate a scratch service port at random from above every hop's ephemeral range to build on instead
  would run a segment echo on test2 at 192.168.1.12:<scratch> and send it 5 round trips of 1 B from test1
  would run a segment echo on test3 at 192.168.1.13:<scratch> and send it 5 round trips of 1 B from test2
  would run the fwd echo on test3 at 127.0.0.1:<scratch> and the rev echo on test1 at 127.0.0.1:<scratch>
  would build a throwaway tunnel test1 → test2 → test3 on the scratch port with carrier socat, for tcp, udp, one protocol at a time, and check `tunnel list` sees it ok
  would send 5 round trips each of 1 B and 1400 B and one checksum-compared bulk of 64 KiB (tcp) / 65000 B (udp), fwd then rev
  would remove the tunnel and every echo, then re-scan every hop to verify nothing tagged survives
  no --dest: the tunnel would deliver to the fwd echo on test3
  no device was contacted — nothing was measured
```

The scratch port shows as `<scratch>`, because a real run picks it from what
the hops report. With `--dest`, the plan says which proof the dest would
get: split for a dest the lab says has no shell, or a choice the dest's
fingerprint will make. A path `tunnel add` would refuse is still refused,
since refusal needs no device. `--report` writes nothing in a dry run, and
the output says so. See {doc}`../dry-run` for what every command's dry run
promises.

## Cleanup and leftovers

After each protocol, whatever happened, otto removes the throwaway tunnel
with `otto tunnel remove`'s code, kills the echoes this run started, and
scans every hop again. The `teardown` row fails, naming what it found, if
anything this run started is still running.

If the tunnel's removal can't be verified, otto leaves that tunnel's echoes
running on purpose, and the row says so: `tunnel <id> was left for the next
check's sweep: its echoes still run and name it`. Those echoes are how the
next run finds the tunnel.

An interrupted run tears down too. One Ctrl-C still runs the teardown, within
the teardown deadline (`OTTO_TEARDOWN_DEADLINE`, 10 seconds by default; see
{doc}`../../architecture/lifecycle`). A second Ctrl-C, the deadline running
out, or a `kill -9` leaves what was running for a later run's sweep, once
it's old enough.

A check cut off like that can leave echoes and a tunnel behind, so
every run starts with a sweep of every hop (and of a full-proof dest). It
finds every running `otto-check:v1` echo, removes each throwaway tunnel those
echoes name (scanning the lab for its processes the way `otto tunnel remove`
does), then kills the echoes. The echoes start before the tunnel is
built, and carry its id, so a run killed at any point leaves an echo that
names whatever needs removing. Only tunnels an echo names are removed: your
own tunnels are never touched. Each echo swept gets a line under the
heading, for example:

```text
swept otto-check echo fwd-echo on test3 (earlier or concurrent run, started <n> min ago); its tunnel <tunnel id> was already gone
```

When removing a swept tunnel can't be verified (a process of it survived, or
a host this check sweeps didn't answer), the line starts `kept otto-check
echo …` instead: that tunnel's echoes are left running, so the next sweep
tries it again.

After killing, the sweep scans the hop again, for up to a second while the
killed echoes exit. An echo still running then is never called swept: most
likely another user started it, and your login can't signal it. Its line
reads `could not remove otto-check echo <role> pid <pid> on <host> (kill
refused — started by another user?)`; that user, or root, can kill it by
that pid. If `ps` can't list processes on that second look, the line reads
`could not confirm otto-check echo <role> pid <pid> on <host> went: ps on
<host> lists nothing` instead.

The sweep takes a run's echoes only once that run is older than
[the sweep's age bound](../check-verdicts.md#leftovers-and-concurrent-checks),
counted from its oldest echo on any hop. A younger run may be a check
running right now, so its echoes are left, and so are the tunnels they name,
each with a `left otto-check echo …` line. How the age rule works, and how
to clear a young leftover by hand, is under
[Leftovers and concurrent checks](../check-verdicts.md#leftovers-and-concurrent-checks).

A hop whose `ps` can't list processes with their ages (a busybox `ps` built
without `-o` support) isn't swept, and the run says `could not list
processes on <host>; sweep skipped`. otto can't tell its own echo there from
another run's either, so the row that starts an echo on that hop fails with
`could not confirm the echo is this run's: ps on <host> lists nothing`, and
`teardown` fails with `could not list processes on <host> to confirm this
run's echoes there are gone`.

Concurrent tunnel checks on hosts they share are safe: the sweep leaves a
running check's echoes alone, and every port two runs need — the check's own
scratch port, and the throwaway tunnel's two carrier ports, picked by the
same `otto tunnel add` code the `build` row calls — is a random pick from
whichever free ports the budget has at that moment, typically thousands,
rather than always the lowest one. Two runs settling on the same one is rare
rather than routine — 1 in however many free ports were on offer for that
pick. If it does happen anyway, it fails loudly, never a silent cross-talk.
On a scratch-port collision, the later run's echo can't bind the address
the other run's echo already holds, so it exits at once. Before trusting
an address, otto checks that the echo it just started is the one running
there. So the row that started that echo, a `segment` row or `build`, fails
with `scratch port <scratch> on <host> is held by something else (another
check?)`. That check looks at `ps` once, though, and an echo whose bind has
just failed can still be listed for a moment before it exits. Caught in
that moment, it passes, and the probes reach the other run's echo instead;
the `teardown` row then finds this run's echo gone and says so, and the
`proven:` line claims nothing (see below). A
carrier-port collision instead fails `otto tunnel add`'s own post-add
verify, naming the port and what holds it (see [Conflicts and
preconditions](add.md#conflicts-and-preconditions)). Either way, run the
check again: the next run draws a fresh set of ports.

A run that goes on longer than the sweep's age bound can still lose its
echoes and tunnel to another run's sweep — the sweep has no way to know a run using
these hosts right now is somebody's. If that happens, the run finds them
gone at its teardown, and its `teardown` row names
exactly what it no longer found — the tunnel, one or more delivery echoes by
role and host, or both. A sweep removes the tunnel first, so a missing
tunnel is put down to one: `tunnel <tunnel id> was gone before teardown —
removed mid-run, most likely by another check's sweep on these hosts, and
the payload rows above are not evidence`. Delivery echoes missing while
their tunnel was still there may just as well have exited on their own, and
the row says both: `the fwd-echo on <host> was gone before teardown — it
exited, or another check's sweep took it, and the payload rows above are not
evidence`. Either way its `proven:` line says `not proven — its tunnel or
echoes were gone before teardown`, because its payload rows may have measured
nothing of its own. Run it again once the other check has finished.

`otto link check --live` is different: two runs impairing the same link both
drive the same interface, so those still need to run one at a time (see
{doc}`../link/check`, under *Cleanup and leftovers*).

## When a row isn't `pass`

Start with the `detail` column and the `hint:` line. Common causes:

**A `segment` row fails with `no echo from <address>:<port>`.** otto's echo
on the second host was listening, but the first byte sent from the first host
never came back. Most often a firewall on or between the two hosts drops it.
If it says `<n> of 5 trips echoed back` instead, the path works but lost a
trip, which points at loss on the path rather than a firewall.
The segment uses the tunnel's own protocol, so a firewall that passes TCP but
not UDP fails only the `udp` column: a UDP tunnel needs UDP allowed between
its hops (see [Host requirements](endpoints.md#host-requirements)). The port in the
detail is the scratch port, not your `--port`. Allow the protocol on the
ports above each hop's ephemeral range (`net.ipv4.ip_local_port_range`),
where both the scratch port and the real tunnel's carrier ports come from. Also check that
the address is the one you meant: `@iface` in `--hosts` picks which
interface's address the tunnel uses. `-v` shows what the probe printed. If
the detail instead says `the segment echo on <host> never listened on
<address>:<port>`, the echo itself couldn't bind that address on that host:
otto waited 5 seconds for it to show in the host's socket listing, and the
row's `ran:` lines include the listing command (`-v` shows what it printed).
If it says `scratch port <port> on <host> is held by something else
(another check?)`, something was already listening on that address when
the echo started, most likely another check that drew the same scratch
port; run the check again.

**`service port` fails.** Something already listens on `--port` on the first
or last hop, for that protocol. The detail shows the host and the listening
socket, and the real `tunnel add` on that port would fail. Stop whatever
holds it (`otto tunnel list` shows whether it's an otto tunnel), or pick
another port. The rest of the check still ran on the scratch port, so the
other rows still tell you whether the path works.

**`rtt` is `unmeasured` with `no-clock`.** The first hop's bash is older than
5.0, and its heading line shows `clock no`. Only timing is lost: every
payload verdict stands, and the run's exit code doesn't change. Install bash
5.0 or newer on that hop if you want the round-trip times.

**`list` fails with `degraded (<found>/<expected>)` or `not listed`.** otto
built the tunnel, and `tunnel add`'s own check found every process running,
but `otto tunnel list`'s discovery afterwards didn't find all of them.
`tunnel remove` finds tunnels the same way, so a real tunnel on this path
could be left half-removed. Either a tunnel process exited after it started,
or a hop's `ps` doesn't show the process tags. The `teardown` row says
whether removal found everything. This is worth a report.

**The bulk rows fail while the `1 B` and `1400 B` rows pass.** This can be a
path-MTU black hole: something on the path silently drops full-size packets,
and only the bulk rows send them. TCP bulk stalls until the probe gives up,
with fewer bytes back than sent (`got … of 65536 B`). A UDP bulk payload is
split into IP fragments, and losing any fragment loses the whole datagram,
so it reads `got 0 of 65000 B`. A path that drops IP fragments fails the UDP
bulk rows alone. How to confirm a black hole with `ping`, and fix it, is in
{ref}`otto link check's entry <link-check-mtu-black-hole>`; run it between
the hops of the path.

**Every row but `service port` is `unmeasured` with `missing-tool`.** A hop lacks one of the tools in
[What each hop needs](#what-each-hop-needs). The detail names the tools and
the hop, and the hint says whether `otto tunnel add` needs them too. Install them and check again.

**`build` fails.** The detail is `otto tunnel add`'s own error: a missing
tool, a port already taken, or a process that didn't come up. See
[Conflicts and preconditions](add.md#conflicts-and-preconditions). If it
says `could not start` an echo, that one `never listened`, or that the
scratch port `is held by something else`, the echo failed before the tunnel
was built.

**`teardown` fails.** The detail names each process that survived and the
host it's on. A tunnel left behind is removed by the next check's sweep, or
by `otto tunnel remove <id>` now. An echo left behind has the `otto-check:v1`
tag shown in `ps`, and you can kill it by its pid.

**The check stops with an error naming a host.** A host stopped answering in
the middle of the check. That's an error (exit 1), not a verdict, and no
table is printed. Make sure the host is up and run the check again; its sweep
cleans up whatever the interrupted run left.

**`fail` with nothing above explaining it.** That is what otto's developers
most want to hear about: your hosts accepted every command, and the payload
still came back wrong. [Send a report](#sending-a-report).

## Sending a report

When to send one, and how, is on the known-good page:
[Sending a report](../known-good.md#sending-a-report). A tunnel check's file
also holds the scratch port it built on, which proof a `--dest` got, and the
sentence its `proven:` line printed.

## From Python

`otto.tunnel.check_tunnel(lab, hosts, port=P, protocol="both", dest=None,
carrier="socat")` runs the same check and returns the `TunnelCheckReport` the
CLI renders and `--report` writes. `hosts` is a list of `(host_id, iface)`
pairs, with `None` for no interface, and `dest` is one such pair. Use it to
check many paths from one script, one after another or concurrently: runs
on hosts they share are safe (see [Cleanup and
leftovers](#cleanup-and-leftovers)), and may share a `Lab` (see [Leftovers
and concurrent checks](../check-verdicts.md#leftovers-and-concurrent-checks)).
See the {doc}`API reference <../../api/tunnel>`.
