# Zephyr source patches for the otto test bed

Version-prefixed patches applied to the Zephyr source trees by both
provisioners (the dev VM's `zephyrproject-v*` workspaces and the `zephyr`
VM's build trees — see the `Vagrantfile`). The apply loop globs
`${ZVER}-*.patch`, so a patch's filename prefix decides which Zephyr
version it targets, and `git apply --reverse --check` makes re-provisioning
idempotent.

otto's default is **stock Zephyr** (Kconfig/devicetree overlays only, no
firmware code). Every patch here is a deliberate, documented exception.

## `v2_7-shell-retcode.patch`

Zephyr 2.7's shell predates the `retval` command (the shell core doesn't
even track a last return value), so otto cannot read command exit codes the
way it does on 3.x. Adds a single line to the shell's command-dispatch path
printing `retCode = <n>` after every command; otto's
`ZephyrInlineRetcodeFrame` parses that in place of `retval`.

## `v3_7-e1000-rx-ring.patch`

Gives the qemu_x86 e1000 driver a multi-descriptor RX ring (stock 3.7
services a single descriptor) plus an `ETH_E1000_RX_QUEUE_SIZE` knob.

## `v2_7-fs-shell-mount-leak.patch` / `v3_7-fs-shell-mount-leak.patch`

Backport of the upstream (4.x) fix for a heap leak in the `fs` shell's
mount commands. In 2.7/3.7, `cmd_mount_fat` `k_malloc`s the mount-point
string (`mntpt_prepare()`) and never frees it when `fs_mount()` fails —
and it has no already-mounted guard, so every re-mount allocates again.
`cmd_mount_littlefs` has the guard but still leaks on a genuinely failed
mount. Zephyr 4.4 fixed both (early `-EBUSY` guard before allocating +
`k_free` on the failure path); these patches mirror that fix in each
version's local style.

This is the leak that slowly drained the FAT bed instance's 16 KB system
heap (~16 bytes per failed re-mount) until SNMP heap metrics flatlined —
diagnosed 2026-07-02 via the FAT-vs-LittleFS differential (LittleFS
instances auto-mount via `zephyr,fstab` and never see the command). otto
no longer *triggers* the failure path (its console transfer probes with a
read-only `fs statvfs` before mounting), so this patch is defense in depth
for any other shell client.

Verification (2026-07-02, dev VM, patched trees):

- 3.7: built `samples/subsys/shell/fs` for `mps2_an385` with FAT + LittleFS
  enabled; under QEMU, first `fs mount fat /RAM:` succeeds and every
  re-mount reports `FAT fs already mounted at /RAM:` **before any
  allocation**; a failing mount (unformatted disk, `-EIO`) frees its buffer
  and survives repeated invocations.
- 2.7: FAT branch compile-verified for `mps2_an385` (the bed's only 2.7
  config is FAT); the littlefs hunk is shape-identical to 3.7's
  QEMU-verified one.
- Both patches `git apply` cleanly onto pristine `v2.7-branch` /
  `v3.7-branch` trees and pass the provisioner's reverse-check idempotency
  path.
