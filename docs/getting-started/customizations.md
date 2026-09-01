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

otto runs no shell command of its own after a session opens, and there is no
hook to add one; the SSH transport has a `post_connect` hook for connection-
level setup such as port forwards ({doc}`../library/connection-options`),
which is a different thing. The usual reason to want one — setting a custom
prompt so the tool can find the end of each command — does not apply here:
every command frame brackets commands with its own markers and never matches
prompt text, which is what the Zephyr page showed. A shell-level post-connect
hook is a follow-up to this section.

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
