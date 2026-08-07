# Remote path tab completion for `otto host get` / `put`

**Date:** 2026-08-06
**Status:** approved design, awaiting implementation plan

## Problem

`otto host <id> get SRC... DEST` and `put SRC... DEST` take paths that live on
the remote host (`get`'s sources, `put`'s destination), but shell completion
today only ever offers local paths. Completing remote paths requires touching
the lab host, which must not happen unless the user actually holds the lab's
reservation — and reservation state churns quickly (short bookings, mid-session
extensions), so any caching must respect booking boundaries.

## Scope

- **In:** SSH hosts; `get.src_files` (files + dirs) and `put.dest_dir`
  (dirs only); reservation gating with a short-lived cache of reservation
  windows; a sidecar listing cache.
- **Out (v1):** telnet hosts (the exec-based listing mechanism is chosen so
  telnet later only needs the gate relaxed, not a new mechanism); embedded and
  docker hosts; completion for `run`/`exec` command arguments.

## Design

### 1. Marker extension

`Arg` and `Opt` in `otto/utils.py` gain one field
(`remote_path: Literal["any", "dir"] | None = None`), preserving the
imports-no-typer property. Markers applied:

- `UnixHost.get.src_files` → `Arg(..., remote_path="any")`
- `UnixHost.put.dest_dir` → `Arg(remote_path="dir")` — only directories are
  offered, each with a trailing `/` so the shell descends rather than closing
  the word.

`build_cli_binding` (`cli/param_synth.py`) attaches
`autocompletion=partial(remote_path_completer, kind=...)` to the synthesized
`typer.Argument`/`typer.Option` in both the variadic and scalar branches.
Overrides on other host classes don't carry the marker, so they get no remote
completion.

### 2. Completer flow (`cli/remote_completion.py`, new module)

Every step fails silently to `[]` — a completer never raises and never prints.

1. **Recover invocation context.** During resilient parsing the `host` group
   callback returns before stashing `ctx.meta`, so walk the parent-context
   chain (same depth-capped walk as `cli/completers.py`) for `host_id`,
   `hop`, `term`, and the root callback's `as_user`.
2. **SSH-only gate.** Resolve the host's effective term backend; anything
   other than `ssh` → `[]`.
3. **Reservation gate** (§3). Not confirmed → `[]`; no connection is opened.
4. **Split `incomplete`** with `posixpath` into the directory to list
   (`~`-relative allowed; empty → remote home) and a basename prefix filter.
5. **Listing cache lookup** (§4). Entry for `(host_id, dir)` fresher than
   45 s → filter by prefix and return.
6. **Live listing on miss.** Build the real host — the same `get_host()` +
   hop/term override path as `resolve_cli_host`, minus `ctx.meta` plumbing —
   then run the listing command (§2a) under a hard 2 s overall deadline
   (`asyncio.wait_for` around connect + exec). Write the cache, return
   entries. Connections are closed in a `finally`; a completion process
   leaves no lingering session.

Presentation rules: directories are suffixed `/`; dotfiles are offered only
when the prefix itself starts with `.`.

The shipped flow builds the real host (step 6) before the cache lookup
(step 5) rather than after, as the steps above are numbered: the SSH gate
needs the resolved effective term, and the cache key uses the canonical
host id, so host resolution has to happen first. Both are purely local
work — no lab contact — so reordering costs nothing and avoids resolving
twice.

### 2a. Listing command (exec-based, not SFTP)

The listing is a remote shell command through the host's own session seam:

    LC_ALL=C ls -1ALp -- '<dir>'

run via `host.exec(..., log=LogMode.QUIET)` so completion doesn't spam logs.
`-1` one entry per line, `-A` includes dotfiles (minus `.`/`..`), `-L`
dereferences symlinks, `-p` marks directories with a trailing `/` — parsing is
"trailing slash = directory". The path is quoted with `shlex.quote`, except a
leading `~` stays outside the quotes so the remote shell expands it. The exit
code is ignored when stdout parsed (a broken symlink must not kill the whole
listing); empty stdout with a nonzero exit → `[]`.

Rationale for exec over SFTP `readdir`: the exec path goes through the same
session abstraction telnet uses, so extending completion to telnet hosts later
is purely a gate change.

### 3. Reservation gate and protocol extension

- New **optional capability protocol** in `reservations/protocol.py`,
  following the existing `SupportsUsernameCompletion` pattern: a
  runtime-checkable `SupportsReservationWindows` with one method,
  `get_reservation_windows(username) -> list[ReservationWindow]`, where
  `ReservationWindow` is a frozen dataclass `(resource: str, start: datetime,
  end: datetime)` (tz-aware datetimes). Detected with `isinstance`; backends
  without it fall back to the plain `get_reserved_resources` set. The JSON
  backend implements it from its stored `expires` (unknown starts are
  represented as the epoch). Exported from `otto.reservations` alongside the
  other protocol names.
- **Required set** = `required_resources(lab)` — identical to the command-time
  gate, so completion never succeeds where the eventual `get`/`put` would be
  refused.
- **Pass condition:** every required resource has a cached window covering
  *now* (or is present in the fallback resource set).
- **Cache validity** (reservations churn fast — short bookings, mid-session
  extensions):

      valid_until = min(fetched_at + 120 s,
                        earliest window edge (any start OR end) after fetched_at)

  Two-minute blocks bound how long churn is invisible; the edge clamp forces
  a refresh the moment any known boundary passes — an expiry stops completion
  immediately, an extension is seen on the first TAB after the old end.
  Window-less backends get the flat 120 s TTL.
- No `[reservations]` section configured → gate is a no-op (matches
  `ReservationGate`). `-R`/`--skip-reservation-check` does **not** bypass the
  completion gate: completion has no channel for the loud-skip warning the
  skip contract requires.
- Backend unreachable with no valid cache → `[]`.
- **Completion-only cache (owner ruling, 2026-08-06):** the reservation cache
  is consumed ONLY by tab completion. Command execution must never read it —
  running a real otto command on possibly-stale reservation data (e.g. via
  shell command recall) is an unacceptable risk; the command-time gate
  (`ReservationGate.evaluate` → `check_reservations`) always queries the
  backend live. The risk is acceptable for completion because TAB is slower
  and more deliberate. Enforced by docstrings on both modules and a static
  guard test asserting no import of `remote_completion_cache` outside the
  completion path and the cache-clear handler.

### 4. Sidecar cache (`.otto/remote_completion_cache.json`)

Separate from `completion_cache.json`, which is a fingerprinted slow-path
snapshot whose lifecycle per-TAB writes would fight. Schema-versioned JSON:

    {"schema": 1,
     "reservations": {"<user>": {"fetched_at", "valid_until",
                                 "windows": [...], "resource_set": [...]}},
     "listings": {"<host_id>": {"<abs dir>": {"fetched_at",
                                              "entries": [{"name", "is_dir"}]}}}}

- Listing TTL: 45 s.
- Atomic write (tmp + rename). Corrupt / missing / wrong-schema file is
  treated as empty and rewritten.
- Expired entries are pruned on write; listings are capped per host
  (~50 dirs, oldest evicted).
- `--clear-autocomplete-cache` deletes this file alongside the main cache.

### 5. Error handling

One rule: every failure — unknown host, non-SSH host, gate refusal, backend
error, connect/auth failure, deadline hit, cache corruption — returns `[]`
silently. The 2 s deadline is the only lab-facing wait.

## Testing

- **Unit:** marker→synthesis wiring (completer attached; `dir` kind for
  `put`); `incomplete` splitting incl. `~` and dotfile rules; listing-command
  parsing (trailing slash, broken-symlink nonzero exit); cache validity
  arithmetic (120 s block, edge clamp for both starts and ends, expiry
  pruning, per-host cap); gate decisions against a stub backend (windows
  present / absent / unreachable / no-config / fallback set); every silent
  failure path. House rule: each gate condition gets a test proving it
  *individually* flips the outcome (no guards that cannot fail).
- **JSON backend:** `get_reservation_windows` round-trip, including expired
  entries.
- **Conformance validator** (`otto/testing/conformance.py`):
  `assert_reservation_backend_conforms` gains a `SupportsReservationWindows`
  section, run only when the backend implements the capability (mirroring the
  `SupportsUsernameCompletion` section): return type is a list of
  `ReservationWindow`; `resource` entries are non-empty `str`; `start` and
  `end` are tz-aware and `start <= end`; and — when `known_user` is given —
  windows agree with the flat view: the set of resources whose window covers
  *now* equals `get_reserved_resources(known_user)`. Validator tests cover a
  conforming windows backend, each violation individually (wrong type, naive
  datetime, inverted window, flat-view disagreement), and that a windows-less
  backend skips the section without failing.
- **E2E** (live bed, `tests/e2e/host/`): completer called directly against a
  real SSH host — listing correctness, trailing `/`, prefix filtering, cache
  hit on the second call. The non-SSH gate itself is covered at unit level
  (`test_non_ssh_host_returns_empty`); it was also live-witnessed when the
  pool's telnet-default host (tomato) returned `[]` under the full gate,
  fixed by the e2e's `--term ssh` override (commit `fb84da71`).

## Docs

- Guide section (completion behavior, reservation gating, the cache file and
  how to clear it).
- Reservation interface docs updated for the optional capability:
  `docs/guide/reservations.md` (implementer-facing: how to opt in, semantics
  of windows, what completion does with them), the reservations architecture
  subsystem page, and the API rst — including the conformance helper's new
  checks, so third-party backend authors validate the capability the same way
  they validate the base contract.

## Decisions log

- **Live listing with a 45 s cache** over per-TAB-only or snapshot-only:
  correctness with bounded latency.
- **SSH-only v1**: telnet consoles are often shared; a completion-time `ls`
  could interleave with an active session. Exec-based listing keeps the door
  open.
- **Exec (`ls`) over SFTP**: same seam as telnet sessions → future
  extensibility; SFTP would have tied the feature to the SSH transport.
- **Protocol windows + 2-minute blocks + edge invalidation** over
  trust-until-end: bookings churn quickly and get extended mid-session; edges
  force refreshes exactly when state can change.
- **Fail closed, silently, no `-R` bypass**: a completer has no error channel;
  withholding suggestions is the only safe analogue to failing loud.
- **Reservation cache is completion-only**: commands never run off cached
  reservation state — stale-cache risk is tolerable for a TAB suggestion,
  never for command execution.
