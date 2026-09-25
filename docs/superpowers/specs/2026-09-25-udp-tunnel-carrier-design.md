# UDP tunnels carry UDP between hops — design

**Status:** approved in conversation 2026-09-25; §2.3 (no idle timeout unless asked for) amended 2026-09-25 at Chris's direction.
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
| ingress | `socat TCP4-LISTEN:<svc>,bind=<ip>,fork,reuseaddr TCP4:<next>:<carrier>` | `socat -b 65535 UDP4-LISTEN:<svc>,bind=<ip>,fork,reuseaddr UDP4:<next>:<carrier>` |
| relay | `socat TCP4-LISTEN:<carrier>,fork,reuseaddr TCP4:<next>:<carrier>` | `socat -b 65535 UDP4-LISTEN:<carrier>,fork,reuseaddr UDP4:<next>:<carrier>` |
| egress | `socat TCP4-LISTEN:<carrier>,fork,reuseaddr TCP4:<deliver>:<svc>` | `socat -b 65535 UDP4-LISTEN:<carrier>,fork,reuseaddr UDP4:<deliver>:<svc>` |

These are the defaults. With `--idle-timeout N` (§2.3), every role of either protocol also carries `-T N`, right after `socat` and any `-b`.

- **`-b 65535`:** socat's transfer block holds the largest datagram whole. The IPv4 UDP payload maximum is 65,507 bytes.
- **No idle timeout by default** (§2.3).
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

### 2.3 Idle timeout: none unless asked for

A tunnel stays up, and every flow through it stays open, until `otto tunnel remove`. However long a flow is quiet, nothing times it out. Users expect a tunnel to survive idle stretches, and a default timeout would break exactly the traffic they don't watch: a datagram the server sends to a client that has been silent longer than the timeout would find that flow's children gone and be dropped.

- **Opting in:** `otto tunnel add --idle-timeout SECONDS` (Python: `add_tunnel(..., idle_timeout=SECONDS)`, default `None`) adds socat's `-T SECONDS` to every process of the tunnel, for TCP and UDP alike.
  - A TCP connection with no traffic in either direction for that long is closed.
  - A UDP flow's per-peer children exit. The listening parents never do, so the tunnel stays up. The flow's next datagram forks a fresh chain: the ingress's new child sends from a new source port, so each downstream hop forks too.
- **Validation:** the value must be a whole number of seconds, 1 or more. The CLI enforces this as a usage error (exit 2); the API raises `ValueError` before any host is contacted.
- **Contract:** each of the carrier's three argv methods takes a keyword-only `idle_timeout: int | None`.
- **Listing:** `tunnel list` does not show the timeout. It is not part of the tunnel's identity or sentinel, and discovery never parses argv.
- **Bed proof:** the spike (§5) proves that `-T` ends idle children and never the listening parent before the option ships. If the parent dies too, the option is dropped and a follow-up is filed.

## 3. Costs (accepted)

1. A firewall between hops must pass **UDP** on the carrier ports for a UDP tunnel. The tunnel docs say so.
2. A stateful firewall or conntrack between hops can age out an idle UDP flow. Only labs with filtering hops are affected.
3. A future carrier that can only carry TCP (e.g. `ssh -L`) would need its own UDP framing.
4. **Without `--idle-timeout`, UDP children accumulate.** `UDP4-LISTEN,fork` forks one child per client source address at every hop, and a UDP child has no end of stream to exit on. So a UDP tunnel holds one socat per hop for every client address it has ever seen, until it is removed. This was already true of today's UDP ingress. It stays small for clients that keep one source port, but a client that picks a fresh source port per request (many DNS resolvers, some SNMP managers) grows it without bound. The docs name this case and point to `--idle-timeout`.

## 4. Documentation

- `docs/cli/tunnel/add.md`: the `--protocol` row stops saying "always relays between hops over a plain-TCP carrier stream". It states per-protocol carriage and the 65,507-byte datagram ceiling.
- `docs/cli/tunnel/add.md`: a new `--idle-timeout` row. By default nothing times out. Setting it closes idle TCP connections and ends idle UDP flows' children, the tunnel itself stays up, and a server-initiated datagram to a client quiet for longer than the timeout is lost. It is recommended for UDP clients that change source port per request (§3 cost 4).
- `docs/cli/tunnel/endpoints.md` / `portability.md` (whichever holds the host requirements): UDP tunnels need UDP carrier ports open between hops.
- `todo/udp_hop_forwarding.md`: the datagram-boundary caveat is resolved for otto tunnels (its SSH-hop transport item is a different thing and stays).

## 5. Testing

- **Bed spike (first, before any code):** on test1→test2→test3, with the new argv launched by hand:
  - datagrams of 1 B, 1400 B and 65,000 B echo back whole;
  - 20 back-to-back datagrams of distinct sizes arrive as 20 datagrams;
  - with the default argv (no `-T`), the children are still there after an idle stretch, and the flow still delivers;
  - with `-T`, an idle child exits while the parent keeps listening, and a new datagram after that is still delivered.
  If `-T` also ends the parent, `--idle-timeout` does not ship (§2.3).
- **Unit:**
  - exact argv for all three roles × both protocols, with and without an idle timeout (TCP's default argv byte-identical to today's);
  - `--idle-timeout` validation (CLI exit 2; API `ValueError` before any host is contacted) and its plumbing into the process plan;
  - the carrier contract with the protocol parameter;
  - the probe command and the parsing of TCP+UDP listeners;
  - `socket_dump_command` for each protocol, with `parse_port_holders` against real `ss -Huan` / `netstat -uan` samples.
- **Bed e2e** (`tests/e2e/test_tunnel_e2e.py`): a new UDP echo test on the 3-hop path test1,test2,test3. From the dev VM:
  - datagrams of 1 B, 1400 B and 65,000 B are echoed back byte for byte;
  - a burst of 20 distinct datagrams comes back as exactly those 20 datagrams;
  - a UDP tunnel added with a short `idle_timeout` drops its idle children, keeps its parents, and delivers again afterwards.
  The existing tunnel e2e (including the centos:7 old-socat container endpoint), the stability suite (`make stability-tunnel`) and the chaos suite stay green.
