# Zephyr fat-board console wedge (2026-08-01) — incident record + chaos scenario proposal

Live specimen of the chaos workstream's thesis, caught unstaged. Recorded
before recovery so the diagnosis survives the restart.

## Incident

- ~2026-08-01 00:15, during a `make coverage` run (the first collecting the
  new tier-2 chaos suite): `embedded-3.7-fat` handshakes started failing
  with "shell never became ready after open"; the bed-health gate marked
  the console wedged and fail-fasted the rest (11 failed + 10 errors per
  run since, all zephyr-tagged).
- NOT self-healing: still wedged ~5 h later with the bed otherwise idle.

## Diagnosis (read-only, from basil — nothing restarted)

- `ss -tn '( sport = :23 or dport = :23 )'` on basil: **empty** — no stale
  client connection holds anything.
- All bed qemu instances alive since Jul 24 (8 days), including the fat
  board's (`v3_7_fat_ram`, tap `zeth-fat`).
- Differential probe from basil: `192.0.2.2:23` still accepts TCP;
  **`192.0.2.1:23` (fat board) fails a bare connect/read cycle**.
- Conclusion: per-board, in-OS wedge — Zephyr's telnet shell service (and
  by now much of its net responsiveness) is dead while qemu itself runs.
  The board's own `-monitor none` / stdio-mux config leaves no other
  window in, so deeper post-mortem requires the restart.
- Candidate causes (indistinguishable from outside): slow in-OS resource
  exhaustion over 8 days of coverage traffic finally tipping under load
  (cf. the 3.7 fs-shell mount leak and the reason
  `test_heap_watermark::test_console_workload_does_not_leak_heap` exists),
  or a client death mid-handshake leaving the single telnet session
  allocated forever.

## Recovery (Chris)

Restart the fat board's qemu (or the zephyr bed's usual bring-up script).
After recovery, a full-green `make coverage` re-certification of merged
main (`cc7f45d2`) is owed — every gate run since the wedge has been
green-except-wedge-signature only.

## Chaos scenario proposal (fold into Plan 4)

This is the remote-side dual of the CLI interrupt work, on the embedded
surface, and the current catalog covers it only obliquely (the
host/session scenarios are SSH-centric):

1. **Scenario: console-client death mid-session** (tier 3, bed leg,
   leased zephyr board): kill/SIGKILL otto mid-handshake and mid-command
   on a telnet console session; assert the NEXT client gets a working
   shell within a bound, else fail naming the board. This directly
   regression-guards whichever half of the candidate-cause pair is real.
2. **BedHygiene check: console responsiveness probe** — pre/post-scenario
   shell round-trip per board, so a creeping wedge is caught by the
   nightly/stability leg at the incident boundary instead of surfacing as
   collateral coverage-run failures hours later.
3. Connects to `todo/chaos-reboot-followups.md` §4 (prompt-then-freeze):
   embedded recovery criteria should be sustained responsiveness over a
   window, not one accepted connect — this incident is the standing
   counterexample where accept ≠ shell on a real device.
