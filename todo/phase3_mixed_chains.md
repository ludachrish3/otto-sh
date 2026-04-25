# Phase 3: Mixed Protocol Chains — Implementation Plan

## Context

Phase 2 delivered SSH-only chains: `otto -> SSH -> hop1 -> SSH -> hop2 -> target`.
Phase 3 extends to mixed chains: `otto -> SSH -> hop1 -> Telnet -> hop2 -> SSH -> target`.

SSH provides native tunneling (port forwarding, channel multiplexing). Telnet provides
none. A telnet hop can only "tunnel" by executing commands inside its session (running
`ssh next_host` or `telnet next_host`). This is inherently more fragile than SSH
tunneling — it depends on prompt detection, login handling, and the hop having the
right tools installed.

## Core Design Challenge

`SshHopTransport` satisfies `HopTransport` by providing:
- `get_tunnel()` → `SSHClientConnection` (used as asyncssh `tunnel` parameter)
- `forward_port()` → local port forward (used for telnet/FTP/netcat connections)

A telnet hop can provide **neither**: no `SSHClientConnection`, no port forwarding.

Telnet hops provide **interactive sessions** — the only mechanism is to execute
commands (ssh, telnet, nc, scp) inside the hop's shell.

## Architecture

### TelnetHopTransport: session-based, not tunnel-based

Instead of providing tunnels and port forwards, `TelnetHopTransport` provides:

1. **`connect_through(dest_ip, user, password, protocol)`** — Runs
   `ssh user@dest` or `telnet dest` inside the hop's telnet session, handles
   login prompts via send/expect, and returns a `TelnetSession` wrapping the
   hop's reader/writer (which now pipe through to the target's shell).

2. **`exec_on_hop(cmd)`** — Runs a command on the hop itself via a separate
   telnet connection (the primary connection is occupied by the connect_through
   session). Required for relay file transfers.

3. **`close()`** — Tears down all connections, cascades to parent.

After `connect_through`, the hop's telnet streams are piped to the target's shell.
The sentinel-based `ShellSession.runCmd()` works transparently over this — it
doesn't care how many layers of ssh/telnet the bytes traverse.

### HopTransport protocol evolution

```python
class HopTransport(Protocol):
    async def get_tunnel(self) -> SSHClientConnection: ...
    async def forward_port(self, dest_host: str, dest_port: int) -> int: ...
    async def close(self) -> None: ...

    @property
    def supports_ssh_tunnel(self) -> bool: ...
```

- `SshHopTransport.supports_ssh_tunnel = True` — all existing methods work
- `TelnetHopTransport.supports_ssh_tunnel = False` — `get_tunnel()` and
  `forward_port()` raise `NotImplementedError`; callers must use
  `connect_through()` instead

### ConnectionManager changes

Add a `create_session()` method that encapsulates the telnet-hop branching:

```python
async def create_session(self) -> ShellSession:
    """Create a shell session to the target, handling telnet hops transparently."""
    if self._hop is not None and not self._hop.supports_ssh_tunnel:
        # Telnet hop: run ssh/telnet inside the hop's session
        user, password = self.credentials
        return await self._hop.connect_through(self._ip, user, password, self._term)

    # Standard path: use SSH tunnel or direct connection
    match self._term:
        case 'ssh':
            return SshSession(await self.ssh())
        case 'telnet':
            conn = await self.telnet()
            return TelnetSession(conn.reader, conn.writer)
```

This keeps the telnet-hop complexity out of `SessionManager`. The three places
in `SessionManager` that currently match on `self._connections.term` to create
sessions (`_ensure_session`, `exec`, `open_session`) delegate to
`create_session()` when a telnet hop is in the chain.

For `exec()` (stateless command execution): through a telnet hop, stateless exec
is not truly stateless — each call needs its own telnet connection to the hop
plus a connect_through. This is expensive. A pragmatic alternative: fall back to
`runCmd()` on a dedicated named session through the telnet hop, accepting the
sequential constraint.

### RemoteHost._build_hop_transport dispatch

Currently always creates `SshHopTransport`. Change to dispatch on `hop_host.term`:

```python
def _build_hop_transport(self):
    hop_host = get_host(self.hop)
    if hop_host.term == 'telnet':
        return TelnetHopTransport(...)
    else:
        return SshHopTransport(...)
```

The telnet hop needs: IP, credentials, port, prompt (optional), and a parent
transport (if the telnet hop itself is behind another hop — the parent provides
port forwarding to reach the telnet hop's port 23).

### File transfer: relay strategy

Through telnet hops, SSH-based transfers (SCP/SFTP) and port-forward-based
transfers (FTP/netcat) are unavailable. Instead, use **relay transfers** that
stage files through the last SSH-reachable host in the chain.

**Relay PUT** (local file -> target through telnet hop):
1. Identify the last SSH-reachable hop in the chain
2. Copy file to that hop via SCP/SFTP (uses existing transfer infrastructure)
3. From the hop, push to target by running `scp <file> user@target:<dest>` via
   `exec_on_hop()` (or netcat if scp is unavailable)
4. Clean up temp file on hop

**Relay GET** (target file -> local through telnet hop):
1. On the target, push file to SSH-reachable hop: run
   `scp <file> user@hop:<tmp>` via the target's interactive session
2. Copy from hop to otto via SCP/SFTP
3. Clean up temp file on hop

Detection: `FileTransfer` checks `self._connections.has_telnet_hop` (new property)
to decide whether relay is needed.

## Work Items

### 3a — TelnetHopTransport core (command execution)

**Goal**: `otto -> Telnet hop -> SSH target` works for command execution.

| # | Item | File(s) | Notes |
|---|------|---------|-------|
| 1 | Add `supports_ssh_tunnel` to `HopTransport` protocol | `transport.py` | Property, `True` for `SshHopTransport` |
| 2 | Create `TelnetHopTransport` class | `transport.py` | `connect_through()`, lazy telnet connection, parent chaining |
| 3 | SSH login handling in `connect_through` | `transport.py` | send/expect for `ssh user@host`: host key prompt (`yes/no`), password prompt, shell ready |
| 4 | Telnet login handling in `connect_through` | `transport.py` | send/expect for `telnet host`: login/password prompts, shell ready |
| 5 | Update `RemoteHost._build_hop_transport` | `remoteHost.py` | Dispatch on `hop_host.term`; pass parent transport for nested chains |
| 6 | Add `ConnectionManager.create_session()` | `connections.py` | Encapsulates telnet-hop branching |
| 7 | Update `SessionManager._ensure_session` | `session.py` | Use `create_session()` when telnet hop present |
| 8 | Update `SessionManager.open_session` | `session.py` | Same — use `create_session()` for named sessions |
| 9 | Update `SessionManager.exec` | `session.py` | Telnet-hop path: use named session (sequential) or fresh chain |
| 10 | Unit tests for `TelnetHopTransport` | `test_hop.py` | Mock telnet session, verify connect_through send/expect sequence |
| 11 | Integration tests (2-VM) | `test_hop_integration.py` | SSH->Telnet target, Telnet hop->SSH target |

### 3b — exec_on_hop and relay transfers

**Goal**: File transfers work through telnet hops via relay staging.

| # | Item | File(s) | Notes |
|---|------|---------|-------|
| 1 | Implement `exec_on_hop(cmd)` | `transport.py` | Opens separate telnet connection to hop for command execution |
| 2 | Add `has_telnet_hop` property | `connections.py` | Checks if hop exists and `not supports_ssh_tunnel` |
| 3 | Implement relay PUT | `transfer.py` | Stage on SSH hop, push from hop to target via `exec_on_hop` |
| 4 | Implement relay GET | `transfer.py` | Pull from target to hop, then from hop to otto |
| 5 | Hop chain inspection | `remoteHost.py` or `connections.py` | Find last SSH-reachable hop for staging |
| 6 | Unit tests for relay transfers | `test_hop.py` | Mock-based, verify staging sequence |
| 7 | Integration tests for relay transfers | `test_hop_integration.py` | File PUT/GET through telnet hop |

### 3c — Edge cases and documentation

| # | Item | Notes |
|---|------|-------|
| 1 | SSH host key acceptance | `connect_through` must handle "Are you sure you want to continue connecting (yes/no)?" |
| 2 | Prompt detection robustness | Handle `$`, `#`, `>`, custom prompts; configurable timeout for banner drain |
| 3 | Timeout and recovery | If `connect_through` fails, detect and report clearly; mark session dead |
| 4 | Multiple concurrent sessions | Each named session through a telnet hop needs its own telnet connection chain |
| 5 | Document limitations | Fragility, tool requirements on hops, no SCP/SFTP through telnet, relay overhead |
| 6 | Update `multi-hop-growth-plan.md` | Mark Phase 3 items as done |

## Key Files

| File | Changes |
|------|---------|
| `src/otto/host/transport.py` | `HopTransport` protocol update, `TelnetHopTransport` class |
| `src/otto/host/remoteHost.py` | `_build_hop_transport` dispatch on hop term type |
| `src/otto/host/connections.py` | `create_session()`, `has_telnet_hop` property |
| `src/otto/host/session.py` | `SessionManager` uses `create_session()` for telnet-hop paths |
| `src/otto/host/transfer.py` | Relay transfer strategy when `has_telnet_hop` |
| `src/otto/host/telnet.py` | Reused as-is for hop connections |
| `tests/unit/host/test_hop.py` | New test classes for `TelnetHopTransport` |
| `tests/unit/host/test_hop_integration.py` | Mixed-protocol integration tests |
| `docs/guide/host.md` | Mixed-chain documentation, limitations |

## Key Design Decisions

**Q: Why not make TelnetHopTransport provide a fake SSHClientConnection?**
asyncssh's `tunnel` parameter requires a real `SSHClientConnection` — it uses the
underlying SSH channel for transport. There is no adapter that can bridge telnet
streams into an asyncssh tunnel. The interactive-session approach (run `ssh` inside
telnet) is the only viable path.

**Q: Why separate `connect_through` and `exec_on_hop`?**
`connect_through` consumes the telnet session — after running `ssh target`, the
hop's reader/writer pipe to the target. You can't run further commands on the hop
through that session. `exec_on_hop` opens a second telnet connection to the hop
for auxiliary operations (relay staging, diagnostics). Telnet doesn't multiplex,
so separate connections are required.

**Q: Why relay transfers instead of netcat-through-telnet?**
Netcat through a telnet hop would require: (1) running `nc -l` on the target
via the interactive session, (2) running `nc target port` on the hop via
`exec_on_hop`, (3) piping data through the hop's nc process. This is possible but
fragile and limited to small files (no flow control). The relay approach via
SCP on the hop is more reliable and handles large files with progress tracking.

**Q: What about the target's `transfer` field when relay is used?**
The target's `transfer` field (scp/sftp/ftp/nc) controls the protocol used between
otto and the target in the normal case. When relay is active, the protocol between
otto and the SSH-reachable hop uses SCP/SFTP (reliable, SSH-native), and the protocol
between the hop and the target uses whatever the hop can do (typically scp if the
target runs sshd). The target's `transfer` field is overridden by the relay strategy.

## Testing Approach

All 3 test VMs (test1/test2/test3) run both SSH and telnet. Two VMs are
sufficient for most scenarios:

| Scenario | Chain | VMs |
|----------|-------|-----|
| SSH hop -> Telnet target | otto -> test1 (SSH) -> test2 (telnet) | 2 |
| Telnet hop -> SSH target | otto -> test1 (telnet) -> test2 (SSH) | 2 |
| SSH -> Telnet -> SSH | otto -> test1 (SSH) -> test2 (telnet) -> test3 (SSH) | 3 |
| Relay PUT through telnet hop | otto -> test1 (telnet hop) -> test2 (target) | 2 |
| Relay GET through telnet hop | otto -> test1 (telnet hop) -> test2 (target) | 2 |

Note: "SSH hop -> Telnet target" already works via Phase 2 port forwarding.
The new scenarios are those with a **telnet hop** (where the hop itself only
supports telnet).

## Limitations (to document)

- **Fragility**: Prompt detection and login handling depend on the hop's shell
  configuration. Non-standard prompts, banner messages, or locale settings may
  break send/expect patterns.
- **No multiplexing**: Each concurrent operation through a telnet hop requires
  its own telnet connection chain. SSH hops multiplex channels over one
  connection.
- **Tool requirements**: The telnet hop must have `ssh` (or `telnet`) installed
  to reach downstream hosts. For relay transfers, the hop must have `scp` or
  an equivalent file transfer tool.
- **Performance**: Relay transfers are slower than direct transfers — files pass
  through an intermediate host with temp storage overhead.
- **No SCP/SFTP through telnet**: These require an `SSHClientConnection` which
  is unavailable through a telnet hop. Always uses relay.

## Implementation Order

Start with **3a** (command execution) — this is the foundation and delivers
the most value. File transfers (3b) can follow independently once command
execution works. Edge cases (3c) should be addressed incrementally throughout
3a and 3b.

Recommended first milestone: `test_echo_through_telnet_hop` — run `echo hello`
on test2 through a telnet hop on test1. This validates the entire
TelnetHopTransport -> ConnectionManager -> SessionManager chain end-to-end.
