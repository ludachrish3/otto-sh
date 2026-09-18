# Power and reboot

Full signatures: {class}`~otto.host.host.BaseHost`.

## Power control

Power can't run on an off host, so otto models the actor as a pluggable
`PowerController`. The built-in `command` controller runs commands on a
*controller* host:

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

Projects register richer controllers (IPMI/redfish/libvirt/PDU) via
`register_power_controller(type_name, cls)` — pass the type-name string and the
`PowerController` subclass:

    from otto.host.power import register_power_controller, PowerController

    class MyIpmiController(PowerController):
        type_name = "ipmi"
        ...

    register_power_controller("ipmi", MyIpmiController)

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

## `reboot` options

```text
otto host <HOST_ID> reboot [--hard] [--wait] [--timeout SECS]
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--hard / --no-hard` | `--no-hard` | Power-cycle via the power controller instead of an in-shell reboot |
| `--wait / --no-wait` | `--no-wait` | Block until the host is reachable again after reboot |
| `--timeout SECS` | `600.0` | Maximum seconds to wait when `--wait` is set |
