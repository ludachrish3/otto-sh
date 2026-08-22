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
that structurally can't be impaired (neither endpoint names an interface, the
local-host refusal, a foreign qdisc — see [Safety](safety.md#safety-rules)) is
skipped and named, since it was never impairable in the first place. The
self-lockout refusals are **not** in that list — they do not apply to a clear
at all (see [Clearing is not creating](safety.md#clearing-is-not-creating)). A link
whose repair fails for a *live* reason (host unreachable, command failed) is
collected as a named failure instead of aborting the rest; if any failures
occurred, the command reports them and **exits non-zero** — a script
checking the exit code learns the sweep was incomplete rather than being
told it fully succeeded.

## When one end is out of scope

`repair` resolves **both** directions of the link, so it can be asked to clear
a placement on a host the loaded lab does not contain. It clears the
placements it *can* resolve and names the ones it cannot, rather than
aborting — the end otto can reach must not be stranded by the end it cannot:

```text
partially repaired bb1350-wire: cleared carrot_seed/bbeth-1350, timers cancelled 0
  could not reach bb1350_qemu/eth0: link references host 'bb1350_qemu' not in the loaded lab
```

The headline reads `partially repaired`, not a green `repaired`, and the
command **exits 1**: "I did not look" is not a clean bill of health. A `--all`
sweep files the same link as a *failure* rather than a skip, for the same
reason — `skipped` means otto declined a link it never impaired, which is
reassurance this link has not earned.

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

