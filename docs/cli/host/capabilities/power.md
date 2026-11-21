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
`wait_until_down` dials through a throwaway connection and closes it, as
`is_reachable(fresh=True)` does, so a login that succeeded early in a slow
shutdown cannot keep the host reading as up.

A poll that fails counts as down only when it shows the host is gone. On the
`ssh` and `telnet` terms these failed polls do not count as down, so the down
phase keeps polling:

- the host answered (its ssh banner arrived, or its telnet server sent
  something), however the poll then failed: the host is still running;
- an ssh poll's connection to the host was open when the poll ran out of
  time: a TCP connection, or the jump host's channel to it (a jump confirms
  the channel only once it has reached the host). With a `proxy_command` the
  pipe to the proxy opens before the proxy reaches anything, so there only
  the banner counts, as it does for telnet: through a jump, telnet's local
  forward accepts before its channel to the host exists;
- the poll failed at the jump host, before it reached for the host: the jump's
  connect or login failed or ran out of time, so the poll learned nothing
  about the host.

A host that is slow while it shuts down therefore keeps the down phase polling
until it really goes down, or until `--down-timeout` ends it with `never went
down`. When every poll of the down phase failed at the jump host, the reboot
fails with ``could not be probed within <N>s of the reboot: every poll failed
at its jump host '<hop>' (or a hop before it) — the reboot outcome is
unknown`` instead, naming the host's `hop`. A refused connection, an
unreachable host, a connect (or the jump's channel to the host) that is
refused or times out, and a connection closed before the ssh banner all count
as down. On a console, and on a telnet line marked `single_client_console`
(every embedded target's is), the server answers whatever the device behind
it is doing, so a console counts every failed poll as down, and such a telnet
line every one except a poll that failed at the jump, which it spares as any
telnet line does (the console rules are in
{doc}`../../../configuration/host-options`).

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
