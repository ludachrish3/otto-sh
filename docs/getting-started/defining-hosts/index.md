# Defining hosts

Defining hosts is the one thing otto cannot run without. The workflow is
short: write down what you know, let otto find out the rest, pin what it
found.

Before the first entry, know where the two things this section grows into
come from. Host *facts* — addresses, credentials, rack positions — can be
typed inline, as this page does, or referenced from an **inventory**: a JSON
file, NetBox, or a backend you write ({doc}`../../library/inventory-backends`).
And the lab can be gated by a **reservation scheduler** ({doc}`../reservations`)
— the JSON file otto ships, or a backend you write
({doc}`../../library/reservation-backends`).
Both seams are pluggable; the pages that follow use the shipped backends and
link to the contract when you outgrow them.

## What you write first

A project is a directory with `.otto/settings.toml`. The example's identity
and where its lab data lives:

```{literalinclude} ../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin identity"
:end-before: "# doc: end identity"
```

```{literalinclude} ../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin lab-sources"
:end-before: "# doc: end lab-sources"
```

And where the project's own code lives: `libs` puts it on the import path,
`init` names the module otto imports as it loads — here
`libs/gs_example/__init__.py`, where the later pages register a command
frame, parsers, a login proxy and a reservation backend:

```{literalinclude} ../../examples/getting-started/.otto/settings.toml
:language: toml
:start-after: "# doc: begin libs"
:end-before: "# doc: end libs"
```

The lab file declares the labs and the elements that join them
({doc}`../../guide/configuration/lab-config` is the field reference). The
three labs the bed declares:

```{literalinclude} ../../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "labs"'
:end-before: '"_doc_end": "labs"'
```

And the first element — a VM named `test1` that is a member of two labs.
Name, address, the accounts it has, where it sits, its interfaces: everything
you can write down before ever connecting.

```{literalinclude} ../../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "test1"'
:end-before: '"_doc_end": "test1"'
```

The third credential is not a password: it names a login *proxy*, code that
{doc}`../customizations` registers from the `init` module above. Leave that
line out until you reach that page — a lab that names an unregistered proxy
refuses to load.

`os_type` selects the host class ({doc}`../../guide/configuration/os-profiles`);
`hop`, absent here, names the host otto must reach this one through.

## What otto finds out

Everything a Unix host's userland does differently — which `stat` it has,
whether `base64` takes `-d` or `--decode`, how it elevates — is a
`userland_options` field, and every one of them defaults to *unset*, which
means "probe it at connect". `otto host <id> probe` runs those probes once
and prints the result as a pin; `--lab unix` names the lab to load, and a
host that belongs to several labs is reached through any one of them:

```{literalinclude} ../../examples/getting-started/captures/probe-test1.txt
:language: text
```

That first line is the example project's `[project]` table at work — it
selects the BusyBox guests, so the unix lab's fleet of interest is empty —
and an explicitly named host like `test1` is never scoped by it;
{doc}`../boards-of-interest` explains the table.

Paste the pin into the host's entry and the next connection skips every
probe. Every `userland_options` field is optional on purpose: the seven
`applet_*` fields answer `"present"` or `"absent"`, the rest name a tool or
a flag (`elevation: sudo`, `base64_flag: -d`, …), and any of them may be
left unset — the third answer a two-valued field cannot give, and what lets
a fresh entry work before anyone has pinned it. The per-field values are
listed at {ref}`userland-capabilities`.

## The pages that follow

```{toctree}
:maxdepth: 1

inventory
unix
busybox
zephyr
```
