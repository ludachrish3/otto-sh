# Zephyr hosts

An embedded target speaks whatever its firmware's shell speaks, and otto has
to bracket every command so it can tell where output starts, where it ends,
and what the exit code was. That bracket is the **command frame**. The
built-in `zephyr` frame is right for Zephyr 3.x and later — three of the bed's
targets name `zephyr-serial` instead, the same framing with a different
handshake for a UART shell bridged through QEMU
({doc}`../../guide/cli/host/embedded` lists every built-in frame). This page
shows how to see one working, and how to define one when none of them fits.

## Connect with the default class and read the frame

The entry for a Zephyr 3.7 target on the bed — `os_type: "zephyr"`, reached
through `test4`, addressed over the console:

```{literalinclude} ../../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "zephyr37_fat"'
:end-before: '"_doc_end": "zephyr37_fat"'
```

Run one command at `--log-level DEBUG` and the log shows the frame: the
command wrapped in a BEGIN marker, a `retval`, and an END marker.

```{literalinclude} ../../examples/getting-started/captures/zephyr37-debug-run.txt
:language: text
```

The `TelnetSession@…: framed write` line is the one to read — the earlier
`LocalSession` one is otto reading the project's git state as it loads; in
the run captured here that read fails and otto moves on. (The capture's
first line names the host id, `zephyr37-fat` — the element name slugged; the
log lines below it say `@zephyr37_fat` — the element itself. Same target,
two spellings.)

If the target's shell has no `retval` builtin, or answers the frame with
something the stock frame does not expect, there is no exit code to read —
and the fix is a frame of your own. (The markers themselves being reported as
`command not found` is expected: that is how the frame finds the edges.)

## The oldest target needs its own frame

Zephyr 2.7 has no `retval`. otto's 2.7 firmware carries a one-line patch that
prints `retCode = <n>` after every command instead, and the frame reads that.
The entry names the frame:

```{literalinclude} ../../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "zephyr27_fat"'
:end-before: '"_doc_end": "zephyr27_fat"'
```

The frame is a subclass of the stock one that changes the handshake, the
framing, and how the exit code and output are read:

```{literalinclude} ../../examples/getting-started/libs/gs_example/zephyr_inline.py
:language: python
```

Registered from the project's `init` module — the one the {doc}`first page
<index>` wires up with `libs` and `init` — before any lab loads, so the name
in `lab.json` resolves:

```{literalinclude} ../../examples/getting-started/libs/gs_example/__init__.py
:language: python
:start-after: "# doc: begin register-frame"
:end-before: "# doc: end register-frame"
```

And the same command on the 2.7 target:

```{literalinclude} ../../examples/getting-started/captures/zephyr27-run.txt
:language: text
```

{doc}`../../guide/cli/host/embedded` lists the built-in frames and filesystems;
{doc}`../../guide/configuration/os-profiles` shows how a named profile bundles
`os_version`, `command_frame` and the rest so each entry declares only its
identity. Monitoring a Zephyr target over SNMP — three of the bed's targets do
— is on {doc}`../customizations`.
