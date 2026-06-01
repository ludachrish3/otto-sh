# Migrate the embedded testbed from `qemu_x86` to ARM Cortex-M

Status: **deferred work item** (separate from, and not blocking, embedded
coverage). Tracked here because the embedded *coverage* bed already lands on
`qemu_cortex_m3` (see `embedded_coverage.md`); this item is about moving the
*rest* of the existing embedded testbed onto the same, better-supported arch.

## Why

The current embedded testbed runs Zephyr on `qemu_x86`. x86 is supported by
Zephyr but is mostly a fast QEMU CI-emulation convenience — **ARM Cortex-M is
Zephyr's first-class, most widely tested architecture** (STM32, Nordic nRF, NXP,
Microchip SAM, TI), with a dedicated *Arm Cortex-M Developer Guide*. Advanced
subsystems land on ARM first and on x86 late (e.g. LLEXT x86 support was only
"basic" as of May 2025). Standardizing the embedded bed on `qemu_cortex_m3`:

- makes the tests representative of how Zephyr is actually deployed,
- puts every embedded feature (not just coverage) on the mature LLEXT arch,
- keeps a single toolchain story (`arm-zephyr-eabi-*`) across the bed.

## Scope — what currently lives on `qemu_x86`

The existing embedded suite exercises (all on `qemu_x86` today):

- The host abstraction: `EmbeddedHost`, telnet console, `ZephyrFrame` command
  framing (`retval`), send/expect/oneshot.
- File transfer over console (`EmbeddedFileTransfer`) across filesystem variants:
  FAT-RAM, LittleFS, and no-FS.
- The Zephyr version matrix: 2.7 (inline-retcode frame), 3.7, 4.4 LTS.
- SNMP monitoring on embedded hosts.
- Multiple QEMU instances on `192.0.2.0/24` reached via an SSH hop.

Relevant code/config: `tests/firmware/zephyr/` (configs, overlays, build),
`tests/lab_data/tech1/hosts.json` (embedded host entries), `Vagrantfile` (the
`zephyr` VM provisioning), `tests/conftest.py` (`_ZEPHYR_BACKEND_NE` matrix).

## What changes

- **Vagrantfile `zephyr` VM:** install `qemu-system-arm` (in addition to / instead
  of `qemu-system-x86`); install the `arm-zephyr-eabi` Zephyr SDK toolchain.
- **Firmware build:** switch `west build -b qemu_x86` → `-b qemu_cortex_m3`
  (MPS2/AN385) for each variant under `tests/firmware/zephyr/configs/`. Re-verify
  the FAT-RAM / LittleFS / no-FS overlays build for the new board.
- **Networking:** confirm the QEMU networking path for `qemu_cortex_m3`
  (MPS2/AN385 SMSC LAN9118) reproduces the current `192.0.2.x` telnet console
  reachability through the hop. **Open question / main risk** — the x86 net-setup
  (`zeth`) path may need replacing with the Cortex-M board's QEMU NIC wiring.
- **Lab data:** update `tests/lab_data/tech1` embedded host entries as needed
  (IPs/hop unchanged where possible; `osType`/profile unchanged — board is a
  firmware-build detail, not an os_profile field).
- **systemd units:** the `zephyr-qemu-<id>.service` ExecStart switches to the ARM
  QEMU binary + new image paths.

## Validation

- All existing embedded unit + integration tests pass on `qemu_cortex_m3`:
  command frame (2.7/3.7/4.4), console transfer across FS variants, SNMP, host
  contract. Triage with `scripts/junit_failures.py`.
- Live path still works: dev VM → ssh hop `10.10.200.14` → telnet `192.0.2.x:23`.

## Sequencing

Do this **after** the embedded coverage feasibility gate has proven LLEXT +
coverage on a single `qemu_cortex_m3` instance — that instance doubles as the
migration's first proof point.
