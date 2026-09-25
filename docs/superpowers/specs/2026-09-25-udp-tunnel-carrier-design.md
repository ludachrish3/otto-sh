# UDP tunnels carry UDP between hops — design

**Status:** approved in conversation 2026-09-25.
**Amends:** `2026-07-08-link-cli-tunnels-design.md` §3 decision 3 ("two-socat UDP↔TCP bridge … TCP carrier") and `2026-07-09-tunnel-2b-design.md`, for `--protocol udp` only.
**Unblocks:** the 64 KiB UDP payload in `otto tunnel check` (`2026-09-24-link-and-tunnel-check-design.md` §5.2).

## 1. Problem

A `--protocol udp` tunnel accepts datagrams at its ingress (`UDP4-LISTEN`) but crosses every hop on a TCP carrier stream (`TCP4-LISTEN` / `TCP4:`), and its egress turns that stream back into datagrams (`UDP4:`). socat copies in blocks of its default 8192 bytes, and a TCP stream has no datagram boundaries. So:

- A datagram larger than 8 KiB leaves the egress as several datagrams.
- Two datagrams sent back to back can leave the egress as one datagram (TCP coalesces them). How they are cut again depends on read timing.

The problem is silent. Request/response traffic with small datagrams (SNMP, DNS) works, which is why it went unnoticed. The 2026-07-08 design chose the TCP carrier so that a future `ssh -L` carrier across ssh-only jump hosts could drop in, and that carrier was never built. `todo/udp_hop_forwarding.md` already records the caveat.

socat on its own cannot put length-prefix framing on a TCP stream, and otto's only hop requirement is socat and bash.

## 2. Decision

For `--protocol udp`, the socat carrier carries **UDP between hops**, so every datagram stays one datagram at every hop. `--protocol tcp` is unchanged.

| Role | TCP tunnel (unchanged) | UDP tunnel (new) |
|---|---|---|
| ingress | `socat TCP4-LISTEN:<svc>,bind=<ip>,fork,reuseaddr TCP4:<next>:<carrier>` | `socat -b 65535 -T 120 UDP4-LISTEN:<svc>,bind=<ip>,fork,reuseaddr UDP4:<next>:<carrier>` |
| relay | `socat TCP4-LISTEN:<carrier>,fork,reuseaddr TCP4:<next>:<carrier>` | `socat -b 65535 -T 120 UDP4-LISTEN:<carrier>,fork,reuseaddr UDP4:<next>:<carrier>` |
| egress | `socat TCP4-LISTEN:<carrier>,fork,reuseaddr TCP4:<deliver>:<svc>` | `socat -b 65535 -T 120 UDP4-LISTEN:<carrier>,fork,reuseaddr UDP4:<deliver>:<svc>` |

- **`-b 65535`:** socat's transfer block holds the largest datagram whole. The IPv4 UDP payload maximum is 65,507 bytes.
- **`-T 120`:** `UDP4-LISTEN,fork` forks one child per peer, and a UDP child otherwise never exits. Today's UDP ingress already leaks one child per client this way. A child idle for 120 s exits. A flow that goes quiet for longer re-forks on its next datagram: the ingress's new child sends from a new source port, so each downstream hop forks a fresh child as well. The listening parent is unaffected. This is verified on the bed before anything relies on it (§5).
- **Unchanged:** the reply path. Each child's `UDP4:` side is a connected socket, so replies return hop by hop, as the TCP carrier's did.
- **Unchanged:** sentinel tags, discovery, `tunnel list`, `remove`, the process plan (2n processes, downstream first), the same carrier port on every hop of a direction, the loopback delivery and the §6.3 bind rules.

### 2.1 Carrier contract

`TunnelCarrier.relay_args` gains the protocol: `relay_args(protocol, carrier_port, next_ip)`, matching `ingress_args`/`egress_args`. otto has no other carrier; third-party carriers must add the parameter (pre-1.0, no compatibility shim).

### 2.2 Ports

- **Allocation:** the free-port probe (`FREE_PORT_PROBE_COMMAND`) reports TCP **and** UDP listeners (`ss -Htln` and `ss -Huln`, with `netstat` fallbacks). The set of used ports is their union, a safe superset for either protocol. `NoFreePortError` stops saying "TCP".
- **Diagnosis:** the dump taken to diagnose a post-add verify failure becomes protocol-specific, `socket_dump_command(protocol)`:
  - `ss -Htan` / `netstat -tan` for TCP;
  - `ss -Huan` / `netstat -uan` for UDP.
  A single-protocol `ss` dump keeps the local address in the same column that `parse_port_holders` reads. A mixed `-tu` dump would add a leading Netid column.

## 3. Costs (accepted)

1. A firewall between hops must pass **UDP** on the carrier ports for a UDP tunnel. The tunnel docs say so.
2. A stateful firewall or conntrack between hops can age out an idle UDP flow. Only labs with filtering hops are affected.
3. A future carrier that can only carry TCP (e.g. `ssh -L`) would need its own UDP framing.

## 4. Documentation

- `docs/cli/tunnel/add.md`: the `--protocol` row stops saying "always relays between hops over a plain-TCP carrier stream". It states per-protocol carriage and the 65,507-byte datagram ceiling.
- `docs/cli/tunnel/endpoints.md` / `portability.md` (whichever holds the host requirements): UDP tunnels need UDP carrier ports open between hops.
- `todo/udp_hop_forwarding.md`: the datagram-boundary caveat is resolved for otto tunnels (its SSH-hop transport item is a different thing and stays).

## 5. Testing

- **Bed spike (first, before any code):** on test1→test2→test3, with the new argv launched by hand:
  - datagrams of 1 B, 1400 B and 65,000 B echo back whole;
  - 20 back-to-back datagrams of distinct sizes arrive as 20 datagrams;
  - an idle child exits after `-T` while the parent keeps listening;
  - a new datagram after that is still delivered.
  If `-T` also ends the parent, drop `-T` and record the leak as a follow-up rather than shipping a tunnel that dies when idle.
- **Unit:**
  - exact argv for all three roles × both protocols;
  - the carrier contract with the protocol parameter;
  - the probe command and the parsing of TCP+UDP listeners;
  - `socket_dump_command` for each protocol, with `parse_port_holders` against real `ss -Huan` / `netstat -uan` samples.
- **Bed e2e** (`tests/e2e/test_tunnel_e2e.py`): a new UDP echo test on the 3-hop path test1,test2,test3. From the dev VM:
  - datagrams of 1 B, 1400 B and 65,000 B are echoed back byte for byte;
  - a burst of 20 distinct datagrams comes back as exactly those 20 datagrams.
  The existing tunnel e2e (including the centos:7 old-socat container endpoint), the stability suite (`make stability-tunnel`) and the chaos suite stay green.
