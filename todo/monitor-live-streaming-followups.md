# Monitor live streaming (Plan 5b) — ship-and-note follow-ups

Triaged by the final whole-branch review. Everything here was consciously
deferred; nothing blocks the merge.

## Spec items not built

1. **The drilled-in unreachable treatment does not exist.** The spec says a
   drilled-in unreachable host shows "last-known data, frozen and dimmed, with
   the *Unreachable for 2m — showing last-known data* banner". `SubjectPage`
   computes no health at all, so a dead host's subject page renders its charts
   normally with no indication. The fleet grid and topology both dim correctly;
   only the drill-in is missing. This is a spec→plan gap — the plan routed
   "unreachable + clock" only to the health/clock tasks, so no per-task review
   could have caught it. **Needs a ruling: build it, or descope to 5c and amend
   the spec.**

2. **Range presets do not set the live window.** The spec's table says "preset
   chosen → sets `windowMs`; still following". Shipped: `windowMs` is a
   `900_000` literal with **no setter**, and the ReviewBar presets call
   `setRange(...)`, which *pins* the view (i.e. pauses). ReviewBar is now hidden
   in live mode, so this is unreachable today rather than wrong — but the live
   window is fixed at 15 minutes with no way to change it.

## Worth doing next time these files are touched

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
