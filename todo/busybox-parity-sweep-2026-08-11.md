# BusyBox transfer parity sweep

Queue for the full-parity workstream named in
`docs/superpowers/specs/2026-08-11-busybox-host-support-design.md` (exit
criterion 6): landmines found while building phase 4's `shell` transfer
backend, each with its measurement, so the sweep starts from evidence rather
than from a fresh survey. One item so far.

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

Not decided here: whether the second codec is an internal strategy inside the
one `shell` backend or a separately registered backend. The measurement argues
the shared part is the staging skeleton (temp-then-rename in the destination's
own directory, integrity-verified BEFORE the rename) and the codec-specific
part is the whole chunk loop, not merely the encode call.
