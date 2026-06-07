# UDP forwarding over an SSH hop

## Motivation

otto reaches hosts behind an SSH jump host via `SshHopTransport`
([src/otto/host/transport.py](../src/otto/host/transport.py)), whose
`forward_port()` sets up **TCP** local forwarding (asyncssh). That covers
telnet/SSH/SFTP/etc. It does **not** cover **UDP** services — and SSH itself
forwards only TCP. So any UDP service on a host more than one hop away
(SNMP/161, syslog/514, DNS, custom telemetry sockets) is currently unreachable
through a hop.

This surfaced while adding SNMP-based performance monitoring (the embedded
plan). The Zephyr test bed sidesteps it because the dev VM and the zephyr VM
share an L2 private network, so a `socat` UDP relay bound on the zephyr VM is
reachable directly (see the `zephyr-snmp-relay-*` units in the Vagrantfile).
But that is a **test-bed-local** workaround. A real Zephyr/embedded target
genuinely behind an SSH-only hop would have no path for SNMP today.

## Proposed feature

Make UDP-over-hop a first-class otto transport capability, engaged
**implicitly** whenever a UDP port on a host more than one hop away must be
reached — the caller asks for a forwarded UDP endpoint and gets a local
`(host, port)` to send datagrams to, exactly as TCP forwarding works today.

Mechanism (socat-bridge approach, no SSH protocol changes):

1. On the hop, spawn a managed `socat UDP4-LISTEN:<p>,fork TCP4:<dest>:<udp_port>`
   (UDP→TCP) — or reuse a relay already there.
2. SSH-forward that TCP port to the local side (existing `forward_port`).
3. Locally, spawn `socat UDP4-LISTEN:<local>,fork TCP4:127.0.0.1:<fwd>` (TCP→UDP)
   so the caller sends plain UDP to `127.0.0.1:<local>`.

i.e. UDP↔TCP socat bridges on each end of the existing TCP tunnel. Lifecycle
(spawn/track/teardown) parallels the current port-forward bookkeeping; the
bridges are cleaned up with the hop.

### Open questions

- **Implicit vs explicit.** Auto-engage for any UDP forward >1 hop, or require
  an opt-in flag on the host/transport? Implicit is friendlier but spawns
  helper processes the user didn't ask for.
- **Dependency on `socat` at both ends.** Acceptable for the lab; for arbitrary
  hops, detect/fallback (or a pure-Python UDP relay coroutine on the otto side,
  avoiding the local socat at least).
- **Datagram boundaries / MTU.** socat's UDP↔TCP framing is stream-based;
  request/response SNMP is fine, but document the caveat for chunked UDP.

### Done means

- `SshHopTransport` (or a sibling) exposes a UDP-forward that returns a local
  UDP endpoint, used transparently by the SNMP manager for hop-reached hosts.
- The test-bed `zephyr-snmp-relay-*` units can then be retired in favor of the
  general path (the embedded plan's Phase 3).
