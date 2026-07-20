# otto Zephyr test-bed configuration

Stock Zephyr shell sample (`samples/subsys/shell/shell_module`) built for
`qemu_x86`, with otto's Kconfig + devicetree overlays layered on. Used to
validate otto's `EmbeddedHost` support against real RTOS targets.

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
patches/
    <zver>-*.patch          # per-version Zephyr source patches (see its README)
configs/
    v3_7_fat_ram/           # FAT on a RAM disk (the default for transfer tests)
        overlay.conf
        app.overlay
    v3_7_lfs/               # LittleFS on the flash simulator
        overlay.conf
        app.overlay
    v3_7_no_fs/             # no filesystem at all — graceful-degradation target
        overlay.conf
        (no app.overlay needed)
```

`common/otto-overlay.conf` is the shared Kconfig overlay applied to every
config. Per-filesystem deltas live in `configs/<id>/overlay.conf` (FS Kconfig,
per-instance IP) and `configs/<id>/app.overlay` (devicetree, when needed).
Builds layer the two via Zephyr's `-DEXTRA_CONF_FILE="a;b"` semicolon list.

## The configs and what they exercise

| id             | IP          | /30 subnet     | Filesystem                      | Why it exists                                                                                       |
|----------------|-------------|----------------|---------------------------------|-----------------------------------------------------------------------------------------------------|
| `v3_7_fat_ram` | `192.0.2.1` | `192.0.2.0/30` | FAT on a 128 KiB RAM disk       | Default target for `EmbeddedFileTransfer`'s console `put`/`get` round-trip                          |
| `v3_7_lfs`     | `192.0.2.5` | `192.0.2.4/30` | LittleFS on the flash simulator | Proves the console backend is FS-agnostic; LittleFS has different mount cost / on-disk semantics    |
| `v3_7_no_fs`   | `192.0.2.9` | `192.0.2.8/30` | (none — no `fs` shell)          | Exercises `EmbeddedFileTransfer`'s graceful-degradation path against a real target, not just a mock |

Each instance is on its own /30 so the host VM's routing table holds a
distinct route per TAP. A shared `192.0.2.0/24` produced overlapping routes
and the kernel picked one, making the other two instances unreachable from
the host.

All three live under `qemu_x86` in the same `zephyr` Vagrant VM, each as its
own systemd-managed QEMU instance on its own `zeth-<id>` TAP. Memory ≈ 3 ×
32 MB inside the 4 GB zephyr VM.

## How it fits

The `zephyr` Vagrant VM (see the project `Vagrantfile`) builds each config
under QEMU, attaches each to its own TAP (host side `192.0.2.{2,4,6}`,
Zephyr side `192.0.2.{1,3,5}`), and runs each as a `zephyr-qemu-<id>.service`
systemd unit. otto reaches each telnet shell at `192.0.2.<n>:23` by
SSH-hopping through the Ubuntu `zephyr` VM (`10.10.200.14`).

## Building (manual, one config at a time)

Inside the `zephyr` VM:

```bash
source ~/zephyr-venv/bin/activate
cd ~/zephyrproject && source zephyr/zephyr-env.sh

# Pick a config:
CFG=v3_7_fat_ram   # or v3_7_lfs, v3_7_no_fs

# Build it. `no_fs` is the one config that does not need a DT overlay.
DT_FLAG=""
if [ -f /vagrant/tests/firmware/zephyr/configs/$CFG/app.overlay ]; then
    DT_FLAG="-DEXTRA_DTC_OVERLAY_FILE=/vagrant/tests/firmware/zephyr/configs/$CFG/app.overlay"
fi
west build -p auto -b qemu_x86 zephyr/samples/subsys/shell/shell_module \
    -d ~/build/$CFG \
    -- -DEXTRA_CONF_FILE="/vagrant/tests/firmware/zephyr/common/otto-overlay.conf;/vagrant/tests/firmware/zephyr/configs/$CFG/overlay.conf" \
       $DT_FLAG
```

`vagrant provision zephyr` does this for all three configs automatically;
rebuild manually after editing overlays. Each config's build dir is
independent, so rebuilding one does not touch the others.

## Filesystem mount notes

- **`v3_7_fat_ram`**: a fresh RAM disk is unformatted; `CONFIG_*_MKFS`
  formats it on first mount. Mount once per boot before transferring with
  `fs mount fat /RAM:`, then `fs read` / `fs write` (and otto's `get` /
  `put`) operate under `/RAM:`.

- **`v3_7_lfs`**: the `zephyr,fstab` node in `app.overlay` has `automount;` so
  the FS is mounted at `/lfs` by the time the shell is up. No manual mount.

- **`v3_7_no_fs`**: no `fs` shell command exists at all. otto's console
  backend detects this and returns a clear error rather than hanging.

## Build verification status (as of 2026-05-23)

The Kconfig and devicetree symbols used in `configs/v3_7_lfs/` and the
`zephyr,ram-disk` binding in `configs/v3_7_fat_ram/app.overlay` are the
expected set for Zephyr 3.7 LTS but **have not been build-verified end to
end**. If `west build` complains about an unknown symbol or binding, the fix
is local to the affected `overlay.conf` / `app.overlay` — none of the other
configs are affected.
