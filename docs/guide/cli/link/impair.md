# otto link impair

```text
otto link impair <link> [--delay <time>] [--jitter <time>] [--loss <percent>] [--rate <rate>]
                         [--corrupt <percent>] [--duplicate <percent>] [--reorder <percent>]
                         [--from <host>] [--expire <seconds>]
```

```bash
otto --lab veggies link impair edge --delay 50
otto --lab veggies link impair edge --loss 2 --delay 10
otto --lab veggies link impair edge --rate 10mbit --from carrot_seed
otto --lab veggies link impair edge --expire 300 --loss 5
```

`<link>` accepts a link's id or its `name` (the same value when a `name` is
declared — see {ref}`lab-links` in {doc}`../../configuration/lab-config`); both tab-complete
from the loaded lab.

| Option | Description |
| ------ | ----------- |
| `<link>` (argument) | Link id or name. |
| `--delay` | Delay: **bare number = milliseconds**, or an explicit `us`/`ms`/`s` suffix. |
| `--jitter` | Jitter, same units as `--delay`. Requires a delay — given now, or already applied to this placement. |
| `--loss` | Packet loss: **bare number = percent**, or a `%` suffix. |
| `--rate` | Rate limit. **No bare-number form** — an explicit tc unit is required (`kbit`, `mbit`, `gbit`, `bps`, `kbps`, `mbps`, `gbps`, …); there is no natural default for bandwidth, so an unsuffixed value is a usage error. |
| `--corrupt` | Corruption: bare number = percent, or a `%` suffix. |
| `--duplicate` | Duplication: bare number = percent, or a `%` suffix. |
| `--reorder` | Reorder: bare number = percent, or a `%` suffix. Requires a delay (given now, or already applied). |
| `--from` | Narrow to the direction *originating* at this host. Omitted, **both directions** are impaired. Must name one of the link's two endpoint hosts (never the in-path middlebox — see [In-path impairment](in-path.md#in-path-impairment)); naming anything else is rejected with an error that names the link's real endpoints. |
| `--expire` | Auto-clear this impairment after N seconds (integer, ≥ 1). Opt-in — see {ref}`auto-clearing <expire-auto-clearing>` below. |

At least one of the seven parameter options is required — `impair` with none
of them (only `--from`/`--expire`) is a usage error, since there would be
nothing to apply.

## Both directions and the RTT math

By default `impair` places the **same** merged parameters independently on
both directions' placements — A→B and B→A each get their own netem qdisc.
That means `--delay 50` doesn't add 50 ms to a round trip, it adds 50 ms to
*each leg*: a client on one end sees 50 ms out and 50 ms back, i.e. **100 ms
of added RTT**. `--from carrot_seed` restricts to the one direction
originating at `carrot_seed`, leaving the other leg — and the far end's view
of RTT — untouched.

## Re-impairing: merge, per-param last-one-wins

Impairing an already-impaired placement **merges** rather than replaces:
otto reads the placement's current netem state, overlays only the
parameters given on *this* call, and replaces the qdisc with the result.
Worked example:

```bash
otto --lab veggies link impair edge --delay 20
# placement is now: delay 20ms

otto --lab veggies link impair edge --loss 2 --delay 10
# placement is now: delay 10ms loss 2%  — delay overridden, loss added
```

## Zero clears

Passing a parameter its **zero value** — `--loss 0`, `--delay 0`, `--rate 0`
— clears just that one parameter on the merge, rather than "setting" it to
zero:

```bash
otto --lab veggies link impair edge --loss 0
# placement is now: delay 10ms  — loss cleared, delay untouched
```

Clearing the *last* remaining parameter this way removes the qdisc entirely
(`tc qdisc del`) — the same end state as `otto link repair` for that one
placement.

Every mutation is **verified**: after applying, otto re-reads the placement
and compares it against what was just merged in. A mismatch — or any
placement failing mid-way across a multi-placement `impair` (e.g. the far
endpoint is unreachable) — rolls every placement already touched in *this*
call back to its prior state before raising. There is never a half-applied
impairment left behind.

(expire-auto-clearing)=

## `--expire`: auto-clearing

`--expire` is opt-in; **the default is indefinite** — an impairment applied
without `--expire` stays until `otto link repair` clears it, which matters
for long-running tests. Given, `--expire <seconds>` launches a detached,
sentinel-tagged timer process on each impaired placement's host (`sleep N`
then clear the qdisc) that survives otto exiting. Every `impair` or `repair`
call first cancels any existing timer for the placements it touches, so a
later indefinite re-impair is never wiped out by a stale timer, and a
repeated `--expire` restarts the countdown rather than stacking timers.

