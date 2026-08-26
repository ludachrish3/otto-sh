# BusyBox transfer parity sweep

Queue for the full-parity workstream named in
`docs/superpowers/specs/2026-08-11-busybox-host-support-design.md` (exit
criterion 6): landmines found while building phase 4's `shell` transfer
backend, each with its measurement, so the sweep starts from evidence rather
than from a fresh survey.

Two items, and they were **one effort**: the second was the restructuring the
first needed in order to land at all.

> **BOTH ITEMS ARE BUILT.** The seam landed as `ShellCodec` /
> `PutChunkLoop` / `GetChunkLoop` with base64's emitted lines proven
> byte-identical, and the uu codec landed on it, selected by
> `ShellFileTransfer._select_codec` from the `applet_uudecode` /
> `applet_uuencode` probe. What is kept below is the MEASUREMENT, which is
> still the argument for the shape and is still what a future change has to
> answer to. Each section's own note says what its work item became. The one
> question neither item closed is the pty path — see the bottom of this file.

## `uuencode`/`uudecode` fallback for BusyBox 1.16.1 (no `base64` applet) — BUILT

`ShellFileTransfer` (`src/otto/host/transfer/shell.py`) used to require a
`base64` applet on the device and declared 1.16.1 an unsupported row for that
reason — 1.16.1 ships no `base64` at all
(`tests/busybox/test_shell_codec_contracts.py`'s `_EXPECTED_BASE64_FLAG` matrix
records `None` for that row). `uuencode`/`uudecode` is the alternative that
closes the gap, and it was measured to work before it was built.

**Measured 2026-08-12, Tier 2 rootfs, all five matrix rows** (1.16.1, 1.21.1,
1.28.1, 1.31.0, 1.35.0):

- payload: 13 bytes, `b"A\x00B\nC\rD\xffE'F\\G"` — NUL, newline, CR, 0xFF,
  single quote, backslash. The same binary-hostile payload
  `test_shell_codec_contracts.py`'s `_HOSTILE_PAYLOAD` uses for the
  `base64`/`dd` codec contracts.
- MD5: `a4119bcf623a896e535fc44c74e94d1d`
- result: `uuencode`/`uudecode` round-trips it on **all five rows, including
  1.16.1** — the entire point of the measurement. 1.16.1 is the one row with
  no `base64` applet, so uu is its only measured path to a working `shell`
  transfer there.

Built, and it round-trips on the exact row that needs it (see
`docs/superpowers/plans/2026-08-12-busybox-phase-4-shell-transfer.md`,
"Decisions taken before planning" §1, for the scoping call that deferred
building this rather than measuring it). What phase 4 did **not** build, all three now done:

- chunking/wrapping behaviour for `uuencode`/`uudecode` — `UuencodeCodec`
  reuses `_SHELL_CHUNK_BYTES` and rides a heredoc, measured against the real
  transport rather than only a chroot pipe (see "Measured 2026-08-14, round 2"
  below);
- a `Userland` probe for which codec a device offers — landed in `fffb0056`
  as `PROBED_APPLETS` / `Userland.has_applet`, one round trip for the whole
  list;
- `ShellFileTransfer` support for a second codec path, selected by that probe
  — `_select_codec`, which prefers base64 wherever it exists and gates every
  refusal on `is_settled`.

### Measured 2026-08-14: uu is NOT a drop-in for base64's algorithm

The chunking question above is now answered, and the answer decides the shape.
Payload 10253 bytes (the hostile head above plus filler), md5
`cec24026d4cb12df00f2ef9be4222224`, split into 4096/4096/2061, run through each
of the five rows' own `uuencode`/`uudecode` in the Tier 2 rootfs.

**`base64` is a stream codec; `uuencode` is a container format.** Appending
three uuencoded chunks to one file and decoding it ONCE returns **only the
first chunk** — 4096 of 10253 bytes — on **all five rows**, because `uudecode`
stops at the first `end` trailer. It exits **rc=0** while doing so. So the
naive port of `_run_put`'s append-then-decode-once shape yields a silently
truncated file that reports success: the same failure class this workstream
exists to remove, reintroduced by a codec swap.

What DOES work on all five rows is the inverse order — decode each chunk
separately and append the **plaintext** (10253 bytes, md5 matches). So the two
codecs need opposite loop shapes:

| aspect | `base64` | `uuencode` |
| --- | --- | --- |
| chunk payload | flattens to ONE line, 5464 chars for 4096 B | 92 lines of ≤61 chars, NOT flattenable |
| loop order | append encoded, decode once at the end | decode per chunk, append plaintext |
| framing | none | `begin <mode> <name>` / `end` per chunk |

Two more consequences of the container framing. `uudecode` writes to the
filename embedded in the header, so otto must always pass `-o` — the header
observed is `begin 664 stored.bin`, and the mode in it comes from the source
file, which is worth checking against `put --mode`. And because a chunk is
inherently multi-line, it cannot ride a single `printf '%s' '<blob>'` command
line the way base64 does; that shape has to change too, which is where the
transport-path line-length question moves.

### Measured 2026-08-14, round 2 — the four questions the build had to answer

Same Tier 2 rootfs, same five rows, plus the Tier 3 dropbear for anything
about a transport. **All five rows answered identically to each other on every
question below.**

1. **`uudecode -o <path>` works on 1.16.1**, the row the whole path depends on
   and the one where an option was most likely to be missing (`base64
   --decode` and `busybox --list` both fail there). rc 0, correct md5, on all
   five. Without `-o` it exits 0 and writes `otto` — the header's name — into
   the working directory, so `-o` is mandatory rather than tidy. It also
   APPLIES the header's mode to its output (`-rw-------` for `begin 600`),
   which is what dissolves the `put --mode` question below: the mode only ever
   describes the scratch, and the plaintext reaches the temp through `cat`.
2. **The per-chunk scratch shape round-trips the hostile payload on every
   row** — the 10253-byte payload above, three chunks with a partial tail, md5
   `cec24026d4cb12df00f2ef9be4222224` on all five. Pinned per row now, by
   `tests/busybox/test_shell_codec_contracts.py`, which runs the codec's OWN
   emitted commands rather than a transcript copied into the test.
3. **Command length: the ceiling is on the whole command STRING, not on its
   longest line.** Measured on Tier 3: a 400-line command of 18-character
   lines (8991 characters) crosses intact, 500 such lines (9009) drops the
   connection — the same ~9000 boundary `_MEASURED_EXEC_LINE_LIMIT` records
   for a single line. So uu's multi-line shape buys no room. For a
   26-character destination a full chunk is **one command** of **5952
   characters** (100 lines: 95 of frame at up to 61 each, and a 186-character
   first line carrying the scratch path twice and the temp path once) against
   base64's single 5533-character line: about 3047 characters of headroom
   rather than base64's 3466, and uu spends it roughly three times as fast per
   character of destination path. What the shape does buy is a longest LINE of
   186 rather than 5533, under the ash line editor's 1022 — though nothing has
   measured a multi-line command on the pty path, so no claim is made for it.

   The rejected alternative is worth recording because it is the one a reader
   reaches for: `printf '%s\n'` with each frame line as a quoted argument.
   uu encodes the byte value 7 as a SINGLE QUOTE, so that form's length is
   payload-dependent — measured against the same paths, 6275 characters for a
   byte ramp and **11330 for a chunk of `0x07`**, past the ceiling for a chunk
   size that is otherwise safe. The heredoc needs no escaping and is a fixed
   5952 whatever the bytes.
4. **Scratch lifetime.** It lives beside the staged temp, named by the same
   `staged_temp_name`, so it is inside the same `max_filename_len` budget —
   `<temp>.uu` would have been 3 characters over for a destination that used
   all of it. It is removed by the same command that creates it, on both that
   command's paths, because a FAILED `uudecode -o` LEAVES ITS OUTPUT FILE
   BEHIND (measured: `uudecode: short file`, rc 1, empty scratch still there).
   A second best-effort sweep covers the command that never completed.

One hazard found while building, not visible from the device at all:
`binascii.a2b_uu` **silently zero-pads** a line clipped short of its declared
length (`a2b_uu("NOTUU")` returns 46 bytes, 43 of them NUL, no error). That is
base64's `validate=False` failure in another costume, and `_uu_unframe` now
rejects any line whose character count does not match its length byte.

`put --mode` (work item 5 below) is **answered, not deferred**: with `-o` the
header's mode applies to the scratch and never to the destination.

## Shell transfer: make the encoding a pluggable unit — BUILT (was DESIGN OPEN)

Raised by Chris 2026-08-14, while the gap-registry raise sites were being
wired: does the `shell` backend have subtypes for `base64` and uu, or should
those be separate backends (`shell_base64` / `shell_uucode`)? Each codec
plausibly has its own options, which is an argument for separating them.

**The recommendation below was taken, and this is what shipped.** The seam is
`ShellCodec`, whose unit is the whole chunk loop; `transfer: shell` is
unchanged and no `ShellOptions` was needed. What follows is the argument as it
was written, kept because it is still the argument.

**Before it, there was no seam at all.** `ShellFileTransfer` assumed `base64`
end to end, not merely at the encode call:

- `_SHELL_CHUNK_BYTES` is 4096 *plaintext* bytes because base64 expands that to
  5464 characters, and the emitted command line measures 5534 against a
  measured 9000-character ssh exec ceiling;
- PUT is `printf '%s' '<b64>' | base64 <flag> >> <temp>`, GET is
  `dd … | base64`;
- there is **no `ShellOptions` class**. The codec is modelled as a *userland
  capability* (`UserlandOptions.base64_flag`), alongside `timeout_style`,
  `checksum`, `stat_size`, `elevation` and `shell_dialect`.

### The recommendation, and why

**One operator-facing `shell` backend. The codec is an internal unit selected
by probe, not a backend name.** Two reasons:

1. Which codec a device has is a **device fact otto probes**, not an operator
   choice. Putting it in the backend name forces the operator to know it, makes
   the `busybox` profile's `valid_transfers` list both spellings, and turns a
   wrong guess into an avoidable failure — the opposite of what `Userland` is
   for. It is also inconsistent with every other device-spelling variance here:
   there is no `nc_openbsd` / `nc_busybox` split, there is `NcOptions.exec_name`
   plus `timeout_style` inside one backend.
2. What the two codecs genuinely share is the **staging skeleton** —
   temp-then-rename in the destination's own directory, integrity-verified
   BEFORE the rename. That is phase 4's hard-won part and it is
   codec-independent.

But note what the measurement above forces: the codec-specific unit is the
**whole chunk loop**, not an `encode()` call. So this is a template method with
a codec-owned loop body, NOT a strategy object with one thin hook. A design that
assumes the latter will not fit uu.

Chris's "distinct options" instinct is right in substance and does not require
separate backends: if per-codec knobs are ever needed, one `ShellOptions` with
per-codec fields covers it without making the operator choose the codec.

| route | pros | cons |
| --- | --- | --- |
| one backend, codec = internal unit (recommended) | `transfer: shell` unchanged, no lab-config migration; codec stays probed; the unit can own chunking, framing and loop order | one class does more; needs the codec probe |
| separate registered backends | clean per-codec namespaces; each algorithm self-contained | operator must know a device fact otto can probe; `valid_transfers` grows; breaks the `nc`/`timeout_style` precedent; duplicates the staging skeleton |
| swap the encode call only | smallest diff | **measured not to work** — see the uu item above |

### Work items — all five done

1. **DONE** (`fffb0056`). A `Userland` codec probe: `base64` / `uuencode` /
   neither. Constraint already
   known: `busybox --list` **does not exist on 1.16.1** (exits 1,
   `--list: applet not found`), so applet *enumeration* cannot be the basis;
   per-applet detection is the portable shape and its round-trip cost at
   resolution time is unmeasured.
2. **DONE** (`f44bb42c`). Restructure `ShellFileTransfer` so the staging
   skeleton and the chunk loop are separable, with the base64 path landing on
   the new seam unchanged — proven byte-identical, and pinned by
   `TestEmittedCommandLinesArePinned`.
3. **DONE.** The uu path, using the **decode-per-chunk, append-plaintext**
   order, with `-o` always passed.
4. **DONE.** Re-measured against the real transport, and the question did
   change shape: the ceiling turned out to bind the whole command string
   rather than its longest line, so the multi-line framing buys nothing. See
   "Measured 2026-08-14, round 2" above.
5. **DONE, and the answer is "they cannot disagree".** `uudecode -o` applies
   the header's mode to the SCRATCH, which otto deletes; the plaintext reaches
   the destination through `cat`, which carries no mode.

### Blocks, and is blocked by — both resolved

- **Blocked `shell-transfer-base64`'s raise-site wiring**, deliberately held
  for exactly the reason given: that record's message used to say "use a
  backend the device supports, or install base64", and the honest message on a
  1.16.1 device is now "otto uses uu instead". The record and the docs page
  were rewritten with the codec, not before it. See
  `todo/busybox-phase-5-followups-2026-08-13.md` §1.
- **Shares its probe question with four other surfaces.** `sftp-transfer`,
  `scp-transfer`, `nc-transfer` and `shutdown-command` were each blocked on a
  device-capability signal that did not exist, and it is the same kind of
  question as the codec probe — one mechanism likely serves all five. That
  mechanism landed as the batched `applet_*` capabilities, and it has now served
  two of them: `shutdown-command` first, where
  `otto.host.unix_host.shutdown_command` reads `applet_shutdown` and
  `applet_poweroff` and emits the spelling the device has, and then
  `scp-transfer`, where `otto.host.transfer.scp.refuse_if_scp_is_absent` reads a
  settled `applet_scp` and declines. `sftp-transfer` gets NO applet capability
  and is the one of the five the mechanism cannot serve — its `measured_on`
  names an absolute path (`/usr/lib/sftp-server`) that is not on `PATH` on a
  healthy GNU host either, so `command -v sftp-server` would answer "absent"
  where sftp works perfectly. `nc-transfer` has one and is not solved by it:
  `NcOptions.exec_name` means presence of *an* `nc` is not the question. Note the
  `busybox` profile declares `valid_transfers: ["shell","scp","sftp","ftp","nc"]`
  **deliberately**, because a device with a real sftp-server or netcat installed
  works, so those refusals must be device-conditional rather than blanket.
  **`nc-transfer` closed 2026-08-25 — by a different mechanism than the one
  queued here.** No capability, no variant, no device-conditional refusal: the
  backend now emits the one spelling every measured netcat accepts, so there
  is no question left to probe. The gap record and its refusal are deleted;
  see `docs/superpowers/specs/2026-08-25-nc-universal-spelling-design.md`.

## Still open after the sweep: the pty path

Neither codec's chunk command has been measured on a line-edited transport. A
`term: telnet` BusyBox host routes this backend through a pooled shell session
rather than a bare exec channel, and BusyBox ash's line editor delivers 1022
characters intact and truncates at 1023 (the `run-command-line-length` gap
record). base64's 5535-character line is over that by a factor of five. uu's
longest LINE is 98, which would fit — but a uu chunk is a multi-line heredoc,
and whether a pooled session carries one at all is a separate question nobody
has asked. No tier puts this backend on a telnet transport, so measuring it
needs a new one.
