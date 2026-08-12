# BusyBox

otto's BusyBox support is developed against **real BusyBox binaries**, not against a
mock of one. A BusyBox userland differs from GNU/coreutils in argument parsing, exit
codes and applet availability, and those differences move between releases — the only
honest way to pin them is to run the releases.

This page is the prerequisite list for running that matrix on your machine, plus the
trust note that comes with executing someone else's binary.

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

So otto always tests the x86 artifact: natively on x86_64 (which CI is), and under
`qemu-user-static` everywhere else. That is not merely a portability workaround — it
means the dev VM and CI execute **identical bytes**. Building or fetching a native
artifact per architecture would silently test two different builds and call the result
one matrix.

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

## Running the matrix

```console
$ make busybox
```

This is the only lane that selects `-m busybox`; every catch-all selector excludes it.
That is deliberate — the tier reaches the public internet on a cold cache, so an
upstream outage must not be able to redden the per-task gate. See {doc}`../test` for
the lane layout generally.

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
the suite as root, in a root container, and the artifact runs as root too. A future
tier that drives individual applets will symlink them into a per-test temporary
directory and put *that* on the `PATH` of the test's own shell only.

Container-isolated tiers, which will additionally run the artifact inside an
unprivileged user namespace, are planned for a later phase and do not exist yet. Do not
read the paragraph above as describing containment it does not have today.

If that trade is not acceptable in your environment, prime the cache from artifacts you
built or vetted yourself, point `OTTO_BUSYBOX_CACHE` at them, and record their hashes
in the pin file. The pin is checked against whatever the cache holds on every call, so
a substitution you did not make is reported rather than run.
