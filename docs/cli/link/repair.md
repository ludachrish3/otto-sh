# otto link repair

```bash
otto --lab unix link repair edge
otto --lab unix link repair --all
```

`repair <link>` clears **every** currently-impaired placement of that link
unconditionally (no merge — a placement with anything applied gets a
`tc qdisc del`) and cancels any live `--expire` timer for it, whether or not
that placement actually had an impairment to clear. This bare form clears a
whole-link impairment OR an entire port-scoped tree, whichever the placement
carries. Adding `--port N|START:END [--proto P] [--side S]` narrows `repair` to one
selector instead of the whole placement — see
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
aborting:

```text
partially repaired bb1350-wire: cleared test1/bbeth-1350, timers cancelled 0
  could not reach bb1350-qemu/eth0: link references host 'bb1350-qemu' not in the loaded lab
```

Asked for one link by name, the headline reads `partially repaired`, not a
green `repaired`, and the command **exits 1**: you asked about both ends, and
otto could look at only one.

A `--all` sweep is different. It clears every end the loaded lab contains and
lists a link whose other end is on a host outside that lab under its own
heading, without failing the sweep:

```text
repaired 0 link(s)
outside this lab scope 1 link(s):
  bb1350-wire: cleared 1 placement(s); bb1350-qemu/eth0 is outside the loaded lab
```

Here the loaded lab contains `test1` but not `bb1350-qemu`, the host at the
wire's other end, so that end is not a failure of the sweep. `otto run cleanup`
reports the same link as declined when it cleared something on the loaded
end, and says nothing of it when it cleared nothing: the link was never
impaired from this scope.

## Repairing one selector

```bash
otto --lab unix link repair edge --port 5201 --proto tcp
otto --lab unix link repair edge --port 5201
otto --lab unix link repair edge --port 5000:5010 --side dst
otto --lab unix link repair edge
```

`repair <link> --port N|START:END [--proto P] [--side S]` clears just that
one selector — deleting the whole classful tree if it was the last selector
standing — and cancels only its own timer, leaving every other selector on
the placement untouched. A bare `repair` (no `--port`) still clears
**everything**, as described above. `--port` and `--all` don't compose:
`--port` repairs one selector on one link, `--all` sweeps every static link,
and passing both is a usage error.

The flags must name the selector exactly as it was impaired — `5000:5000`
and `5000` are the same selector, but a narrower `--port` never carves a
piece out of a wider one, so `--port 5205` cannot partially clear a
`5200:5220` selector. See
[Overlapping selectors: nest or collide](port-scoped.md#overlapping-selectors-nest-or-collide)
for how selectors compose and how impairing gives `--port` its target.

