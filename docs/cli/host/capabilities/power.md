# Power and reboot

Full signatures: {class}`~otto.host.host.BaseHost`.

## Power control

Power is driven from outside the host, by a pluggable `PowerController`. The
built-in `command` controller runs commands on a *controller* host:

```json
{
    "power_control": {
        "type": "command",
        "controller": "hypervisor1",
        "on_cmd": "virsh start {name}",
        "off_cmd": "virsh destroy {name}",
        "status_cmd": "virsh domstate {name}",
        "status_on": "running"
    }
}
```

Then:

    await host.power("on")     # or "off"
    await host.power()         # toggle (needs status_cmd)

Projects register richer controllers (IPMI/redfish/libvirt/PDU) with
`register_power_controller`, as a config model plus a factory; see
{ref}`power-controllers` for the how-to.

With no controller configured,
{meth}`~otto.host.host.BaseHost.power` and `reboot(hard=True)` raise.

## Reboot & shutdown

    await host.reboot()                       # soft: in-shell reboot (UnixHost: sudo reboot)
    await host.reboot(wait=True)              # soft reboot, then block until back up (10-min default)
    await host.reboot(hard=True)              # power-cycle via the controller
    await host.reboot(hard=True, wait=True)               # hard reboot, block until back up (10-min default; returns Failed on timeout)
    await host.reboot(hard=True, wait=True, timeout=300)  # ...or override the wait timeout (seconds)
    await host.shutdown()                     # in-shell power-off

{class}`~otto.host.local_host.LocalHost` {meth}`~otto.host.host.BaseHost.reboot`
and {meth}`~otto.host.host.BaseHost.shutdown` raise (never reboot the test runner).
`DockerContainerHost` also inherits the base raising `reboot` (soft path) and
`shutdown` with no override — both raise `NotImplementedError` at runtime.
`EmbeddedHost` overrides the soft-reboot path (`kernel reboot cold`) but inherits
the base `shutdown`, so `shutdown` raises on embedded hosts too.

## Reachability

    if await host.is_reachable(): ...
    await host.wait_until_up(120)     # after a reboot/power-on  (timeout is required)
    await host.wait_until_down(60)    # after a shutdown          (timeout is required)

The down phase never trusts a cached connection: each poll of
`wait_until_down` calls `is_reachable(fresh=True)`, which dials through a
throwaway connection and closes it, so a login that succeeded early in a slow
shutdown cannot keep the host reading as up.

## `reboot` options

```text
otto host <HOST_ID> reboot [--hard] [--wait] [--timeout SECS] [--down-timeout SECS]
                           [--poll-interval SECS]
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--hard / --no-hard` | `--no-hard` | Power-cycle via the power controller instead of an in-shell reboot |
| `--wait / --no-wait` | `--no-wait` | Block until the host is reachable again after reboot |
| `--timeout SECS` | the host's `reboot_options` | Whole-wait bound in seconds when `--wait` is set |
| `--down-timeout SECS` | the host's `reboot_options` | Down-phase bound in seconds; `0` skips the down phase |
| `--poll-interval SECS` | the host's `reboot_options` | Seconds between reachability probes |

The three bounds default to the host's `reboot_options`, layered from the lab
data and `[host_preferences]` ({ref}`reboot-options-reference` has the defaults); a
flag given here is the final word and is held to the same bounds as lab data.
