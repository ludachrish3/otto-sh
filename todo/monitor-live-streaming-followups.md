# Monitor live streaming (Plan 5b) — ship-and-note follow-ups

Triaged by the final whole-branch review. Everything here was consciously
deferred; nothing blocks the merge. (Resolved items pruned 2026-07-25 —
items 1–4, 6, 8, 9 shipped across this branch, the Untitled UI adoption
branch, and later sweeps; only the two below remain.)

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

## Known, by design

- **`MetricCollector` is exempt from the 1s interval floor.** It is the
  mechanism, not a human-facing knob, and the monitor tests drive it at
  0.01–0.2s against *fake* hosts. Flooring it would cost real seconds per tick
  and protect nobody — no real host is polled on that path.
- **The live store keeps every point.** A multi-day run belongs in `--db` and
  should be reviewed from the archive, not held in a tab.
