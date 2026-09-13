# otto Zephyr test-bed configuration

Stock Zephyr samples with otto's Kconfig + devicetree overlays layered on,
used to validate otto's `EmbeddedHost` support against real RTOS targets. Four
guests build `samples/subsys/shell/shell_module` for `qemu_x86`; three build
for the ARM `mps2_an385`, two of those from `samples/subsys/llext/shell_loader`.

**The samples stay stock** — otto ships no `main.c` and patches no sample.
What this directory adds beyond configuration is limited to two deliberate,
Kconfig-gated out-of-tree modules: `snmp_agent/` (monitoring channel for the
x86 beds) and `ext_svc/` (base-owned service-thread helper for LLEXT
extensions on the ARM cov beds; see its README for what it deliberately does
NOT include). Any sentinel/marker behavior otto relies on comes from what the
stock Zephyr shell already does.

## Layout

```text
common/
    otto-overlay.conf       # always-on bits: shell, networking, runtime stats
    otto-overlay-v3_7.conf  # per-version supplements, applied when present
    otto-overlay-v4_4.conf
patches/
    <zver>-*.patch          # per-version Zephyr source patches (see its README)
configs/
    v3_7_fat_ram/           # x86: FAT on a RAM disk (the default for transfer tests)
        overlay.conf
        app.overlay
    v3_7_lfs/               # x86: LittleFS on the flash simulator
        overlay.conf
        app.overlay
    v2_7_fat_ram/           # x86: the 2.7 FAT bed
        overlay.conf
        app.overlay
    v4_4_lfs/               # x86: the 4.4 LittleFS bed
        overlay.conf
        app.overlay
    cov_an385/              # ARM: shared by both LLEXT bases
        overlay.conf
    v3_7_no_fs_arm/         # ARM: no filesystem at all — graceful-degradation target
        overlay.conf
    v3_7_no_fs/             # vestigial: the pre-ARM x86 no-fs config, no longer built
        overlay.conf
snmp_agent/                 # out-of-tree module registered on the x86 builds
ext_svc/                    # out-of-tree module registered on the ARM builds
```

`common/otto-overlay.conf` is the shared Kconfig overlay applied to every
config. Per-filesystem deltas live in `configs/<id>/overlay.conf` (FS Kconfig,
per-instance IP) and `configs/<id>/app.overlay` (devicetree, when needed).
Builds layer the two via Zephyr's `-DEXTRA_CONF_FILE="a;b"` semicolon list.

## The configs and what they exercise

Four networked `qemu_x86` guests, each on its own TAP and /30:

| id             | otto host       | IP           | /30 subnet      | Filesystem                      | Why it exists                                                                                    |
|----------------|-----------------|--------------|-----------------|---------------------------------|--------------------------------------------------------------------------------------------------|
| `v3_7_fat_ram` | `zephyr37_fat`  | `192.0.2.1`  | `192.0.2.0/30`  | FAT on a 128 KiB RAM disk       | Default target for `EmbeddedFileTransfer`'s console `put`/`get` round-trip                       |
| `v3_7_lfs`     | `zephyr37_lfs`  | `192.0.2.5`  | `192.0.2.4/30`  | LittleFS on the flash simulator | Proves the console backend is FS-agnostic; LittleFS has different mount cost / on-disk semantics |
| `v2_7_fat_ram` | `zephyr27_fat`  | `192.0.2.13` | `192.0.2.12/30` | FAT on a RAM disk               | The oldest supported Zephyr; its shell predates `retval`                                         |
| `v4_4_lfs`     | `zephyr44_lfs`  | `192.0.2.29` | `192.0.2.28/30` | LittleFS on the flash simulator | The newest supported Zephyr, against the same FS as the 3.7 twin                                 |

Each of those is on its own /30 so the host VM's routing table holds a
distinct route per TAP. A shared `192.0.2.0/24` produced overlapping routes
and the kernel picked one, making the other instances unreachable from the
host.

Three ARM `mps2_an385` guests reach their console over a telnet port on the
hop instead. They have no TAP and no /30 — the address below is identity, not
a route:

| id              | otto host        | Address      | Telnet | Sample         | Why it exists                                                          |
|-----------------|------------------|--------------|--------|----------------|------------------------------------------------------------------------|
| `v3_7_no_fs_arm`| `zephyr37_nofs`  | `192.0.2.37` | `2325` | `shell_module` | `EmbeddedFileTransfer`'s graceful-degradation path against a real target |
| `cov_an385`     | `zephyr37_llext` | `192.0.2.33` | `2323` | `shell_loader` | LLEXT base for embedded coverage on 3.7                                |
| `cov_an385`     | `zephyr44_llext` | `192.0.2.34` | `2324` | `shell_loader` | LLEXT base for embedded coverage on 4.4                                |

All seven run in the same `zephyr` Vagrant VM, each as its own
systemd-managed QEMU instance.

## How it fits

The `zephyr` Vagrant VM (see the project `Vagrantfile`) builds each config
under QEMU and runs each as a `zephyr-qemu-<id>.service` systemd unit. The
four x86 guests each get their own TAP (host side `192.0.2.{2,6,14,30}`,
Zephyr side `192.0.2.{1,5,13,29}`) and otto reaches their telnet shell at
`192.0.2.<n>:23`. The three ARM guests bridge their console to a telnet port
on the hop itself (`2323`/`2324`/`2325`). Either way otto SSH-hops through the
Ubuntu `zephyr` VM at `10.10.200.14`, which it knows as the host `test4`.

## Building (manual, one config at a time)

Inside the `zephyr` VM:

```bash
source ~/zephyr-venv/bin/activate
cd ~/zephyrproject && source zephyr/zephyr-env.sh

# Pick a config:
CFG=v3_7_fat_ram   # or v3_7_lfs, v2_7_fat_ram, v4_4_lfs

# Build it. Only the x86 configs ship a DT overlay; the recipe below is the
# x86 one. The ARM guests build for `mps2_an385` with `ext_svc` registered
# instead of `snmp_agent` — see the Vagrantfile's ARM_INSTANCES table.
DT_FLAG=""
if [ -f /vagrant/tests/firmware/zephyr/configs/$CFG/app.overlay ]; then
    DT_FLAG="-DEXTRA_DTC_OVERLAY_FILE=/vagrant/tests/firmware/zephyr/configs/$CFG/app.overlay"
fi
west build -p auto -b qemu_x86 zephyr/samples/subsys/shell/shell_module \
    -d ~/build/$CFG \
    -- -DEXTRA_CONF_FILE="/vagrant/tests/firmware/zephyr/common/otto-overlay.conf;/vagrant/tests/firmware/zephyr/configs/$CFG/overlay.conf" \
       $DT_FLAG
```

`vagrant provision zephyr` does this for all seven guests automatically;
rebuild manually after editing overlays. Each config's build dir is
independent, so rebuilding one does not touch the others.

## Filesystem mount notes

- **`v3_7_fat_ram`**: a fresh RAM disk is unformatted; `CONFIG_*_MKFS`
  formats it on first mount. Mount once per boot before transferring with
  `fs mount fat /RAM:`, then `fs read` / `fs write` (and otto's `get` /
  `put`) operate under `/RAM:`.

- **`v3_7_lfs`**: the `zephyr,fstab` node in `app.overlay` has `automount;` so
  the FS is mounted at `/lfs` by the time the shell is up. No manual mount.

- **`v3_7_no_fs_arm`**: no `fs` shell command exists at all. otto's console
  backend detects this and returns a clear error rather than hanging.

## Build verification status (as of 2026-05-23)

The Kconfig and devicetree symbols used in `configs/v3_7_lfs/` and the
`zephyr,ram-disk` binding in `configs/v3_7_fat_ram/app.overlay` are the
expected set for Zephyr 3.7 LTS but **have not been build-verified end to
end**. If `west build` complains about an unknown symbol or binding, the fix
is local to the affected `overlay.conf` / `app.overlay` — none of the other
configs are affected.

## Known guest defects (kept on purpose)

The 3.7 guests carry two Zephyr-side bugs that otto users with real 3.7
hardware can hit. They are **not** worked around on the bed: a flaky host is
the cheapest way to keep otto honest about the shapes it must survive. Read
from `subsys/shell/backends/shell_telnet.c` at `799e7a82` and verified by
packet capture on the hop, 2026-09-13.

- **A connection closing mid-send can kill the telnet backend for the guest's
  life.** If any close (FIN *or* RST) lands while `shell_telnet` still has
  queued output, its next `telnet_send` fails — `net_tcp_queue` returns
  `-ENOTCONN` for a socket no longer in `TCP_ESTABLISHED` — logging `Failed to
  send N, shutting down` and closing the client fd. A `net_socket_service`
  POLLERR for that same fd, already queued, then reads `SO_ERROR` on a closed
  fd (`Telnet socket N error (0)`), no longer matches the client slot, and
  drops into `telnet_restart_server()`. The rebind fails `EADDRINUSE (-112)` —
  the just-closed contexts still hold port 23 and the code sets no
  `SO_REUSEADDR` — and the backend is unregistered:
  `Telnet fatal error, failed to restart server`. When the rebind happens to
  succeed you get the other face: `Telnet shell backend initialized` followed
  by `Telnet client already connected` forever (#260). Either way the only
  recovery is `make qemu-restart` (or `systemctl restart
  zephyr-qemu-<id>.service` on the hop for one guest). The guest itself stays
  up: ICMP, SNMP `sysUpTime`, and the systemd unit all answer — only the
  console is gone. otto's part of this — releasing a console connection while
  the guest is still sending — is tracked as a tunnel-safe teardown in #324;
  the restart-path defects below are Zephyr's.

- **3.7's RST-to-SYN is malformed, so a dead console reads as a hang, not a
  refusal.** With no listener, the guest answers a SYN to port 23 with an RST
  whose ack equals the SYN's own ISS instead of ISS+1. A Linux client in
  SYN-SENT must drop that segment (RFC 793), so `connect()` retransmits until
  its timeout. The healthy 3.7 guests do the same on any closed port (`nc -z
  192.0.2.5 2323` times out); 4.4 refuses correctly. Consequence for every
  console probe: **silence is not evidence** — a passive `recv()` on a healthy
  3.7 guest also looks like silence, because 3.7 sends no unsolicited banner.
  Assert a real `~$` after writing CRLF, before and after anything that churns
  the console; and count journal signatures scoped to the unit invocation
  (`journalctl -u <unit> _SYSTEMD_INVOCATION_ID=<id>`), never `-b`, which
  counts every restart's `backend initialized`.

Diagnostic recipe for "is it dead or just held?": on the hop, `sudo tcpdump -S
-ni zeth-<id> 'tcp port 23'` around one `nc -z 192.0.2.<n> 23`. `[S.]` back
means alive and held or serving; `[R.]` with `ack == the SYN's seq` means the
backend is gone (restart it); nothing back at all means the guest's network
stack is down.
