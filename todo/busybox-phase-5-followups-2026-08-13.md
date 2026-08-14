# BusyBox phase 5 follow-ups

Open items left by phase 5 (`4f93b756` — Tier 3 over real ssh, the gap registry,
its docs page and the docs-sync test). Everything here was found by measurement
while building or reviewing that branch, and each entry carries its evidence so
the work starts from a fact rather than a fresh survey.

Two queues already exist and are **not** duplicated here:

- `todo/busybox-tier3-fidelity-2026-08-13.md` — legacy dropbear on loopback, a
  parametrizable Tier 3 endpoint, and the two untested gaps. Agreed for after
  phase 5.
- `todo/busybox-parity-sweep-2026-08-11.md` — the `uuencode` codec for 1.16.1
  and a BusyBox `nc` backend. The separate full-parity workstream.

---

## 1. The registry renders messages that nothing invokes

**Status: by design in phase 5, and the largest open item.**

`Gap`, `GAPS`, `gap_for()`, `refuse_if_gapped()` and
`UnsupportedOnUserlandError.for_gap()` all exist and are tested, but **no
product call site consults them**. `refuse_if_gapped`'s only `raise` is inside
itself; nothing under `src/otto/` calls it. Of the eight measured-broken
surfaces, exactly one — `shell-transfer-base64` — refuses today, and only
incidentally, because `_run_put` probes `base64_flag`.

This is stated honestly in `src/otto/host/userland.py` and the docs page's
status lines are subjunctive for the same reason. Wiring a raise site is a
**behaviour change** that belongs with each call site, one at a time, each with
its own test — which is why phase 5 did not do it wholesale.

Per-surface, the call sites to wire are named in each record's `measured_on`.

## 2. Two product bugs recorded as gaps, not fixed

Both are registry entries with measurements. Recording them was phase 5's job;
fixing them was explicitly out of scope.

### `run-command-line-length` — silent truncation over a persistent session

`UnixHost.run()` passes `term_type="dumb"` (`src/otto/host/session.py:765,770`),
so it allocates a pty. `exec()` does not. BusyBox `ash` **silently truncates a
typed line at 1023 characters** (`CONFIG_FEATURE_EDITING_MAX_LEN`) — measured
identical against OpenSSH and against a bare local pty, so it is the shell's
line editor, not the transport. Any `run()` command over 1022 characters
against a BusyBox target is silently truncated.

This is the one open item in a data-corruption class rather than a
refusal-clarity class. `exec()` is unaffected, and `ShellFileTransfer` uses
`exec()`, so the transfer path is safe — see the `_SHELL_CHUNK_BYTES` docstring
for why `term: telnet` remains the hostile case (telnet has no stateless exec
primitive and routes through a pooled shell session).

### `file-ops-base64` — hard-coded codec

`read_file`/`write_file` emit `base64` / `base64 -d`
(`src/otto/host/file_ops.py:131,156`) without consulting
`Userland.base64_flag`, so both break on BusyBox 1.16.1, which ships no `base64`
applet at all. They can neither refuse up front nor adapt: the caller gets the
device's own `not found`, attributed to the file it asked for.

Closing this properly overlaps the uu-codec item in the parity sweep — a device
with no `base64` needs a second codec, not just a better error.

## 3. Coverage the exit criteria do not actually have

### Criterion 3 — Tier 3 drives one matrix row, not the matrix

`tests/busybox/test_tier3_shell_transfer.py` is the only place under
`tests/busybox/` that drives the `shell` backend at all, and it runs
`BUSYBOX_MATRIX[-1]` (1.35.0). `TIER3_RELEASE`'s docstring argues for one row on
cost grounds and the argument is sound, but the criterion says "on the matrix".

Matrix-wide evidence today is Tier 2's codec contracts (`base64 -d`,
chunk-append, `dd` ranges on the four rows that have `base64`) and Tier 1's
applet table — the **primitives**, not the backend. Deciding whether to widen
Tier 3 or to reword the criterion is the open question; do not silently treat
the primitives as backend coverage.

### Criterion 7 — CLOSED: the CI half is now demonstrated

**Resolved by CI run `31759194001` on `901326a4`: success, every job green,
including `busybox-artifacts (arm64)` and `busybox-artifacts (x86_64)`.** Tier 3
cannot reach a passing state with any of the three preconditions below unmet, so
all three are now demonstrated rather than asserted. Criterion 7 is met.

Kept for the record, because each is a live dependency that a future change to
the runner image or the tier could break, and the failure modes are worth
recognising:

1. **`/usr/lib/sftp-server` exists on both runner images.** This is the one that
   fails hardest: `mount --bind` onto a missing target aborts the daemon script
   and takes the whole tier down at session setup, reported as "the Tier 3
   dropbear died at startup", which slightly misnames the cause.
2. **`openssh-client` is present.** Nothing installs it — not the Vagrantfile,
   not CI. The tier needs `ssh-keygen` (`dropbearkey` writes a format asyncssh
   cannot read), plus `scp` and `sftp` for the refusal contract. Documented in
   the guide page as of phase 5, but there is **no plumbing test and no install
   step**, unlike `dropbear-bin`. Failures are named, so it is a completeness
   gap rather than a silent one.
3. **`qemu-x86_64` registers with the `F` flag on the arm64 leg.** The busybox
   job is a two-row matrix (`ubuntu-latest` and `ubuntu-24.04-arm`), and since
   `TIER3_RELEASE.arch` is `x86_64` the arm64 leg takes the foreign-arch branch
   — the same one as the dev VM, and the branch that asserts `F`. That is
   precisely where a missing `F` would first bite.

Only criterion 3's "on the matrix" half remains open under this heading.

## 4. Small deferred items

- **`staged_temp_name(dest, 1)` returns `"."`**, a basename resolving to the
  containing directory. Documented as out of contract in both the source
  docstring and the test, because making it visible means raising, which is a
  behaviour change. No matrix target declares such a limit.
- **`pytest.UsageError` from `pytest_runtest_setup` does not abort the
  session** — measured: pytest reports a per-item ERROR *with a conftest
  traceback* and continues, exit 1. So versus `pytest.fail(..., pytrace=False)`
  it is strictly noisier, not more decisive, which is the opposite of what the
  name suggests. `tests/busybox/conftest.py`'s raise site is left alone;
  changing the mechanism is a behaviour change.
- **`install`/`stage` was never measured.** It sits with the rejected
  design-time candidates but, unlike `pgrep`, `reboot` and `sudo`, it was
  cleared by reasoning rather than by running the matrix. Either measure it or
  move it to the untested entries.

## 5. Not BusyBox: the empty-lane-leg gate

Recorded here because it has now been sighted twice and there is no gate for it.

**A lane leg that selects zero tests exits 5 and aborts `make`.** Unmarking a
test can empty one: issue #229 removed `serial_timing` from the last two members
of `serial_timing and concurrency`, which was the selector for the second leg of
`make stability-unit`, and the nightly would have gone red across a 5-way Python
matrix *after* paying the full ×100 soak.

No gate catches it. `makefile_serial_lane_gaps()` in
`tests/unit/test_lane_invariants.py` audits leg **text** — "does a recipe that
spells `not serial_timing` also carry a paired `-n0` leg?" — never leg
**membership**. `make coverage` stayed green throughout because its serial legs
use different intersections.

First sighting was `dashboard-soak`, whose Makefile comment already records the
identical hazard in prose. The natural template is
`test_no_lane_but_the_busybox_lane_can_select_the_busybox_tier` in
`tests/unit/test_tier_marker_invariants.py`, which states its rule over what a
lane can **select** rather than over the shape of its expression, and re-derives
its premise on every run.

Not built: a new gate needs its own mutation proof, and the house rule is that a
gate which cannot be shown red does not land.
