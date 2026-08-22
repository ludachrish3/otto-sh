# `busybox-over-a-real-network`: what the NIC move closed, and what it did not

Written after the bed's guests moved onto real NICs (`6b9a1e6c`, on main) and
171 bed tests crossed the new path on three interpreters. The question asked was
whether the gap record's `untested` status had gone stale.

**It has not. Do not flip it.** This entry exists to record why, because the
question is a natural one to ask again in three months.

## The record already draws the line correctly

`src/otto/host/userland.py` (`surface="busybox-over-a-real-network"`) says, as
of 2026-08-22, that the guests now drive an e1000 through their own kernel stack
onto a TAP on the hop — "so Ethernet framing, MTU and window behaviour are
genuinely on the path now" — and that what is still missing is **a wire**: "a
TAP is host-local, with no propagation delay and no loss".

That is the right distinction, and it survives the new evidence. Two different
things were conflated when this was raised:

| claim | true? |
| --- | --- |
| A real NIC, driver and kernel stack are on the path | **Yes**, since the NIC move. QEMU's user-mode stack used to terminate the connection and re-originate it; it no longer does. |
| A real *network* is on the path | **No.** A TAP on the hop is host-local. No propagation delay, no loss, no contention, no MTU discovery across a router. |

The gap is about the second. 171 passing bed tests are evidence about the first.

## What the tests actually established

Worth recording so the next person does not re-measure it:

- Bed suite green on **3.10, 3.11 and 3.14** against all five guests
  (`bb1161`…`bb1350`), 171 tests each — including the telnet transport, the pty
  line-budget chunking and both transfer backends.
- The BusyBox `ip` applet's `ip -o addr show` output parses with
  `otto.link.placement.parse_ip_addr` — measured when the chaos arm reached the
  guest end through the hop. Not previously known.
- Synthetic impairment on the TAP does bite the guest (2.65ms → 302.5ms under
  `--delay 300`). That is *emulated* timing on a host-local device, so it does
  not close this gap — but it does mean the bed can now stage latency
  deliberately, which the old slirp bed could not.

## What would close it

Unchanged from the record's own `queued_for`: aim a BusyBox target across a
physical link and measure over it. The harness the original Tier 3 item named is
retired and the guests run no ssh daemon, so this needs a rig of its own.

The cheapest honest version is probably not a physical wire at all but an
admission that the surface splits in two — "real stack" (now measured) and "real
wire" (not) — and that only the second is still open. If the record is ever
reworded, that split is the wording to use.

## Related

- `todo/busybox-tier3-fidelity-2026-08-13.md` — item B, the original queue entry.
- `docs/architecture/subsystems/busybox-support.md` — the rendered gap table.
- `tests/unit/host/test_gap_registry.py:452` — pins this surface and
  `legacy-dropbear-crypto` as the two `untested` records.
