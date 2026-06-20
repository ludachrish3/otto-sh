# Power, reboot & reachability

## Power control

Power can't run on an off host, so otto models the actor as a pluggable
`PowerController`. The built-in `command` controller runs commands on a
*controller* host:

    # lab data (per host)
    [power]
    type       = "command"
    controller = "hypervisor1"          # host id that runs the commands
    on_cmd     = "virsh start {name}"
    off_cmd    = "virsh destroy {name}"
    status_cmd = "virsh domstate {name}"
    status_on  = "running"

Then:

    await host.power("on")     # or "off"
    await host.power()         # toggle (needs status_cmd)

Projects register richer controllers (IPMI/redfish/libvirt/PDU) via
`register_power_controller`. With no controller, `power()` and
`reboot(hard=True)` raise.

## Reboot & shutdown

    await host.reboot()                 # soft: in-shell reboot (UnixHost: sudo reboot)
    await host.reboot(hard=True)         # power-cycle via the controller
    await host.reboot(hard=True, wait=True)               # block until back up (10-min default; returns Failed on timeout)
    await host.reboot(hard=True, wait=True, timeout=300)  # ...or override the wait timeout (seconds)
    await host.shutdown()                # in-shell power-off

`LocalHost.reboot()`/`shutdown()` raise (never reboot the test runner).

## Reachability

    if await host.is_reachable(): ...
    await host.wait_until_up(timeout=120)     # after a reboot/power-on
    await host.wait_until_down(timeout=60)    # after a shutdown
