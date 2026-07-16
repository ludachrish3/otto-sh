# otto link

`otto link` inspects and impairs the lab's **static links** — the topology
edges declared in `lab.json`'s `links` section, or derived from each host's
management `hop` (see {ref}`lab-links` in {doc}`../setup/lab-config`). A link is
where `tc qdisc` actually attaches; tunnels (see {doc}`tunnel`) ride *over*
links but are never impaired directly — impairing a link a tunnel happens to
ride affects that tunnel realistically, for free. For why links and tunnels are
modelled as separate underlay and overlay layers, see
{doc}`../../architecture/subsystems/network`.

Every capability below is a plain callable first — `otto link` is a thin CLI
wrapper over `otto.link.impair_link` / `repair_link` / `repair_all` /
`read_link_states`. See [Python API](#python-api) below and the
{doc}`API reference <../../api/link>` to call them directly.

```{note}
Impairment state lives in the kernel, not in otto (see
{doc}`../../architecture/subsystems/network`) — `otto link list` reads it back
live from `tc`. Both directions of a link are impaired by default; `--from`
narrows to one.
```

## Impairing a link: `otto link impair`

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
declared — see {ref}`lab-links` in {doc}`../setup/lab-config`); both tab-complete
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
| `--from` | Narrow to the direction *originating* at this host. Omitted, **both directions** are impaired. Must name one of the link's two endpoint hosts (never the in-path middlebox — see [In-path impairment](#in-path-impairment)); naming anything else is rejected with an error that names the link's real endpoints. |
| `--expire` | Auto-clear this impairment after N seconds (integer, ≥ 1). Opt-in — see {ref}`auto-clearing <expire-auto-clearing>` below. |

At least one of the seven parameter options is required — `impair` with none
of them (only `--from`/`--expire`) is a usage error, since there would be
nothing to apply.

### Both directions and the RTT math

By default `impair` places the **same** merged parameters independently on
both directions' placements — A→B and B→A each get their own netem qdisc.
That means `--delay 50` doesn't add 50 ms to a round trip, it adds 50 ms to
*each leg*: a client on one end sees 50 ms out and 50 ms back, i.e. **100 ms
of added RTT**. `--from carrot_seed` restricts to the one direction
originating at `carrot_seed`, leaving the other leg — and the far end's view
of RTT — untouched.

### Re-impairing: merge, per-param last-one-wins

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

### Zero clears

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

### `--expire`: auto-clearing

`--expire` is opt-in; **the default is indefinite** — an impairment applied
without `--expire` stays until `otto link repair` clears it, which matters
for long-running tests. Given, `--expire <seconds>` launches a detached,
sentinel-tagged timer process on each impaired placement's host (`sleep N`
then clear the qdisc) that survives otto exiting. Every `impair` or `repair`
call first cancels any existing timer for the placements it touches, so a
later indefinite re-impair is never wiped out by a stale timer, and a
repeated `--expire` restarts the countdown rather than stacking timers.

## In-path impairment

By default (**endpoint mode**) a link's two directions land on the netem
placement resolved by their own physical endpoint — the {ref}`lab-links`
`endpoints[].interface`. A link can instead declare an `impair` field: a
**bare host id** naming an in-path middlebox that services the link's
impairment instead:

```json
{
    "name": "dataplane",
    "endpoints": [
        { "host": "carrot_seed", "interface": "eth1.100" },
        { "host": "tomato_seed", "interface": "eth1.200" }
    ],
    "impair": "pepper_seed"
}
```

With `impair` set, both directions place on `pepper_seed` instead of the
endpoints, and the facing interface toward each endpoint is auto-resolved — you
never declare it. See {doc}`../../architecture/subsystems/network` for how that
resolution works and why a middlebox that isn't actually in the path fails
loud.

The link-level `impair` field names only *where* impairment is serviced; the
host-level `impairer` pin (see [Custom impairers](#custom-impairers)) separately
selects *which* `LinkImpairer` a host uses — one field per concern.

```{note}
**Netdev granularity.** A netem qdisc attaches to an *interface*, not a
flow. If two links resolve their placements onto the **same** middlebox
interface — e.g. two endpoints sharing one segment behind the middlebox —
impairing one link impairs both: they share the qdisc, a second `impair`
merges over the first exactly as a re-impair of the same link would, and
`otto link list` will truthfully report both links impaired. There is
currently no flow-scoped (per-destination) impairment on a shared
interface; in the common one-interface-per-segment middlebox layout,
placements never collide.
```

## Port-scoped impairments

Everything above this section impairs a placement's **entire interface** —
every packet traversing that netdev, degraded the same way. `--port` narrows
one `impair` call to a single service's traffic, leaving everything else on
the link clean:

```bash
otto --lab veggies link impair edge --port 5201 --delay 200
otto --lab veggies link impair edge --port 53 --proto udp --loss 5
```

### Selector semantics

`--port N` matches traffic whose **source OR destination** port is `N` —
otto never needs to know which endpoint of the link is running the server,
so one flag covers both directions of a service's traffic. `--proto tcp` or
`--proto udp` narrows to one L4 protocol; omitted, both tcp and udp match.
`--proto` without `--port` is a usage error (exit code 2, `--proto needs
--port.`) — there's nothing for it to narrow.

Omitting `--port` is **not** a new mode: it's exactly today's
whole-interface impairment, byte-identical commands and semantics. Port
scoping is strictly opt-in, per invocation.

| Option | Description |
| ------ | ----------- |
| `--port` | Scope this impairment to one service port (1-65535), matching source OR destination. |
| `--proto` | With `--port`: narrow to `tcp` or `udp`. Omitted, both match. Requires `--port`. |

### Exclusivity: whole-link and port-scoped never mix (v1)

A placement's netdev is either whole-link impaired (today's exact
root-netem shape) or port-scoped (a classful tree of per-selector bands) —
never both. Otto refuses to mix the two on the same placement, and the
error names the remedy:

```bash
otto --lab veggies link impair edge --delay 50
# ... placement now has a whole-link impairment ...
otto --lab veggies link impair edge --port 5201 --delay 200
# Error: link edge has a whole-link impairment — repair it first
```

```bash
otto --lab veggies link impair edge --port 5201 --delay 200
# ... placement now has a port-scoped impairment ...
otto --lab veggies link impair edge --delay 50
# Error: link edge has port-scoped impairments — repair them first or impair with --port
```

Repair enforces the same rule from the other side: `otto link repair edge
--port 5201` against a whole-link impairment raises `link edge has a
whole-link impairment — repair it without --port` — use a bare `otto link
repair edge` instead.

### Multiple selectors: independent params, per-selector merge, cap 8

Each selector carries its own parameter set. Re-impairing a selector merges
over **that selector's own** current state, not the whole netdev's — same
per-param last-one-wins and explicit-zero-clears rules as whole-link
impairment (see [Re-impairing](#re-impairing-merge-per-param-last-one-wins)
and [Zero clears](#zero-clears) above), just scoped narrower:

```bash
otto --lab veggies link impair edge --port 5201 --proto tcp --delay 20
# 5201/tcp is now: delay 20ms

otto --lab veggies link impair edge --port 5201 --proto tcp --loss 2 --delay 10
# 5201/tcp is now: delay 10ms loss 2%  — delay overridden, loss added; other selectors untouched
```

`Selector(5201)` (both protocols) and `Selector(5201, "tcp")` are **distinct
selectors** — the former's filters simply match a superset of the latter's
traffic, so both can coexist on the same port at once, if unusual.

A placement caps at **8 concurrent selectors**; a 9th raises a loud error
naming the link, host, and netdev rather than silently dropping one or
overwriting another. `--expire <seconds>` composes exactly as with
whole-link impairment (see {ref}`auto-clearing <expire-auto-clearing>`
above), but per selector: it auto-clears only that one selector, and a
repeated `--expire` on it restarts only its own countdown — every other
selector's timer (and any whole-link timer, which can't coexist with scoped
state anyway) is untouched.

### Repairing one selector

```bash
otto --lab veggies link repair edge --port 5201 --proto tcp
otto --lab veggies link repair edge --port 5201
otto --lab veggies link repair edge
```

`repair <link> --port N [--proto P]` clears just that one selector —
deleting the whole classful tree if it was the last selector standing — and
cancels only its own timer, leaving every other selector on the placement
untouched. A bare `repair` (no `--port`) still clears **everything**, as
described in [Repairing: `otto link repair`](#repairing-otto-link-repair)
below. `--port` and `--all` don't compose: `--port` repairs one selector on
one link, `--all` sweeps every static link, and passing both is a usage
error.

### Listing: selector rows

`otto link list` prints one indented line per active selector under its
link's normal summary row:

```text
edge  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via -  a->b: port-scoped (1)  b->a: -
  a->b  5201/tcp  delay 200ms
dataplane  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via pepper_seed  a->b: foreign qdisc — not otto's  b->a: -
```

A direction's summary column reads `port-scoped (N)` when that placement
carries N active selectors, in place of a parameter summary or `-`. A
placement carrying a root qdisc otto did not create renders `foreign qdisc —
not otto's` instead: `list` reports a foreign tree, but `impair`/`repair`
refuse to mutate **or** clear it — a root qdisc otto didn't generate could be
anything, and otto only ever touches trees whose shape it recognizes as its
own — so clear it manually with `tc` if it's expendable.

### Mechanism

A scoped placement is a `prio` qdisc — the kernel-default bands pass unmatched
traffic through untouched, plus one `netem` band per selector steered by a pair
of `u32` port filters. See {doc}`../../architecture/subsystems/network` for the
full tree shape and why nothing is cached otto-side.

```{note}
**u32 caveat.** The `dport`/`sport` filters match by assuming a standard
20-byte IP header (no IP options) on a non-fragmented packet — the same
assumption `tc`'s own `u32 match ip dport/sport` shorthand makes. Acceptable
for lab traffic; a packet carrying IP options, or an IP fragment, won't
match a selector's filters and falls through to the unmatched bands (i.e.
behaves as clean for that one packet).
```

### Custom impairers and `--port`

The scoped surface — `supports_selectors` plus the `scoped_*` command
builders and the scoped parser — is **optional** on a `LinkImpairer` (see
[Custom impairers](#custom-impairers) below) and defaults off, so an
existing third-party impairer is unaffected by this feature. A `--port`
request routed to a host whose impairer doesn't declare
`supports_selectors = True` is a loud capability error naming the impairer
and the host — never a silent fallback to whole-link impairment.

```{note}
**Tunnels are out of scope.** Port-scoped impairment is a link-only
feature: it operates on `otto.link` placements exclusively, and
`otto.tunnel` is untouched by it — there is no "impair a tunnel" surface.
Impairing a link that tunnel traffic happens to ride
remains possible exactly as it is today (`tc` cannot know what a port
belongs to), with no added coupling between the two packages.
```

## Repairing: `otto link repair`

```bash
otto --lab veggies link repair edge
otto --lab veggies link repair --all
```

`repair <link>` clears **every** currently-impaired placement of that link
unconditionally (no merge — a placement with anything applied gets a
`tc qdisc del`) and cancels any live `--expire` timer for it, whether or not
that placement actually had an impairment to clear. This bare form clears a
whole-link impairment OR an entire port-scoped tree, whichever the placement
carries. Adding `--port N [--proto P]` narrows `repair` to one selector
instead of the whole placement — see
[Repairing one selector](#repairing-one-selector) above.

`repair --all` walks every static link in the lab and never raises: a link
that structurally can't be impaired (no named endpoint interface, the
mgmt-interface refusal, the local-host refusal — see [Safety](#safety)) is
silently skipped, since it was never impairable in the first place. A link
whose repair fails for a *live* reason (host unreachable, command failed) is
collected as a named failure instead of aborting the rest; if any failures
occurred, the command reports them and **exits non-zero** — a script
checking the exit code learns the sweep was incomplete rather than being
told it fully succeeded.

## Listing: `otto link list`

```bash
otto --lab veggies link list
```

Prints one line per static link:

```text
edge  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via -  a->b: delay 10ms  b->a: -
dataplane  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via pepper_seed  a->b: -  b->a: -
```

- **via** is the link's `impair` middlebox host id, or `-` for endpoint mode.
- Each direction's text is either a compact parameter summary (`delay 10ms
  loss 2%`) for a whole-link impairment, `port-scoped (N)` for N active
  selectors (each printed on its own indented row below — see
  [Listing: selector rows](#listing-selector-rows) above), `foreign qdisc —
  not otto's` for a root qdisc otto did not generate, `-` for a clean
  (unimpaired) placement, or `?` when that placement's host couldn't be
  reached this pass — absence there means "unknown," not "clean."
- A link that structurally can't be impaired shows `n/a` in both direction
  columns rather than attempting to resolve a placement.

If any link's state came back partial (at least one placement host was
unreachable), `list` still prints every row it *could* read, then adds a
trailing `partial scan — could not fully read: <ids>` warning rather than
silently dropping those links from the picture — the same
never-silently-wrong philosophy as `otto tunnel list`.

## Safety

Two refusals are enforced on **every** resolved placement, in both endpoint
and in-path mode, and apply regardless of `--expire`:

- **Management-interface refusal.** otto refuses to impair the interface it
  reaches a host *through* — resolved live by matching the host's
  management `ip` against the placement's netdev. This covers the in-path
  case too: if a middlebox's facing interface toward an endpoint happens to
  also be its own management interface, that placement is refused. Without
  this, impairing a link could sever otto's own path to the host it just
  impaired. The same refusal also covers *transit*: a placement is refused
  when its netdev carries the hop/management path of any **other** host that
  reaches otto only by hopping through the placement host (its `hop` chain,
  transitively) — degrading that netdev would lock otto out of the dependent
  host one indirection away.
- **Local-host refusal.** A link with the **local host** as either endpoint
  is never impairable, in any placement mode — the local host's
  connectivity to the bed IS otto's own management path, so degrading it
  (even indirectly, at a middlebox) degrades otto itself.

Both refusals raise before any host is mutated and are reported as a plain
error (CLI exit code 1).

**Elevation.** `tc qdisc` needs root. `impair`/`repair` mutations run
through the placement host's elevation mechanism (`sudo` unless the
connected user is already `root`); reads (`list`, and the pre-mutation
current-state check) need no privilege. A placement host with no elevation
configured fails loud, naming the host, rather than silently no-opping.

## Custom impairers

Impairment is pluggable the same way term/transfer backends are — see
{doc}`../hosts/extending-backends` for the shared registration philosophy. A
`LinkImpairer` builds the shell commands for one placement's impairment:

```python
class LinkImpairer:
    host_families: ClassVar[frozenset[str]] = frozenset()

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str: ...
    def read_command(self, netdev: str) -> str: ...
    def clear_command(self, netdev: str) -> str: ...
    def parse_read(self, output: str) -> ImpairmentParams | None: ...
```

`otto.link.netem.NetEmImpairer` (`host_families = {"unix"}`) is the only
first-party registrant, built on `tc`/netem. A custom impairer registers
from an `init` module, before any lab data loads:

```python
# .otto/init.py — registered via [init] in .otto/settings.toml
from typing import ClassVar

from otto.link import ImpairmentParams, LinkImpairer, register_impairer


class MyImpairer(LinkImpairer):
    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        return f"my-shaper set {netdev} {params.describe()}"

    def read_command(self, netdev: str) -> str:
        return f"my-shaper show {netdev}"

    def clear_command(self, netdev: str) -> str:
        return f"my-shaper clear {netdev}"

    def parse_read(self, output: str) -> ImpairmentParams | None:
        ...  # parse `my-shaper show` output back into ImpairmentParams


register_impairer("my_impairer", MyImpairer)
```

`host_families` is mandatory and non-empty — an impairer that could never
apply to any host family is rejected at registration time. Impairment is a
**Unix-host** capability today (the `impairer` field lives on the Unix host
spec; there is no embedded impairer analog).

Which impairer a placement host actually uses is resolved the same way as
`term`/`transfer`:

- The host's `valid_impairers` menu (defaults to `["netem"]`) gates what's
  selectable, the same as `valid_terms`/`valid_transfers`.
- The host's `impairer` field pins a specific selection from that menu.
- `[host_preferences]` in `.otto/settings.toml` can override the pin with an
  ordered preference list under the `impairer` key, product-wins-over-lab
  like `term`/`transfer` (see {ref}`host-preferences` in {doc}`../setup/lab-config`):

  ```toml
  [host_preferences.".*"]
  impairer = ["my_impairer"]
  ```

- Resolution happens **per placement host** at impair time — in endpoint
  mode the link's two endpoints may legitimately resolve to different
  impairers.

## Python API

`otto link impair`/`repair`/`list` are thin renderers over four functions in
`otto.link` — the single API the CLI, a future monitor/GUI topology
overlay, and any direct importer all call exactly the same way:

```python
from otto.link import ImpairmentParams, impair_link, read_link_states, repair_link

report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
for applied in report.applied:
    print(applied.placement.host_id, applied.placement.netdev, applied.params.describe())

states = await read_link_states(lab)  # every link's current impairment, list's feed

await repair_link(lab, "edge")
```

`selector` is the same optional keyword on both mutators — pass a `Selector`
to route through the port-scoped path instead of the whole-interface one;
omitted (the default), behavior is unchanged:

```python
from otto.link import Selector, impair_link, repair_link

report = await impair_link(
    lab, "edge", ImpairmentParams(delay_ms=200.0), selector=Selector(5201, "tcp")
)
await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
```

`read_link_states`'s result shape follows: each `LinkState.by_direction`
value is a `DirectionState` (`whole: ImpairmentParams | None`, `scoped: dict[Selector,
ImpairmentParams]`, `foreign: bool` — at most one of `whole`/`scoped` is
ever populated, since the two are exclusive per placement) or `None` when
that direction's host couldn't be read this pass.

`find_link`, `repair_all`, and the
`ImpairReport`/`RepairReport`/`LinkState`/`Selector`/`DirectionState`/`ScopedState`
result types round out the surface. Nothing in this layer prints or knows
about exit codes — see the {doc}`API reference <../../api/link>` for full
signatures.
