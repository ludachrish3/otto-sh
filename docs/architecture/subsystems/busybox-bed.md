# BusyBox

otto's BusyBox support is developed against **real BusyBox binaries**, not against a
mock of one. A BusyBox userland differs from GNU/coreutils in argument parsing, exit
codes and applet availability, and those differences move between releases — the only
honest way to pin them is to run the releases.

It is run in two places, and the split is deliberate. Pinned **artifacts** answer
questions about the binaries themselves; five live QEMU **guests** answer what otto
does to a BusyBox device. This page is how to run both on your machine: what each one
needs, what it covers, and the trust note that comes with executing someone else's
binary.

```{note}
**It does not say what otto can and cannot do against a BusyBox host.** That is
{doc}`busybox-support`, which renders the declared gap
registry: the surfaces otto has measured broken on such a userland, the surfaces that
are merely untested, and the evidence behind each verdict. This page is about the
harness; that page is about the behaviour. Neither restates the other.
```

## The two halves, and which one answers what

| | Artifact tier | The bed |
| --- | --- | --- |
| Question it answers | Are these bytes the version they claim, and how does *this build* spell an applet? | What does otto do when it talks to a BusyBox device? |
| What it drives | The pinned binary as a local subprocess, from dash with `PATH` scoped to a symlink dir | Five QEMU guests over telnet, through a hop, via otto's own host API |
| Needs | An x86 interpreter (native or `qemu-user-static`), and the network on a cold cache | The Vagrant lab up |
| How to run it | `make busybox` | `make coverage-unix`, `make stability-unix`, `make chaos` |
| Runs in CI | Yes — the `busybox-artifacts` job, on x86_64 and arm64 | No: it needs the lab |
| Lives in | `tests/busybox/` | `tests/integration/busybox_bed/`, plus BusyBox rows in the generic suites |

The line between them is "does this question need a kernel". Applet argument parsing
does not — the binary answers it as a subprocess, hermetically, on any machine. A
transfer over a real transport into a real filesystem does, and no local rig
substitutes for one convincingly; an earlier chroot-and-dropbear harness tried, and
the bed replaced it.

Both halves rest on the **same bytes**: `scripts/build_busybox_guest_images.py` builds
each guest's initramfs from the artifact the pin file names, through the same
fetch-and-verify layer the artifact tier uses. That is why the narrow CI job still
guards the bed — a pin or a banner that drifts there is a guest whose userland is no
longer the version its lab entry claims.

## The artifacts

The matrix is fetched from the BusyBox project's own prebuilds at
`https://busybox.net/downloads/binaries/`, over HTTPS from the canonical site, and
cached locally. Versions are chosen at known behaviour transitions rather than evenly
spaced:

| Version | Arch | Why this one |
| --- | --- | --- |
| 1.16.1 | i686 | The oldest artifact published anywhere. Nothing exists for 1.0-1.15. |
| 1.21.1 | i686 | A second old-userland sample, before the `timeout` convention changed. |
| 1.28.1 | x86_64 | The first version with a published x86_64 build. |
| 1.31.0 | x86_64 | After the `timeout` argument-convention change. |
| 1.35.0 | x86_64 | Recent, and published under a differently-named directory. |

**No version floor is enforced in code.** otto probes for capabilities rather than
checking versions, so a host older than 1.16.1 either works or fails loudly on the
capability it lacks. Below 1.16.1 is *untested, not unsupported* — please report holes.

## Why the artifacts are x86, on every machine

Two facts decide this, and both are measured rather than assumed:

- **Upstream publishes no aarch64 build for any version.** There is no arm64 artifact
  to fall back to, at any point in the matrix.
- **32-bit ARM is not a way out either.** The aarch64 dev VM's ARMv8 cores have no
  AArch32 EL0, so a 32-bit ARM static binary fails with `ENOEXEC` there regardless of
  the kernel's `CONFIG_COMPAT=y`.

So otto always tests the x86 artifact: natively on x86_64 (which CI's first leg is),
and under `qemu-user-static` everywhere else. That is not merely a portability
workaround — it means the dev VM and CI execute **identical bytes**. Building or
fetching a native artifact per architecture would silently test two different builds
and call the result one matrix.

Note that the matrix is mixed: 1.16.1 and 1.21.1 are **i686**, because upstream
published no x86_64 build before 1.28.1. An `x86_64` interpreter does not run an i686
binary, so both handlers matter.

## Prerequisite: `qemu-user-static`

Needed only on machines that are not x86_64. An x86_64 kernel runs both arches
natively (32-bit needs `CONFIG_IA32_EMULATION`, which is on in every distribution
kernel otto targets), so nothing is required there.

```console
$ sudo apt update && sudo apt install qemu-user-static
```

The index refresh is part of the instruction, not decoration: installed against a
stale apt list, the download 404s on the `.deb` and reads exactly like "no such
package".

The package registers its own `binfmt_misc` handlers at install time, so there is
nothing further to configure and **no command-line prefix to type** — the artifact is
executed directly and the kernel routes it to the interpreter. Confirm the
registration took:

```console
$ cat /proc/sys/fs/binfmt_misc/qemu-x86_64
enabled
interpreter /usr/libexec/qemu-binfmt/x86_64-binfmt-P
...
```

The **first line must read `enabled`**. A handler that has been switched off
(`echo 0 > /proc/sys/fs/binfmt_misc/qemu-x86_64`) leaves the file in place and writes
`disabled` there, so the file existing is not the same as the interpreter working.
Check `qemu-i386` too if you intend to run the 1.16.1 and 1.21.1 entries.

```{note}
On the otto dev VM this package is installed by the `dev-root` provisioner in the
repository's `Vagrantfile`, so a rebuilt VM has it. Installing it by hand on a VM
without that entry lasts only until the next `vagrant destroy`.
```

## Running the artifact tier

```console
$ make busybox
```

This is the only lane that selects `-m busybox`; every catch-all selector excludes it.
That is deliberate — the tier reaches the public internet on a cold cache, so an
upstream outage must not be able to redden the per-task gate. See {doc}`../../guide/cli/test/index` for
the lane layout generally.

What it measures is the artifacts and nothing else: each file still hashing to its
committed SHA-256, each binary announcing the version it is filed under, and the argv
spellings otto's userland probe depends on (`timeout`, `base64`, `stat`, `wc`, `nc`)
read off each build. Behaviour against a BusyBox *host* is not here — it is on the bed
below.

If the interpreter is missing, the tier says so by name and points back here. It does
**not** quietly skip: a silent skip is how BusyBox coverage evaporates while the lane
keeps reporting green.

## Priming the cache, including offline

```console
$ make busybox-cache
```

Fetches and verifies every matrix artifact into `~/.cache/otto/busybox`. It needs the
network and **does not need an interpreter**, so it runs on any architecture.

Set `OTTO_BUSYBOX_CACHE` to use a different directory. For an air-gapped lab or an
egress-restricted CI runner, prime on a networked machine and copy the directory
across (or point every machine at one shared path):

```console
$ make busybox-cache                       # on a networked box
$ rsync -a ~/.cache/otto/busybox/ lab:/srv/busybox-artifacts/
$ OTTO_BUSYBOX_CACHE=/srv/busybox-artifacts make busybox   # on the lab machine
```

A cold cache with no network fails with a named error identifying the artifact and how
to prime it, rather than skipping.

## The bed: five guests, one per pinned version

Each guest is a tiny x86 initramfs whose **userland is the pinned artifact itself** —
BusyBox 1.16.1 the device is really running BusyBox 1.16.1, not a modern build asked
to behave like an old one. They run under QEMU on `test1` (the lab VM otto's lab data
calls `carrot`) and are reachable only from inside it, so every guest is addressed
through the hop:

| Version | Lab `ne` | `host1` backend id | Telnet port on the hop | `nc` data window | systemd unit |
| --- | --- | --- | --- | --- | --- |
| 1.16.1 | `bb1161` | `busybox_1161` | 2316 | 9160-9169 | `busybox-qemu-1.16.1.service` |
| 1.21.1 | `bb1211` | `busybox_1211` | 2321 | 9210-9219 | `busybox-qemu-1.21.1.service` |
| 1.28.1 | `bb1281` | `busybox_1281` | 2328 | 9280-9289 | `busybox-qemu-1.28.1.service` |
| 1.31.0 | `bb1310` | `busybox_1310` | 2331 | 9310-9319 | `busybox-qemu-1.31.0.service` |
| 1.35.0 | `bb1350` | `busybox_1350` | 2335 | 9350-9359 | `busybox-qemu-1.35.0.service` |

Facts worth knowing before you read a failure:

- **Telnet only, and that is the device being honest.** These guests run no ssh
  daemon — real BusyBox devices frequently do not either — so their lab entries
  declare `telnet` and hop through `carrot_seed`. Their transfers are `shell` and
  `nc`; `nc` is *declared and refused*, by a measured gap in the `nc` applet's
  argument parsing, and the refusal itself is pinned by the bed suite.
- **One account: `root`.** The password is baked into the image by the builder and
  recorded in the guests' lab-data credentials.
- **They are emulated, on two cores.** x86 guests on an aarch64 host means TCG with no
  KVM, and all five share `test1`. That is why every test that touches a guest joins
  ONE xdist group (`busybox_bed`) rather than one group per guest: a second worker
  would not parallelise them, it would timeshare the same two cores and take cycles
  from guests that already pay for emulation.
- **The port windows are not decoration.** The `nc` transfer needs the guest-side and
  hop-side port numbers to match, which is why each guest gets a ten-port window
  forwarded straight through rather than a single mapped port.

## Provisioning, recovery and logs

The bed is provisioned by the `busybox-qemu` provisioner in the repository's
`Vagrantfile`, on `test1`:

```console
$ vagrant provision test1
```

It fetches an Ubuntu amd64 kernel (one kernel serves the i686 userlands too, via IA32
emulation) and its `e1000.ko`, decompressing the module because BusyBox `insmod`
cannot read `.ko.zst`; then it builds the five images from the pinned artifacts and
writes one systemd unit per guest. Re-provisioning is safe and cheap: the builder
reports which images actually changed, and only those guests — plus any that are not
running — are restarted. A healthy, unchanged guest is left alone.

Health and recovery are the same two commands the Zephyr bed uses:

```console
$ make vm-health      # every lab VM + every QEMU guest, with uptimes
$ make qemu-restart   # restart the QEMU units on the hops, then health-check
```

Both shell out to `ssh` through `sshpass`, so those two programs must be on your
`PATH`; they are present on the dev VM but nothing in this repository installs them.

For anything deeper than "is it up", read the guest's console log on `test1`, where
each unit's stdout is its guest's serial console:

```console
$ vagrant ssh test1 -c 'journalctl -u busybox-qemu-1.35.0 -n 100'
```

One restart policy is worth understanding before it surprises you. The guests run with
`-no-reboot` and `panic=-1`, so a kernel panic — or any in-guest reboot — makes QEMU
*exit* rather than reset in place, and that exit carries status **0**. `Restart=always`
is therefore deliberate: `Restart=on-failure` would read a panicked guest as a clean
shutdown and leave the bed one guest short. A deliberate `systemctl stop` still sticks,
because systemd never applies `Restart=` to a stop it initiated.

## First-party parity: where the BusyBox rows live

BusyBox is not a special case with a suite of its own. The guests are backends in the
same machinery every other first-party OS rides, which is what keeps their coverage
honest as otto changes:

- **Host contract** — `tests/integration/host/test_host_contract.py`. The guests are
  `host1` backend ids, in the same parametrized matrix as the other hosts.
- **Transfers** — `tests/integration/host/test_unix_host_integration.py`, through the
  `transfer_host` parametrization: the `shell` backend against each of the five
  userlands.
- **Stability** — `tests/integration/host/test_host_stability_contract.py`, riding
  `make stability-unix` with everything else in that lane.
- **Chaos** — `tests/e2e/chaos/`. A guest arm lands in each module whose injection
  mechanism genuinely reaches a telnet-over-hop QEMU guest; where it does not, the
  module says so and why, next to the mechanism it is about.
- **Bed-only questions** — `tests/integration/busybox_bed/`. What is left when parity
  is subtracted: applet resolution in the guest's own ash, the shell transfer's codec
  choice per version, the session frame, the `nc` refusal, and a smoke test per guest.

Lane-wise that means the contract and transfer rows ride `make coverage-unix`, the
stability rows ride `make stability-unix`, and the chaos arms ride `make chaos` — the
same lanes, the same gates, no BusyBox-shaped exception. The one accommodation is the
`busybox_bed` xdist group described above, and it is enforced rather than remembered:
an item that reaches a guest from outside the group fails at setup.

## Trust: the one unsigned executable otto runs in CI

State this plainly, because it is true: **the BusyBox prebuilts are the only unsigned
third-party executables otto downloads and runs.** Upstream publishes no checksums and
no signatures for them — the source tarballs ship `.sha256` files, the prebuilt
binaries do not.

Verification is therefore two layers, and they do different jobs:

- **The behavioural gate is primary.** Each artifact is executed and must announce the
  version it is filed under. A failure there is a real finding about interface drift,
  which is the thing this matrix exists to detect. A byte-level check is not.
- **A committed SHA-256 is secondary**, in `tests/_fixtures/busybox_pins.json`. It is
  trust-on-first-use, and its narrow but real value is that CI re-fetches on every cold
  cache: the pin converts per-run trust in busybox.net into one-time trust taken at a
  reviewed moment. A mismatch is **investigated, not rubber-stamped** — if upstream
  legitimately rebuilt, the pin is updated in a reviewed commit that says so.

The remaining mitigation is blast radius. Be precise about what that does and does not
mean, because a comforting summary here would be worse than none: the artifact is
executed **in place, from the artifact cache** — `~/.cache/otto/busybox` by default,
which is an ordinary persistent user-owned directory, not a scratch space that
disappears after the run. What bounds it is that it is never installed, is never added
to the system `PATH`, and runs as whatever user runs the tests — which on a developer
machine and on CI is an unprivileged one. Nothing in the fixture *enforces* that: run
the suite as root, in a root container, and the artifact runs as root too. The applet
tier does scope its own `PATH`: it symlinks the applet names it is measuring into a
per-test temporary directory and runs dash with `PATH` set to that directory alone —
but the symlinks resolve to the cached binary, so the bytes still execute from the
cache.

The one place the artifact runs somewhere other than a developer machine or a CI
runner is inside a bed guest, where it *is* the userland of a throwaway initramfs on
an emulated machine — and the reach of that machine is worth stating exactly, since
this is the section where a comforting summary does the most damage. The guest is
unaddressable: it sits behind QEMU's user-mode (slirp) networking on `test1`, so
nothing on the lab network can open a connection to it and the only way in is a
hostfwd bound to `test1`'s own loopback. Outbound is the other direction and is not
closed: the guest takes a default route to slirp's gateway (`10.0.2.2`), which NATs
through `test1`, which has internet. So the isolation here is locality and a one-way
door, not an absence of egress — and it is isolation by virtue of where the guests
live, not a sandbox the artifact tier applies to itself. Container-isolated
local tiers were once planned and are not coming: the bed answered the question they
were for. Do not read the paragraph above as describing containment the local tier does
not have.

If that trade is not acceptable in your environment, prime the cache from artifacts you
built or vetted yourself, point `OTTO_BUSYBOX_CACHE` at them, and record their hashes
in the pin file. The pin is checked against whatever the cache holds on every call, so
a substitution you did not make is reported rather than run.
