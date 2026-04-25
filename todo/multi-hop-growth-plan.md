# Multi-Hop Connectivity: Growth Plan

This document outlines the roadmap for extending otto's multi-hop host
connectivity beyond the Phase 1 implementation (single SSH hop).

## Current State (Phase 1)  ✅ DONE

- `RemoteHost.hop: str | None` -- host ID of an intermediate SSH hop
- `ConnectionManager` accepts a `tunnel_factory` for SSH tunneling
- SSH connections use asyncssh's native `tunnel` parameter
- FTP and Telnet connections use SSH local port forwarding
- Netcat transfers through hops are not yet supported
- CLI `--hop` option for one-off overrides
- Recursive hop resolution with cycle detection

## Phase 2: Full SSH Chains  ✅ DONE

**Goal:** `otto -> SSH -> hop1 -> SSH -> hop2 -> ... -> any_protocol -> target`

asyncssh tunnels compose naturally -- each hop's `SSHClientConnection`
becomes the `tunnel` parameter for the next connection.

### HopTransport Abstraction

`HopTransport` protocol (`src/otto/host/transport.py`) decouples the
transport mechanism from `ConnectionManager`:

```python
class HopTransport(Protocol):
    async def get_tunnel(self) -> SSHClientConnection: ...
    async def forward_port(self, dest_host: str, dest_port: int) -> int: ...
    async def close(self) -> None: ...
```

Concrete implementations:

- **`SshHopTransport`** (`src/otto/host/transport.py`): wraps an
  `SSHClientConnection`. Owns the tunnel connection and port-forward
  listeners. Supports a `parent` transport for cascade cleanup in
  multi-hop chains.

- **`TelnetHopTransport`** (Phase 3): wraps a telnet session on the hop
  and runs commands to establish connectivity to the next host.

### Netcat Through SSH Hops

- **PUT**: supported via `forward_local_port` — otto connects to a
  forwarded local port that tunnels to the remote nc listener.
- **GET**: supported via reversed-listener approach — remote host runs
  `nc -l <port> < <file>` as a listener, otto connects through an SSH
  port forward and reads the file data. Same tunnel mechanics as PUT,
  reversed data flow.

### Work Items

1. ✅ Extract `SshHopTransport` from the inline tunnel logic in `ConnectionManager`
2. ✅ Update `ConnectionManager` to accept `HopTransport` instead of a raw factory
3. ✅ Add netcat PUT support through SSH port forwarding
4. ✅ Integration tests for multi-hop SSH chains (`@pytest.mark.hops`) —
   3-VM topology (test1/test2/test3) in Vagrantfile
5. ✅ FTP through SSH hops via `TunneledFtpClient` — subclasses
   `aioftp.Client` to intercept PASV data connections and route them
   through SSH port forwards

## Phase 3: Mixed Protocol Chains

**Goal:** `otto -> SSH -> hop1 -> Telnet -> hop2 -> SSH -> target`

### The Core Challenge

SSH provides native tunneling (port forwarding, channel multiplexing).
Telnet provides none. A telnet hop can only "tunnel" by executing
commands on the hop host (e.g., running `ssh next_host` or
`nc next_host port` inside the telnet session).

### TelnetHopTransport

A `TelnetHopTransport` wraps an established `TelnetSession` on the
intermediate host. To reach the next host, it runs commands:

- **SSH to next host**: execute `ssh user@next_host` in the telnet
  session, then handle login prompts with send/expect patterns.
- **Telnet to next host**: execute `telnet next_host` in the telnet
  session.
- **Arbitrary commands**: `exec_on_hop(cmd)` runs a command on the
  intermediate host.

This is inherently more fragile than SSH tunneling -- it depends on
the hop having the right tools installed and correct prompt patterns.

### File Transfer Through Mixed Chains

All-SSH chains compose natively for SCP/SFTP. Mixed chains with telnet
hops cannot tunnel file transfers at the transport level.

**Recommended approach: Relay transfers.**

Implement a `RelayTransfer` strategy that detects telnet hops in the
chain and automatically stages files through SSH-reachable hops:

1. PUT: copy file to the last SSH-reachable hop via SCP/SFTP, then
   from that hop push to the target using whatever the target supports.
2. GET: reverse the process -- pull from the target to the hop, then
   from the hop to otto.

The combinations are finite and otto already has all the building blocks
(file transfer to any host, command execution on any host).

### Testing With 2 VMs

2 VMs are sufficient for most mixed-protocol testing:

- **SSH -> Telnet**: otto -> VM1 (SSH hop) -> VM2 (telnet target).
  VM2 runs both sshd and telnetd.
- **Telnet -> SSH**: otto -> VM1 (telnet hop) -> VM2 (SSH target).
  Otto opens a telnet session to VM1, runs `ssh VM2` inside it.

3 VMs would only be needed for 3-hop chains. This can be deferred.

### Work Items

1. Implement `TelnetHopTransport` with send/expect-based SSH tunneling
2. Design and implement the `RelayTransfer` file staging mechanism
3. Handle edge cases: login prompts, key acceptance, timeouts
4. Integration tests for mixed-protocol chains
5. Document limitations (fragility, tool requirements on hop hosts)
