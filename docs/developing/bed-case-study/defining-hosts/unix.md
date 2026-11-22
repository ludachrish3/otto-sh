# Unix hosts

A Unix host needs an address and `os_type: "unix"` — the default — and
usually a credential. `creds` is optional, but by default leaving it out
only works for a host reached with no login step, such as a console that
lands at a prompt, and that term needs `login: false` too; see
[the `creds` field](../../../configuration/lab/host-fields.md#common-optional) for what
otto does with none. `valid_terms` and `valid_transfers` are menus otto
chooses from (`ssh` and `scp` first, by default); everything else otto works
out for itself on first connect.

```{important}
**Probe every Unix host, current Debian, Ubuntu and Fedora boxes and BusyBox
included, and paste the pin it prints into its entry:**
`otto --lab <lab> host <id> probe`. Unpinned, each otto command that needs the
host's userland probes it again.
{doc}`../../../configuration/lab/probing-hosts` shows what the pin looks like
and where it goes.
```

`hop` is the field this family uses most: the BusyBox guests on the next page
are reached through `test1`, and a Zephyr target through `test4`. otto opens
the jump session itself.

## A serial console behind a telnet server

Some Unix hosts are worth reaching over their serial console too: it
answers when the network does not, and it is the only way in before the
device's network is up. On the bed, `test2`'s serial port is served by a
telnet server on `test1`, port 4001, and its entry says so. It is one of the
Ubuntu VMs the {doc}`first page <index>`'s `ubuntuVm` function writes, so its
call states only what is its own: its rack position, its term and transfer
menus, the console, and the console's resource:

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin test2"
:end-before: "// doc: end test2"
```

`console_options` names the *server* by its lab ID and the port on it —
not `test2`'s own address. `console` comes last in `valid_terms`, so otto
still uses telnet by default; `--term console` picks the console for one
invocation:

```bash
otto --lab unix host --term console test2 login
```

A console serves one client and must be found at its `login:` prompt, so
it behaves differently from the other terms in ways worth reading before
you rely on it: {ref}`console-term` covers the options, the dial modes and
every error it can report, and {doc}`../../../cli/host/login` covers putting a
console that was left logged in back at `login:`.
