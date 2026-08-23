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

## Diagnosis (read-only, from test4 — nothing restarted)

- `ss -tn '( sport = :23 or dport = :23 )'` on test4: **empty** — no stale
  client connection holds anything.
- All bed qemu instances alive since Jul 24 (8 days), including the fat
  board's (`v3_7_fat_ram`, tap `zeth-fat`).
- Differential probe from test4: `192.0.2.2:23` still accepts TCP;
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

---

## Second incident, 2026-08-09 — same signature on `embedded-2.7-fat`, now ROOT-CAUSED

Caught during a 30-session seed-variability sweep (`nox -s tests_all-<py>`,
one pass each, distinct `--randomly-seed`). Runs 1-6 clean; run 7 broke the
board and runs 8-30 were the health gate reporting that one fault 24 times.

### ★ The 08-01 record's key assumption was wrong — there IS a post-mortem window

Above: *"the board's own `-monitor none` / stdio-mux config leaves no other
window in, so deeper post-mortem requires the restart."* Not so. The stdio mux
is captured by **systemd**, so the guest's own serial console — Zephyr's
`<err>` lines included — is readable after the fact with:

    journalctl -u zephyr-qemu-<board> --no-pager -o short-iso

That is what root-caused this one without restarting anything, and it would
have worked on 08-01 too. Read it BEFORE recovering; a restart is still needed
to clear the fault, but no longer to investigate it.

### Evidence

Run 7 occupied 15:24:45-15:27:54 local. From the guest's console:

    15:26:01  <err> shell_telnet: Failed to send -128, shutting down
    15:28:43  <err> eth_e1000: Out of buffers      <- first
    17:58:32  <err> eth_e1000: Out of buffers      <- still going, 432 total

Host side, from test4: qemu alive (`NRestarts=0`, up since 08-02, same as every
other guest), tap `zeth-27fat` UP with 192.0.2.14/30, `ping 192.0.2.13` 100%
loss, `ip neigh` = **INCOMPLETE**. The guest is not answering ARP, which is why
otto reports `CONNFAIL [Errno 113] No route to host` and the handshake fails
with `shell never became ready after open`.

### The discriminator: this is 2.7-only

    guest            shell_telnet errs   Out of buffers
    v2_7_fat_ram             7                432
    v3_7_fat_ram           862                  0
    v3_7_lfs                45                  0
    v4_4_lfs                74                  0

(Counts every `shell_telnet` `<err>` line, which lumps `Failed to send` in with
`Telnet client already connected`. Split apart in "Which tests actually cause
it" below — the split is what identifies the trigger, and it shows 2.7 has
never logged a single `already connected`.)

`shell_telnet` send failures are ROUTINE and harmless everywhere — the 3.7 fat
board has logged 862 of them and is up. Only on 2.7 does one of them progress
to permanent network-buffer exhaustion. So the bug is not "the client died
mid-handshake"; it is that **Zephyr 2.7 does not reclaim the net buffers held
by a telnet shell session it tears down on a send failure**, and the e1000
driver then starves for good.

### This picks between the 08-01 candidate causes

Neither exactly, and the distinction matters. It is resource exhaustion, but
not the "slow drift over 8 days of coverage traffic" version — the board ran
7 days clean and died 2m42s after a single teardown event. It is a leak
*triggered by* one specific failure path, so uptime is a red herring and
"how long since restart" is the wrong thing to watch.

### Consequences for the chaos proposal above

- Scenario 1 (client death mid-session) is well aimed, and 2.7 should be in
  its matrix specifically — the version differential is the finding.
- Scenario 2's probe must assert **network** liveness (ARP/ping or a UDP
  round-trip), not just a shell round-trip. By the time the shell is
  unreachable here the board has been unreachable at L2 for minutes, and a
  shell-only probe cannot tell "wedged shell" from "dead stack".
- Worth a bed-hygiene check that greps each guest's journal for
  `Out of buffers` since the last restart: it is a leading indicator that
  costs one ssh and names the board before any test fails.

### What landed from this, and what did not

Three of the recommendations were implemented:

- **The wedge gate now attaches the journal tail** (`_guest_console_tail`) to
  the first failure per backend, as a report section. The whole reason this
  incident needed a second investigation is that the first one concluded the
  window did not exist; a note in a todo file would not have prevented the
  repeat, and a line in the failure output does. Fires only on the failure
  path, one ssh bounded at 20s, and degrades to a "capture unavailable" note
  plus the manual command rather than changing which test fails.
- **Per-device xdist grouping is now invocation-independent** and checked at
  setup. Found while reproducing this incident: the grouping that keeps two
  workers off one console was silently inert whenever the directory was named
  on the command line (`pytest tests/integration/host`), because that makes
  this conftest an *initial* conftest whose collection hook registers after
  xdist's. `tryfirst` pins the order; `_unhonored_group` fails the run if the
  suffix is ever missing again. **This is NOT how run 7 killed the board** — an
  earlier draft of this note said it "very likely" was, and that was checked and
  disproved; see below.
- **Zephyr 2.7 is held out of the fan-out matrix** (`_FANOUT_EXCLUDED` in
  `test_embedded_host_integration.py`), and only the fan-out matrix — all 17
  collected 2.7 items still run.

### Which tests actually cause it, and why 2.7 is held out of just those

Established 2026-08-10 from the surviving sweep logs and 24 days of hop journal.

**Not the grouping bug.** `testpaths` is `tests/unit|integration|e2e`, so
`tests/integration/host/conftest.py` is never an *initial* conftest under `nox`
or `make coverage` — grouping was already honored there before the `tryfirst`
fix. Verified by deleting `tryfirst` and running the nox-shaped invocation: the
`@zephyr_27_fat` suffixes were still present and the lane passed 72/8. The
ordering bug only ever affected directory-targeted runs. Consistent with the
journal: **2.7 has logged zero `Telnet client already connected` in 24 days**,
so no client ever contended for its console.

**It is the fan-out class.** Run 7 (`seed-7-3.11-155436.log`) failed exactly
twice, both `TestConcurrentEmbeddedTransfer`. Its connection to `192.0.2.13` at
15:26:00 is followed one second later by the guest's
`shell_telnet: Failed to send -128` — ENOTCONN in Zephyr's minimal libc, i.e.
the client vanished mid-send. Four opens follow on a ~16s cadence (the 15s
readiness ceiling plus reconnect) at :00, :17, :33, :50, so four teardowns and
four leak events; `eth_e1000: Out of buffers` starts at 15:28:43 and never
stops.

**The trigger is not 2.7-specific; the reaction is.** Same 24 days:

    unit            Failed to send   Out of buffers   FATAL   already connected
    v2_7_fat_ram          1               436           4            0
    v3_7_fat_ram          2                 0           0          873
    v3_7_lfs              5                 0           0           21
    v4_4_lfs              6                 0           0           53

Every board takes this trigger. Only 2.7 fails to reclaim the buffers. That is
why the holdout is scoped to 2.7's participation in fan-out rather than to the
fan-out tests themselves, and why the per-device 2.7 suite is untouched — the
per-device tests open one client at a time and have never triggered it.

**Also worth stating plainly:** the sweep's "24 failed runs" were *one*
incident. Runs 8-30 were the health gate reporting an already-dead board. What
made this feel like a recurring intermittent was that nothing cleared the fault
between runs, not that it kept happening.

Deliberately NOT done, and why:

- **No host-side change** (no `Restart=`/watchdog tuning, no supervisor that
  notices a silent guest). It would convert this failure into an automatic
  recovery, i.e. hide the exact signal that made the 2.7 net-buffer leak
  findable. The bed should keep failing loudly here.
- **No proactive `Out of buffers` sweep before each run.** Still worth doing,
  but it is a bed-hygiene job, not a pytest gate — a pre-run probe that reads
  every guest's journal belongs next to `make qemu-restart`, not in a conftest
  that only runs when tests already are.
- **The 2.7 net-buffer leak itself is unfixed.** It is upstream Zephyr 2.7
  behaviour on a version we keep deliberately as a firmware-drift sentinel.
  Recovery is still `make qemu-restart`; what changed is that the next person
  will see *why* before they run it.
