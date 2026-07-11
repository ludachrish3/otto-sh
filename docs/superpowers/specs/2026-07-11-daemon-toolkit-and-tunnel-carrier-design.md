# Daemon toolkit + tunnel carrier seam — design

**Date:** 2026-07-11
**Source:** structure review 2026-07-10, findings #2 (tunnel↔link parallel machinery) and #5
(tunnel has no carrier seam); triaged with Chris during the post-extraction-polish wrap-up.

## Problem

`otto.link` and `otto.tunnel` are deliberately decoupled (no import edge in either
direction), and each hand-rolls the same managed-remote-process machinery:

1. **Sentinel framing.** `tunnel/sentinel.py` and `link/sentinel.py` implement the same
   `<prefix>:<version>:<percent-encoded segments>` wire scheme with parse-to-None
   semantics; only the domain payload differs (tunnel: 9 segments, link: 2).
2. **PS scan.** `DISCOVERY_PS_COMMAND` (tunnel/socat.py) and `IMPAIR_PS_COMMAND`
   (link/sentinel.py) are the same command modulo the grep pattern — including a
   duplicated copy of the procps-ng 3.3.10 separate-`-eo`-flags lesson — and
   `parse_process_discovery` / `parse_impair_ps` are the same parse loop.
3. **Kill strings.** Both build `kill <sorted pids>` lines.
4. **Launch** is already shared (`otto.host.detached.launch_command`, extracted in
   link #3) — the precedent this design completes.

Separately, socat is hardcoded as the tunnel transport: `socat.py` bakes in the argv
builders and TCP4 carrier, and `manage.py` bakes in the tool probe and protocol menu.
Every other pluggable seam in otto rides `Registry` (19/20 per the structure review);
tunnels should too. A concrete future consumer exists: the TODO's docker-daemon-over-
tunneled-SSH idea wants a non-socat carrier.

## Decisions (adjudicated with Chris, 2026-07-11)

- **Scope:** extraction + carrier seam in one workstream; doc-drift micro-fixes ride
  along. `Provenance.DYNAMIC` topology wiring stays deferred to monitor Plan 4.
- **Carrier depth:** argv-builder seam only. The 2n process topology (ingress/relay/
  egress × fwd/rev), sentinel v1 wire format, discovery, verify, and remove are
  carrier-agnostic and unchanged. A carrier only decides what each tagged process
  executes and what tools it needs. Mesh-style carriers that change process topology
  would need a sentinel v2 — explicitly out of scope.
- **Shared-module home:** grow the existing module, renamed `otto.host.detached` →
  **`otto.host.daemon`** (clean break, no alias — house style). Singular, noun.
- **Extraction shape:** toolkit, not framework. `otto.host.daemon` is pure builders and
  parsers with **no I/O**; link and tunnel keep their own orchestration, error
  policies, and reporting.

## Hard constraints

- **Wire and command bytes are invariant.** Sentinel tokens (`otto-tunnel:v1:…`,
  `otto-impair:v1:…`) and both ps scan command strings are stability contracts.
  Golden-string tests pin them; existing tests that pin wire bytes must pass
  unmodified.
- **No link↔tunnel import edge**, before or after. Both import `otto.host.daemon`.
- **Behavior-identical bar:** existing tunnel + link unit/integration/e2e suites pass
  with nothing but import-path updates.

## Design

### 1. `otto.host.daemon` — the shared toolkit

`src/otto/host/detached.py` → `src/otto/host/daemon.py`. The module owns the full
lifecycle vocabulary for sentinel-tagged daemons on remote hosts (launch → discover →
reap), as pure string builders and parsers:

- `launch_command(sentinel: str, argv: list[str]) -> str` — unchanged behavior; the
  `socat_args` parameter generalizes to `argv`; the systemd-run/setsid/sudo-splice
  lore stays in the docstring. The back-compat re-export in `tunnel/socat.py` is
  dropped and import sites updated.
- `ps_scan_command(prefix: str) -> str` — builds
  `ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' <prefix>:' || true`.
  One home for the procps-ng 3.3.10 lesson (separate `-eo` flags, `|| true`,
  formatted `etime` not `etimes`).
- `DaemonProcess` frozen dataclass: `pid: int`, `age_seconds: int`, `token: str`.
- `parse_ps_output(output: str, prefix: str) -> list[DaemonProcess]` — the shared
  loop: split fields, require ≥3, pid must be all-digits, token = first word in
  `fields[2:]` starting with `<prefix>:`. Absorbs `parse_etime` (moves here from
  `tunnel/discovery.py`, returning 0 for unparseable input as today).
- Sentinel framing (framing only — **no blanket encoding**):
  - `enc(value: str | int | None) -> str` / `dec(segment: str) -> str` — the
    percent-encoding convention (`quote(..., safe="")`, empty = None).
  - `encode_token(prefix: str, version: str, segments: Sequence[str]) -> str` —
    `<prefix>:<version>:<segments joined with ':'>`. *segments* are the **payload
    only** (tunnel: 9, link: 2) and are caller-**final** strings; framing never
    re-encodes, so tunnel's double-encoded path segment stays byte-identical.
  - `split_token(token: str, prefix: str, version: str, count: int) -> list[str] | None`
    — *count* is the payload segment count (tunnel: 9, link: 2); checks prefix,
    version, and exact total length, parse-to-None semantics; returns exactly
    *count* raw payload segments for the caller to decode.
- `kill_command(pids: Iterable[int]) -> str` — `kill <sorted pids>`.

### 2. Domain refactor

- `tunnel/sentinel.py` — keeps `SENTINEL_PREFIX`, `SENTINEL_VERSION`, `ParsedSentinel`,
  and the domain payload codec (path double-encoding, port/enum decoding); framing and
  percent-encoding go through `otto.host.daemon`.
- `tunnel/discovery.py` — `parse_process_discovery` becomes a thin wrapper
  (`daemon.parse_ps_output` → `parse_sentinel` per token → `Observation`);
  `parse_etime` moves out; the ps command constant moves here from socat.py, defined
  as `ps_scan_command(SENTINEL_PREFIX)`. `_scan_hosts` (async fan-out, best-effort,
  unreachable tracking) stays put.
- `link/sentinel.py` — `IMPAIR_PS_COMMAND` defined via `ps_scan_command`;
  `parse_impair_ps` wraps `daemon.parse_ps_output` (ignores the age field); encode/
  parse ride the framing helpers. Public signatures unchanged.
- Reap orchestration stays domain-side: tunnel's lab-wide discover→kill→re-scan→
  survivors report and link's single-host timer cancel share only `kill_command`.

### 3. `otto.tunnel.carrier` — the carrier seam

Mirrors `link/impairer.py`:

```python
class TunnelCarrier:
    """Builds the argv each tunnel process executes. Stateless; manage.py runs them."""

    supported_protocols: ClassVar[frozenset[str]] = frozenset()
    requirements_command: ClassVar[str]  # complete shell probe line; prints "ok" iff satisfied

    def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port) -> list[str]: ...
    def relay_args(self, carrier_port, next_ip) -> list[str]: ...
    def egress_args(self, protocol, service_port, deliver_ip, carrier_port) -> list[str]: ...

CARRIERS: Registry[type[TunnelCarrier]] = Registry(
    "carrier", register_hint="otto.tunnel.register_carrier()"
)
def register_carrier(name, cls, *, overwrite=False) -> None: ...  # rejects empty supported_protocols
def build_carrier(name) -> type[TunnelCarrier]: ...
```

- `socat.py` keeps its pure builder functions and grows `SocatCarrier` delegating to
  them (`supported_protocols = frozenset({"tcp", "udp"})`), registered at module
  import — the NetEm pattern (`netem.py:112`) verbatim. `otto.tunnel.__init__`
  exports `TunnelCarrier`, `CARRIERS`, `register_carrier`, `build_carrier` the way
  `otto.link` exports the impairer family.
- `manage.py`: `add_tunnel(..., carrier: str = "socat")`; resolve once via
  `build_carrier`; `_process_plan` calls carrier methods; `_require_tools` runs the
  carrier's `requirements_command` (same loud host-named failure); the protocol check
  consults `carrier.supported_protocols` (unsupported protocol error now names the
  carrier). CLI `otto tunnel add` grows `--carrier` (default `socat`), chain-wide.
- Carrier-agnostic and unchanged: the 2n process plan shape, sentinel v1 (the carrier
  name is deliberately NOT on the wire — a tunnel's identity is path+protocol+port),
  free-port probing and `pick_free_port`, discovery, verify, remove. Remove reaps by
  pid from the sentinel scan, so it tears down any carrier's processes even if the
  registrant is no longer installed.

### 4. Deliberately divergent policies (do not unify)

- **Errors:** tunnel discovery is best-effort with transparent `unreachable`
  reporting; link operations raise loud host-named errors (never-skip-on-host-down).
- **Privilege:** link mutations sudo-if-not-root; tunnel processes unprivileged.
- **Timeouts:** each domain keeps its own 30s constant; the toolkit does no I/O.
- **Rollback:** on a mid-way FAILURE, `add_tunnel` kills every process launched by
  the failed call (nothing existed before it), while `impair_link` re-applies each
  touched placement's pre-call params (or clears placements that were clean) — it
  cannot just clear, because merge semantics mean a placement may carry state from an
  earlier successful impair call that this failed call must not destroy. Success
  paths are unaffected: a successful `impair_link` applies the requested merged
  params. Domain orchestration, not toolkit.

## Testing

- **Byte-identity pins:** golden-string tests assert `ps_scan_command("otto-tunnel")`
  and `ps_scan_command("otto-impair")` equal today's literal commands, and sentinel
  encodes produce today's exact tokens. Existing wire-pinning tests unmodified.
- **Toolkit units:** `parse_ps_output` table tests (short fields, non-digit pid,
  foreign tokens, malformed lines); `parse_etime` cases move with it; framing
  round-trips plus every parse-to-None branch.
- **Carrier units:** `SocatCarrier` argv equality against pre-refactor builder golden
  outputs; a fake carrier registered in-test proves plan→launch-argv end-to-end with
  no hosts; unknown-carrier rich error; `register_carrier` validation (non-empty
  `supported_protocols`).
- **Suites:** full tunnel + link suites green with only import-path updates; live-bed
  e2e (multi-hop add/remove, impair/repair, centos:7 hops) is the real proof of
  behavioral identity. Gates: `make coverage` + lint + ty + docs.

## Ride-alongs

- Fix `tunnel/model.py` module docstring's false claim that tunnel imports otto.link.
- Drop `tunnel/socat.py`'s back-compat `launch_command` re-export.

## Out of scope

- Port-scoped impairment (impair only selected ports/protocols on a link instead of
  the whole netdev) — flagged by Chris 2026-07-11 as a high-value follow-up; needs
  its own design (classful tc tree + filters, per-selector state model, read-back
  parsing, timer identity). Cleaner AFTER this extraction: the impair sentinel would
  gain a selector segment via the shared framing.
- Sentinel v2 / carriers that change process topology (WireGuard-style mesh).
- `Provenance.DYNAMIC` production (monitor Plan 4 topology).
- Socat tunnel stability tests (separate TODO item).
- Whole-tree unification of `asyncio.wait_for` host-call wrappers.
