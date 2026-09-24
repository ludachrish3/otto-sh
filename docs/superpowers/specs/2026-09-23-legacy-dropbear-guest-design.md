# Legacy SSH hosts: a 2012-era dropbear on the bb1350 bed guest, per-host `kex_algs`, and negotiated-algorithm visibility

**Date:** 2026-09-23
**Status:** approved in discussion; this document is the written record

## Why

A user on otto 0.12.1 reaches a legacy BusyBox device (old dropbear) through
one hop and reported three things:

1. They could not tell whether their SSH algorithm options reached the wire.
   otto logs nothing about what it hands `asyncssh.connect()` or what the
   handshake negotiated, and `ssh_options` carries no `kex_algs` field, so the
   only route is the undocumented `extra` pass-through.
2. Every connection printed two `CryptographyDeprecationWarning`s from
   `asyncssh/crypto/dh.py` lines 30 and 43: *"Diffie-Hellman over finite
   fields (FFDH) is deprecated and support will be removed in a future
   release."* cryptography 50 deprecates finite-field DH; asyncssh 2.24 routes
   every `diffie-hellman-group*` key exchange through it, so the warning fires
   twice per handshake exactly when a legacy sshd is on the other end. The
   repo's lock pins cryptography 49, which is why the dev venv cannot show it.
   otto can do nothing about the device's algorithm set, so the noise must go,
   and the removal that the message announces must not take legacy key
   exchange away from otto silently.
3. A DEBUG timeline showed the target's own SSH handshake taking 2.5 s of a
   3.4 s command, on an emulated CPU. The hop handshake took 116 ms.

otto has no test bed with a legacy sshd. The five BusyBox QEMU guests on
`test1` run no ssh daemon at all (by design: a standing drift-table
true-negative). The 2026-08-22 investigation
(`todo/busybox-dropbear-canary-2026-08-22.md`) cross-built dropbear 2012.55,
proved it negotiates with otto's stock options on loopback, and stopped short
of the bed. This spec takes it to the bed and adds the otto-side pieces the
user asked for, because the bed is what proves them.

## Scope

**In:**

- dropbear **2012.55** on the **bb1350** guest (BusyBox 1.35.0), built at
  provision time on `test1`, host key generated once and persisted there.
- Curated `kex_algs` and `mac_algs` fields on `SshOptions` and
  `SshOptionsSpec`, documented with a legacy-host example.
- The hop tunnel connect honours the hop host's own `ssh_options`.
- DEBUG log lines: the redacted kwargs handed to `asyncssh.connect()` before
  every connect, and the negotiated cipher/MAC/compression plus the server's
  version banner after it. An opt-in `OTTO_SSH_DEBUG` env var that raises
  asyncssh's own debug level so its "Key exchange alg" line appears.
- A warning filter scoped to the FFDH deprecation as attributed to
  `asyncssh.crypto.dh`, installed at otto's asyncssh import seams and mirrored
  in the pytest configuration; a unit test that injects the warning to prove
  the filter matches, since the locked cryptography cannot emit it.
- A unit test pinning the legacy algorithm names in asyncssh's registries, and
  an integration cell that proves the wire negotiation against the real 2012
  daemon, with and without per-host `kex_algs`.
- The docs, the userland gap registry, and the test pins that name any of the
  above.

**Out (each its own spec later):**

- A sixth guest. `test1` has roughly 500 MB free with five emulated guests
  resident, and its `vb.memory` is not measured anywhere.
- `signature_algs`, certificate options, or any other asyncssh knob.
- Health-probing the guest's sshd from `scripts/lab_health.py`. Its routing is
  by shape (creds plus hop means the telnet console path) and stays so; the
  bed doc says the sshd is not health-probed.
- Any change to session init timeouts or the auth-method order. The 2.5 s is
  the device's handshake; the levers this spec adds (`kex_algs`, the DEBUG
  lines, `OTTO_SSH_DEBUG`) are what lets a user measure and then choose.

## Part 1: the bed rig

### Version and provenance

dropbear **2012.55** and nothing later. dropbear grew ECC in 2013.56 and ships
curve25519/ecdsa on by default from 2013.62; asyncssh prefers those, so any
later daemon exercises the modern path with an older banner. 2012.55's whole
offer is:

```
kex     : diffie-hellman-group1-sha1, diffie-hellman-group14-sha1
hostkey : ssh-rsa, ssh-dss
cipher  : aes128-ctr 3des-ctr aes256-ctr aes128-cbc 3des-cbc aes256-cbc twofish*
mac     : hmac-sha1-96, hmac-sha1, hmac-md5
```

Upstream publishes source only, unsigned for that release. The repo pins the
tarball by sha256:

```
dropbear-2012.55.tar.bz2  04982af2a10b220fa940f9f72f276d612c9bb643cfbb5ee1416e5a0f00de9b0f
```

The pin is spelled once in `scripts/build_busybox_guest_images.py`
(`DROPBEAR_PIN`), the Vagrantfile literal mirrors it, and the existing
hostless guard that already pins the Vagrantfile's guest table to the script's
table pins this too. The provisioner verifies the checksum before extraction
and fails loudly on mismatch, exactly as the BusyBox artifact fetch does.

### Build, on `test1`, at provision time

`test1` is aarch64; the guests boot an amd64 kernel under TCG. Two builds
from one extracted tree:

- **Native `dropbearkey`** (aarch64, `build-essential`), used once to generate
  the host key. dropbear's key file format is its own, so this is the honest
  way to produce one without shipping a second architecture's binary to run
  under an emulator.
- **Cross `dropbear`** for the guest: `gcc-x86-64-linux-gnu` and
  `libc6-dev-amd64-cross` from ports, plus an amd64 `libcrypt.a` obtained with
  `apt-get download libcrypt-dev:amd64` and `dpkg-deb -x` into a scratch
  sysroot. The provisioner already enables the amd64 foreign architecture and
  the archive source for the kernel fetch, so this is the same pattern, not a
  new one. Configure with `--build=aarch64-unknown-linux-gnu
  --host=x86_64-linux-gnu --disable-zlib`, link static, strip. The 2012
  `config.guess` predates aarch64, which is why `--build` is explicit.

Both artifacts land under `$BED` beside `vmlinuz` and are built only when
absent, so a re-provision is cheap. The build tree is a `mktemp -d` removed by
an `EXIT` trap, as the kernel fetch does.

The Vagrantfile block is the source of truth for other contributors. The
same steps are run by hand on `test1` for this branch, from the worktree's
copy of the scripts (the `/vagrant` mount is Chris's host checkout, not this
branch), and the Vagrantfile block is written from what actually worked.

### Host key

RSA 1024, `dropbearkey`'s default and what a device of that era carries;
asyncssh 2.24 accepts it. Generated once into `$BED/dropbear_rsa_host_key`
if absent, so the guest keeps a stable host key across reboots and
re-provisions. Never in the repo. The image build takes it as an input, and
`build_initramfs_bytes` stays byte-deterministic given the same key file,
which keeps the rebuild-only-on-change stamping intact.

### Guest image

`GUEST_TABLE` gains an `sshd` column (`dropbear` for 1.35.0, `none` for the
rest); the Vagrantfile's copy of the table gains the same column and the
hostless guard pins both. `build_busybox_guest_images.py` gains
`--dropbear PATH` and `--dropbear-host-key PATH`; for a guest whose column
says `dropbear`, `cpio_newc_entries` adds:

```
etc/dropbear/                          0755
etc/dropbear/dropbear_rsa_host_key     0600
bin/dropbear                           0755   (static x86_64)
```

and the inittab gains one line beside telnetd:

```
::respawn:/bin/dropbear -F -E -r /etc/dropbear/dropbear_rsa_host_key -p 22
```

`-F` foreground under BusyBox init, `-E` log to stderr (the serial console,
so a refused login is readable in the unit's journal), `-p 22` the honest
port. Root authenticates against the existing MD5-crypt `/etc/shadow` entry;
libxcrypt 4.4 still serves `$1$`, measured on 2026-08-22. The guest's `rcS`
already mounts devtmpfs and devpts, which `openpty()` needs. The binary is
static, so the image ships no libc.

Image cost, measured 2026-08-22: about +0.7 MB gzipped, about +1.4 MB
resident of the guest's 96 MB.

### Lab record

bb1350's `valid_terms` becomes `["telnet", "ssh"]`. Telnet stays first:
`CapabilityResolver.resolve_active` falls through to the first entry when a
host pins no `term`, so every existing session on that guest stays telnet, and
the unit pin `host.term == "telnet"` still holds. No `ssh_options` on the
record: stock asyncssh negotiates 2012.55. `valid_transfers` stays
`["shell", "nc"]`: 2012.55 ships no sftp-server and BusyBox has no scp
binary.

The record lives in two distinct files that change together: the fixture
lab data `tests/_fixtures/lab_data/tech1/lab.json`, which the bed suites read,
and the getting-started example `docs/examples/getting-started/lab_data/lab.json`.

### Bringing it up on this branch

Order, each step gated before the next:

1. Health-gate the bed (all five guests answer telnet with a real prompt).
2. Build on `test1` by hand, mirroring the Vagrantfile block.
3. Rebuild only the 1.35.0 image from the worktree's build script and restart
   only `busybox-qemu-1.35.0.service`. That restart is a guest reboot and is
   part of the approved work; the other four units are not touched.
4. Health-gate again, then `ssh root@198.51.100.17` from `test1` with
   OpenSSH's legacy options to prove the daemon before any otto test runs.

### What the rig cannot do

It reproduces the user's device shape: a SHA-1-only sshd on a TCG-emulated
CPU behind a hop. It does not reproduce the FFDH warning while the lock pins
cryptography 49; the unit test in Part 2 covers the filter until the lock
moves, and the moment it moves to cryptography 50 the bed cell would red
without that filter, which is the point.

## Part 2: otto

### `kex_algs` and `mac_algs`

Two new curated fields, `list[str] | None = None`, on `SshOptions`
(`src/otto/host/options.py`) and `SshOptionsSpec`
(`src/otto/models/options.py`), forwarded by `_kwargs()` and `to_runtime()`
exactly as `encryption_algs` is. Names are asyncssh's; an unknown name is
asyncssh's `ValueError` at connect time, not otto's, so the boundary spec
validates shape only. `extra` still overrides on conflict.

The field docstrings, which the API docs render, and the `[host_preferences]`
section of `docs/configuration/settings.md`, where `ssh_options` is
introduced today, show the legacy case:

```toml
[ssh_options]
kex_algs = ["diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"]
server_host_key_algs = ["ssh-rsa", "ssh-dss"]
encryption_algs = ["aes128-ctr", "aes128-cbc", "3des-cbc"]
mac_algs = ["hmac-sha1", "hmac-md5"]
```

with the note that order is preference order and the client's first entry the
server supports wins.

### The hop connect honours the hop host's `ssh_options`

`RemoteHost._build_hop_transport._create_tunnel` currently passes ip,
username, password, `known_hosts=None` and `tunnel` and nothing else, so a
hop host's port, algorithm lists, keys and timeouts are silently ignored. It
now spreads the hop host's own `SshOptions._kwargs()` (which carries
`known_hosts=None` by default) under the username, password and tunnel, and
hands those kwargs to asyncssh exactly as the hop host's own session would.
It applies NONE of the hop host's structured forwards
(`local_forwards`/`remote_forwards`/`socks_forwards`) or its `post_connect`
hook: a tunnel is a transport to the hop, not a session on it, and a hop
host record with a fixed `local_forwards` port would otherwise try to bind
that port once per hopped target. `tests/unit/host/test_hop.py`'s pins on
the exact call shape move with it.

### Visibility

Two helpers in `src/otto/host/connections.py`, used by `ssh()`, `ssh_as()`
and the hop factory:

- Before connect, at DEBUG: `<name>: asyncssh.connect(<ip>) kwargs=<dict>`
  with `password` and `passphrase` replaced by `'***'` and everything else
  as passed, so a user can see their `kex_algs` leave otto.
- After connect, at DEBUG: `<name>: negotiated server=<server_version>
  cipher=<send>/<recv> mac=<send>/<recv> compression=<send>/<recv>` from
  `conn.get_extra_info`. asyncssh exposes no kex algorithm after the
  exchange, which is what the next item is for.

`OTTO_SSH_DEBUG=<1..3>` (name in `src/otto/config/env.py`), read at the
asyncssh import seams: when set, otto calls `asyncssh.set_debug_level(n)`.
(asyncssh's own range; anything else is reported and ignored) A valid value
also lifts otto's own `asyncssh` library-logger floor (otto pins `asyncssh`
at WARNING by default) to DEBUG, so under `--log-level DEBUG` level 2 prints
the offered and chosen key-exchange, host-key, cipher and MAC lists. The
lift takes effect from the first connect on and overrides a repo's
`[logging.levels]` `asyncssh` entry for the rest of the process — an
explicit per-invocation opt-in to a trace beats a standing noise-floor
entry; unset `OTTO_SSH_DEBUG` to get the repo's floor back. Opt-in because
level 2 is chatty on every connection.

### The FFDH warning

At otto's asyncssh import seams (`ssh_connect` in `connections.py` and the
hop factory's import), an idempotent `install_asyncssh_warning_filters()`
registers:

```python
warnings.filterwarnings(
    "ignore",
    message=r"Diffie-Hellman over finite fields \(FFDH\) is deprecated",
    category=CryptographyDeprecationWarning,
    module=r"asyncssh\.crypto\.dh",
)
```

Scoped three ways on purpose: a different deprecation, the same deprecation
reached from otto's own code, or the same message from another module all
still surface. `pyproject.toml`'s `filterwarnings` gains the equivalent
`ignore:` line so the bed cell stays green when the lock moves to
cryptography 50; `"error"` remains first, so every other warning is still
red.

`CryptographyDeprecationWarning` subclasses `UserWarning`, not
`DeprecationWarning`, so without this filter Python's default filters print it
in the CLI once per call site.

### Guards against a silent drop

Chris's acceptance criteria for this branch, verbatim: "crypto deprecation
warnings are suppressed, a crypto support matrix defined and tested, and a
gate that fails if the underlying crypto library drops support for an
algorithm currently supported by the conformance matrix", and: "instead of
focusing on just the one algorithm I mentioned, I'd like Otto's docs and
support matrix to list ALL support[ed] algorithms so that it's clear what's
supported. Otherwise users will have no easy way to figure out what's
available." The three bullets below are what that closes to.

- **Registry pin**, `tests/unit/host/test_legacy_ssh_algorithms.py`: every
  algorithm the installed asyncssh registers, across all five families (kex,
  host key, cipher, MAC, compression), has a row in the single-source
  matrix (`tests/_fixtures/ssh_algorithm_matrix.py`), and every matrix row
  is registered unless its note names the optional library (liboqs,
  libnettle) that gates it — both directions, so a version bump that adds
  or drops an algorithm reds here before the docs page or the bed run. The
  matrix's `stock` column is pinned against asyncssh's default offer the
  same way, both directions: a row leaving or entering the defaults is a
  user-visible change either way. `DROPBEAR_2012`, the bb1350-relevant
  subset, gets its own pin that those rows stay unconditionally registered.
- **Handshake exercise**, `tests/unit/host/test_ssh_algorithm_handshake.py`:
  the registry pin alone cannot catch the FFDH removal, because asyncssh
  2.24 registers every `diffie-hellman-group*` key exchange
  UNCONDITIONALLY — `kex_dh.py` has no availability flag, and
  `crypto/dh.py` imports `cryptography...asymmetric.dh` at module import but
  only touches it inside `DH.__init__`. So when cryptography removes
  finite-field DH while keeping the module importable, the name stays
  registered, the registry pin stays green, and the first red would be the
  next bed run. This module runs an in-process asyncssh server and client
  through every exercisable matrix row and asserts the negotiated
  cipher/MAC/compression via `get_extra_info`, so the same dependency bump
  reds hostless, on the bump.
- **Docs page**, `docs/architecture/ssh-algorithms.md`: every matrix row
  rendered as a table, one family per section, linked from settings.md's
  Legacy SSH servers section and from host-options.md's SSH section.
  `tests/unit/docs/test_ssh_algorithms_page.py` parses the page back into
  the same dataclass and asserts it equals the matrix, in order, so the
  docs page cannot fall behind the fixture the other two pins hold.
- **Filter proof**, `tests/unit/host/test_legacy_ssh_algorithms.py`:
  `warnings.warn_explicit` with the FFDH
  message, the category, and `module="asyncssh.crypto.dh"` is swallowed under
  the installed filter, while the same message attributed to another module
  and a different message from `asyncssh.crypto.dh` both still raise under
  `-W error`. The hostile condition is injected, not inherited.
- **Wire proof**, `tests/integration/busybox_bed/test_legacy_dropbear.py`,
  opening bb1350 with `term="ssh"` through `test1`:
  1. stock options: an exec round-trip succeeds, and the negotiated tuple is
     `diffie-hellman-group14-sha1` / `aes256-ctr` / `hmac-sha1`. Kex is
     asserted from asyncssh's debug-2 log line captured with `caplog`;
     cipher and MAC from `get_extra_info`;
  2. `SshOptions(kex_algs=["diffie-hellman-group1-sha1"])`: the negotiated
     kex is group1, proving a per-host `kex_algs` reaches the wire;
  3. `SshOptions(encryption_algs=["3des-cbc"])`: still connects, proving a
     legacy cipher path end to end.

  All three run under the repo's `filterwarnings = ["error"]`, so any
  unfiltered deprecation on the handshake is a failure, not noise.

## Tests that change

- `tests/integration/busybox_bed/test_guest_smoke.py::test_ssh_is_dead_by_construction`
  becomes per-guest: four guests assert no sshd; bb1350 asserts `/bin/dropbear`.
- `tests/integration/busybox_bed/test_probe_survey.py`: the ssh true-negative
  holds on four guests; bb1350 expects ssh supported and declared.
- `tests/unit/host/test_busybox_bed_lab_entries.py`: bb1350's `valid_terms`.
  The five-element guest roster is unchanged.
- `tests/unit/scripts/test_build_busybox_guest_images.py`: the `sshd` column
  in both tables, the dropbear pin, the inittab line and the image members for
  the dropbear guest, and that the other four images are byte-identical to
  before.
- `tests/unit/host/test_options.py` and `tests/unit/models/test_option_specs.py`:
  the two new fields round-trip and reach `_kwargs()`.
- `tests/unit/host/test_hop.py`: the tunnel connect call shape.

## Docs that change

- `docs/architecture/subsystems/busybox-bed.md`: the "telnet only" paragraph,
  the guest table (bb1350 also answers ssh on 22), the not-health-probed note.
- `docs/configuration/settings.md`: the two fields and the legacy example.
- `src/otto/host/userland.py` gap registry: the registry records only what
  otto cannot do (`measured-broken`, `untested`), so `legacy-dropbear-crypto`
  is removed once the wire proof passes, together with its row on the
  rendered docs page and the pins that name it. The measurement itself is
  recorded in the bed doc.
- `tests/e2e/chaos/_bed.py`: the docstring that says the asyncssh oracle
  cannot be used on bb1350 because it has no sshd.
- `todo/busybox-dropbear-canary-2026-08-22.md` and
  `todo/busybox-tier3-fidelity-2026-08-13.md` §C: closed by this spec.

## Risks

- Nothing has yet run on a real system-mode TCG guest: `openpty()` against the
  guest's devpts, entropy at early boot, and BusyBox `init` supervising two
  respawn lines. Step 4 of the bring-up is where these surface.
- The repo's warning filter and pin test protect against the FFDH removal;
  they do not protect against asyncssh moving `diffie-hellman-group14-sha1`
  out of its default list. The matrix's stock column, asserted both ways,
  is that guard.
- The chaos suite's anchor is bb1350. Its tests use telnet and do not assert
  ssh absence, so they are unaffected; the roster pins say so.
