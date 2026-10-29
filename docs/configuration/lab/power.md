(lab-power-control)=

# Power control

The optional `power_control` block configures a pluggable power controller for
the host.  The built-in `"command"` controller runs configured shell commands on
a *controller* host in the lab:

| Field | Type | Description |
|-------|------|-------------|
| `power_control` | object or string | Power controller spec.  A string selects a registered controller by type name, and its config model parses an empty table; an object takes the fields below.  Omit to leave the host without power control.  An unregistered name is refused as `type` is below. |
| `power_control.type` | string | Controller type name (e.g. `"command"`).  Selects the registered `PowerController` implementation; must name a registered controller. An unregistered name, or a table without `type`, is refused when the lab file is validated. |
| `power_control.controller` | string | Host id of the lab host that runs the on/off/status commands.  `null` or absent runs commands on the local otto machine. |
| `power_control.on_cmd` | string | Shell command to power the host on.  Templated with `{name}`, `{ip}`, `{id}`, each inserted exactly as it is. Any placeholder other than exactly `{name}`, `{ip}` or `{id}` is refused: a conversion (`{name!r}`), a format spec (`{id:>8}`), an attribute, an index, a positional `{}` and an unknown name such as `{host}` alike. Only the placeholders the template uses are checked, and each value may hold only letters, digits and `_.:@%+=,/-`, so it means the same word however many shells read it (`ssh hv virsh start {name}` included); a host whose name, address or ID used by the template holds any other character is refused when a power command is run, naming the placeholder and the character. |
| `power_control.off_cmd` | string | Shell command to power the host off.  Same template variables. |
| `power_control.status_cmd` | string | Shell command to query power state (optional).  Same template variables. |
| `power_control.status_on` | string | Substring of `status_cmd` output that means *on* (default `""`). |

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

The controller's config model parses the table, without its `type`, when the
host is validated — so `otto init`'s doctor, host listings and the load all
report a table its controller cannot parse, naming the host and never quoting
the value. A table with no `type`, or an unregistered one, is refused when the
lab file is validated. The
controller itself is built only when the host is; a controller that fails to
build fails that host's construction with
{class}`~otto.host.PowerConstructionError`, naming the stage and the host.

See {doc}`../../cli/host/capabilities/power` for the runtime API (`host.power()`,
`host.reboot(hard=True)`), and {ref}`power-controllers` for registering a
custom controller.
