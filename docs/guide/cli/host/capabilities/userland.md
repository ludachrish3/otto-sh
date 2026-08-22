(userland-capabilities)=

# Userland capabilities

otto adapts to the device's userland rather than assuming a GNU one: which
elevation mechanism exists, which `timeout` calling convention the applet
speaks, whether `base64` is there at all. Those answers are settled by a probe
round the first time a fresh host object needs one — cheap on a server, slow on
a BusyBox device — and they can be *pinned* in that host's `userland_options`
so the round never happens again.

`probe` is how you get the pin:

```text
otto host <id> probe
```

It resolves the capabilities and prints two things. First a reading of every
capability with its value **and its source**:

```text
capability       value      source
applet_base64    present    probed
applet_nc        present    probed
applet_scp       absent     probed
base64_flag      -d         probed
checksum         md5sum     probed
elevation        sudo       probed
shell_dialect    bash       probed
stat_size        stat       probed
timeout_style    coreutils  probed
```

The source is the actionable column. `declared` means the value is already
pinned in this host's `userland_options` and is never re-probed; `probed` means
the device answered, so it is worth pinning; `assumed` means otto could not ask
and the value is only what otto did before it asked anything.

Then the pasteable payload — the settled answers, under the key a `lab.json`
host entry carries:

```json
"userland_options": {
  "elevation": "sudo",
  "timeout_style": "coreutils",
  "applet_base64": "present"
}
```

Paste that into the host's entry and the next connection issues no probe at
all. See {doc}`../../../configuration/host-options` for where the table lives and how it layers.

**Assumed values are deliberately absent from the payload.** Inside a JSON
object a guess is indistinguishable from a measurement, and a pinned value is
never re-probed — so pinning one would make a momentary blip permanent. The
reading above the payload is where those values are visible, labelled for what
they are. A host that could answer nothing therefore prints an empty pin and
says why, rather than offering thirteen guesses.

`LocalHost` and `DockerContainerHost` build no capability resolver at all, so
`probe` on those reports that hole plainly instead of printing a pin. That is
recorded rather than accidental — see
{class}`~otto.host.userland.UserlandHost` for what giving them one would cost.
