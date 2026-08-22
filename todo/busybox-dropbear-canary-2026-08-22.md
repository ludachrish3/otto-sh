# Dropbear on a BusyBox bed guest — the measured record, and the decision it needs

> **Rescued from a retired worktree, 2026-08-22.** This was an investigation
> report living only in `.claude/worktrees/busybox-real-nic/`; one `git clean`
> would have taken it. Nothing here has been implemented — it is a decision
> waiting on Chris, plus a set of measurements worth keeping either way.
>
> **It corrects two other documents.** See *Corrections this investigation puts
> on the record* at the end: `todo/busybox-tier3-fidelity-2026-08-13.md` §C and
> `docs/superpowers/specs/2026-08-11-busybox-host-support-design.md` both state
> things now known to be false. Read this before acting on either.
>
> **The headline:** the investigation overturned its own premise. otto already
> negotiates `group14-sha1` / `ssh-rsa` / `aes256-ctr` / `hmac-sha1` against
> dropbear 2012.55 with **stock options** — so the "legacy SHA-1 crypto" risk
> the tier was built to test does not exist for asyncssh 2.24. What remains is
> whether to build a *canary* that keeps it that way, which is Options A/B/C
> below.

---


Investigation only. Nothing in the repo was changed. Every number below was
produced on the dev VM on 2026-08-22; no lab VM was touched, no guest was
modified, `vagrant` was never invoked.

Scratch dir with all artifacts, logs and scripts:
`/tmp/claude-1000/-home-vagrant-otto-sh/12fe6f88-f787-4a3f-b4d9-7bf773872a68/scratchpad/dropbear/`

One change was made to the dev VM to enable measurement: `sudo apt-get install
gcc-x86-64-linux-gnu libc6-dev-amd64-cross`. Reversible with `apt remove`.

---

## The binding constraint

**It is not the toolchain, and it is not static linking. Both of those turned
out to be easy, and I have working binaries to prove it. The binding constraint
is that the risk this exercise was built to test does not exist.**

asyncssh 2.24 still ships `diffie-hellman-group14-sha1`, `ssh-rsa` (SHA-1 RSA
signatures) and `hmac-sha1` **in its default, out-of-the-box algorithm lists**.
I built dropbear **2012.55** — the last release before dropbear had any elliptic
curve support at all, whose entire offer is `diffie-hellman-group{1,14}-sha1`
for kex, `ssh-rsa`/`ssh-dss` for host keys, and `hmac-sha1{,-96}`/`hmac-md5` for
MAC — pointed otto's own `SshOptions` at it with **nothing configured but the
port**, and got a working authenticated session with an exec channel, a pty and
20 concurrent channels.

```
otto SshOptions kwargs: {'port': 22229, 'known_hosts': None}
CONNECTED
  send cipher: aes256-ctr
  send mac   : hmac-sha1
  recv cipher: aes256-ctr
  recv mac   : hmac-sha1
  exec rc    : 0
  exec out   : hello-from-dropbear | uid=0(root) gid=0(root) ...
  pty out    : echo PTY-OK | exit | # PTY-OK | #
  sftp       : SFTPConnectionLost: Channel not open for sending
  20 concurrent channels: 20
```

Negotiated: `diffie-hellman-group14-sha1` / `ssh-rsa` / `aes256-ctr` /
`hmac-sha1` against `SSH-2.0-dropbear_2012.55`.

So the design spec's sentence —

> Old dropbear negotiates only SHA-1-era algorithms that modern asyncssh
> disables by default […] otto's `ssh_options` already carries cipher/host-key/
> kex lists, so this is configuration rather than code

— is now **0 for 2** on its own terms, not merely unverified. asyncssh does not
disable those algorithms by default (measured), and `ssh_options` does not carry
a kex list (it carries `encryption_algs` and `server_host_key_algs` only;
`kex_algs`/`mac_algs`/`signature_algs` are reachable only through the
`extra` passthrough — `src/otto/host/options.py:73-190`). The first error made
the second one moot: no kex list was ever needed.

**That is the whole answer to the question the retired Tier 3 was built for, and
I got it on loopback in an afternoon without going near the bed.** Whatever case
remains for putting dropbear on a guest, it is no longer "we have never tested
SHA-1-era crypto" — we have now, and it works with zero configuration.

The table from `todo/busybox-tier3-fidelity-2026-08-13.md`, re-scored:

| original claim | verdict now |
| --- | --- |
| ships no `sftp-server` | **TRUE of a source build.** 2012.55 built from source returned `SFTPConnectionLost: Channel not open for sending`. The earlier FALSE was an artifact of *Debian's packaging* of 2022.83, not of dropbear. |
| channel limits differ from `MaxSessions` | **TRUE**, unchanged. 20 concurrent `conn.run()` channels all succeeded on 2012.55. |
| SHA-1-era crypto needs `ssh_options` | **FALSE.** otto's stock defaults negotiate it. Nothing to configure. |

---

## Q3 (taken first, because it decides everything) — does asyncssh still do SHA-1-era crypto?

**Yes, and mostly by default. Nothing has been removed.**

asyncssh pinned at `2.24.0` (`uv.lock:123`, floor `asyncssh>=2.22.0` at
`pyproject.toml:41`); the venv has exactly 2.24.0.

Enumerated from the installed package's own registries:

| algorithm | implemented? | in asyncssh's **default** client list? | reachable how |
| --- | --- | --- | --- |
| `diffie-hellman-group14-sha1` | yes | **YES — default ON** | nothing to do |
| `diffie-hellman-group1-sha1` | yes | no (registered `default=False`, `kex_dh.py:822`) | `ssh_options.extra.kex_algs` |
| `diffie-hellman-group-exchange-sha1` | yes | no | `extra.kex_algs` |
| `ssh-rsa` (SHA-1 signature) | yes | **YES — default ON** (`rsa.py:314`, `register_public_key_alg(b'ssh-rsa', RSAKey, True)`) | nothing to do |
| `ssh-dss` | yes | no (`dsa.py:252`, `default=False`) | `ssh_options.server_host_key_algs` (a curated field) |
| `hmac-sha1` | yes | **YES — default ON** (`mac.py:197`) | nothing to do |
| `hmac-sha1-96`, `hmac-md5` | yes | no | `extra.mac_algs` |
| `aes128-cbc`, `3des-cbc`, `blowfish-cbc`, `arcfour*` | yes | no | `ssh_options.encryption_algs` (curated) |

Full default client offer, captured live from a real handshake
(`scratchpad/dropbear/probe.log:12-17`), includes `diffie-hellman-group14-sha1`
in kex, `ssh-rsa` in host keys and `hmac-sha1` in MACs.

**Nothing is removed outright.** There is no algorithm an old dropbear needs
that asyncssh cannot produce. So this never becomes a product finding on the
"asyncssh dropped it" axis.

Two smaller product notes that *are* real:

1. **`ssh_options` has no curated `kex_algs`/`mac_algs`/`signature_algs`
   field.** The design doc's "already carries cipher/host-key/kex lists" is
   wrong about kex. The `extra` dict does reach them
   (`options.py:178`, `kw.update(self.extra)`, tested at
   `tests/unit/host/test_options.py:57-66`), so it is expressible — as
   `ssh_options.extra.kex_algs`, not as a first-class key. Whether that is worth
   promoting to a curated field is a separate, small question, and this
   investigation produces **no evidence that anyone needs it**: the default list
   sufficed against the oldest dropbear that exists.
2. **otto's source, tests and docs contain zero occurrences of any SHA-1-era
   algorithm name** (`ssh-rsa`, `hmac-sha1`, `diffie-hellman-group1*`,
   `ssh-dss`, `3des-cbc`, `aes128-cbc`, `rsa-sha2*`). Grepped whole tree
   excluding `.venv`. Nothing to migrate; nothing pinned that could rot.

---

## Q1 — architecture: how does an x86 dropbear come to exist?

**Answer: you cross-compile it. There is no prebuilt to pin, and none is
needed.**

Assessed, with evidence:

- **Upstream publishes source only.** `https://matt.ucc.asn.au/dropbear/releases/`
  carries `.tar.bz2`/`.tar.gz` and nothing else. There is no
  binary-artifact analogue of `busybox.net/downloads/binaries/`, so the bed's
  pin-and-verify-a-prebuilt model **cannot** be reused verbatim. What you would
  pin is a source tarball.
  - Modern tarballs are PGP-signed (`dropbear-2026.94.tar.bz2.asc` exists).
    **2012.55 has no `.asc` and no `.sha256`** — checked the directory listing
    directly. So a 2012-era pin is trust-on-first-use, exactly like the BusyBox
    prebuilts, with the meaningful improvement that *you* compile the executed
    bytes rather than executing someone else's.
- **apt cross toolchain on arm64: available, and it works.**
  `gcc-x86-64-linux-gnu 4:13.2.0-7ubuntu1` (noble/main/arm64) and
  `libc6-dev-amd64-cross 2.39-0ubuntu8cross1` (noble/universe/arm64). Installed
  and used for the build below.
  - **One gap:** neither `libcrypt-dev` nor any `libcrypt*-amd64-cross` package
    exists on ports.ubuntu.com. Modern glibc no longer provides `crypt()` — it
    moved to libxcrypt — so the daemon fails to link with
    `undefined reference to 'crypt'` until you supply an amd64
    `libcrypt.a` yourself. I got one by fetching the amd64
    `libcrypt-dev_4.4.36-4build1_amd64.deb` from archive.ubuntu.com and
    `dpkg-deb -x`-ing it into a scratch sysroot. Workable, but it is a second
    pinned third-party binary artifact in the chain, and worth naming.
- **qemu-user-static: already present** (`1:8.2.2+ds-0ubuntu1.18`, arm64) with
  `qemu-x86_64` and `qemu-i386` binfmt handlers registered. It is what let me
  *run* the x86_64 binary on the arm64 dev VM for every measurement here. It is
  not needed to build.
- **i686 vs x86_64 is a non-issue.** The bed boots one Ubuntu **amd64** kernel
  for all five guests (`docs/architecture/subsystems/busybox-bed.md`: "one kernel
  serves the i686 userlands too, via IA32 emulation"). So a single **x86_64**
  static dropbear runs on any of the five, including the two i686-userland
  guests. No second build.

**Verdict: not the binding constraint. Cross-building is straightforward on the
dev VM as it stands today.**

---

## Q2 — is static linking mandatory?

**Confirmed. Your reading is correct.**

`scripts/build_busybox_guest_images.py::cpio_newc_entries` writes exactly this
member list and nothing else:

- directories `bin dev dev/pts etc etc/init.d lib lib/modules proc root sys tmp`
- `bin/busybox` (the pinned artifact)
- `init` → symlink to `bin/busybox`
- `dev/console`, `dev/null` (character devices, encoded by hand)
- `etc/inittab`, `etc/init.d/rcS`, `etc/passwd`, `etc/shadow`, `etc/group`
- `lib/modules/e1000.ko`

**No libc, no `ld-linux-x86-64.so.2`, no `/lib/x86_64-linux-gnu`, no `/usr`.**
`lib/` exists solely to hold `modules/e1000.ko`. A dynamically-linked binary
would fail at exec with ENOENT on its interpreter. Static is the only option
unless you also start shipping a loader and libc, which would end the property
that the guest's userland *is* the pinned artifact.

Static-glibc traps I checked, because they are the usual ones:

- The link emits five `Using 'getpwnam'/'getspnam'/'initgroups'/'getaddrinfo'
  in statically linked applications requires at runtime the shared libraries
  from the glibc version used for linking` warnings. **These are obsolete for
  the files-based lookups dropbear needs**: glibc merged `nss_files` into libc
  proper at 2.34, and the cross libc here is 2.39. Measured, not assumed —
  `dropbearkey` printed `vagrant@otto` (a successful `getpwuid()` from a static
  binary), and password auth against `/etc/shadow` succeeded end to end.
- `crypt()` against the bed's baked `$1$bb$…` MD5-crypt hash works: libxcrypt
  4.4.36 still supports `$1$`, and the bed's hash is exactly what I
  authenticated with.
- `config.h` from a cross-configure sets `HAVE_OPENPTY 1` and leaves
  `USE_DEV_PTMX` undefined, so pty allocation goes through glibc `openpty()` →
  `/dev/ptmx` + `/dev/pts`. The guest's `rcS` already mounts devtmpfs and
  devpts, so this is satisfied.
- The pid file (`/var/run/dropbear.pid`, no `/var` in the image) is **not**
  fatal — `svr-main.c:160` does `fopen(...)` then `if (pidfile)`.

---

## Q4 — which version, and does it build?

### Version: the todo doc's suggestion would have produced a false green

`todo/busybox-tier3-fidelity-2026-08-13.md` proposes "the 2013-era range
matching the BusyBox artifact matrix". **That range would not have tested
anything.** ECC landed in dropbear 2013.56, and by 2013.62 `options.h` ships
`DROPBEAR_ECDSA`, `DROPBEAR_ECDH` and `DROPBEAR_CURVE25519` all enabled by
default. A 2013.62 server offers `curve25519-sha256@libssh.org` and
`ecdsa-sha2-nistp*`, asyncssh prefers those, and you would have exercised
**exactly the same modern path as 2022.83** — the same non-finding, from an
older binary. That is the shape of green that reads like a result and is not
one.

**The last genuinely SHA-1-only release is 2012.55.** Its complete offer, read
from `common-algo.c` and then confirmed on the wire:

```
kex     : diffie-hellman-group1-sha1, diffie-hellman-group14-sha1
hostkey : ssh-rsa                     (ssh-dss also compiled in)
cipher  : aes128-ctr 3des-ctr aes256-ctr aes128-cbc 3des-cbc aes256-cbc
          twofish256-cbc twofish-cbc twofish128-cbc
mac     : hmac-sha1-96, hmac-sha1, hmac-md5     (no SHA-2 at all)
```

If this work ever happens, **2012.55 or 2011.54 is the only defensible pick.**

### The build: it works, with two real speed bumps

Both are the kind of thing "builds cleanly on modern toolchains" hand-waves past.

1. **`configure: error: cannot guess build type; you must specify one.`**
   The bundled 2012 `config.guess` predates aarch64. Fixed by passing
   `--build=aarch64-unknown-linux-gnu` explicitly. Nothing to patch.
2. **`undefined reference to 'crypt'`** at link. glibc dropped `crypt()`;
   no cross libxcrypt in apt. Fixed by extracting `libcrypt.a` from the amd64
   `.deb` (Q1).

After that, `make PROGRAMS="dropbear dropbearkey" STATIC=1` succeeds with
**five compiler warnings and zero errors** on gcc 13.3.0. Old C against a modern
toolchain turned out fine here — dropbear really is small and clean.

```
dropbear:    ELF 64-bit LSB executable, x86-64, statically linked   1,512,992 B stripped
dropbearkey: ELF 64-bit LSB executable, x86-64, statically linked   1,126,912 B stripped
sha256(dropbear-2012.55.tar.bz2) = 04982af2a10b220fa940f9f72f276d612c9bb643cfbb5ee1416e5a0f00de9b0f
```

Exact reproduction:

```console
$ ./configure --build=aarch64-unknown-linux-gnu --host=x86_64-linux-gnu \
      --disable-zlib --disable-syslog --disable-lastlog \
      --disable-utmp --disable-utmpx --disable-wtmp --disable-wtmpx \
      --disable-pututline --disable-pututxline CC=x86_64-linux-gnu-gcc
$ make PROGRAMS="dropbear dropbearkey" STATIC=1 \
      LDFLAGS="-static -L<sysroot>/usr/lib/x86_64-linux-gnu" \
      LIBS="libtomcrypt/libtomcrypt.a libtommath/libtommath.a -lcrypt"
```

**Verdict: not the binding constraint either.**

---

## Q5 — host keys, and a correction to the record

### The `rsa.c:164` measurement in the todo doc is misattributed

The doc says:

> asyncssh 2.24 dies on `Failed assertion (rsa.c:164): key != NULL` against an
> ed25519-only host key while `ssh(1)` stays green

**asyncssh does not die. Dropbear does.** `rsa.c` is dropbear's file, and line
164 of `dropbear-2022.83/rsa.c` is precisely `dropbear_assert(key != NULL);`
inside `buf_put_rsa_pub_key`. I reproduced it against the dev VM's installed
`dropbear-bin 2022.83-4`:

```
[246021] Exit before auth from <127.0.0.1:55722>: Failed assertion (rsa.c:164): `key != NULL'
```

asyncssh's side reports only `ConnectionLost: Connection lost`. The message that
was recorded came from the **server's** log.

### The root cause, and it is a since-fixed upstream dropbear bug

`svr-runopts.c:508` in 2022.83:

```c
static void disablekey(int type) {
    for (i = 0; sigalgs[i].name != NULL; i++)
        if (sigalgs[i].val == type) { sigalgs[i].usable = 0; break; }
}
```

called at line 627 as `disablekey(DROPBEAR_SIGNKEY_RSA)` when no RSA key loaded.
But `sigalgs[]` stores `DROPBEAR_SIGNATURE_*` values, and `signkey.h` aliases
them to `DROPBEAR_SIGNKEY_*` for **every** type except RSA:

```c
DROPBEAR_SIGNATURE_ED25519 = DROPBEAR_SIGNKEY_ED25519,
DROPBEAR_SIGNATURE_DSS     = DROPBEAR_SIGNKEY_DSS,
DROPBEAR_SIGNATURE_RSA_SHA1   = 100,   /* deliberately NOT aliased */
DROPBEAR_SIGNATURE_RSA_SHA256 = 101,
```

So the RSA disable silently matches nothing, dropbear advertises
`rsa-sha2-256,ssh-rsa` it cannot serve, and any client preferring RSA aborts it.
OpenSSH's `ssh(1)` prefers ed25519 first and never trips it; asyncssh's default
order starts `rsa-sha2-256`, so it trips every time. **`ssh(1)` staying green was
luck of list ordering, not correctness.**

Upstream fixed this: `dropbear-2026.94/src/svr-runopts.c` takes
`enum signature_type` and calls `disablekey(DROPBEAR_SIGNATURE_RSA_SHA256)` and
`disablekey(DROPBEAR_SIGNATURE_RSA_SHA1)` separately.

**Consequence: "an RSA host key is mandatory" was never an otto or asyncssh
constraint. It was a bug in Debian's dropbear 2022.83.**

### Old dropbear handles this correctly

2012.55 with a DSS key and no RSA key logs
`Exit before auth: No matching algo hostkey` — a clean refusal, not an abort. The
old server is better-behaved than the modern one here.

### What key types 2012.55 can serve, measured against asyncssh 2.24 defaults

| host key | generated by `dropbearkey` | asyncssh 2.24 accepts |
| --- | --- | --- |
| RSA 2048 | yes (`-t rsa -s 2048`) | **yes**, negotiated `ssh-rsa` |
| RSA 1024 (dropbearkey's **default**) | yes | **yes** — no minimum-size rejection |
| DSS 1024 (fixed size) | yes | **yes**, with `ssh_options.server_host_key_algs = ["ssh-dss"]` (a curated field) |
| ecdsa / ed25519 | **no** — not in 2012.55 | n/a |

The RSA-1024 result matters: a genuinely old device would have a 1024-bit key,
and asyncssh takes it without complaint.

### Getting a key into an initramfs rebuilt from scratch every provision

Two shapes, both viable, with different consequences:

- **Baked at build time.** `build_initramfs_bytes` is deliberately
  byte-deterministic (`gzip.compress(..., mtime=0)`) and the builder only
  rebuilds an image whose sha256 changed. A baked key keeps that property and
  gives a **stable host key across restarts**, which is what a real device looks
  like and what `known_hosts` would want. Cost: a private key committed or
  generated-and-stored somewhere on test1, and a decision about whether it lives
  in the repo.
- **Generated at first boot into the tmpfs rootfs.** Costs nothing at rest, but
  the key **changes on every boot** — and the bed's `Restart=always` plus
  `-no-reboot`/`panic=-1` means reboots are routine. Anything asserting host-key
  identity would churn. otto's default `known_hosts=None`
  (`src/otto/host/options.py`) means it would not *break*, but it also means
  the host-key path is never really exercised, which is half the reason to have
  an sshd.
  - Keygen cost is **not** a blocker: 0.06 s (1024-bit) and 0.16-0.38 s
    (2048-bit) measured under qemu **user-mode**. Full-system TCG is slower, but
    not by the order of magnitude that would matter.

---

## Q6 — blast radius on the existing bed

Note: this worktree (`worktree-busybox-real-nic`, HEAD `ee615f6d`) has already
moved the bed off QEMU slirp/hostfwd onto **real TAP NICs with a /30 per guest**
(`scripts/build_busybox_guest_images.py:88-93`; TEST-NET-2 `198.51.100.0/24`,
`guest = 4n+1`, `host = 4n+2`). That changes the cost of a sixth guest
substantially — see below.

### What the ssh-absence is load-bearing for

`docs/superpowers/specs/2026-08-20-host-probe-protocol-survey-design.md:172-180`:

> The five per-milestone-version BusyBox QEMU guests (… `term = telnet`,
> transfers `shell`/`nc`, ssh dead by construction) … **their permanently dead,
> undeclared ssh gives the drift table a standing true-negative.**

and `:227-229` plans the e2e suite around "**the permanent ssh true-negative in
the drift table**". The bed spec says the same
(`2026-08-20-busybox-bed-and-tier-migration-design.md:80-81`, and `:194` lists
"ssh/dropbear on the guests" as explicitly **out of scope**).

### What breaks, concretely

1. **`tests/integration/busybox_bed/test_guest_smoke.py:68-76`,
   `test_ssh_is_dead_by_construction`, fails immediately** — it runs
   `command -v sshd || echo no-sshd` on each guest and asserts `no-sshd`.
2. **The drift-table fixture degrades from "five guests, one permanent
   true-negative" to "four".** If the lab entry is *not* updated, the converted
   guest becomes a live **working-but-undeclared** drift row — the exact
   opposite of the fixture. If it *is* updated, the true-negative is gone by
   construction.
3. **A silent protocol flip.** `valid_terms` order is load-bearing:
   `CapabilityResolver.resolve_active` (`src/otto/host/capability.py:28-50`)
   falls through to `menu[0]`, and the guests pin no `term`. Writing
   `["ssh", "telnet"]` would silently switch **every** otto session on that
   guest from telnet to ssh. `["telnet", "ssh"]` avoids it. This is pinned today
   by `tests/unit/host/test_busybox_bed_lab_entries.py:42`
   (`host.term == "telnet"`).
4. **Docs go stale**: `docs/architecture/subsystems/busybox-bed.md:186-190`
   ("These guests run no ssh daemon").
5. **`scripts/lab_health.py` would be silently blind to it.** `_is_ssh_host`
   (lines 286-305) routes by *shape* — `"creds" in host and not host.get("hop")`
   — so a hop-fronted guest always goes down the telnet-console probe path. An
   sshd on a guest would never be health-checked unless that is changed
   deliberately.
6. **The matrix stops being a matrix.** The five versions exist to be compared
   against each other; converting one makes that one non-comparable to its four
   peers on every ssh-adjacent question. This is the cost the task statement
   already anticipated, and I think it is the real one.

### If it happened anyway: which guest?

**1.35.0 / `bb1350`.** It is already the chaos-suite element
(`tests/e2e/chaos/_bed.py:186`), its userland is the most capable, and it is the
guest whose "old BusyBox" fidelity value is lowest — so it is the cheapest of the
five to make non-comparable. It is also the guest whose `_bed.py:232-239`
docstring currently explains that the asyncssh oracle "CANNOT be used here — the
guest has no sshd at all, by construction", so the contradiction would at least
be in one obvious place.

### A sixth guest instead

Materially cheaper than it used to be, and I recommend it over converting one if
this is done at all:

- **Addressing: free.** Next /30 is `198.51.100.20/30` — guest `.21`, TAP peer
  `.22`, name `bbeth-<version>`. No port allocation at all under the TAP scheme
  (the old 2316/2321/… + 9160-9359 hostfwd windows are gone). No ssh port
  collision is even possible: it would bind `:22` on its own address.
- **Tooling: free.** `scripts/lab_health.py` restarts by glob
  (`'busybox-qemu-*.service'`) and enumerates guests from lab data. Zero changes
  for a sixth unit.
- **Memory: the real cost, and it is not measured anywhere.** test1 is
  `vb.memory = 2560` (Vagrantfile:516, overriding the 1552 default), with the
  comment that TCG plus five resident guests need the headroom. Five guests
  declare `5 x 96 = 480 MB`; the remaining ~1008 MB above the 1552 baseline
  covers all five qemu processes' own overhead. A sixth adds 96 MB declared plus
  its own TCG/translation-cache overhead. **Nothing in the repo pins this
  arithmetic** — it is inferred from that comment, not measured — so a sixth
  guest plausibly wants another explicit `vb.memory` bump, and that is a
  provisioning change on Chris's lab.
- **Test wiring: two places fail loudly, by design.**
  `tests/unit/host/test_busybox_bed_lab_entries.py:103-135` asserts
  `_BUSYBOX_BACKEND_NE` equals the lab.json roster exactly (its docstring
  explicitly discusses the sixth-guest case), and
  `tests/unit/test_lab_data_hops.py:38-42` pins the five-element tuple. Both are
  guards working as intended, not obstacles.
- **CPU is the quiet one.** All guests share test1's two cores under TCG with no
  KVM — which is why everything joins one `busybox_bed` xdist group. A sixth
  emulated guest is a sixth consumer of those two cores.

---

## Q7 — resources

Measured with the repo's own `cpio_newc`/`gzip` path, real busybox artifacts
from `~/.cache/otto/busybox`, and a 190 KB incompressible stand-in for
`e1000.ko` (I cannot reach test1 to get the real module):

| | 1.35.0 (x86_64) | 1.16.1 (i686) |
| --- | --- | --- |
| initramfs today, gz | 894,674 B | 744,248 B |
| + `dropbear` + host key, gz | 1,576,472 B (+681,798) | 1,426,599 B (+682,351) |
| + `dropbearkey` too, gz | 2,083,758 B (+1,189,084) | 1,933,889 B (+1,189,641) |
| uncompressed cpio today | 1,329,224 B | 1,096,632 B |
| uncompressed cpio + both | 3,970,328 B | 3,737,736 B |

Reading it:

- **RAM is the number that matters, and it is fine.** An initramfs *is* the
  rootfs: the uncompressed content is resident tmpfs. Going from ~1.3 MB to
  ~4.0 MB spends **~2.7 MB of 96 MB (≈2.8%)**. Ship only `dropbear` and a baked
  key and it is **~1.4 MB (≈1.5%)**.
- **`dropbearkey` costs more than the daemon.** 1.13 MB stripped, and it is only
  needed if keys are generated in-guest. Baking the key at build time halves the
  size delta and removes the binary entirely.
- **Boot time is unaffected in any way I can measure from here.** The gz roughly
  doubles, but decompressing ~2.6 MB more cpio is not a meaningful cost even
  under TCG.
- **Runtime footprint**: the daemon's text pages come from the tmpfs image
  already counted; each session forks a child (single-digit MB RSS). Not a
  concern at 96 MB.

---

## What I could NOT measure, and why

- **Anything on a real guest.** No lab VM was touched, per instruction. Every
  result above is loopback on the dev VM, with the x86_64 binary running under
  `qemu-user-static` on aarch64 rather than natively on an x86_64 kernel. The
  binary is genuinely x86_64 and statically linked, but *system*-mode TCG inside
  a 96 MB guest is a different environment. Untested there: real boot,
  `openpty()` against the guest's devpts, `/dev/urandom` entropy at early boot,
  and whether BusyBox `init`/`inittab` supervises dropbear cleanly alongside
  `telnetd`.
- **The real `e1000.ko` size.** It lives on test1. I used 190 KB of random bytes;
  the real module compresses better, so the "today" baselines above are slightly
  pessimistic and the *deltas* — the numbers that matter — are unaffected.
- **test1's actual memory headroom.** The sixth-guest arithmetic is inferred
  from the Vagrantfile comment. Nothing measures it, and I could not run
  `free`/`ps` on the hop.
- **The authenticated-session test used a one-line patched binary.** Dropbear's
  `svr-chansession.c:892` `if (getuid() == 0)` setgid/initgroups block was
  changed to `if (0)`, because `setgroups(2)` is unconditionally denied in an
  unprivileged user namespace and I would not run a root sshd on the shared dev
  VM. The patch touches **privilege dropping only** — no protocol, crypto or
  channel code. The *unpatched* binary reached `CONNECTED` and authenticated
  successfully in the same sandbox; only the post-auth child exec failed, with
  `Error changing user group`. So the crypto and auth results stand on the stock
  binary; only the exec/pty/sftp/20-channel line used the patched one.
- **Interactions with a real network path** (MTU, latency, window) — the
  original argument for todo item B. The bed's move to TAP /30s means a guest
  sshd *would* now be on a real path, which is the one genuinely new thing a
  guest would add over loopback.
- **Whether GitHub runners need `kernel.apparmor_restrict_unprivileged_userns=0`.**
  Still open, still Tier 2's question first.

---

## Options

### Option A — do not do it. Write down what was measured and close the item.

The named risk has been answered: otto talks to the oldest dropbear in
existence with its stock configuration, over an exec channel, a pty and 20
concurrent channels. Cost: a paragraph in the gap registry saying so, plus
correcting two errors now on the record (the `rsa.c:164` misattribution, and
"asyncssh disables SHA-1-era algorithms by default"). Keeps the five-guest
matrix comparable and keeps the drift-table true-negative that two specs are
built on.

What you give up: nothing measured. The residual unknown is "old dropbear over a
real network path", which is a thin slice given telnet already crosses that path
on all five guests and the transfer backends are shared.

**This is where the evidence points.**

### Option B — a sixth guest, `bb-dropbear`, running BusyBox 1.21.1 + dropbear 2012.55

If the point is fidelity to a real 2012-era device — old userland *and* old sshd
on one box, on a real NIC — this is the honest shape. `198.51.100.20/30`, its own
unit, `lab_health.py` picks it up by glob, and the two roster guards fail loudly
until the lab data is updated. Preserves all five existing guests' comparability
and the ssh true-negative (five of six guests still have no sshd; the drift-table
fixture is stated per-guest, not per-bed).

Costs, honestly: another emulated guest on two shared TCG cores; a probable
`vb.memory` bump on test1 that nothing currently measures; a cross-toolchain +
libxcrypt-extraction step in the provisioner that has no precedent in this repo
(the bed has never compiled anything); a second source-pin with no upstream
signature for 2012.55; and a decision about where a host private key lives.

And be clear-eyed about the return: **the crypto question is already answered.**
This buys the real-network-path dimension and a live old-sshd target for the
probe survey, not the SHA-1 finding.

### Option C — keep it on loopback, as a hermetic tier, and pin what was measured

Build 2012.55 in CI (or pin a prebuilt the repo produces itself), run the phase-5
style harness against it, and assert the *specific* negotiated tuple —
`diffie-hellman-group14-sha1` / `ssh-rsa` / `hmac-sha1` — reached with otto's
default `SshOptions`. That converts today's one-off measurement into a
regression guard: if a future asyncssh finally drops `group14-sha1` or `ssh-rsa`
from its defaults, this reds and the gap registry stops lying. Hermetic, runs on
any machine via qemu-user, touches the bed not at all.

Costs: a compile step in CI, and a second unsigned source pin. The guard has real
value precisely because asyncssh's defaults are the thing most likely to move
out from under otto — but note it guards *asyncssh's* behaviour, not otto's, and
that is a fair question to ask of it.

---

## Corrections this investigation puts on the record

Independent of what gets built, three things in the repo are now known to be wrong:

1. `docs/superpowers/specs/2026-08-11-busybox-host-support-design.md:224-228` —
   "SHA-1-era algorithms that modern asyncssh disables by default" is **false**
   for asyncssh 2.24. `group14-sha1`, `ssh-rsa` and `hmac-sha1` are all default-on.
2. The same sentence's "`ssh_options` already carries cipher/host-key/**kex**
   lists" is **false**. There is no `kex_algs` field; `extra` reaches it.
3. `todo/busybox-tier3-fidelity-2026-08-13.md:57-61` attributes
   `Failed assertion (rsa.c:164): key != NULL` to asyncssh. It is **dropbear
   2022.83's own abort**, from a since-fixed upstream bug in `disablekey()`. The
   conclusion drawn from it — "an RSA host key is mandatory" — does not hold for
   the reason given.
