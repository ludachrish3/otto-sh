# Unix hosts

A Unix host needs an address, at least one credential, and `os_type: "unix"`
— the default. `valid_terms` and `valid_transfers` are menus otto chooses from
(`ssh` and `scp` first, by default); everything else otto works out for itself
on first connect.

Is `probe` worth running on one? On a current Debian, Ubuntu or Fedora box,
no: the probe finds exactly the GNU answers otto's transfer and command
paths are built around, so it confirms rather than discovers — compare the
`test1` capture on {doc}`index`, where every row's source is `probed` and
what it found is the modern-GNU answer on every row. The command earns its
keep on the *other* Unix: an old release, a stripped image, a vendor build
with `busybox` behind `/bin/sh`. There, one probe replaces a connection that
fails halfway through a transfer for a reason nobody can see.

`hop` is the field this family uses most: the BusyBox guests on the next page
are reached through `test1`, and a Zephyr target through `test4`. otto opens
the jump session itself.

## A serial console behind a telnet server

Some Unix hosts are worth reaching over their serial console too: it
answers when the network does not, and it is the only way in before the
device's network is up. On the bed, `test2`'s serial port is served by a
telnet server on `test1`, port 4001, and its entry says so:

```{literalinclude} ../../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "test2"'
:end-before: '"_doc_end": "test2"'
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
every error it can report, and {doc}`../../cli/host/login` covers putting a
console that was left logged in back at `login:`.
