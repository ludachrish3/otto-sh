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
