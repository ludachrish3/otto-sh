# BusyBox transfer parity sweep

Queue for the full-parity workstream named in
`docs/superpowers/specs/2026-08-11-busybox-host-support-design.md` (exit
criterion 6): landmines found while building phase 4's `shell` transfer
backend, each with its measurement, so the sweep starts from evidence rather
than from a fresh survey.

Two items, and they are **one effort**: the second is the restructuring the
first needs in order to land at all. Do not schedule them separately.

## `uuencode`/`uudecode` fallback for BusyBox 1.16.1 (no `base64` applet) — MEASURED-FEASIBLE

`ShellFileTransfer` (`src/otto/host/transfer/shell.py`) requires a `base64`
applet on the device and declares 1.16.1 an unsupported row for that reason —
1.16.1 ships no `base64` at all (`tests/busybox/test_shell_codec_contracts.py`'s
`_EXPECTED_BASE64_FLAG` matrix records `None` for that row).
`uuencode`/`uudecode` is the not-yet-built alternative that closes the gap,
and it has already been measured to work.

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

Queued as **measured-feasible, not a hypothesis**: the codec round-trips on
the exact row that needs it (see
`docs/superpowers/plans/2026-08-12-busybox-phase-4-shell-transfer.md`,
"Decisions taken before planning" §1, for the scoping call that deferred
building this rather than measuring it). What phase 4 did **not** build, and
what this item covers:

- chunking/wrapping behavior for `uuencode`/`uudecode` analogous to
  `_SHELL_CHUNK_BYTES`'s base64 chunking (line-length conventions differ
  between the two encodings and have not been measured against the transport
  path, only against a chroot's direct pipe);
- a `Userland` probe for which codec a device actually offers (`base64` vs
  `uuencode` vs neither) — today `ShellFileTransfer` assumes `base64` and
  raises rather than falling back;
- `ShellFileTransfer` support for a second codec path, selected by that probe.

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

Not decided here: how the second codec is structured. That is the next item,
which this measurement is the input to.

## Shell transfer: make the encoding a pluggable unit — DESIGN OPEN

Raised by Chris 2026-08-14, while the gap-registry raise sites were being
wired: does the `shell` backend have subtypes for `base64` and uu, or should
those be separate backends (`shell_base64` / `shell_uucode`)? Each codec
plausibly has its own options, which is an argument for separating them.

**Today there is no seam at all.** `ShellFileTransfer` assumes `base64` end to
end, not merely at the encode call:

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

### Work items

1. A `Userland` codec probe: `base64` / `uuencode` / neither. Constraint already
   known: `busybox --list` **does not exist on 1.16.1** (exits 1,
   `--list: applet not found`), so applet *enumeration* cannot be the basis;
   per-applet detection is the portable shape and its round-trip cost at
   resolution time is unmeasured.
2. Restructure `ShellFileTransfer` so the staging skeleton and the chunk loop
   are separable, with the base64 path landing on the new seam unchanged. This
   should be provable: the refactor is done right when base64's emitted command
   lines are byte-identical before and after.
3. The uu path, using the **decode-per-chunk, append-plaintext** order, with
   `-o` always passed.
4. Re-measure the emitted line length for uu against the real transport. base64's
   5534-vs-9000 headroom does not transfer: a uu chunk is 92 lines rather than
   one, so the question changes shape rather than scaling.
5. Check `begin 664` against `put --mode` — uu bakes the source file's mode into
   its header, and otto has a `--mode` feature that may disagree with it.

### Blocks, and is blocked by

- **Blocks `shell-transfer-base64`'s raise-site wiring**, which is deliberately
  held. That record's message currently says "use a backend the device supports,
  or install base64"; once a codec probe exists, the honest message on a 1.16.1
  device becomes "otto will use uu instead". Wiring it first means writing a
  message we would then revise. See `todo/busybox-phase-5-followups-2026-08-13.md` §1.
- **Shares its probe question with four other surfaces.** `sftp-transfer`,
  `scp-transfer`, `nc-transfer` and `shutdown-command` are each blocked on a
  device-capability signal that does not exist, and it is the same kind of
  question as the codec probe — one mechanism likely serves all five. Note the
  `busybox` profile declares `valid_transfers: ["shell","scp","sftp","ftp","nc"]`
  **deliberately**, because a device with a real sftp-server or netcat installed
  works, so those refusals must be device-conditional rather than blanket.
