# BusyBox Tier 3 fidelity: legacy dropbear, a remote endpoint, and the gaps to declare

Queued **after phase 5**, agreed 2026-08-13. Phase 5 builds Tier 3 as a rootless
**modern** dropbear on loopback. That is the right first tier — it is hermetic
and runs in CI — but it leaves two fidelity gaps, one of which the design
document believes is already closed. Everything below was measured while
building phase 5, so this workstream starts from evidence rather than a survey.

Scope agreed: **C + B + D**. Option A — a real BusyBox host on the lab network
(`test1`/`test2`/`test3`, 10.10.200.11-13, all `autostart: false`) — was
considered and **declined for now**: it needs VM provisioning, which is Chris's
call, and it is not a prerequisite for C or B.

## The gap that matters most: Tier 3 does not test the risk it was built for

`docs/superpowers/specs/2026-08-11-busybox-host-support-design.md` §4 "The
dropbear risk" justifies the whole tier:

> Old dropbear negotiates only SHA-1-era algorithms that modern asyncssh
> disables by default; it ships no `sftp-server`; and its channel limits differ
> from OpenSSH's `MaxSessions`. otto's `ssh_options` already carries
> cipher/host-key/kex lists, so this is configuration rather than code — but it
> is untested, and it would bite immediately on a genuinely old device.
> **Tier 3 below addresses it directly.**

Measured against what phase 5 actually built, that paragraph is 1 for 3:

| claim | status |
| --- | --- |
| ships no `sftp-server` | **FALSE as packaged.** Debian builds dropbear with `SFTPSERVER_PATH=/usr/lib/sftp-server`, and `openssh-sftp-server` ships that symlink — present on the dev VM and on GitHub runners. Baseline dropbear served `sftp`, `scp` and `scp -O` at rc 0. Phase 5 masks it explicitly. |
| channel limits differ from `MaxSessions` | **TRUE.** 120 concurrent `conn.run()` channels all succeeded (~2 s wall, genuinely concurrent); OpenSSH refused everything past 10 with `ChannelOpenError`. But the real dropbear ceiling is elsewhere: `MAX_UNAUTH_PER_IP` = **5 simultaneous pre-auth connections**, excess reset in ~0.0003 s with **no server log line at all** (re-measured; see `MAX_UNAUTH_PER_IP` in `tests/_fixtures/busybox_dropbear.py` — the 0.02-0.04 s first recorded here was fan-out scheduling, not the reset). |
| SHA-1-era crypto, "addressed directly" | **NOT ADDRESSED.** Tier 3 runs `dropbear-bin 2022.83-4` from apt, which is modern. We know no legacy negotiation is exercised *by construction*: asyncssh connects with its DEFAULT algorithm set, which an old dropbear would have refused. |

So the one risk the tier was named for is the one it does not cover.

## C — an old dropbear on loopback

Run the phase-5 harness against a period-appropriate dropbear (the 2013-era
range matching the BusyBox artifact matrix) instead of 2022.83. Hermetic, stays
in CI, and attacks the actual named risk.

The harness needs no redesign: the daemon binary and its flags are already the
only dropbear-specific surface (`tests/_fixtures/busybox_dropbear.py`).

**Two things to MEASURE before promising this works** — both are currently
assertions in the spec, and two of that paragraph's three sibling claims have
already failed:

1. **Does an old dropbear build on this toolchain?** The spec says "Dropbear is
   small and builds cleanly on modern toolchains, unlike old BusyBox." Untested.
   Old C against a modern glibc/gcc is exactly where that kind of claim breaks.
2. **Do otto's `ssh_options` cipher/host-key/kex lists actually suffice to talk
   to it?** The spec says "this is configuration rather than code". Untested. If
   it turns out to need code, that is a product finding, not a test-tier one,
   and is the whole point of running this.

Note the interaction with a phase-5 measurement: an **RSA host key is
mandatory** even on modern dropbear, because asyncssh 2.24 dies on
`Failed assertion (rsa.c:164): key != NULL` against an ed25519-only host key
while `ssh(1)` stays green. An older dropbear may narrow the host-key options
further.

## B — parametrize the Tier 3 endpoint

Let the harness aim at a remote host when one is configured, defaulting to
loopback. Cheap, reuses the phase-5 harness unchanged, and makes a lab-VM run
possible without committing to option A.

Why it is worth having even though loopback exercises the same SSH semantics:
loopback has a ~64 KB MTU and no real latency, so it cannot surface interactions
between the transfer's chunking and a real path's MTU, window and timeouts. It
also collapses every client to one source IP, which is what makes
`MAX_UNAUTH_PER_IP` = 5 bite in the first place — a real deployment reaching one
device from one otto host has the same shape, so this is fidelity in both
directions.

Today **no BusyBox test reaches a real host**: `tests/busybox/` is deliberately
outside `tests/integration/` (its conftest records why — the integration
auto-stamp put a ~5 MB busybox.net fetch inside the bed lane) and nothing there
carries the `integration` marker.

## D — declare both gaps in the registry

Phase 5 builds the gap registry; these are two of its entries, and they must be
worded as what is *untested* rather than what is broken. The spec's own firing
rule governs: **"Measured-broken refuses up front; unmeasured runs."** Blocking
an untested surface would convert "we do not know" into "does not work".

- Tier 3 exercises modern dropbear only; legacy SHA-1-era negotiation is
  untested, and otto's `ssh_options` sufficiency for it is unverified.
- Every BusyBox tier is local; no BusyBox target is exercised over a real
  network path.

## Also unmeasured, and adjacent

Whether GitHub runners need the same
`kernel.apparmor_restrict_unprivileged_userns = 0` the dev VM sets from its
Vagrantfile. Tier 2 already depends on it, so it is Tier 2's question before it
is Tier 3's, but any remote or CI-shape change here should settle it.

## Related

- `todo/busybox-parity-sweep-2026-08-11.md` — the separate full-parity queue
  (uu codec for 1.16.1, BusyBox `nc` backend). Distinct workstream; do not merge.
- `docs/superpowers/plans/2026-08-13-busybox-phase-5-tier3-and-gap-registry.md`
  — phase 5's plan, whose "Measured inputs" table is the evidence base for the
  numbers quoted above.

## Addendum, 2026-08-21 — after the bed migration branch

The bed-and-tier-migration branch (Phase B) retired the dropbear/rootfs rig
this document describes; `tests/_fixtures/busybox_dropbear.py` and the other
files cited above no longer exist. The citations stand as history. Open items
that survive that branch, queued here so they are not lost:

- **BusyBox `nc` dialect** (Chris ruling, 2026-08-21): otto's nc backend
  cannot round-trip with any BusyBox userland (`-N` rejected on get; OpenBSD
  listener spelling on put). The bed pins the loud get-refusal as the
  contract; making nc WORK is its own brainstorm→spec, with the five live
  guests as the measurement instrument.
- **Interrupted shell PUT leaves an inert staged temp** on the device
  (`<dest>.otto-<uuid>`): `_cleanup_temp` never runs after cancellation. A
  shielded cleanup is the candidate fix; the chaos suite characterizes the
  behavior without pinning either direction.
- **Bed probe ergonomics** (final-review F2 + m11): `_require_guest` and the
  `transfer_host` busybox arm use a raw TCP connect probe that calls a
  booting guest healthy; a login-grade probe would name the guest earlier.
- **`chaos_lane` group-spelling exposure** (m29): ten literal spellings, no
  shared constant, no runtime guard — same shape `busybox_bed` was given in
  the migration branch; wants the same treatment.
