# Monitor live streaming (Plan 5b) — ship-and-note follow-ups

Triaged by the final whole-branch review. Everything here was consciously
deferred; nothing blocks the merge.

## Resolved

1. **The drilled-in unreachable treatment — BUILT.** `SubjectHealthBanner`
   (`web/src/shell/SubjectHealthBanner.tsx`) now wraps the drill-in's chart
   stack. It reads `health.ts`'s `healthForHost` — extracted from
   `healthForHosts` so the drill-in can ask about one host without forking
   the down rule — and renders per spec §A: a host subject shows "Unreachable
   for 2m — showing last-known data" and dims its chart stack; an element
   subject instead names each unreachable member with that member's *own*
   outage duration (e.g. "tech2 (2m), tech3 (20s) unreachable — showing
   last-known data") and does **not** dim, because its healthy members'
   charts are still live and correct and dimming them too would lie.

2. **Range presets do not set the live window — BUILT, but not the way this
   item imagined.** The spec's table wanted the ReviewBar's presets to set
   `windowMs` directly. Shipped instead: the live follow-window is resizable
   via `reviewStore`'s new `setWindow(windowMs)` action, driven by a
   dedicated 5m/15m/1h `ButtonGroup` in the AppBar that only appears in live
   mode (`web/src/shell/AppBar.tsx`). The historical ReviewBar's presets still
   call `setRange(...)`, which *pins* an absolute range — a pinned range and
   a follow-window are different concepts, and the two rows must not disagree
   by sharing one setter.

## Resolved in the Untitled UI adoption branch (2026-07-14)

Items **3, 4, 6, 8** and **9** were cleaned up before that branch merged; see
`todo/untitled-ui-adoption-followups.md` for what the same sweep left behind.

- **3 — a resync can strand the session picker.** Fixed: `resyncMonitorSessions`
  now falls back to `sessions[0]` + `range: null` **only** when the previous id is
  absent from the new snapshot, so a server restart re-seeds the picker while a
  transient blip still preserves a paused/pinned view.
- **4 — the snapshot→stream gap.** Not closed (it is small and the same shape at
  boot and at resync), but it is now stated plainly in the code instead of being
  papered over by the spec's "provably correct".
- **6 — `stop()` did not cancel a pending reconnect.** Fixed: `stop()` cancels the
  timer, and the reconnect path checks `stopped` *before* it hydrates — so a
  stopped stream can no longer fire one last hydrate over the store, and mode
  switches / HMR no longer leak EventSources.
- **8 — the engine-exemption test pinned source text.** Rewritten to assert the
  BEHAVIOUR: the collector still ticks at a sub-second interval. Proof it now
  bites: flooring `MetricCollector` turns the new test red, while the old
  `inspect.getsource` assertion stayed green under the identical mutation.
- **9 — the small cleanups.** Dead `_drain()` deleted; the duplicated live
  frame/lab guard factored into `_require_live_snapshot_body()`; the
  `jsonschema.py` docstring corrected (it had `setdefault`'s guarantee
  *backwards* — it silently keeps the document's value and discards the
  fragment's, and nothing "shows up as a difference"); the generated `ChartMap1`
  duplicate removed at the generator, not by hand-editing the generated file.

## Still open

5. **Browser/server clock skew flips fleet health — ACCEPTED RISK, not fixed.**
   Health compares browser `Date.now()` against server-stamped samples, so skew
   greater than `HEALTH_K × cadence` (15s at a 5s interval) would mark the whole
   fleet down, and negative skew would hide real outages by the skew amount.
   Ruled out of scope (Chris, 2026-07-14): skew that large is extremely unlikely
   in a lab. Revisit only if a lab without NTP shows up.

7. **SeriesPanel's checkbox tree never retires PIDs — DEFERRED, but we do intend
   to retire them.** Chart *series* are retired; the selector sidebar still lists
   every PID the session ever saw, forever, so a long run's sidebar grows without
   bound. Deliberately deferred (Chris, 2026-07-14) rather than accepted as
   permanent behaviour — this is not "by design", it is unfinished. The tension to
   resolve when it is picked up: retiring the selector too costs you the ability
   to tick a PID that died early in a long archive.

## Superseded — the original list

3. **A resync can strand the session picker.** `resyncMonitorSessions` keeps
   `activeSessionId` unconditionally. If the monitor *server restarts* while a
   tab is open, the reconnect resync delivers a fresh session set, the stale id
   resolves to null, and the shell sits in its empty state until a manual
   reload. Fall back to `sessions[0]` + `range: null` **only** when the previous
   id is absent from the new snapshot. (The pre-fix code self-healed this by
   destroying the user's paused view on every blip — which was the bug we fixed,
   so don't just revert.)

4. **Snapshot→stream gap.** Points published between the `/api/monitor_sessions`
   response and the SSE connection opening are never replayed and never
   re-fetched. The window is small and the same shape at boot and at resync, but
   the spec's "provably correct" claim overstates it. Worth a comment at
   minimum.

5. **Browser/server clock skew flips fleet health.** Health compares
   browser `Date.now()` against server-stamped samples, so skew greater than
   `HEALTH_K × cadence` (15s at a 5s interval) marks the whole fleet down —
   and negative skew *hides* real outages by the skew amount. Labs without tight
   NTP will hit this. Consider deriving an offset from received samples, or at
   least documenting it.

6. **`stop()` does not cancel a pending reconnect,** and the resync runs before
   the `stopped` check — a stopped stream can still fire one hydrate and
   overwrite the store. Latent today (production never stops the stream), but
   that also means mode switches / HMR leak EventSources.

7. **SeriesPanel's checkbox tree never retires PIDs.** Chart *series* are
   retired, but the series-selector sidebar lists every PID the session ever
   saw, forever. May be intentional (it lets you select a PID that died early in
   a long archive) — needs a product decision.

8. **The engine-exemption test pins source text, not behaviour.**
   `test_interval_floor.py` asserts `"validate_interval" not in
   inspect.getsource(MetricCollector.run)`. A floor added in `__init__` would
   slip straight through. Assert the engine still ticks fast instead.

9. **Small cleanups the reviews flagged:** the dead `_drain()` helper in
   `test_stream_fragments.py`; the duplicated live frame/lab guard in
   `server.py` (`_require_document_body()` is the precedent for factoring it
   out); a `jsonschema.py` docstring that overclaims what `setdefault`
   guarantees; the cosmetic `ChartMap1` duplicate in `export.gen.ts`.

## Known, by design

- **`MetricCollector` is exempt from the 1s interval floor.** It is the
  mechanism, not a human-facing knob, and the monitor tests drive it at
  0.01–0.2s against *fake* hosts. Flooring it would cost real seconds per tick
  and protect nobody — no real host is polled on that path.
- **The live store keeps every point.** A multi-day run belongs in `--db` and
  should be reviewed from the archive, not held in a tab.
