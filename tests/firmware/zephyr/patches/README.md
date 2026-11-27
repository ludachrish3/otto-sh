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

## `v3_7-socket-service-stale-revents.patch`

Fixes a defect in Zephyr 3.7's socket service
(`subsys/net/lib/sockets/sockets_service.c`) that froze a telnet shell
session when a second client connected to the same device.

The telnet shell backend serves one client. One service thread polls the
listening socket and the client socket and calls the backend for each one
that is ready. After every callback, 3.7 copies the service's saved events
back into the poll array, and the saved event of the client socket still
carries the "readable" flag from the last data that client sent. So after
the listening socket's callback has refused a second client, the same poll
round calls the backend's receive function for the client socket with
nothing to read. That function takes the backend's receive lock and blocks
in `recv`. Until the connected client sends something or disconnects:

- no further connection is accepted, so a refused client's next attempt
  waits unanswered;
- the shell thread cannot take the receive lock, so a command the connected
  client has already sent is never run.

The patch clears the copied-back flag. A socket that really is readable is
reported again by the next poll. Zephyr 4.4's socket service no longer has
the copy-back (it restores only the descriptor of the socket whose callback
ran), so only the 3.7 images need it. This patch is not a backport of that
change: it is the smallest edit that stops the replay. Of those, only the two that
use the telnet shell backend are affected (`v3_7_fat_ram`, `v3_7_lfs`); the
ARM instances reach their shell over an emulated serial line.

On the bed this showed as
`test_concurrent_clients_to_one_console_contend_and_recover` running past
its 30-second timeout about once in many gate runs (otto issue 1366): whether it
stalled depended on which side of the winner's first byte the loser's
connection was refused.

Verification (2026-11-27 UTC, `zephyr37_fat` on the bed, each step timed):

| Sequence | Stock 3.7 | Patched |
|---|---|---|
| A client runs a command and stays connected; a second client connects | second client fails after 17.4 s (6 of 6 rounds) | 2.2 s (4 of 4) |
| The same, and the first client sends one more command at +6 s | that command gets no reply for 30 s (3 of 3) | it returns at once (4 of 4) |
| A third client holds the console; two more connect at once | 17.4 s and 32.6 s, and the holder's next command fails (3 of 3) | 2.2 s each, the holder's next command succeeds (8 of 8) |

With the patch the guest logs one `Telnet client already connected` line
for every refused connection and no `Telnet accept error` or `Out of
buffers` line. The patch applies cleanly to the `v3.7-branch` tree both
provisioners use and passes the reverse-check path.
