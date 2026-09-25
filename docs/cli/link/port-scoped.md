# Port-scoped impairments

{doc}`impair` impairs a placement's **entire interface** —
every packet traversing that netdev, degraded the same way. `--port` narrows
one `impair` call to one port or a range of ports, leaving everything else on
the link clean:

```bash
otto --lab unix link impair edge --port 5201 --delay 200
otto --lab unix link impair edge --port 53 --proto udp --loss 5
otto --lab unix link impair edge --port 5000:5010 --side dst --delay 50
```

## Selector semantics

`--port N` scopes to one service port; `--port START:END` scopes to an
inclusive range (`5000:5010` covers ports 5000 through 5010 — `N:N`
normalizes to the same selector as the bare port `N`). By default a selector
matches traffic whose **source OR destination** port falls in its range —
otto never needs to know which endpoint of the link is running the server,
so one flag covers both directions of a service's traffic. `--side dst` or
`--side src` narrows a selector to just that side; omitted, either side
matches. Under `--side dst`, only packets whose destination port is in
range are delayed — the request leg, not the reply — while the default,
either side, delays the replies too (for a delay impairment, that's roughly
twice versus four times the configured delay over a fresh connection plus
one echo). `--proto tcp` or `--proto udp` narrows to one L4 protocol; omitted,
both tcp and udp match. `--proto` or `--side` without `--port` is a usage
error (exit code 2, `--proto needs --port.` / `--side needs --port.`) —
there's nothing for either to narrow.

Omitting `--port` gives the ordinary whole-interface impairment. Port
scoping is opt-in, per invocation.

| Option | Description |
| ------ | ----------- |
| `--port` | Scope this impairment to one port (1-65535) or an inclusive `START:END` range, matching source OR destination unless narrowed by `--side`. |
| `--proto` | With `--port`: narrow to `tcp` or `udp`. Omitted, both match. Requires `--port`. |
| `--side` | With `--port`: match only the `dst` or `src` port. Omitted, either side matches. Requires `--port`. |

## Exclusivity: whole-link and port-scoped never mix

A placement's netdev is either whole-link impaired (a root netem qdisc) or
port-scoped (a classful tree of per-selector bands) — never both. Otto
refuses to mix the two on the same placement, and the error names the remedy:

```bash
otto --lab unix link impair edge --delay 50
# ... placement now has a whole-link impairment ...
otto --lab unix link impair edge --port 5201 --delay 200
# Error: link edge has a whole-link impairment — repair it first
```

```bash
otto --lab unix link impair edge --port 5201 --delay 200
# ... placement now has a port-scoped impairment ...
otto --lab unix link impair edge --delay 50
# Error: link edge has port-scoped impairments — repair them first or impair with --port
```

Repair enforces the same rule from the other side: `otto link repair edge
--port 5201` against a whole-link impairment raises `link edge has a
whole-link impairment — repair it without --port` — use a bare `otto link
repair edge` instead.

## Multiple selectors: independent params, per-selector merge, cap 8

Each selector carries its own parameter set. Re-impairing a selector merges
over **that selector's own** current state, not the whole netdev's — same
per-param last-one-wins and explicit-zero-clears rules as whole-link
impairment (see [Re-impairing](impair.md#re-impairing-merge-per-param-last-one-wins)
and [Zero clears](impair.md#zero-clears)), just scoped narrower:

```bash
otto --lab unix link impair edge --port 5201 --proto tcp --delay 20
# 5201/tcp is now: delay 20ms

otto --lab unix link impair edge --port 5201 --proto tcp --loss 2 --delay 10
# 5201/tcp is now: delay 10ms loss 2%  — delay overridden, loss added; other selectors untouched
```

A placement caps at **8 concurrent selectors**; a 9th raises a loud error
naming the link, host, and netdev rather than silently dropping one or
overwriting another. `--expire <seconds>` composes exactly as with
whole-link impairment (see {ref}`auto-clearing <expire-auto-clearing>`),
but per selector: it auto-clears only that one selector, and a
repeated `--expire` on it restarts only its own countdown — every other
selector's timer (and any whole-link timer, which can't coexist with scoped
state anyway) is untouched.

## Overlapping selectors: nest or collide

A selector's *scope* is its (protocol, side) pair — port-range width is
never part of it. Two selectors whose port ranges intersect may coexist only
if one's scope is **strictly narrower** than the other's — a narrower
protocol, a narrower side, or both. `Selector(5201)` (both protocols, either
side) and `Selector(5201, "tcp")` compose this way: the second is strictly narrower,
so it carves tcp traffic on 5201 out of the first's broader match. An
overlap where neither selector is narrower is refused rather than left to
whichever was applied first:

| existing | new | result |
| --- | --- | --- |
| `5200:5220` | `5200:5210` | collision (same scope) |
| `5200:5220` | `5200:5210/tcp` | composes; tcp in 5200-5210 → new |
| `5200:5220` | `5205 dst` | composes; dst 5205 → new |
| `5200:5220/tcp` | `5205/tcp dst` | composes |
| `5200:5220/tcp` | `5200:5220/udp` | composes (disjoint) |
| `5005/tcp` | `5005 dst` | collision (neither narrower) |
| `5005 dst` | `5005 src` | composes (disjoint sides) |

A refused overlap names both selectors, the placement, and the overlapping
span:

```text
5205/tcp collides with 5200:5220/tcp on edge a->b (test1/eth1.100): same
scope, overlapping ports 5205. Clear it first (otto link repair edge --port
5200:5220 --proto tcp), narrow it (--proto/--side on the CLI, proto/side on
the API) so it nests inside 5200:5220/tcp, or choose a non-overlapping
range.
```

The "narrow it" option appears only while the new selector leaves `--proto`
or `--side` unset; one that already sets both has no narrower scope to take.

**Precedence.** Because every pair of selectors on a placement either nests
or is refused up front, matching a live packet has one fixed order: otto
checks the destination port first, then the source port; on each side, the
narrowest-*scoped* matching selector wins. This holds regardless of the
order the selectors were applied in.

Nesting is an `impair`-side rule only — `repair --port` is exact-match: it
clears just the selector named exactly as it was impaired (`5000:5000` and
`5000` are the same selector) and never carves a sub-range out of a wider
one. See [Repairing one selector](repair.md#repairing-one-selector).

## Cross-side matches

A packet carries two ports, so a destination-scoped selector and a
source-scoped selector can both match the very same packet even when their
port ranges don't overlap. A selector on `5005` (either side) and a wider
one on `40000:60000` (either side) both match a packet `45000 → 5005`: 5005
on the destination side, 40000:60000 on the source side. Destination-first
precedence decides which one actually applies: here `5005` wins (5005 is the
packet's destination port); the reply `5005 → 45000` lands in `40000:60000`
for the same reason (45000 is now the destination port) — so the two
directions of one connection are impaired differently. The escape hatch:
scope every selector on a link to the same `--side`, and a cross-side match
becomes impossible.

## With `--from`

`--side` composes with [`--from`](impair.md#both-directions-and-the-rtt-math)
to single out one leg of one service's traffic. `--from a --port 5201
--side dst` impairs only `a`'s requests to `b`'s service — `a`'s own
placement, matching packets destined for port 5201 — while `--from b --port
5201 --side src` impairs only `b`'s replies, matching packets sourced from
port 5201. No separate ingress/ifb mode is needed: on a static link,
traffic arriving at `b` *is* traffic leaving `a`, so `--from` alone already
selects that direction.

## Mechanism

A scoped placement is a `prio` qdisc — the kernel-default bands pass unmatched
traffic through untouched, plus one `netem` band per selector, steered by a
`u32` filter per (side, protocol) it covers. See
{doc}`../../architecture/subsystems/network` for the full tree shape, the
filter-pref layout that encodes precedence, and why nothing is cached
otto-side.

A range is not one filter: otto decomposes it into the fewest mask-aligned
port blocks that cover it exactly (a single port is always one block), up to
30 blocks for a 16-bit range, and adds each as its own filter entry. The
kernel walks a slot's entries linearly per packet, so a wide, unaligned
range on many concurrent selectors adds per-packet cost.

```{note}
**u32 caveat.** The `dport`/`sport` filters match by assuming a standard
20-byte IP header (no IP options) on a non-fragmented packet — the same
assumption `tc`'s own `u32 match ip dport/sport` shorthand makes. Acceptable
for lab traffic; a packet carrying IP options, or an IP fragment, won't
match a selector's filters and falls through to the unmatched bands (i.e.
behaves as clean for that one packet).
```

