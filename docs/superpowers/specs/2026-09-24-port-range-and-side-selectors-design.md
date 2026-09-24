# Port-range and side-scoped impairment selectors — design

**Date:** 2026-09-24
**Source:** Chris, following up the port-scoped impairment feature
(`2026-07-11-port-scoped-impairment-design.md`): impair a *range* of ports,
optionally only the source or only the destination side, with overlapping
selectors either composing predictably or colliding loudly.

## Problem

`otto link impair --port N [--proto tcp|udp]` scopes netem to ONE service port,
matched on either side. Three gaps:

1. **No ranges.** Impairing an ephemeral range or a block of service ports
   needs one selector per port, and the cap is 8.
2. **No side scoping.** A selector always matches source OR destination port;
   there is no way to impair only traffic *to* a port, or only traffic *from*
   it.
3. **Overlap is order-dependent and undocumented.** Bands are assigned
   lowest-free and filter pref is `band*10 + slot`, so the kernel's first-match
   walk sends a packet to whichever overlapping selector was applied first.
   With `5201` applied before `5201/tcp`, the `/tcp` selector receives no
   traffic at all; in the other order it takes all tcp. The docs only say the
   two "coexist". Ranges make overlap common, so this must be defined.

## Decisions (adjudicated with Chris, 2026-09-24)

- **Range syntax: `--port START[:END]`.** `5000:5010` is inclusive. `N:N`
  normalizes to the single port `N`; `END < START` is a usage error.
- **Ranges expand into multiple u32 mask filters.** No flower, no iptables
  marks — the old-userland floor (centos:7, iproute2-ss170501) is kept.
- **New optional `--side src|dst`.** Omitted = both sides, exactly today's
  either-side semantics. `--side` is purely additive. (Considered and
  rejected: making `--side` required. It does not remove cross-side
  ambiguity — see "Precedence" — and it forces two invocations plus knowledge
  of which endpoint is the server for the everyday bidirectional-service case,
  reversing a deliberate decision of the 2026-07-11 spec.)
- **Collision rule: overlapping selectors must be strictly nested.** Two
  selectors whose coverage intersects compose only if one's scope is strictly
  narrower than the other's; otherwise the new one is loudly refused.
- **Precedence: destination port first, then source port; within a side, the
  narrowest selector wins.** Fixed, independent of application order.
- **Not a breaking change in practice.** The port-scoped feature has no users
  yet (Chris, 2026-09-24); the filter pref layout changes freely.

## Hard constraints (unchanged from 2026-07-11)

- Kernel qdisc/filter state is the ONLY state; everything reconstructs from
  `tc` read-back. Collision checks run against read-back state.
- Whole-link impairment is byte-identical to today.
- No half-impairments: a mid-way failure restores every touched placement to
  its full pre-call shape.
- otto parses only trees it generated; anything else is `foreign`.
- Third-party impairers are unaffected: NetEm remains the only
  `supports_selectors` impairer.

## Design

### 1. Selector model (`otto.link.params`)

```python
@dataclass(frozen=True, slots=True)
class Selector:
    port: int                      # first port, 1-65535
    proto: str | None = None       # "tcp" | "udp" | None = both
    end: int | None = field(default=None, kw_only=True)   # last port; None = single
    side: str | None = field(default=None, kw_only=True)  # "dst" | "src" | None = both
```

- Validation: `port <= end <= 65535`; `end == port` normalizes to `None`
  (in `__post_init__`), so `5000` and `5000:5000` are one key.
- `Selector(5201)` / `Selector(5201, "tcp")` keep working unchanged.
- `Selector.parse(port_text, proto=None, side=None)` is the ONE text parser,
  shared by the CLI (`impair` and `repair`) and the sentinel decoder. Accepts
  `N` and `N:M`; rejects empty halves (`5000:`, `:5010`), non-digits,
  whitespace, out-of-range, and `M < N`, each with a message naming the input.
- `describe()` → `5201`, `5000:5010`, `5000:5010/tcp`, `5000:5010/tcp dst`,
  `5000:5010 src`. Used by `list`, errors, and verify messages.
- Pure helpers, unit-testable without tc:
  - `scope_contains(a, b)`: every (proto, side) of `b` is in `a`.
  - `overlaps(a, b)`: protos intersect AND sides intersect AND port ranges
    intersect.
  - `collides(a, b)`: `a != b` and `overlaps(a, b)` and neither scope strictly
    contains the other.
  - `tier` property: number of dimensions NOT narrowed (0 = proto AND side
    narrowed, 1 = one of them, 2 = neither).

**Python API parity.** The public mutators `otto.link.impair_link(…,
selector=…)` and `repair_link(…, selector=…)` keep their signatures; ranges
and sides reach them purely through the richer `Selector`:

```python
await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0),
                  selector=Selector(5000, "tcp", end=5010, side="dst"))
await repair_link(lab, "edge", selector=Selector(5000, "tcp", end=5010, side="dst"))
```

The CLI is only a text front-end onto `Selector`; the collision check (§5),
precedence, and read-back all live below it in `otto.link`, so API callers get
identical semantics and identical refusals.

### 2. CLI (`otto.cli.link`)

- `--port` on `impair` and `repair` becomes `str`, help `PORT or START:END`.
- New `--side src|dst` on both commands; like `--proto`, it requires `--port`
  (usage error, exit 2, otherwise).
- `repair --port … [--proto …] [--side …]` names ONE selector exactly. It
  does not carve a sub-range out of a wider selector; a non-matching repair is
  today's "no such selector" outcome.
- `list` rows sort by `(port, end, proto, side)`.

### 3. Range decomposition (`otto.link.netem`)

`port_prefixes(lo, hi) -> list[PortPrefix]` (`value`, `mask` 16-bit): the
minimal set of mask-aligned blocks covering exactly `[lo, hi]` (standard
range-to-prefix: repeatedly take the largest power-of-two block aligned at
`lo` that fits). Example: `5000:5010` → `5000/0xfff8` (5000–5007),
`5008/0xfffe` (5008–5009), `5010/0xffff`. Worst case for 16 bits is **30**
prefixes, so no additional cap. A single port is one prefix with mask
`0xffff` — the same filter shape as today.

### 4. Filter layout and precedence

Each selector occupies one band (unchanged: lowest free of 4–11, cap 8) and
one **slot** per (side, proto) it covers — 1, 2 or 4 slots. Each slot gets one
pref:

```
pref = 1000*side + 200*tier + 10*band + proto
       side: 0 = dst, 1 = src     tier: 0..2 (§1)
       band: 4..11                 proto: 0 = tcp, 1 = udp
```

Every prefix of a slot is added as its own u32 entry **under that slot's
single pref**. Consequences:

- **Precedence falls out of the kernel's ascending-pref walk.** All dport
  filters precede all sport filters (destination first). Within a side, a
  strictly narrower selector has a strictly lower tier, so it is matched first.
  Two selectors in the same (side, tier, proto) can never match the same
  packet on that side — that would be a collision (§5) — so the band term only
  disambiguates prefs, never outcomes. The winner is therefore unique and
  independent of application order.
- **Clear is unchanged in shape.** `tc filter del … pref P protocol ip u32`
  removes every entry under `P`, so `scoped_clear_selector_commands` still
  emits one delete per slot plus the leaf delete, the expire timer's embedded
  clear sequence keeps its shape, and rollback of a partially-added range is
  the same per-pref delete.
- Filter count: prefixes × slots, worst case 30 × 4 = 120 per selector, ~960
  per netdev at the cap. u32 walks these linearly per packet; the docs note
  that very wide, unaligned ranges on many selectors add per-packet cost.

**Cross-side matches.** A packet carries two ports, so a dst-covering selector
and a src-covering selector can both match one packet even when their ranges
are disjoint (`5005` and `40000:60000`: a packet 45000→5005 matches both).
This cannot be detected statically without forbidding almost every pair of
selectors, so it is resolved by precedence: the destination-port match wins.
The two directions of one connection may therefore land in different
selectors. Docs state this and give the escape hatch: scope every selector on
a link to the same `--side` and no cross-side match can occur.

### 5. Collision check (`otto.link.manage`)

In `_apply_selector`, after the existing foreign/whole-link exclusivity checks
and before any `tc` command: for each existing selector `e` on the placement,
if `collides(new, e)`, refuse with `ValueError` — the module's convention for
a structural refusal, like `_ensure_not_foreign` and the whole-link/scoped
exclusivity raises: nothing failed, otto is declining, no state is touched.
E.g.

```
5205/tcp collides with 5200:5220/tcp on edge a->b (h1/eth1): same scope,
overlapping ports 5205. Clear it first (otto link repair edge --port 5200:5220
--proto tcp) or choose a non-overlapping range.
```

The message names both selectors, the placement, the overlapping port span,
and why they don't nest ("same scope" or "neither scope is narrower"). The
check runs per placement, so `--from` narrows it with the placement. Re-impairing
the identical selector is today's merge, never a collision.

| existing | new | result |
|---|---|---|
| `5200:5220` | `5200:5210` | collision (same scope) |
| `5200:5220` | `5200:5210/tcp` | composes; tcp in 5200–5210 → new |
| `5200:5220` | `5205 dst` | composes; dst 5205 → new |
| `5200:5220/tcp` | `5205/tcp dst` | composes |
| `5200:5220/tcp` | `5200:5220/udp` | composes (disjoint) |
| `5005/tcp` | `5005 dst` | collision (neither narrower) |
| `5005 dst` | `5005 src` | composes (disjoint sides) |

Dry-run (`_plan_impair`) cannot read state, so it adds an
`_UNCHECKED_COLLISION` entry alongside `_UNCHECKED_BAND`.

### 6. Read-back (`parse_scoped_outputs`)

- Each u32 block decodes its pref into (side, tier, band, proto); every field
  must be in range and `flowid 1:<band>` must equal the pref's band. The proto
  match must equal the pref's proto; the port match must sit in the pref's
  side half (dport: mask `0000xxxx`, sport: `xxxx0000`) with a contiguous
  leading-ones mask.
- Blocks group by band → slot → set of `(value, mask)`. A band's slots must
  form a full product of a side set × a proto set, and its tier must equal
  that scope's tier.
- Per slot: merge the prefixes into one interval `[lo, hi]` (must be
  contiguous and non-overlapping), then require `port_prefixes(lo, hi)` to
  equal the read set exactly — a non-minimal or hand-edited decomposition is
  `foreign`. Every slot of a band must yield the same interval.
- Otherwise unchanged: leaves ↔ filter bands must match; empty-but-ours root
  is `clean`.

Trees written by the pre-change layout (`band*10 + slot`) parse as `foreign`
and need a manual `tc qdisc del dev <if> root`. No live trees exist.

### 7. Sentinel and timers (`otto.link.sentinel`)

New timers encode **v3**:
`otto-impair:v3:<link-id>:<netdev>:<port>:<end-or-empty>:<proto-or-empty>:<side-or-empty>`,
decoded through `Selector.parse`. v1 and v2 stay parseable (v2 → a single-port,
both-sides selector) so `repair` still finds and cancels them. `repair
--port …` matches a timer by full `Selector` equality (port, end, proto,
side). The timer script is unchanged in shape.

## Verify-first gate

Before building on it, capture live on BOTH the unix bed (iproute2 6.1.0) and
test3's oldos image (iproute2-ss170501):

1. A second `tc filter add … pref P … u32` with an existing `P` appends an
   entry rather than failing or replacing.
2. `tc filter show … parent 1:` prints each entry as its own block carrying
   the same `pref P`, and a masked port prints as e.g.
   `match 00001388/0000fff8 at 20`.
3. `tc filter del … pref P protocol ip u32` removes every entry under `P`.
4. Prefs up to 1511 are accepted.

Captured bytes become the read-back fixtures, as in 2026-07-11. Any deviation
stops the work and returns to design.

## Testing

- `port_prefixes`: property test over random `lo <= hi` — the union is
  exactly `[lo, hi]`, blocks are disjoint and aligned, and the count equals a
  brute-force minimum; edge cases `1:65535`, single ports, power-of-two
  boundaries.
- `Selector.parse` / validation: every accepted and rejected form.
- `overlaps` / `collides` / `tier`: exhaustive over the 9 scopes × a small
  range grid, asserting the §5 table and the rule's symmetry.
- Precedence: a pure model — given selectors and a packet
  `(proto, sport, dport)`, walk the generated prefs in ascending order and
  assert the winner equals "dst first, narrowest wins" over random selector
  sets that pass the collision check.
- Golden `tc` commands for single ports (new prefs), ranges, and sides.
- Read-back round-trip for every scope and range shape; `foreign` for a
  non-minimal decomposition, a tier/scope mismatch, a pref/flowid band
  mismatch, and the old pref layout.
- Sentinel v3 round-trip; v1/v2 still decode.
- CLI: usage errors for bad `--port`, `--side` without `--port`, and a
  collision surfacing as a non-zero exit naming both selectors.
- Python API: `impair_link` / `repair_link` with a range + side `Selector`
  apply, merge, collide (`ValueError`), and repair exactly as the CLI does.
- Live bed lane: range + side apply, verify, `list`, expire, repair on the
  modern and oldos images.

## Documentation

- `docs/cli/link/port-scoped.md`: ranges, `--side`, the collision table,
  precedence ("destination first; narrowest wins"), the cross-side note with
  the one-side escape hatch, and the per-packet cost of wide ranges. Replace
  the "coexist … if unusual" paragraph. Show how `--side` composes with the
  existing `--from` (link direction = which sender's egress netdev): e.g.
  `--from a --port 5201 --side dst` impairs only a's requests to b's service,
  and `--from b --port 5201 --side src` only b's replies. No ingress/ifb
  mode is added — on a static link, ingress at b IS egress at a, which
  `--from` already selects.
- `docs/architecture/subsystems/network.md`: the pref layout and why it
  encodes precedence.
- `docs/cookbook/network-api.md`: `Selector(…, end=…, side=…)`.

## Out of scope

- Carving a sub-range out of an existing selector (`repair` is exact-match).
- IPv6, and IPv4 packets carrying IP options (`match ip dport` assumes a
  20-byte header) — both pre-existing limitations.
- Tunnels (`otto.tunnel`), as in 2026-07-11.
- Migrating trees written by the old pref layout.
