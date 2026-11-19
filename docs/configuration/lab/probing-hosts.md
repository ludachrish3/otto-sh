(probing-hosts)=

# Probe and pin every Unix host

Run `otto --lab <lab> host <id> probe` once for every Unix host, BusyBox
included, and paste what it prints into the host's entry. Probe once the host
answers its first command.

## Why

otto adapts to a Unix host's userland by asking the device: which elevation
mechanism it has, which `timeout` convention its applet speaks, whether
`base64` is there. It asks in one probe round, the first time a host object
needs an answer, which is normally once per otto command. The answers are
cached in memory for that object's lifetime, and nothing persists between
commands, so the next command that needs one asks the device again. On a
BusyBox device that round is slow. Capabilities that got no answer may be
asked again after a cooldown.

sudo is measured separately. An unset `sudo_form` is measured once per
login. After a refused password, every later sudo `exec` tries again, and
those tries count against the account's lockout.

A declared value is never measured again, not even by `probe`. Pinning stops
the userland probes for the values you pin, and only those. Pinning does not
turn off other discovery, such as `$HOME` discovery for staging or terminal
calibration when a session opens. Ordinary commands never repeat the protocol
survey; only `probe` runs it.

## The step

Once a Unix host answers its first command, probe it:

```bash
otto --lab <lab> host <id> probe
```

It prints every capability with its value and its source, then the settled
answers as a pin to paste, then its {doc}`protocol survey
<../../cli/host/capabilities/protocols>`, which ends with a menu or port pin
of its own when the survey disagrees with the lab record. The userland pin
looks like this:

```json
"userland_options": {
  "elevation": "sudo",
  "sudo_form": "shell",
  "timeout_style": "coreutils",
  "applet_base64": "present"
}
```

Paste the `userland_options` fragment into the host's entry (see
[Where the pins go](#where-the-pins-go)), then probe again: the pasted values
now read `declared`. The survey's menu lines print as `valid_terms = [...]`
and `valid_transfers = [...]`; in a JSON lab file, write each as a JSON
member, such as `"valid_terms": ["ssh"]`. {doc}`../../cli/host/capabilities/protocols`
shows the survey's whole pin and when it prints one.

Only settled answers appear in the pin, so a partial pin is normal. Leave
`assumed` or `unmeasured` values unset, and never fill them in by hand; a
later probe may measure them.

## Where the pins go

- **A host in a lab file:** its host entry.
- **A host from a JSON or NetBox inventory:** the lab file's host entry, beside
  its `inventory` key, never the inventory record
  ({doc}`inventory-references`).
- **A host from a custom host source:** the host record that source emits. A
  later json source that restates the element replaces it whole, so it cannot
  add only a pin ({doc}`../host-sources`).

Product `[host_preferences]` can override option tables, `userland_options`
among them ({doc}`../host-options`).

## When to probe again

After a software or image change on the host, remove the affected
declarations from its `userland_options` first, because a declared value is
never measured again. Then probe and paste again.

## Hosts that need no userland pin

Embedded, Docker-container and `local` hosts have no userland resolver, so
they need no userland pin. `probe` still runs its protocol survey on them.

## Detail

- {doc}`../../cli/host/capabilities/userland`: every capability, its sources,
  and how `sudo_form` is measured.
- {doc}`../../cli/host/capabilities/protocols`: the survey's tiers, states and
  pin.
