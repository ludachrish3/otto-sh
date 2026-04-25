# Telnet Login Silence-Drain Optimization

## Status
Deferred optimization. Captured here so the problem + design space are preserved; pick up only if telnet session-open latency becomes the dominant cost again after the warm-up work in `plans/` lands.

## Current behavior

[src/otto/host/telnet.py](../src/otto/host/telnet.py) lines 178–189:

```python
if self.prompt is not None:
    # Wait for the user-supplied prompt to confirm login succeeded.
    await self.reader.readuntil(self.prompt.encode())
else:
    # Drain any remaining login output ...
    while True:
        try:
            await asyncio.wait_for(self.reader.read(4096), timeout=1.0)
        except asyncio.TimeoutError:
            break  # silence means the shell is ready
```

After credentials are sent, `login()` has to decide when the shell is ready to receive commands. Two paths:

1. **User supplied `prompt`**: block on `readuntil(prompt)`. Fast (no fixed cost), but requires the caller to know the exact prompt string for the device.
2. **No `prompt`**: block until the stream goes quiet for 1.0 s. Adds a **fixed ~1 s tax on every cold telnet session open** — the single largest unit cost in a session handshake.

## Why the drain exists today

The sentinel-based command protocol in [src/otto/host/session.py](../src/otto/host/session.py) (`ShellSession._ensure_initialized` at lines 93–105) would, in principle, make post-login prompt matching redundant: once the shell starts, sending `stty -echo 2>/dev/null; echo __OTTO_..._READY__\n` and waiting for the READY marker self-synchronizes regardless of what banner/MOTD text precedes it.

The drain survives because `login()` runs *before* the sentinel protocol takes over, and it's the last chance to:

- **Detect login failure.** If credentials are bad, the server keeps the user in the login/password prompt loop instead of spawning a shell. A subsequent `echo READY_MARKER` would either be interpreted as a username (looping), swallowed by the login prompt, or echoed raw — no READY line ever arrives. Without a timeout here, a bad password stalls the whole session open until some later layer times out.
- **Let MOTD / last-login / banner text settle** so the first real command's output isn't interleaved with login chatter inside the same stream window.

So the 1 s drain is cheap insurance against a correctness problem (undiagnosed login failure) rather than a pure waste.

## Alternative solutions (brainstorm)

Listed roughly in increasing ambition. Any of these can stand alone; some combine.

### A. Require (or strongly default) `prompt` in `TelnetOptions`

Zero-code path: configure `prompt` for every lab entry that uses telnet. `readuntil(prompt)` returns immediately when the shell appears, and we stop paying the 1 s drain altogether.

- **Pros:** Simplest. No protocol change. Already a supported path.
- **Cons:** Requires per-device configuration; some devices use dynamic prompts (hostname/cwd embedded) that are hard to express as a single literal.
- **Mitigation for dynamic prompts:** allow `prompt` to be a regex and switch `readuntil` → an incremental regex scan.

### B. Sentinel-driven login readiness

Replace the drain with the same marker pattern `ShellSession._ensure_initialized` uses. Immediately after sending the password, send `echo __OTTO_LOGIN_READY_<uuid>__\n` and wait for that exact string with a generous timeout (e.g. 5 s, configurable).

- **On success:** the marker appears on a shell line, login is done, hand off to sentinels.
- **On bad credentials:** the marker never appears; the timeout fires and raises a clear "login failed" error.
- **Pros:** No fixed tax on happy path (marker arrives as soon as the shell is ready — often <100 ms). Login-failure detection becomes a proper error rather than an indefinite hang. Aligns post-login with the rest of the session protocol.
- **Cons:** More moving parts inside `login()`; edge cases around servers that echo input back and cause the marker to appear in our own send stream (the `ShellSession` code already solves this with `stty -echo` + anchor-on-`\n`, reusable here).
- **Risk:** some devices delay the shell start significantly after a successful login (slow MOTD generation); tune the timeout, don't panic-bail.

### C. Learn-and-cache the prompt per host

On the very first session open for a given host, pay the current 1 s drain *once* — but observe what the shell printed last (usually the prompt) and cache a regex matching it. Every subsequent session open uses (A) with the learned prompt.

- **Pros:** No configuration burden; cost amortizes across N sessions per host. For the nc transfer case (multi-session warm-up), the first session pays the tax and the rest go fast.
- **Cons:** Cache invalidation: the prompt can change (user `export PS1=`, su, etc.). Bound the cache to per-lab session lifetime to minimize risk.
- **Implementation note:** a simple heuristic — the last line of drained output, stripped of trailing whitespace — is usually the prompt. A more careful version could match the idle pattern against several subsequent session openings before trusting it.

### D. Adaptive drain window

Replace the fixed 1 s timeout with an exponentially shrinking window: start at 0.2 s, extend by 0.2 s each time data is still flowing, cap at the current 1 s. On a quiet login (no banner) this completes in ~0.2 s instead of 1 s.

- **Pros:** Zero-config, no protocol change.
- **Cons:** Chatty MOTDs still pay close to the original cost. Smaller win than (B) or (C).

### E. Prompt-free login via `stty -echo` injection

Right after sending the password, immediately write `stty -echo 2>/dev/null; echo __OTTO_LOGIN_READY_<uuid>__\n` without waiting. Read lines; discard everything that isn't the marker; on marker, proceed. This is effectively (B) with a head-start — the readiness probe is in flight before the shell is even ready, so the round-trip is fully overlapped with shell-spawn.

- **Pros:** Same correctness as (B), a little faster on slow links.
- **Cons:** Strictly more complex than (B) for little extra win; worth measuring first.

### F. Keep-alive / session reuse at a higher layer

Today each `RemoteHost` owns its sessions. Across multiple `otto` invocations or long-lived runs, we could persist a pool of already-authenticated telnet sessions keyed by host and reuse them. Amortizes login across invocations entirely.

- **Pros:** Largest possible win for workflows that re-run commands frequently.
- **Cons:** State persistence across processes is a big architectural change; security / lifecycle concerns (passwords in memory/disk, stale sessions). Realistic only in a daemon-style otto, not the current CLI.

## Recommended order if this gets picked up

1. Ship (B) behind an opt-in flag; measure on a representative lab.
2. Layer (C) on top so prompt caching kicks in after the first login.
3. Fall back to today's drain only if both (B) and (C) fail — the drain becomes the final safety net, not the default.

## Files likely touched

- [src/otto/host/telnet.py](../src/otto/host/telnet.py) — `login()` body.
- [src/otto/host/session.py](../src/otto/host/session.py) — possibly expose the marker-generation helper for reuse inside `login()`.
- Lab config schema — if (A) becomes stricter about requiring `prompt`, or (C) adds a cache field.

## Verification when picked up

- Unit: mock the telnet reader/writer and assert the new login path completes without a fixed 1 s wait on the happy path; assert it raises a clear error on simulated login failure.
- Integration: run against a real (or recorded) telnet device and compare `login()` latency before/after.
- Regression: existing telnet login tests must stay green — this is login-protocol code and a silent break would break every telnet lab.
