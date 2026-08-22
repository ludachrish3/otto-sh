# otto link list

```bash
otto --lab veggies link list
```

Prints one line per static link:

```text
edge  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via -  a->b: delay 10ms  b->a: -
dataplane  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via pepper_seed  a->b: -  b->a: -
```

- **via** is the link's `impair` middlebox host id, or `-` for endpoint mode.
- Each direction's text is either a compact parameter summary (`delay 10ms
  loss 2%`) for a whole-link impairment, `port-scoped (N)` for N active
  selectors (each printed on its own indented row below — see
  [Listing: selector rows](#listing-selector-rows)), `foreign qdisc —
  not otto's` for a root qdisc otto did not generate, `-` for a clean
  (unimpaired) placement, or `?` when that placement's host couldn't be
  reached this pass — absence there means "unknown," not "clean."
- A link that can't be impaired shows `n/a` in both direction columns and
  states why on its own indented row:

  ```text
  dut--gw  gw@- <-> dut@-  via -  a->b: n/a  b->a: n/a
    not impairable: 'gw', 'dut' has no named interface
  ```

  Every *implicit* link lands here, so on a lab that declares no links of its
  own this is the entire table — a bare `n/a` explained none of it. The reason
  covers both the structural refusals (no named interface, the local host as
  an endpoint) and the live ones found during the scan (management interface,
  hop transit); see [Safety](safety.md#safety-rules).

  The structural half is also available on its own, as
  `otto.link.placement.impairment_refusal(link)` — no lab, no `await`, no live
  address fetch — because `find_link` resolving a link and `impair` being able
  to act on it are different questions. It takes the directions you mean, and
  they matter: a link between one interfaced host and one bare host is refused
  both ways but accepted for `--from` the interfaced end.

  :::{warning}
  Impairing such a half-interfaced link with `--from` currently strands it.
  `list` reports the link `n/a` in both columns (it asks about both
  directions, and one is refused), `repair --all` skips it for the same
  reason, and `repair <link>` refuses outright — so the impairment is live,
  invisible, and clearable only by hand with `tc`. Give both endpoints a named
  interface before impairing them.
  :::

If any link's state came back partial (at least one placement host was
unreachable), `list` still prints every row it *could* read, then adds a
trailing `partial scan — could not fully read: <ids>` warning rather than
silently dropping those links from the picture — the same
never-silently-wrong philosophy as `otto tunnel list`.

## Listing: selector rows

`otto link list` prints one indented line per active selector under its
link's normal summary row:

```text
edge  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via -  a->b: port-scoped (1)  b->a: -
  a->b  5201/tcp  delay 200ms
dataplane  carrot_seed@eth1.100 <-> tomato_seed@eth1.200  via pepper_seed  a->b: foreign qdisc — not otto's  b->a: -
```

A direction's summary column reads `port-scoped (N)` when that placement
carries N active selectors, in place of a parameter summary or `-`. A
placement carrying a root qdisc otto did not create renders `foreign qdisc —
not otto's` instead: `list` reports a foreign tree, but `impair`/`repair`
refuse to mutate **or** clear it — a root qdisc otto didn't generate could be
anything, and otto only ever touches trees whose shape it recognizes as its
own — so clear it manually with `tc` if it's expendable.

