# Customizing hosts

Everything on the earlier pages is data in a lab file. Some things a host
needs are code: how to become a user, which metrics to chart, how to read a
metric the userland reports differently. Each lives in the project's `init`
module and is registered by name before any lab loads.

## Becoming another user: a login proxy

`test1`'s third credential is not a password. It names a **proxy** — a
registered async function that becomes the target account from the `via`
account:

```{literalinclude} ../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "test1"'
:end-before: '"_doc_end": "test1"'
```

```{literalinclude} ../examples/getting-started/libs/gs_example/proxies.py
:language: python
:start-after: "# doc: begin sudo-proxy"
:end-before: "# doc: end sudo-proxy"
```

A proxy is exercised wherever otto becomes another account: `otto host test1
login --user root` opens the interactive shell through it, and the
library's `host.as_user()` runs a block through it and unwinds afterwards.
(`otto host test1 run --sudo` is different — it prefixes each command with
the host's elevation and never changes the session's user.) The example
project carries the shortest script that proves the round trip:

```{literalinclude} ../examples/getting-started/as_root.py
:language: python
:start-after: "# doc: begin as-root"
:end-before: "# doc: end as-root"
```

```{literalinclude} ../examples/getting-started/captures/as-root-test1.txt
:language: text
```

The built-in `su` proxy is registered the same way —
{doc}`../library/extending-backends` shows it, the contract, and a
container-entering example.

## After connecting

otto runs two commands of its own on every shell it opens — the readiness
handshake and, on Unix hosts, history suppression — and then hands the shell
over. Anything a host needs beyond that is a **session setup**: project code,
registered by name like the proxy above, that runs once on every session
after the handshake and after every login-proxy hop, with a real session in
hand. `test1` declares one:

```{literalinclude} ../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "test1"'
:end-before: '"_doc_end": "test1"'
```

```{literalinclude} ../examples/getting-started/libs/gs_example/setup.py
:language: python
:start-after: "# doc: begin provision-app"
:end-before: "# doc: end provision-app"
```

```{literalinclude} ../examples/getting-started/libs/gs_example/__init__.py
:language: python
:start-after: "# doc: begin register-setup"
:end-before: "# doc: end register-setup"
```

The names come from `otto` (`register_session_setup`), from
`otto.host.command_frame` (`register_command_frame` and `FRAME_CLASSES`), and
from the project's own modules — so the block is the registration and
nothing else.

The hook exports a variable every session inherits, and on the default
session only — `ctx.kind` tells it which session it is on — does the
one-time work, here through the `python3` REPL as an `AppShell`. When it
returns, otto runs the shell's readiness handshake once more: that repeat is
the confirmation that the hook left a shell fit to use.

```{literalinclude} ../examples/getting-started/captures/session-setup-test1.txt
:language: text
```

### A different shell at the end

Sometimes the shell otto lands in is not the shell it should end up in: a
Linux login shell in front of a vendor CLI, or — the stand-in this page can
run on the bed — in front of a `python3` REPL. Then the entry declares two
dialects. `landing_frame` is the shell otto lands in; `command_frame` is the
application's dialect, a frame the project registers exactly as it registered
the Zephyr 2.7 frame earlier; and the hook is what gets from one to the other:

```{literalinclude} ../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "test1-py"'
:end-before: '"_doc_end": "test1-py"'
```

```{literalinclude} ../examples/getting-started/libs/gs_example/enter_python.py
:language: python
:start-after: "# doc: begin enter-python"
:end-before: "# doc: end enter-python"
```

```{literalinclude} ../examples/getting-started/libs/gs_example/pyrepl_frame.py
:language: python
:start-after: "# doc: begin pyrepl-frame"
:end-before: "# doc: end pyrepl-frame"
```

The hook's `run()` is bash-framed; `send`/`expect` navigate; and on return
otto enters the `pyrepl` frame — its handshake, in the REPL — and every
session from then on, `exec` included, speaks Python:

```{literalinclude} ../examples/getting-started/captures/session-setup-pyrepl.txt
:language: text
```

An `AppShell` is the other tool for a REPL, and the difference is where you
want to end up: an `AppShell` is a detour inside a bash session that hands
bash back on exit; a two-dialect host makes the application *the* shell.

### A landing that answers nothing

A console may land somewhere no dialect can confirm — a boot menu, an
autoboot countdown. `landing_frame: "raw"` sends no handshake at all; the
hook works with `send`/`expect` alone until the real shell answers, and frame
entry confirms it. The Zephyr guest below is reached exactly as before, by
telnet through the `test4` hop; only the landing changed:

```{literalinclude} ../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "zephyr37_lfs"'
:end-before: '"_doc_end": "zephyr37_lfs"'
```

```{literalinclude} ../examples/getting-started/libs/gs_example/land_on_zephyr.py
:language: python
:start-after: "# doc: begin land-on-zephyr"
:end-before: "# doc: end land-on-zephyr"
```

```{literalinclude} ../examples/getting-started/captures/session-setup-zephyr37-lfs.txt
:language: text
```

The inventory twin of the `unix` lab does not show the hook: it declares no
init module on purpose, and a hook is project code. The field reference is
in {ref}`per-host-session-setup`; the contract, and what a two-dialect host
can and cannot do, in {doc}`../library/extending-backends`.

## Adding a metric

A metric is a command and a parser, in one class. This one charts kernel
entropy on every host that has no parser set of its own:

```{literalinclude} ../examples/getting-started/libs/gs_example/monitor.py
:language: python
:start-after: "# doc: begin entropy-parser"
:end-before: "# doc: end entropy-parser"
```

## Changing a metric's command and parser

otto's built-in sockets metric runs `ss -s`, and BusyBox ships no `ss`
applet. A host without `ss` produces a shell error the parser cannot match,
so the series simply never appears — a missing command is not an error otto
recovers from (see *Parser health* in {doc}`../library/custom-parsers`). The
fix is the same shape as adding one — a parser with the same series names
and a command the guest does have — registered for those hosts only:

```{literalinclude} ../examples/getting-started/libs/gs_example/monitor.py
:language: python
:start-after: "# doc: begin busybox-sockets"
:end-before: "# doc: end busybox-sockets"
```

```{literalinclude} ../examples/getting-started/libs/gs_example/__init__.py
:language: python
:start-after: "# doc: begin register-parsers"
:end-before: "# doc: end register-parsers"
```

## Proving the stats land

A registration is only worth anything if the guest answers the command it
brings. This script builds a collector for one host — which resolves that
host's parser set exactly as the monitor does — polls it for a few ticks,
and prints one line per series collected. `otto test --monitor`
({doc}`../guide/cli/monitor/during-tests`) is the same collector driven by
the suite runner; this page drives the collector directly so the proof needs
no suite.

```{literalinclude} ../examples/getting-started/collect_metrics.py
:language: python
:start-after: "# doc: begin collect-metrics"
:end-before: "# doc: end collect-metrics"
```

```{literalinclude} ../examples/getting-started/captures/monitor-bb1350.txt
:language: text
```

`Entropy` is the added parser. `Established` and `Time-wait` are the
replacement's, under the built-in parser's own series names, so they land in
the same chart the default would have filled on a host that has `ss`. otto's
*default* sockets parser still runs `ss -s` on a guest without `ss`; a
per-profile set of defaults is a follow-up, and until it lands the
registration above is what a project does.

Three of the bed's Zephyr targets are monitored over SNMP rather than a
shell — an `snmp` block on the entry, otto's descriptors for the enterprise
OIDs its test firmware serves. This page does not yet capture that run; the
pointer is the whole of it, and {doc}`../guide/cli/monitor/metrics` covers it
under *SNMP monitoring*. {doc}`../library/custom-parsers` is the reference for
everything else on this page.
