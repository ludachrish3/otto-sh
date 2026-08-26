# nc transfer: one universal spelling for every measured netcat

**Goal:** close the `nc-transfer` gap — the single defect behind all ten
`measured-broken` cells in the support matrix (`transfer-roundtrip` and
`transfer-mode` across the five BusyBox profiles) — by changing otto's `nc`
command spellings to ones every measured netcat accepts, instead of building
a per-userland variant.

**Outcome:** the matrix moves from 41 ok / 10 broken / 3 not-observable to
51 ok / 3 not-observable. The three `timeout x zephyr` cells are a separate,
deliberately-declared non-observable (see `test_timeout_contract.py`'s
`applicable_cell`) and are out of scope.

## The gap today

`src/otto/host/userland.py`'s `nc-transfer` record (status `measured-broken`)
documents it: the backend emits three commands, and two of their spellings are
OpenBSD-isms the BusyBox `nc` applet rejects.

| direction | emitted today | BusyBox's answer |
| --- | --- | --- |
| PUT (device listens) | `[prefix]nc -l -w SECS PORT < /dev/null > dst` | `bad address 'PORT'` — parses PORT as HOST |
| GET (device sends) | `nc -N IP PORT < src` | `invalid option -- N` / `unrecognized option: N` |
| GET, tunneled (device serves) | `[prefix]nc -Nl -w SECS PORT < src` | both rejections in one option string |

`[prefix]` is `_nc_listener_prefix` — `timeout 3600 ` in the userland's
timeout dialect, the remote-side orphan backstop. The GET direction is wired
to a refusal (`refuse_if_nc_rejects_dash_n`, predicate: a settled `nc_dash_n`
probe of `rejected`); the PUT direction is a `PATH_OPEN` GapPath and fails as
a listener timeout. On the bed this xfails twenty conformance items (2
contracts + 2 positive controls x 5 `bed-busybox[*:telnet:nc]` cells), which
the collator folds into the ten broken matrix cells.

## Measurement campaign (2026-08-25)

All numbers below were measured this day, with 256 KiB binary-hostile
payloads (NUL, CR/LF, 0xFF, quote, backslash), byte-verified by md5, against:
OpenBSD netcat 1.226 (`1.226-1ubuntu2`, the dev VM's and test1's identical
build — test1 probed over the real lab network, 10.10.200.0/24) and all five
BusyBox matrix artifacts run natively from the verified artifact cache.
Probe scripts: session scratchpad, throwaway.

### The universal spellings work everywhere

| probe | OpenBSD 1.226 | bb 1.16.1 | bb 1.21.1 | bb 1.28.1 | bb 1.31.0 | bb 1.35.0 |
| --- | --- | --- | --- | --- | --- | --- |
| listener `-l -p PORT` (receive) | ok | ok | ok | ok | ok | ok |
| listener `-l -p PORT < src` (serve) | ok | ok | ok | ok | ok | ok |
| sender, no `-N`, receiver closes at N bytes | ok | ok | ok | ok | ok | ok |
| control: receiver never closes | HANGS | HANGS | HANGS | HANGS | HANGS | HANGS |

Every "ok" is: bytes intact, process exits within 0.02s of the peer's close,
rc 0. The control row is the load-bearing one: **on every userland the
receiver's close is what terminates the `-N`-less sender**. Size-terminated
reads are the mechanism that replaces `-N`, not an optimization — and they
work identically on the netcat that HAS `-N`, which is what makes one
spelling universal.

### `-w` is an idle timeout on the data connection, and must go

Measured with `-l -w 2 -p PORT`:

| shape | OpenBSD 1.226 | bb 1.16.1 | bb 1.21.1 | bb 1.28.1 | bb 1.31.0 | bb 1.35.0 |
| --- | --- | --- | --- | --- | --- | --- |
| unconnected listener, `-w 1` | stays | exits ~1s rc 1 | exits ~1s rc 1 | exits ~1s rc 1 | exits ~1s rc 1 | exits ~1s rc 1 |
| 3s inter-chunk gaps (gap > w) | KILLED rc 0 | KILLED rc 0 | survived | KILLED rc 0 | KILLED rc 0 | KILLED rc 0 |
| continuous slow send, total >> w | survived | survived | survived | survived | survived | survived |

Three findings. First, on five of the six userlands a mid-transfer stall
longer than `-w` kills the connection — **with rc 0 and a partial output
file**, the silent-truncation shape. Second, this includes OpenBSD 1.226:
its man page's "-w has no effect on the -l option" describes only the accept
wait (row 1), not an accepted connection. The current code's comment reads
that sentence as "such a listener waits forever", which is true before a
client arrives and false after. Third, continuous data never trips it — `-w`
punishes exactly and only the stall, violating this module's own stated
principle that a healthy transfer of any size must not be cut off mid-flight.

Today's blast radius: on PUT, `_verify_nc_dest_size` catches the shortfall
(an avoidable failure, not a corruption); on **tunneled GET the idle-kill FIN
is indistinguishable from completion** — read-to-EOF delivers a truncated
file as success. That is a live silent-truncation defect in the current GNU
path, found by this measurement.

### Remote exit codes are not portable

Receiver closing early (at N/2): rc 0 on OpenBSD and bb 1.28.1, rc 1 on the
other four rows. Same split for a half-read serve. Integrity must therefore
stay on otto's own byte accounting — no design below reads the remote rc as
a success signal.

## Design

One universal spelling per direction. No variant, no per-userland selection,
no new probe — the selection problem the gap record calls unprobeable ("a
listener probe would bind a port") is dissolved rather than solved.

| direction | new spelling |
| --- | --- |
| PUT | `[prefix]nc -l -p PORT < /dev/null > dst` |
| GET | `nc IP PORT < src` |
| GET, tunneled | `[prefix]nc -l -p PORT < src` |

Diff from today: `-p` added before the port, `-w SECS` and `-N` removed.
`NcOptions.exec_name` keeps working unchanged as the any-netcat escape hatch;
the universal syntax widens what it can point at (today's `-l PORT` is the
one spelling traditional netcat rejects, and `-l -p PORT` is the syntax it
requires and ncat mimics — documented-compatible, unmeasured; the measured
set is OpenBSD 1.226 plus the five BusyBox rows).

### Size-terminated GET reads

`_get_files_nc` already prefetches every source's size over the control plane
via `_control_run` for progress totals. Under size-termination that size is
the transfer's stopping point, so the prefetch must report what the sender
will actually send: it is spelled `stat -L -c '%s %F'`. `-L` because the
sender's `< src` follows symlinks while a bare `stat` reports the LINK's own
length (measured 2026-08-25 on every pinned BusyBox row and coreutils: 23 for
a link to a 12-byte file, 12 with `-L`); `%F` so a source that is not a
regular file is refused by name rather than read against a size that is not
its content (directories, device files). `%F` cannot single out procfs/sysfs
pseudo-files — `/proc/version` reports `0 regular empty file` on every
measured `stat` — so those are documented as untransferable by this backend
(`NcOptions`, `host-options.md`), not refused. The read loop changes from
read-until-EOF to read-exactly-`sizes[src]`-then-close:

- **exactly N bytes read** → close the connection (this is what terminates
  the remote sender; measured on every userland) → success;
- **EOF before N** → an explicit short-read error naming got/expected. This
  is strictly stronger than today, where a clean FIN mid-transfer (including
  the `-w` idle-kill above, or a sender killed mid-file) is silent
  truncation;
- **the stat prefetch fails** → an explicit error for that file, before any
  listener or connection exists. Today `sizes[src]` degrades to 0, which only
  breaks the progress display; under size-termination a 0 would read zero
  bytes and "succeed", so the degrade arm becomes a refusal. The error names
  the stat command and the host, in the style of the backend's existing
  messages;
- **the stat reports a non-regular file** → an explicit refusal naming the
  type, before any listener or connection exists;
- **no data for `_NC_STALL_TIMEOUT` (5 s) mid-read** → an explicit error
  naming the bytes received, on BOTH arms. Size-termination moves "when do
  we stop" from the sender's FIN to otto's close, so a sender that delivers
  fewer than N bytes and then waits for that close (what every measured
  netcat does at stdin EOF) would otherwise park the read forever and strand
  the remote process; the tunneled arm already carried this bound, the plain
  arm now does too. The close happens on every arm, because the close is
  what ends the sender.

The tunneled GET applies the same loop to the hop-forwarded stream. Excess
bytes past N are never read: the close discards them (a file that GREW
between stat and read delivers the N bytes that were measured; a transfer of
a concurrently-modified file is undefined in every backend, unchanged here).

TCP retransmission cannot shift the count: the kernel dedupes and reorders by
sequence number, and the socket delivers each stream byte exactly once, in
order. The count is over the application stream, not packets.

### Orphan bounding after the `-w` drop

The `timeout 3600` prefix (`_NC_LISTENER_HARD_CAP_S`, dialect via
`_nc_listener_prefix`) becomes the sole remote-side bound where the userland
has a `timeout` at all — `_nc_listener_prefix` degrades to nothing for the
`absent` and untaught styles, and those hosts have no remote-side bound if
otto dies outright; `_cancel_and_reap` owns every other exit. Where present
it has the right properties: it kills with a signal (observable, never an
rc-0 partial file) and cannot fire mid-healthy-transfer. otto-side bounds
are unchanged except for the read-stall bound both GET arms now carry (see
"Size-terminated GET reads"): `_cancel_and_reap` on every error path,
`listener_timeout` as the post-transfer wait for listener exit,
stall-bounded sending. The BusyBox `-w`
accept-bound (row 1 above) is lost; that case was already owned by
`_cancel_and_reap` — the code's comment records bed listeners alive after
three days under `-w 30`, so `-w` was never the mechanism.

Comments that read `-w` as inert on OpenBSD listeners are corrected where
they stand (`_put_files_nc`, `_get_files_nc_tunneled`,
`NcOptions.listener_timeout`'s docstring — which also loses its "Also passed
as ``nc -w``" sentence).

## Demolitions

Nothing emits `-N` any more, so the machinery that predicted its rejection
goes:

- `refuse_if_nc_rejects_dash_n` and its call in `_get_files_nc`, plus
  `tests/unit/host/test_nc_transfer_refusal.py`'s arrival guards;
- the `Userland._probe_nc_dash_n` probe, the `NC_DASH_N_*` verdict constants
  and the `nc_dash_n` capability plumbing;
- **`UserlandOptions.nc_dash_n`** — an operator-facing field, removed with
  its schema entry (regenerate via `make schema`) and docs. Kept-but-dead was
  considered and rejected: the field's only consumer was the refusal, its
  probe costs a control round trip at resolution, and its docstring would
  claim a relevance the backend no longer has;
- the `nc-transfer` Gap record. The registry has no fixed state
  (`_STATUSES = [MEASURED_BROKEN, UNTESTED]`) because it is a registry of
  gaps: a closed surface exits it, and the history lives in git and in this
  spec. The docs page (`docs/architecture/subsystems/busybox-support.md`)
  and every test pinning the registry's derived counts move in the same
  commit.

The lab-config docs example naming `nc_options = { exec_name = ... }` is
unaffected.

## Conformance and the matrix

- `test_transfer_contract.py`'s `expected_failure` returns its reason for
  `kind == BED_BUSYBOX and transfer == "nc"`; that arm is deleted with the
  `_NC_ON_BUSYBOX` reason text and the module banner section about it. The
  xfails are strict, so the bed run FORCES this ordering: an unremoved
  declaration over a fixed product is twenty hard-erroring XPASSes.
- Re-measure with `make conformance-bed` (bed access required; the collator
  is the only writer of `measured-*` verdicts). The ten cells flip
  broken -> ok, which the directional gate classifies as ALLOWED; any GNU
  cell regressing ok -> non-ok BLOCKS, which is the safety net for the
  spelling change riding into cells that pass today (the eight
  `bed-unix` nc cells, 16 items, per the gap record's own measurement).
- `schemas/support_matrix.json` lands in the same change as the product fix
  per the collator's contract, and `docs` regenerates the support-matrix
  page.

## Testing

Unit (hermetic, red-first, mutation-checked):

- emitted-command pins for all three spellings — asserting the FULL command
  string including redirections, so a reintroduced `-w` or `-N` or a dropped
  `-p` each redden a named pin;
- the GET short-read arm: a fake stream that FINs at N-1 bytes must produce
  the short-read error, not success (this is the tunneled-GET truncation
  defect's regression guard);
- the GET stat-failure arm: a control-plane stat answering rc != 0 must
  refuse that file before any listener exists;
- the exactly-N arm: a stream carrying N bytes then silence must succeed
  without waiting for EOF (mutation: revert the loop to read-to-EOF; the
  test must hang-fail, bounded by its own timeout, or assert the close).

BusyBox tier (`tests/busybox/test_applet_contracts.py`): per-row Tier-1
contracts for the two applet-facing claims — `-l -p PORT` accepted and
peer-close terminates an `-N`-less sender — in the file's existing
argv-level style, so an upstream applet change reddens the row that changed.

Bed: the twenty un-xfailed conformance items, plus the directional gate over
the collated matrix.

## Out of scope

- `ncat` / `nc.traditional` measurement (documented-compatible; neither is
  installed on any reachable host);
- a presence refusal for devices with NO `nc` at all — unchanged behavior
  today (absent was never refused; the transfer fails at listener-bind);
  a follow-up could key one on the existing `applet_nc` probe in the
  `scp-transfer` pattern;
- the `daemon-launch` gap and the three `timeout x zephyr` not-observable
  cells;
- the pty-path question for the SHELL backend recorded at the bottom of
  `todo/busybox-parity-sweep-2026-08-11.md` (different backend, unaffected).
