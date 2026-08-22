# The impairment refusals are netdev-level, so a harmless port scope is refused too

Found while declaring the bed's first chaos link
(`carrot_seed:bbeth-1350 <-> bb1350_qemu:eth0`, on main). The link is declared
and its arm asserts otto's **refusal**; this is what an *injecting* guest arm is
waiting on, and it is a product decision rather than a defect.

The sibling asymmetry from the same investigation — `impair` placing state no
`repair` could clear — is **RESOLVED** on main (`a14258e1`, guards in
`tests/unit/link/test_impair_repair_symmetry.py`). What follows is what that
work deliberately did not touch: it was about *undoing* impairments, and this is
about which ones are safe to *create*.

## 1. A `--port` scope that locks nobody out is still refused

`ensure_not_hop_transit` (and `ensure_not_mgmt`) match on the **netdev**. They
never look at the selector, so a port-scoped impairment is judged as though it
degraded the whole wire.

Concretely, on the bed guest: `--port 9000 --loss 100` on `carrot/bbeth-1350`
would blackhole the `nc` transfer's data channel and leave telnet/23 — the
guest's management path — untouched. Nobody is locked out. It is refused
anyway.

That the mechanism works was measured on the live bed before the arm was
written:

| measurement | result |
| --- | --- |
| `--delay 300` on `carrot/bbeth-1350` | carrot→guest ping RTT **2.65ms → 302.5ms** |
| `--port 23 --proto tcp --loss 100` | full prio/netem/u32 tree built on the TAP; ICMP stayed at **0.96ms** (wire and guest both fine, only telnet blackholed) |
| effect on otto | `otto host bb1350_qemu run` failed rc 1 after **134s**, stalled in `Performing telnet login`, impairment verified still in place at the moment of failure |
| `--expire` backstop | cleared the whole tree on schedule; guest answered immediately, no restart |

So the injection is real and recoverable. Only the guard's granularity stands
between this and a genuine BusyBox chaos arm.

**Shape (not decided).** Two candidates, and they are not equivalent:

- An explicit override flag. Cheap, and puts the judgement on the operator —
  but a self-lockout guard with a bypass is a guard people learn to bypass.
- Teach the refusals to reason about the selector: refuse only when the scope
  could touch the dependent's management port. Strictly better behaviour, and
  more work — the predicate has to know which port the management path uses,
  which for a hop-reached host is the hop's term port, not a fixed number.

The second is the one worth designing. It wants a brainstorm, not an
implementation.

**Why the guest is the hard case at all.** A BusyBox guest has ONE NIC, so its
data plane *is* its management path — the same property that made the link
declarable makes every whole-link impairment on it a self-lockout. The veggies
scenario only works because tomato keeps a management `eth1` while its `eth2` is
blackholed; the guest has no `eth1`.

## 2. A blackholed telnet session fails with a bare `Aborted.`

Measured in the same run. When telnet/23 was blackholed, `otto host <guest> run`
failed after 134s printing **`Aborted.`** and nothing else, with an **empty**
`verbose.log`. The DEBUG stream shows exactly where it stalled (`Performing
telnet login`), so the information exists and is simply not reaching a
default-level operator.

The equivalent ssh path at least names `ConnectionLost`. Worth levelling up: a
timeout inside login should say which phase timed out, at default verbosity.

Independent of §1 — it is a diagnostics gap, not a placement question.

## 3. No routine lane exercises telnet under impairment

Noticed while auditing what confirmed the telnetlib3 4.0.5 → 5.0.0 bump
(2026-08-22). The chaos suite is where telnet-under-impairment lives, and it
carries `pytest.mark.stability`, which every default lane excludes:

- `make coverage` runs `-m "not stability ..."` — chaos deselected, correctly.
- `make chaos` is its own lane and the Makefile documents it as requiring
  **exclusive bed use** ("never co-run with other bed lanes").

That is a deliberate boundary, not a bug. Recording it because it means a
dependency bump to the telnet stack can be fully green across `make coverage`
and the whole CI matrix while nothing has driven telnet through a degraded link.
A deliberate `make chaos` run belongs in the checklist for changes to the telnet
or session transport, and nowhere else.

## Related

- `tests/e2e/chaos/test_connection_drop.py` — the declared link, the refusal
  arm, and the module docstring recording what an injecting arm still needs.
- `docs/guide/cli/link/safety.md` — the three refusals and the
  clearing-is-not-creating rule.
- `todo/busybox-tier3-fidelity-2026-08-13.md` — the adjacent BusyBox fidelity
  queue.
