# BusyBox hosts

A BusyBox guest is a Unix host whose userland answers most of the probe
questions differently: `ash` instead of `bash`, `su` instead of `sudo`, a
`base64` that may or may not exist. The bed runs five versions, 1.16.1 to
1.35.0, each a QEMU guest behind `test1`. The oldest:

```{literalinclude} ../../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "bb1161"'
:end-before: '"_doc_end": "bb1161"'
```

Two things to notice. `hop: "test1"` — otto reaches the guest through the
VM that hosts it. And the `userland_options` block is a **pin** — every
value declared, none left for the probe. That is what a probe on a pinned
host reports — here the newest guest, `bb1350_qemu` (the element name plus
the board otto appends):

```{literalinclude} ../../examples/getting-started/captures/probe-bb1350.txt
:language: text
```

Every source is `declared`: the entry told otto, otto asked nothing. On a new
BusyBox build, leave the block out, probe, paste.

Per-version differences the pins record — `base64_flag` is `absent` on 1.16.1
and `-d` from 1.21.1; `timeout_style` changes at 1.31.0 — are why a
per-profile set of defaults is worth having. Today the defaults are one set
for every Unix host and the pin carries the difference; a review of those
defaults per profile is a follow-up to this section, and
{doc}`../customizations` shows what to do in the meantime when a default
metric command does not exist on a guest.
