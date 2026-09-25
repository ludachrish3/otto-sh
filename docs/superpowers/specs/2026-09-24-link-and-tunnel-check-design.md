# `otto link check` and `otto tunnel check` — design

**Status:** approved in conversation 2026-09-24; written spec awaiting review.
**Follow-up issue:** #440 (payload-verified TCP/UDP to `--dest` devices that run no socat).

## 1. Purpose

otto's link and tunnel features depend on things that vary widely between labs: tc and kernel versions, userland (GNU/BusyBox), ISA, installed tools, interface layout. A feature can work well on otto's bed and then fall flat on a user's lab in a way nobody predicted. otto will not grow a conformance matrix across tc/kernel versions; its test budget can't afford that.

Instead, users get a **setup-time check** they run against their own lab:

- **`otto link check`**: how complete is netem feature support for this link, as measured on the hosts that would impair it?
- **`otto tunnel check`**: can this tunnel be built on this path, with payloads flowing through it end to end, for TCP and UDP?

Each check has two readers:

- **The user**, who gets a plain answer: what works, what doesn't, why, and what to do about it.
- **otto's developers**, who receive the same result as a `--report` JSON file attached to an issue: host fingerprints and per-feature verdicts with evidence. This is how otto learns which environments work and grows its documented proven range.

The CLI is the primary surface. The Python API returns the same result object, for complex labs (long multi-hop tunnels, mixed host vintages) and for bulk surveys that the CLI deliberately doesn't offer.

## 2. Scope

**In scope (v1)**

- `otto link check <link> [--live] [--feature …] [--from HOST] [--report PATH]` and `check_link(...)`.
- `otto tunnel check --hosts … --port P [--protocol tcp|udp|both] [--dest host[@if]] [--carrier NAME] [--report PATH]` and `check_tunnel(...)`.
- A shared `otto.check` core: verdicts, fingerprints, rendering, report schema, the proven-range data file.
- A docs page "Known-good environments" rendered from the proven-range file.

**Out of scope (v1)**

- More than one link or tunnel per CLI call. Bulk surveys go through the Python API.
- Checking an **existing** tunnel by id. The check builds its own throwaway tunnel, and `tunnel list` already covers process health for live tunnels.
- Payload proof on the last segment to a `--dest` device that runs no socat (#440).
- Throughput testing beyond the netem rate probe (no iperf-class tooling).
- A conformance matrix of tc/kernel versions in otto's own CI.

## 3. Shared core: `otto.check`

### 3.1 Verdicts

| Verdict | Meaning | Counts as failure |
|---|---|---|
| `pass` | applied and measured as configured | no |
| `fail` | applied (tool accepted it) but measured wrong | yes |
| `unsupported` | the tool or kernel rejected the command: a missing capability | yes |
| `unmeasured` | could not produce evidence either way; always carries a reason code | no |
| `skipped` | a prerequisite step failed, so this step did not run | no |

`unmeasured` reason codes, v1: `missing-tool` (names the tool), `noisy-baseline` (the control measurement was too unstable to trust; the baseline figures are included), `no-reply-oracle` (UDP to a device that runs nothing of otto's), `no-clock` (no sub-second clock on the host; timing is omitted but a payload proof still stands).

`fail` vs `unsupported` is the split that matters most to developers: "your tc can't do this" versus "otto does this wrong on your host".

### 3.2 Positive control

Before any timing-based feature is judged, a control runs: the unimpaired baseline, or a direct send without a tunnel, must behave as expected and be stable. If it isn't, every timing-dependent row reports `unmeasured (noisy-baseline)` instead of a pass that can't be trusted. Pass bounds are stated as `want X ±tolerance`, and the tolerance comes from the control's measured spread plus a fixed floor.

### 3.3 Host fingerprint

Collected on every host the check touches, before anything else runs, in one batched probe per host:

- kernel (`uname -r`), ISA (`uname -m`), userland (otto's existing GNU/BusyBox detection);
- privilege (root, or passwordless sudo as otto already uses it);
- link check: iproute2/tc version (`tc -V`), whether the netem module is available, `ip netns` support, `ping`, and `socat`/`nc`/`python3` (only the rate and port probes need these);
- tunnel check: socat version, the launcher that would be used (`systemd-run --user`, or the `setsid` fallback), bash, and `$EPOCHREALTIME` availability.

Tool presence uses the batched `command -v` idiom already used for userland detection.

### 3.4 Proven range

`src/otto/check/proven.json` holds one entry per component: tc/iproute2, kernel, ISA, userland, socat, launcher, bash. Each entry records the versions otto has proven, with where and when (bed host and date). Every fingerprint line is labeled against it as **within**, **older than** or **newer than** the proven range.

- Rows are added only from otto's own bed runs. User reports propose new rows; they don't add them automatically.
- The docs page "Known-good environments" is generated from this file, so there is one home for it. It asks users outside the range to send a `--report`.

### 3.5 Rendering (stdout)

One block per host: a fingerprint line, a proven-range line, then a feature table (rich, borderless, matching the existing link/tunnel scan tables). Passing rows stay one line. Any row that isn't `pass` gets evidence lines beneath it, in this order:

1. measured vs wanted, with the tolerance;
2. the exact command otto ran, and the tool's stderr for `unsupported`;
3. a hint with a docs link when the cause is known: a missing module or tool, a feature newer than the host's tc, a safety refusal (naming the rule), or a hop-derived link that can't be impaired (pointing at how to declare the link with interfaces).

`-v` adds the raw probe output for every row (ping summaries, `tc -s qdisc` stats, socat stderr). The run ends with one summary line per host: counts per verdict.

Addresses are always shown on stdout and always included in the report. Nothing about them is secret, and users can redact before posting.

### 3.6 Report (`--report PATH` only)

Nothing is written by default. With `--report PATH`, the result object is written as JSON with a versioned schema (`schema: "otto-check/1"`): otto version, proven-range revision, per-host fingerprint, and per-feature verdict, reason code, measured and wanted values, tolerance, the commands run, and the relevant stderr. The docs ask users to attach this file when they report a problem.

### 3.7 Exit codes

- `0`: nothing failed. `unmeasured` and `skipped` don't count against the host.
- `1`: any `fail` or `unsupported`.
- `2`: usage error, following the CLI's existing convention.

### 3.8 Dry run

The global `--dry-run`/`-n` contacts no device and prints the plan:

- which hosts would be fingerprinted and sandboxed;
- which live steps would run, and roughly how long each disturbs the link;
- which scratch ports and namespace names the check would use.

It states plainly that nothing was measured.

### 3.9 Cleanup

Every artifact a check creates carries an otto sentinel tag: `otto-check-*` network namespaces, echo listeners, throwaway tunnels, and live impairments (each also has a short `--expire` as a dead-man switch). All of them are removed in a `finally`. Each run first sweeps up leftovers from an earlier run that was killed, and says so in its output.

### 3.10 Naming

The Python functions are `check_link` and `check_tunnel`, and the CLI verb is `check` to match. Nothing public starts with `test_`: pytest would collect such a name wherever users import it into their test modules.

## 4. `otto link check`

```text
otto link check <link> [--live] [--feature F[,F…]] [--from HOST] [--report PATH]
```

```python
check_link(lab, "edge", live=False, features=None, from_host=None) -> LinkCheckReport
```

### 4.1 Which hosts

The link's existing placement decides, per direction, which host and netdev would impair it: the endpoint host, or the in-path middlebox. Those hosts are the ones fingerprinted and sandboxed. `--from HOST` limits the check to one direction. If placement refuses the link (for example, a hop-derived link with no named interface), the check reports that refusal as its result, with the hint for declaring the link. It doesn't go on to sandbox an unrelated host.

### 4.2 Sandbox (always runs)

On each impairing host, otto creates a throwaway netns `otto-check-<id>` with a veth pair and applies each feature through the same `netem.py` command builders and read-back parser that `impair`/`list` use. The user's interfaces are never touched. This needs root and netns support; without them, every sandbox row is `skipped`, and the hint says what's missing.

| Feature | Probe | Pass rule |
|---|---|---|
| read-back | apply a known tree, parse it with otto's reader | parses back exactly to what was written |
| delay | ping RTT: baseline vs impaired | delta ≈ configured delay |
| jitter | ping mdev | spread rises by roughly the configured jitter |
| loss | 200 fast pings at a configured loss | loss inside a binomial tolerance band |
| duplicate | ping duplicate count | duplicates observed |
| reorder | sequence order under delay | out-of-order replies observed, or `unmeasured` with the reason |
| corrupt | loss rise from corrupted frames | observed, or `unmeasured` |
| rate | time a fixed-size transfer (socat, nc or python3) | throughput ≈ configured rate |
| port range / side | TCP connect: in-range vs out-of-range port, dst vs src | only the matching port and side are delayed |
| expire | apply with a short expire | tree cleared by the timer |

`--feature` narrows the set. read-back always runs, because every other verdict depends on it.

### 4.3 Live (`--live`)

`--live` runs after the sandbox. It performs a short, real cycle on the link itself, limited to the features where a real netdev can behave differently from a veth pair:

| Feature | Why it's checked live |
|---|---|
| read-back | the real tree may hold other qdiscs; real interface naming (VLAN sub-interfaces, bonds) |
| delay | proves placement: the right host, netdev, direction and middlebox |
| port range / side | the u32 fixed-offset match can break under real encapsulation (VLAN tags, tunnels) |
| rate | NIC offloads (TSO/GSO) and multi-queue roots can skew shaping on real hardware |
| loss | cheap; proves the drops land on the real direction |

Probes on the live link measure between the link's endpoints, comparing each impaired port against a clean port on the same link, the differential pattern otto's own e2e tests use. Each impairment carries a short `--expire`. The cycle ends with a repair and a heal check. All normal safety refusals apply, and a refusal is reported as the result, naming the rule. A feature that failed in the sandbox is `skipped` live. The other features show `n/a` in the live column, with a legend line: "n/a: sandbox result applies; a real link doesn't change this feature".

## 5. `otto tunnel check`

```text
otto tunnel check --hosts h0[@if],…,hn-1[@if] --port P [--protocol tcp|udp|both]
                  [--dest host[@if]] [--carrier NAME] [--report PATH]
```

```python
check_tunnel(lab, hosts=[...], port=P, protocol="both", dest=None, carrier=None) -> TunnelCheckReport
```

The arguments mirror `tunnel add`, so a user can check a path and then build the same tunnel for real. `--protocol both` (the default) runs the TCP and UDP checks in turn.

### 5.1 The only dependency is socat

`tunnel add` already requires `bash` and `socat` on every hop, so the check adds no new requirement. socat plays every role:

| Role | Where | Command shape |
|---|---|---|
| TCP echo | delivery point | `socat TCP-LISTEN:<scratch>,fork,reuseaddr PIPE` |
| UDP echo | delivery point | `socat UDP-RECVFROM:<scratch>,fork PIPE` |
| client | first hop | `socat - TCP:<ingress>:<scratch>` / `UDP:…` |
| segment listener | hop i+1 | socat listener on a scratch port |

Echo listeners are launched and tagged exactly like tunnel processes (`systemd-run --user`, or the `setsid` fallback, with an argv sentinel), so teardown and later sweeps find them. Timing uses bash's `$EPOCHREALTIME`. Without it, RTT is `unmeasured (no-clock)` and the payload verdicts stand.

### 5.2 Steps

0. **Fingerprint** every hop (§3.3).
1. **Segment reachability.** For each consecutive pair of hops: a scratch listener on hop i+1, and a connect from hop i to the address the tunnel would use. This locates a failure: "hop 2 → hop 3: connection refused on 10.10.200.13:41873".
2. **Build and probe.** Build the tunnel on a scratch service port, delivering to an otto echo, so the user's real service is never touched. Payloads carry a nonce and must echo back byte for byte:
   - sizes of 1 B, ~1400 B (near the MTU) and 64 KiB (TCP streaming / UDP fragmentation);
   - both directions (each tunnel is bidirectional);
   - several round trips on one connection.

   The RTT through the tunnel is reported next to the sum of the segment RTTs from step 1. `tunnel list`'s view of the throwaway tunnel must be `ok`, which proves the sentinel tags reconstruct it.
3. **Teardown and verify.** Remove the tunnel and echo, then confirm no tagged process survives on any hop.

### 5.3 A `--dest` device that runs nothing of otto's

When `--dest` names a device that doesn't run socat (an embedded target, for example), the proof is split, and the report says so:

- **Hop chain:** the throwaway tunnel delivers to an echo on the last *hop*, which payload-verifies every otto-managed segment.
- **Last segment (last hop → device):**
  - TCP: a socat connect from the last hop to the device's real port. A completed handshake is `pass` with the qualifier "handshake only".
  - UDP: `unmeasured (no-reply-oracle)`.

A summary line states exactly what was proven, e.g. "hop chain: payload-verified; last segment to zephyr: TCP handshake only". Closing this gap is #440.

## 6. Code layout

- `src/otto/check/`: verdicts, dataclasses (`FeatureResult`, `HostFingerprint`, `LinkCheckReport`, `TunnelCheckReport`), fingerprint probe, proven-range loader and comparison, renderer, report writer, `proven.json`.
- `src/otto/link/check.py`: `check_link`, sandbox and live probes. It reuses the `netem.py` builders and parser, and placement.
- `src/otto/tunnel/check.py`: `check_tunnel`, segment and payload probes. It reuses the carrier, launcher and sentinel.
- `src/otto/cli/link.py`, `src/otto/cli/tunnel.py`: the `check` verbs (thin wrappers, as with the other verbs).
- The measurement helpers now private to `tests/e2e/test_link_impair_e2e.py` (ping parsing, timed connect+echo) move into library code under `otto.check`/`otto.link.check`, and the e2e tests switch to them.

## 7. Documentation

- `docs/cli/link/check.md` and `docs/cli/tunnel/check.md`: usage, verdict meanings, reading the evidence, sample output, and "attach `--report` to issues".
- "Known-good environments": generated from `proven.json`; linked from both check pages and from the link/tunnel index pages.
- API reference entries for `check_link`, `check_tunnel` and the report types.
- The verdict vocabulary is defined once (on a shared page), and the two check pages link to it.

## 8. Testing

- **Unit:**
  - verdict decisions over recorded probe outputs;
  - fingerprint parsing and proven-range comparison (within / older / newer);
  - renderer golden output;
  - report schema round-trip;
  - parsers for ping, `tc -s`, `tc -V`, socat versions and `$EPOCHREALTIME`.
- **Fault injection:** each verdict has a test that forces it and a positive control that proves the check can go red. tc rejecting a feature must give `unsupported`, not `fail`; a missing tool gives `unmeasured (missing-tool)`; a noisy control gives `unmeasured (noisy-baseline)`; a dead segment names its hop pair.
- **Live on the bed:**
  - `link check` sandbox on a modern-userland host and on an old-userland host;
  - `link check --live` on the `edge` link;
  - `tunnel check` over a 3-hop path for TCP and UDP;
  - `tunnel check --dest` against the zephyr target, asserting the split verdict;
  - cleanup verified: no namespaces, listeners or tunnels left after a normal run or a killed one.
- **Dry run:** it contacts no device, and its plan names every host, port and live step.

## 9. Open follow-ups

- #440: payload-verified TCP/UDP last segment to `--dest` devices that run no socat.
- Checking an existing tunnel by id (declined for v1; reconsider if users ask).
