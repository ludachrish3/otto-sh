# otto Zephyr test-bed configuration

A stock Zephyr shell sample (`samples/subsys/shell/shell_module`) built for
`qemu_x86`, with `otto-overlay.conf` layered on as an `EXTRA_CONF_FILE`. Used
to validate otto's `EmbeddedHost` support against a real RTOS target.

**There is no otto-authored firmware here** — not even an empty `main.c`. The
only thing this directory ships is `otto-overlay.conf`, a Kconfig overlay
that enables the standard Zephyr options otto needs (telnet shell backend,
networking with a static IPv4, per-thread runtime stats, and later the `fs`
shell). The shape of the dependency is the same as `sshd_config` on a Unix
host: configuration that has to be in place for otto to talk to the target,
not code we inject into it. Any sentinel/marker behavior otto relies on
comes from what the stock Zephyr shell already does.

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
    -- -DEXTRA_CONF_FILE=/vagrant/tests/firmware/zephyr/otto-overlay.conf
```

The provisioning step does this once on first `vagrant up`; rebuild manually
after editing `otto-overlay.conf`.

## Phase 1 spike

With the service running, from the Ubuntu side:

```bash
telnet 192.0.2.1 23
```

Confirm: (1) an unknown command (e.g. `__OTTO_test_BEGIN__`) produces a line
echoing the token; (2) input echo behavior; (3) `kernel threads` /
`kernel stacks` output formats (Phase 6 parsers).
