# Port-scoped link impairment — design

**Date:** 2026-07-11
**Source:** Chris's follow-up from the daemon-toolkit workstream ("does impairment
support application to specific ports in a link as opposed to all traffic on the
link? That would be extremely helpful and flexible"), named in the daemon-toolkit
spec's out-of-scope list.

## Problem

`otto link impair` applies netem to a placement netdev's **root qdisc** — all
traffic on that direction of the link is impaired. There is no way to degrade one
service's traffic (an iperf stream, a DNS port) while everything else on the link
— including other test traffic — flows clean. The `ImpairmentParams` model, CLI,
read-back parser, and expire timers all assume exactly one impairment per netdev.

## Decisions (adjudicated with Chris, 2026-07-11)

- **Selector = service port, either side.** `--port N [--proto tcp|udp]` matches
  traffic whose SOURCE OR DESTINATION port is N (proto omitted = both). One flag
  covers both directions of a service's traffic; otto never needs to know which
  endpoint is the server.
- **No `--port` = whole-interface impairment, exactly as today.** Omitting
  `--port` is not a new mode and not a default selector: it is today's
  whole-link impairment, byte-identical commands and semantics. Port scoping is
  strictly opt-in per invocation.
- **Links only — this feature does not apply to tunnels.** It operates on link
  placements (`otto.link`) exclusively; `otto.tunnel` is untouched by this
  workstream. There is no tunnel-aware selector, no impairment of tunnels as
  entities, and no coupling added between the packages.
- **Exclusive with whole-link impairment, per link (v1).** A netdev is either
  whole-link impaired (today's exact root-netem shape, zero change) or
  port-scoped (classful tree). Mixing is a loud error naming the remedy. The
  state model treats whole-link as the degenerate `ALL` selector, so a future
  unified-classful evolution is an increment, not a redesign.
- **Multiple selectors per link**, independent params each, capped at **8**
  (loud error beyond; prio supports 16 bands, we use 3 + N).
- **Mechanism: `prio` + `u32` inside the NetEm impairer.** Works on the same
  old-userland floor the link feature already targets; all complexity contained
  in `otto.link.netem`; read-back is deterministic because otto parses only
  trees it generated. (Rejected: flower/htb — needs kernel ≥ 4.2, abandons the
  floor for no gain at this selector complexity; iptables-mark — a second
  subsystem to apply/verify/rollback/repair.)

## Hard constraints

- **Kernel qdisc/filter state is the ONLY state** (unchanged principle). No
  otto-side selector registry; everything reconstructs from `tc` read-back.
- **Whole-link impairment is byte-identical** to today: same apply command, same
  root-netem shape, same read-back, same goldens. Existing live impairments and
  human-applied root netem keep working unchanged.
- **No half-impairments** (unchanged): a mid-way failure restores every touched
  placement to its full pre-call shape — including a complete scoped mapping.
- **Timer sentinel evolves via the shared framing**: `otto-impair:v2` with the
  selector in the payload; v1 tokens stay parseable so repair cancels timers
  launched by older otto. The `otto-tunnel:v1` sentinel is untouched.
- Third-party impairers are unaffected: the scoped surface is optional,
  capability-gated, and defaults off.

## Design

### 1. Selector and state model

`Selector` (frozen dataclass, `otto.link.params` or a sibling module):
`port: int` (1–65535), `proto: str | None` (`"tcp"`, `"udp"`, `None` = both).
String forms `"5201"` / `"5201/tcp"` are used uniformly by CLI, `list`, errors,
and the v2 sentinel. `(5201, None)` and `(5201, "tcp")` are distinct keys —
allowed; the former's filters simply match a superset (documented).

Per placement, read-back yields one of:

- **Clean** — no otto state.
- **Whole-link** — today's root netem (`ImpairmentParams`).
- **Scoped** — `{Selector → ImpairmentParams}` (≤ 8 entries).
- **Foreign** — a root qdisc otto did not generate: reported in `list`, loudly
  refused on mutate (today's posture, extended).

Merge semantics are today's rules applied per selector: re-impairing a selector
merges over ITS current params (per-param last-one-wins, explicit zero clears
one param); a selector whose merged params come out empty is cleared. Exclusivity
errors: `--port` against whole-link state → "link X has a whole-link impairment —
repair it first"; bare impair against scoped state → "link X has port-scoped
impairments — repair them first or impair with --port".

Expire timers are per-selector: sentinel
`otto-impair:v2:<link-id>:<netdev>:<port>:<proto-or-empty>` (4 payload segments
via `otto.host.daemon.encode_token`/`split_token`; percent-encoding per segment
as in v1). Timer cancellation before mutation targets the selector; bare repair
cancels every v1 AND v2 timer for the netdev.

### 2. tc tree layout and the netem surface

Scoped layout (generated and parsed only by otto):

```text
tc qdisc replace dev X root handle 1: prio bands <3+N> priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1
tc qdisc replace dev X parent 1:<3+i> handle <(3+i)*10>: netem <params>
tc filter add dev X parent 1: pref <(3+i)*10>   protocol ip u32 \
    match ip protocol <6|17> 0xff match ip dport <port> 0xffff flowid 1:<3+i>
tc filter add dev X parent 1: pref <(3+i)*10+1> protocol ip u32 \
    match ip protocol <6|17> 0xff match ip sport <port> 0xffff flowid 1:<3+i>
```

- Bands 1–3 keep the kernel-default priomap semantics: **unmatched traffic
  behaves exactly as with no qdisc** (pfifo_fast equivalence).
- Selector bands are `1:4` onward; the band number derives the netem handle and
  the filter prefs, making read-back deterministic. Each selector owns pref slots
  `band*10 .. band*10+3` in a fixed order — dport/tcp, sport/tcp, dport/udp,
  sport/udp — with a single-proto selector using only its two slots. `proto=None`
  therefore emits four filters.
- Band assignment is orchestration-owned and stable: an existing selector keeps
  its band (from read-back); a new selector takes the lowest free band.
- Clearing one selector: delete its filters (by pref) and its band's netem — an
  unfiltered band receives no traffic. Clearing the LAST selector deletes the
  root, restoring pristine. Bare repair on a scoped netdev is one
  `tc qdisc del dev X root`.
- Read-back = `tc qdisc show dev X` (netem params per band, existing token
  parser) + `tc filter show dev X parent 1:` (selectors from the u32 hex:
  `match 00001451/0000ffff at 20` → dport 5201; the mask/offset pair
  distinguishes sport (ffff0000/at 20) from dport (0000ffff/at 20); the
  `at 8` protocol match gives tcp/udp). Only otto's own conventions are parsed;
  anything else → Foreign.
- Documented u32 caveat: `match ip dport/sport` assumes a 20-byte IP header (no
  IP options) and non-fragmented packets — acceptable for lab traffic.

### 3. Impairer contract (optional scoped surface)

`LinkImpairer` gains `supports_selectors: ClassVar[bool] = False` and optional
stateless builders/parsers (same no-I/O philosophy as the existing four):

- `scoped_root_command(netdev, bands) -> str`
- `scoped_band_command(netdev, band, params) -> str`
- `scoped_filter_commands(netdev, band, selector) -> list[str]`
- `scoped_clear_selector_commands(netdev, band, selector) -> list[str]`
- `scoped_read_commands(netdev) -> list[str]` and
  `parse_scoped(qdisc_output, filter_output) -> ScopedState`
  (`{selector → (band, params)}` | whole-link | clean | foreign — one
  discriminated result type shared with orchestration)

`NetEmImpairer` implements all of it. A `--port` request routed to an impairer
with `supports_selectors=False` is a loud capability error naming the impairer.
`register_impairer` validation is unchanged.

### 4. Orchestration

`impair_link(..., selector: Selector | None = None)`,
`repair_link(..., selector: Selector | None = None)`.

Apply flow per placement (invariants unchanged, now selector-keyed): cancel the
selector's timers → read state → exclusivity check → merge over the selector's
current params → validate (netem coupling rules as today) → band assignment →
root-ensure + band + filters via `_root_run` → re-read → `equivalent()` per
selector (existing canonical-key + tick-tolerance machinery, reused) → launch
the v2 timer when `--expire` is given.

Rollback: the pre-mutation snapshot captures the placement's full shape (clean /
whole-link params / complete scoped mapping); restore rebuilds exactly that.
Registered before mutating, failing placement included — today's rule.

`read_link_states`: `LinkState.by_direction` values become `DirectionState`
(`whole: ImpairmentParams | None`, `scoped: dict[Selector, ImpairmentParams]`,
`foreign: bool`). Breaking shape change to the link read API — sanctioned, the
API predates external users. `read_link_states` stays never-raising;
`repair_all` semantics unchanged (scoped links repaired fully).

### 5. CLI and presentation

- `otto link impair <link> --port 5201 [--proto tcp|udp] <param flags> [--expire N] [--from H]`
  — one selector per invocation; `--proto` without `--port` is a usage error;
  `--from` composes orthogonally (direction narrowing as today). Without
  `--port`, the command is today's whole-interface impairment, unchanged.
- `otto link repair <link> [--port N [--proto P]]` — bare clears everything,
  port form clears one selector.
- `otto link list`: one row per selector under its link
  (`a->b  5201/tcp  delay 200ms`), whole-link rows unchanged, foreign trees
  render as "foreign qdisc — not otto's".
- Guide (`docs/guide/link.md`) gains a "Port-scoped impairments" section with
  the selector semantics, the exclusivity rule, the cap, and the u32 caveat.

### 6. Testing

- **Unit goldens**: exact command bytes for root/band/filter builders (the u32
  lines are the new stability-critical strings); parser round-trips against
  fixtures **captured from the live bed** during implementation (modern AND old
  iproute2 output formats, the existing dual-format posture).
- **Manage-level fake-host tests**: exclusivity both directions; per-selector
  merge and zero-clear; band stability across re-impairs; the 8-cap; rollback
  restoring a full scoped mapping; v2 timer identity + v1 cancellation compat;
  capability error for a non-supporting impairer.
- **Live-bed e2e** (extends the impair e2e pattern): scoped impair → read-back
  equivalence; two concurrent selectors; `--expire` clears only its selector;
  repair to pristine; and a **differential traffic proof** — measured latency on
  the impaired port visibly higher than on an unimpaired port over the same
  link.
- Whole-tree gates as always: `make coverage` + lint + ty + docs.

## Out of scope

- **Tunnels, in any form.** This feature is per-link only. `otto.tunnel` code,
  models, CLI, and behavior are untouched; there is no "impair a tunnel"
  surface and none is planned by this spec. (Impairing a link that tunnel
  traffic happens to traverse remains possible exactly as it is today — tc
  cannot know what a port belongs to — but otto adds no tunnel integration.)
- Whole-link/scoped coexistence (unified classful tree) — future evolution; the
  selector model already accommodates it.
- Port ranges, port lists, full 5-tuple selectors.
- flower/htb backends.
- Monitor GUI overlay for selector state (rides `read_link_states` when the
  topology plan lands).
- Sentinel v1 retirement.
- The pre-existing `parse_ps_output` isdigit/int hardening and carrier-name
  case-sensitivity items from the daemon-toolkit final review (unrelated;
  tracked in that workstream's notes).
