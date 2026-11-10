# The chaos reboot guest

A test never power-cycles a hypervisor-hosted lab VM. The chaos lane's reboot scenarios
used to warm-reboot `test1` and `test2`, Ubuntu VMs under VirtualBox, and VirtualBox's
ARM64 EFI firmware can lose the boot entry on a warm reset (#978), stranding a VM that
only a person at the console can recover. One gate run could then block every session on
the shared bed. The reboot chaos therefore runs against a purpose-built Ubuntu 24.04
guest under QEMU behind `test4`, where a reboot is a QEMU reset under a read-only
firmware and cannot strand anything.

otto treats the guest as any other unix host: it drives `exec`, `tunnel add`,
`link impair`, `link repair` and `reboot --wait` at it through the hop with no product
change. This page is the guest's one user-facing home: what it is, what lives where on
`test4`, how a scenario holds its shutdown, what a reboot costs, and how to provision it.

## Identity

| Field | Value |
| --- | --- |
| Element, host id | `ubu2404`, `ubu2404-qemu` (board `qemu`, like the BusyBox guests) |
| Hop | `test4`, the Zephyr VM |
| OS | Ubuntu 24.04 server cloud image, a dated release pinned by SHA-256; the guest's architecture follows the hop's (`arm64` on an ARM64 host, `amd64` on x86_64) |
| `eth0` | QEMU user-mode NIC: egress for the archive and the default route, the shape of the lab VMs' NAT `eth0` |
| `eth1` (management) | `203.0.113.1/30`; TAP `ubeth-mgmt` on `test4` at `203.0.113.2`. otto logs in over this one, through the hop |
| `eth2` (data plane) | `203.0.113.5/30`; TAP `ubeth-data` on `test4` at `203.0.113.6`. The declared link and the tunnel use this one |
| Credentials | `vagrant`/`vagrant` (sudo without a password) and `test`/`Password1`, the lab VMs' two |
| sshd | OpenSSH, password authentication on |
| Console | ttyS0 to a file under the guest's directory; no console term is declared |
| Firmware | ARM64: the EDK2 code image mounted read-only with no variable store, so the firmware boots the image's removable-media path every time and there is no boot entry to lose. x86_64: SeaBIOS, which has no NVRAM at all |
| Resources | 1024 MB, 2 vCPUs, TCG with multi-threading, 4 GB virtual disk |
| QEMU | no `-no-reboot`; virtio disk and NICs; monitor on a unix socket |
| Unit | `chaos-qemu-ubu2404.service` on `test4`, `Restart=always`, both TAPs in `ExecStartPre` and `ExecStopPost` |

The addresses are TEST-NET-3 (203.0.113.0/24, RFC 5737): `test4`'s Zephyr instances hold
TEST-NET-1 and `test1`'s BusyBox guests hold TEST-NET-2, so the three blocks never
collide. There are two TAPs because a guest whose only NIC carries its management path
has no data plane to impair: `link impair` refuses that as a self-lockout, by design.

The identity lives in one constant, `CHAOS_GUEST` in
`scripts/render_chaos_guest_seed.py`. The seed, the QEMU wrapper and the systemd unit are
all rendered from it, the lab data mirrors it, and hostless guards pin every mirror. This
page describes the guest; the constant is what is true.

## Files on test4

Everything is under `/home/vagrant/` on `test4`:

| Path | What |
| --- | --- |
| `chaos-guest/<image>` | the pristine cloud image for the host's architecture, checksum-checked, never written |
| `chaos-guest/ubu2404.qcow2` | the guest's qcow2 overlay on the pristine image; "rebuild the guest" is "delete the overlay" |
| `chaos-guest/seed/`, `seed.iso` | the rendered cloud-init NoCloud seed and its ISO |
| `chaos-guest/console.log` | the guest's serial console |
| `chaos-guest/monitor.sock` | the QEMU monitor |
| `run-chaos-qemu-ubu2404.sh` | the rendered QEMU wrapper |

Plus `/etc/systemd/system/chaos-qemu-ubu2404.service` and a journal cap,
`/etc/systemd/journald.conf.d/otto-chaos.conf` (`SystemMaxUse=200M`), because the QEMU
consoles write to the journal. The footprint is the image (0.6 GB) plus the overlay
(under 1.5 GB after first boot and the package install); provisioning refuses when
`/home/vagrant` has under 3 GB free.

First boot is configured by the seed: the two users, sshd password authentication, a
`network-config` matching each NIC by the MAC the wrapper assigns and naming it `eth0`,
`eth1` or `eth2` (no gateway on either TAP), `socat`, the hostname, the lab VMs'
`reboot.target` override (`JobTimeoutSec=90`, `JobTimeoutAction=reboot-force`) and the
shutdown hold below. cloud-init runs the seed on the first boot only, so every later
chaos reboot finds the instance already configured.

## The shutdown hold

`chaos-shutdown-hold.service` lets a scenario make the guest's shutdown slow on demand,
with sshd accepting logins throughout. Its `ExecStop` sleeps for the number of seconds in
`/run/chaos/shutdown_hold` when that file exists and returns at once otherwise. It is
ordered after `systemd-user-sessions.service`, `ssh.service` and `ssh.socket`, so on the
way down systemd stops it before all three. The user-sessions unit matters as much as
sshd: its stop creates `/run/nologin`, after which sshd still answers but PAM refuses
every login, and otto's probe (a login) would read that as "down" at once.

A scenario sets the hold over the guest's shell before it issues the reboot:

```bash
echo 75 > /run/chaos/shutdown_hold
```

The hold unit creates `/run/chaos` with mode 1777, so a login creates and removes its
own hold without sudo, and the bed reap can remove a leftover one.

The hold's `TimeoutStopSec` is 600, well above any hold, but the lab's `reboot.target`
bound of 90 s applies on the guest too, so a hold is effective up to about 85 s and a
scenario sets 75. `/run` is a tmpfs, so a hold never outlives the reboot it was set for.

## What a reboot is on the guest

`otto host ubu2404-qemu reboot` runs `sudo -S reboot` exactly as it does on any unix lab
host. systemd stops units in reverse order: the hold unit first (sleeping if asked), then
sshd and user sessions, then the rest, bounded by `JobTimeoutSec=90`. The kernel resets;
QEMU, without `-no-reboot`, resets the machine and the firmware boots the overlay again.
The QEMU process, both TAPs, `test4`'s addresses, routes and qdiscs survive, and sshd
comes back with the same host key (it is on the overlay).

Measured with the ARM64 image under TCG on `test4`:

| Event | Time |
| --- | --- |
| first boot, sshd answering | 141 s |
| first boot, cloud-init finished (package install) | 426 s |
| `reboot --wait --down-timeout 120` without a hold, end to end | 170 s |
| the same with a 75 s hold, command to kernel reset | 81 s |
| boot to sshd after a reset | about 130 s |
| `reboot --wait` with a 75 s hold, end to end | 233 s |

What the lane pays for it (`make chaos`, measured 2026-11-10, one xdist worker per
scenario): the happy-path reboot 172 s, the link-repair scenario 207 s, the
tunnel-survivors scenario 247 s and the declared-window scenario, two reboots, 468 s; the
whole unix leg, 30 scenarios, about 21 min.

## Provisioning

The provisioner is `provision_chaos_guest` in the `Vagrantfile`, a shell provisioner named
`chaos-guest` that runs as the last step of the `zephyr` define. Run it from the host
checkout, against a checkout that holds the branch you want on the bed:

```bash
vagrant provision zephyr --provision-with chaos-guest
```

It is idempotent and touches nothing but its own files, the journal cap and its unit. It
installs `cloud-image-utils` and `qemu-utils`, checks the free disk before downloading
anything, downloads and checksum-checks the pinned image once, creates the overlay once,
and renders the seed, the wrapper and the unit by calling the renderer. The `Vagrantfile`
holds none of the guest's addresses, MACs or QEMU flags. The journal cap is a journald
drop-in, and the provisioner restarts `systemd-journald` once, only when that drop-in
changes.

**The re-seed rule.** The seed is rendered into a staging directory and compared with the
installed one. If it differs, the provisioner stops the unit, deletes the overlay and the
ISO, moves the new seed in and rebuilds the ISO, so the next boot re-seeds: a changed
identity reaches the guest only this way. A crash between any two of those steps leaves a
state the next run repairs. The unit is restarted only when the seed, the wrapper or the
unit changed, or when it is not running.

A re-seed also gives the guest a new SSH host key (the key lives on the overlay the
re-seed drops), so the first connection after it is refused as a changed key until the old
entry is removed from the dev VM's `~/.ssh/known_hosts`:

```bash
otto host ubu2404-qemu forget-key
```

Run that from any checkout that connected to the guest before the re-seed; the next
connection records the new key.

To restart the guest without re-provisioning use `make qemu-restart`; to read its
supervisor's log use `journalctl -u chaos-qemu-ubu2404` on `test4`.

## Why not a BusyBox guest, and why not a lab VM

A BusyBox guest cannot stand in: it has no systemd, so it has no real shutdown ordering,
no `reboot.target`, and no OpenSSH, which is what the reboot scenarios prove. A lab VM
cannot stand in either: its warm reset runs through VirtualBox's firmware, which is the
failure (#978). The guest keeps what the lane proves (the sudo reboot path, systemd's
stop ordering with sshd still answering, OpenSSH, netem, socat, bash from the archive)
and removes the hypervisor from the reboot. Re-enabling the chaos lanes on it is #985.

## The declared link and the `embedded` lab

The guest is declared in its own lab, `chaos`, with a link `test4--ubu2404-qemu` between
`test4:ubeth-data` and `ubu2404-qemu:eth2`. `test4` belongs to both `chaos` and
`embedded`, but the guest belongs only to `chaos`, so from `embedded`'s side that link has
one end outside the lab. Its `link repair --all` reports the dangling end by design (the
#473 shape). That is why the chaos lab exists: `embedded` is the Zephyr matrix, and the
guest is in no other lab, including the `unix` pool the lease draws from.
