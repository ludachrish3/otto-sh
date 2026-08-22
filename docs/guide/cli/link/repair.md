# otto link repair

```bash
otto --lab veggies link repair edge
otto --lab veggies link repair --all
```

`repair <link>` clears **every** currently-impaired placement of that link
unconditionally (no merge — a placement with anything applied gets a
`tc qdisc del`) and cancels any live `--expire` timer for it, whether or not
that placement actually had an impairment to clear. This bare form clears a
whole-link impairment OR an entire port-scoped tree, whichever the placement
carries. Adding `--port N [--proto P]` narrows `repair` to one selector
instead of the whole placement — see
[Repairing one selector](#repairing-one-selector).

`repair --all` walks every static link in the lab and never raises: a link
that structurally can't be impaired (no named endpoint interface, the
mgmt-interface refusal, the local-host refusal — see [Safety](safety.md#safety-rules)) is
silently skipped, since it was never impairable in the first place. A link
whose repair fails for a *live* reason (host unreachable, command failed) is
collected as a named failure instead of aborting the rest; if any failures
occurred, the command reports them and **exits non-zero** — a script
checking the exit code learns the sweep was incomplete rather than being
told it fully succeeded.

## Repairing one selector

```bash
otto --lab veggies link repair edge --port 5201 --proto tcp
otto --lab veggies link repair edge --port 5201
otto --lab veggies link repair edge
```

`repair <link> --port N [--proto P]` clears just that one selector —
deleting the whole classful tree if it was the last selector standing — and
cancels only its own timer, leaving every other selector on the placement
untouched. A bare `repair` (no `--port`) still clears **everything**, as
described above. `--port` and `--all` don't compose: `--port` repairs one selector on
one link, `--all` sweeps every static link, and passing both is a usage
error.

