# otto Zephyr test-bed configuration

A stock Zephyr shell sample (`samples/subsys/shell/shell_module`) built for
`qemu_x86`, with `otto-overlay.conf` layered on as an `EXTRA_CONF_FILE`. Used
to validate otto's `EmbeddedHost` support against a real RTOS target.

**There is no otto-authored firmware here** — not even an empty `main.c`. This
directory ships only two configuration files:

- `otto-overlay.conf` — a Kconfig overlay enabling the standard Zephyr options
  otto needs: the telnet shell backend, networking with a static IPv4,
  per-thread runtime stats, and a FAT filesystem with the `fs` shell.
- `app.overlay` — a devicetree overlay adding a RAM-backed disk for that
  filesystem to live on (`qemu_x86` ships none).

The shape of the dependency is the same as `sshd_config` on a Unix host:
configuration that has to be in place for otto to talk to the target, not code
we inject into it. Any sentinel/marker behavior otto relies on comes from what
the stock Zephyr shell already does.

## How it fits

The `zephyr` Vagrant VM (see the project `Vagrantfile`) builds the stock
sample under QEMU, attaches it to a TAP interface `zeth` (host side
`192.0.2.2`, Zephyr side `192.0.2.1`), and runs it as the `zephyr-qemu`
systemd service. otto reaches the telnet shell at `192.0.2.1:23` by
SSH-hopping through the Ubuntu zephyr VM.

## Building (manual, for the spike)

Inside the `zephyr` VM:

```bash
source ~/zephyr-venv/bin/activate
cd ~/zephyrproject && source zephyr/zephyr-env.sh
west build -p auto -b qemu_x86 zephyr/samples/subsys/shell/shell_module \
    -d ~/build/test_app \
    -- -DEXTRA_CONF_FILE=/vagrant/tests/firmware/zephyr/otto-overlay.conf \
       -DEXTRA_DTC_OVERLAY_FILE=/vagrant/tests/firmware/zephyr/app.overlay
```

The provisioning step does this once on first `vagrant up`; rebuild manually
after editing `otto-overlay.conf` or `app.overlay`.

## Phase 5 — console file transfer

otto's `console` transfer backend drives the stock `fs` shell. The Kconfig +
devicetree overlays enable a FAT filesystem on a 128 KiB RAM disk for it to
target.

> **Build-verify needed.** The `fs`/FAT/RAM-disk Kconfig symbols and the
> `zephyr,ram-disk` devicetree binding are the *expected* form for the pinned
> Zephyr 3.7 LTS but have not been confirmed against a real `west build`.
> Adjust if the build reports unknown symbols or binding errors.

The RAM disk is not persistent and is not pre-formatted: `CONFIG_*_MKFS`
formats it on first mount. Mount it once per boot before transferring —

```bash
fs mount fat /RAM:
```

— then `fs read` / `fs write` (and otto's `get` / `put`) operate under `/RAM:`.

## Phase 1 spike

With the service running, from the Ubuntu side:

```bash
telnet 192.0.2.1 23
```

Confirm: (1) an unknown command (e.g. `__OTTO_test_BEGIN__`) produces a line
echoing the token; (2) input echo behavior; (3) `kernel threads` /
`kernel stacks` output formats (Phase 6 parsers).
