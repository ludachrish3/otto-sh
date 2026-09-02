# Unix user= parity — authenticate as the user

**Status:** draft for review
**Date:** 2026-09-01
**Depends on:** 2026-08-31-docker-container-users-design.md (shipped: the
uniform `user` parameter, container semantics, refusals elsewhere)

## 1. Problem

The container-users design gave every host verb a `user` parameter but
implemented it only on containers; UnixHost refuses `exec/run/put/get
(user=...)` loudly, with the refusal pointing at "a sudo/su story" as the
ledgered follow-up. Designing that story surfaced a hard constraint —
`exec` is a stateless channel with no expect support, so a sudo/su wrap
cannot answer a password prompt — and a better question: why impersonate
at all, when the lab's cred store can authenticate AS the target user?

## 2. Decision summary

1. **On UnixHost, `user=X` means AUTHENTICATE AS X** — not elevation.
   otto opens (and caches) a second SSH connection whose transport is
   authenticated as X, using X's own cred from the host's cred chain.
   The operation genuinely runs as X: no sudoers coupling, no password
   prompts (auth is non-interactive from the cred store), and a su-only
   BusyBox host behaves identically to a sudo host.
2. **Scope: `exec`, `put`, `get`.** Channels and transfers ride X's
   connection. `run(user=...)` KEEPS its refusal on unix: a persistent
   session's identity is the elevation context manager's job
   (`async with host.as_user(...)`), and a per-call wrap would be a
   worse duplicate of it. This is the one deliberate asymmetry with
   containers (whose run channel binds `-u` at open); the cross-family
   contract stays "the operation acts as X", per-family mechanism.
3. **The sudo/su machinery is untouched.** `run(sudo=True)` (measured
   elevation) and `as_user`/`switch_user` (session identity with undo,
   expects available) keep their jobs unchanged. This design CLOSES the
   "unix run/exec(user=) via sudo/su" follow-up from the container-users
   spec §8 — superseded, not implemented.
4. **Eligibility: direct-cred users only.** `resolve_chain(creds, X)`
   already answers this: X qualifies when the chain resolves with ZERO
   hops (X has a directly-loginable cred). A proxy-only user refuses
   loudly — hops are interactive send/expect steps that connection-level
   auth cannot replay, and reaching such users per-call would be the
   sudo story this design deliberately does not build.

## 3. Mechanism — per-user connections

- `ConnectionManager` grows a per-user cached connection map, keyed by
  the resolved direct cred's login. `ssh()` keeps today's meaning (the
  login-target connection); a new accessor `ssh_as(user)` resolves
  the chain, refuses on hops, opens the connection on first use, and
  caches it. Same ip/port/tunnel path as the primary connection.
- Lifecycle: per-user connections close with the host (`close()`), and
  `rebuild_connections()` drops them with everything else. Idle per-user
  connections are not reaped separately in v1.
- Cost: one extra SSH handshake per (host, user), first use only.
- `term == "ssh"` only in v1. Telnet hosts refuse loudly: their exec
  pool is built from interactive shell sessions, a different story.

## 4. Per-verb semantics (UnixHost)

- **`exec(cmd, user=X)`** — the exec channel is created on X's
  connection; everything else (timeout, log, concurrency safety) is
  unchanged. The process is X's own; auth logs show X's login.
- **`put(src, dest, mode=, user=X)`** — the transfer backend runs over
  X's connection, so files land OWNED BY X natively; there is no chown
  step to fail. `mode` applies exactly as today (as X — chmod of files X
  owns). Destinations X cannot write refuse with the backend's own
  error, truthfully attributed to X.
- **`get(src, dest, user=X)`** — reads run with X's permissions; this
  subsumes the previously-ledgered "elevated source reads" follow-up
  (e.g. `user="root"` fetches root-owned logs when a root cred exists).
  Files land locally owned by the invoking user, as always.
- **`run(cmds, user=X)`** — refusal stands, reworded to point at the CM:
  the persistent session's identity is `as_user`'s job.
- **Backends:** scp/sftp/nc ride the connection naturally (nc's remote
  listener/sender commands run via X's exec channel, so created files
  are X's). The ftp backend authenticates separately with its own creds
  and refuses `user=` loudly.

## 5. Refusals (distinct, anchorable, exact-phrase tested)

- Proxy-only user: names the user, the hop chain's shape, and the
  eligible alternative (`login(user=...)`/`as_user`, which CAN replay
  hops).
- Telnet term: names the term and the v1 scope.
- ftp backend: names the backend.
- `run(user=...)`: points at `as_user`.
- Unknown login: `resolve_chain`'s existing "unknown login ... creds
  define: ..." error surfaces as-is (already loud and named).
- LocalHost and embedded hosts: all existing refusals unchanged.

## 6. What does NOT change

- Docker semantics (exec/login `-u`, bind-at-open run channel, cp+chown
  put, accept-and-ignore get) — untouched.
- `sudo=True`, `as_user`, `switch_user`, the root `--as-user`
  reservation flag, userland elevation measurement — untouched.
- The declared per-service `users` table is a container concept; no unix
  equivalent is introduced.
- CLI: no new code — the synthesized `--user` on `put`/`get` starts
  working on unix hosts the moment the refusals are replaced; `run
  --user` on unix keeps refusing at runtime with the reworded message.

## 7. Testing

- Unit: connection-manager per-user cache (hit/miss/close/rebuild);
  zero-hop eligibility gate (direct → connection, hops → exact-phrase
  refusal); exec-channel-on-X wiring pinned at `await host.exec(...)`
  with a mocked connection layer; put/get backend receives X's
  connection (transcript pins); every refusal branch executed.
- Mutation honesty: cache-key mutation (per-user connections collapsing
  onto the primary → named test red); eligibility gate dropped → red.
- Live bed (post-merge, manual or bed lane): `exec("id", user=...)` →
  the target uid on test1/test3; `put(user=...)` → `stat -c %U` shows
  the target owner; BusyBox bed leg proves su-less parity; `get` of a
  root-owned file with a root cred.

## 8. Follow-ups (ledgered, not this design)

- Lifting `login(user=X)`'s "starting a fresh connection ... is not
  supported" refusal for direct-cred users (same primitive, interactive
  surface).
- Telnet-term per-user sessions.
- Proxy-chain users on stateless surfaces (would require a hop-replay
  exec primitive — likely never worth it).
- ftp backend user support.
