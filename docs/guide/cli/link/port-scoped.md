# Port-scoped impairments

{doc}`impair` impairs a placement's **entire interface** —
every packet traversing that netdev, degraded the same way. `--port` narrows
one `impair` call to a single service's traffic, leaving everything else on
the link clean:

```bash
otto --lab unix link impair edge --port 5201 --delay 200
otto --lab unix link impair edge --port 53 --proto udp --loss 5
```

## Selector semantics

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

## Exclusivity: whole-link and port-scoped never mix (v1)

A placement's netdev is either whole-link impaired (today's exact
root-netem shape) or port-scoped (a classful tree of per-selector bands) —
never both. Otto refuses to mix the two on the same placement, and the
error names the remedy:

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

`Selector(5201)` (both protocols) and `Selector(5201, "tcp")` are **distinct
selectors** — the former's filters simply match a superset of the latter's
traffic, so both can coexist on the same port at once, if unusual.

A placement caps at **8 concurrent selectors**; a 9th raises a loud error
naming the link, host, and netdev rather than silently dropping one or
overwriting another. `--expire <seconds>` composes exactly as with
whole-link impairment (see {ref}`auto-clearing <expire-auto-clearing>`),
but per selector: it auto-clears only that one selector, and a
repeated `--expire` on it restarts only its own countdown — every other
selector's timer (and any whole-link timer, which can't coexist with scoped
state anyway) is untouched.

## Mechanism

A scoped placement is a `prio` qdisc — the kernel-default bands pass unmatched
traffic through untouched, plus one `netem` band per selector steered by a pair
of `u32` port filters. See {doc}`../../../architecture/subsystems/network` for the
full tree shape and why nothing is cached otto-side.

```{note}
**u32 caveat.** The `dport`/`sport` filters match by assuming a standard
20-byte IP header (no IP options) on a non-fragmented packet — the same
assumption `tc`'s own `u32 match ip dport/sport` shorthand makes. Acceptable
for lab traffic; a packet carrying IP options, or an IP fragment, won't
match a selector's filters and falls through to the unmatched bands (i.e.
behaves as clean for that one packet).
```

