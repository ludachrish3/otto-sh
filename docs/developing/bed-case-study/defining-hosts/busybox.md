# BusyBox hosts

A BusyBox guest is a Unix host whose userland answers most of the probe
questions differently: `ash` instead of `bash`, `su` instead of `sudo`, a
`base64` that may or may not exist. The bed runs five versions, 1.16.1 to
1.35.0, each a QEMU guest behind `test1`. Like the Ubuntu VMs on the
{doc}`first page <index>`, they share one shape, so one function in the lab
file writes them:

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin busybox"
:end-before: "// doc: end busybox"
```

`busyboxPin` is the probe's answers on a 1.21.1 guest, written down: a
**pin**, every value declared, none left for the probe. `busybox()` writes
the pin it is given as the host's `userland_options`, and writes none when
it is given none. The oldest guest pins `busyboxPin` with two answers
changed, merged over it the same way `racked(3) { ... }` is:

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin bb1161"
:end-before: "// doc: end bb1161"
```

Notice `hop: 'test1'` in the function: otto reaches the guest through the
VM that hosts it.

The newest guest, `bb1350`, differs from the others in more than its pin.
It also takes SSH and a serial console that `test1` serves, and its `eth0`
is wired to `test1`'s `bbeth-1350` interface (the document's `links` join
the two):

```{literalinclude} ../../../examples/getting-started/lab_data/lab.jsonnet
:language: jsonnet
:start-after: "// doc: begin bb1350"
:end-before: "// doc: end bb1350"
```

On a pinned host the probe asks nothing. Here it is on `bb1350-qemu` (the
element name plus the board otto appends):

```{literalinclude} ../../../examples/captures/probe-bb1350.txt
:language: text
```

Every source is `declared`: the entry told otto, otto asked nothing. A new
BusyBox build is one more `busybox(...)` call in the `elements` list of
{ref}`the lab document <bed-lab-document>` at the end of the file. Leave the
pin out, as in `busybox(5, '1.36.1')`, so otto probes every capability; then
paste the pin the probe prints as its `pin=` argument. That guest's host id
is `bb1361-qemu`, not `bb1361`: `otto -l busybox lab explain bb1361-qemu`.
`otto -l busybox --list-hosts` and `otto -l busybox --show-lab` show the new
host; `otto --list-labs` lists the labs only, not their hosts.

The pins also record per-version differences. 1.16.1 has no `base64`
applet, so its `applet_base64` is `absent` and its `base64_flag` is `absent`
where later guests have `-d`; `timeout_style` changes to `coreutils` at
1.31.0. Each guest's `pin=` argument writes exactly those differences over
`busyboxPin`. The defaults are one set for every Unix host, and the pin
carries the difference;
{doc}`../customizations` shows what to do when a default metric command does
not exist on a guest.
