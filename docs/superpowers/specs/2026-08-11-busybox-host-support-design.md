# BusyBox host support, round 1: design

**Date:** 2026-08-11
**Status:** approved design, not yet implemented
**Supersedes the "Approach" half of:** `todo/busybox-host-family-2026-08-10.md` (its
"Findings" half remains the measured record and is cited throughout)

## The problem

otto's Unix interfaces assume a GNU/coreutils userland with bash. BusyBox
diverges in enough places that a BusyBox target is plausibly its own host
family. Nothing in any test lane exercises an ash shell or a BusyBox applet
today: the bed is four Ubuntu VMs and a Zephyr board.

Two properties of BusyBox drive every decision below.

**Behaviour is a function of version AND build config.** Applets can be
compiled out entirely, and `sh` may be `ash` or `hush` depending on how the
image was built. So a declared version number is never sufficient evidence for
a behavioural decision.

**The deviations are concentrated, not diffuse.** A survey of the unix path
finds `bash` (11 sites in `daemon.py`, 8 in `unix_host.py`), `sudo`
(`privilege.py`, `unix_host.py`, `daemon.py`), `base64` (8 in `file_ops.py`),
`timeout`, and `reboot`/`shutdown`. That is a short list, and it is why this is
a seam-population job rather than a new architecture.

## Scope

**Round 1 supports a declared subset of otto's surface on a *stock* BusyBox
userland** — nothing installed alongside. Full parity is the goal but a later
workstream. Surfaces outside the subset are documented as holes; landmines
found along the way are queued for the parity sweep.

In scope: `login`, `run`, `exec`, `open_session`, `send`/`expect`, `close`,
`is_reachable`, `wait_until_up`/`wait_until_down`, history suppression, and one
`get`/`put` pair.

Out of scope for round 1, and therefore documented holes: `daemon` (hard-wired
`bash -c`), product `install`/`stage`/`uninstall`, `reboot`/`shutdown`, the
existing `nc` transfer backend, monitor collectors, and coverage/toolchain.

## 1. Representation

### JSON

A BusyBox host is an ordinary unix host with a profile selector:

```json
{
  "ip": "10.10.200.20",
  "element": "gateway",
  "os_type": "busybox",
  "term": "ssh",
  "transfer": "shell",
  "userland_options": { "elevation": "su", "version": "1.28.1" }
}
```

`os_type: "busybox"` resolves an `OsProfile(base="unix", defaults={...})`
carrying `command_frame`, `valid_transfers`, and any `userland_options`
defaults. `OsProfile.defaults` already holds "raw values exactly as a
`lab.json` entry" including `*_options` tables, merged beneath the host's own
fields, so this needs no new merging machinery.

Every `userland_options` key defaults to `"auto"`, so the minimal entry is just
`os_type`, and any single field can be pinned inline without abandoning the
profile.

### Objects

No new host class in round 1. Three pieces, mirroring the `NcOptions` pattern:

| piece | location | role |
| --- | --- | --- |
| `UserlandOptions` | `src/otto/host/options.py` | declared answers, all defaulting to `"auto"`/`None` |
| `UserlandOptionsSpec` | `src/otto/models/options.py` | lab-data boundary, with `to_runtime()` |
| `Userland` | `src/otto/host/userland.py` (new) | resolved answers: probe once, cache, degrade on failure |

`os_type: "busybox"` therefore needs **no subclass** — `OS_PROFILES` is already
a pure data bundle over a base family, registerable from `settings.toml` or
code. If a method later genuinely needs overriding, a thin `BusyBoxHost` can be
registered under the same `os_type` without touching any lab data.

**The names are deliberately not `busybox_*`.** The `timeout`-convention probe
that motivated this work already rescues a non-BusyBox class (Alpine <= 3.8,
OpenWrt <= 18.06, which pair an old BusyBox userland with a real netcat).
Scoping these capabilities to a BusyBox type would wrongly exclude the hosts
that already benefit.

### What is data and what is probed

**The rule: if a human can know it from the device's identity, it is profile
data; if only the device can tell you, it is a probe.**

`version` is profile data — documentation and profile selection only. It never
gates behaviour, because build config can contradict it.

Round-1 probe set, each with a real call site in the subset:

| capability | answers | consulted by |
| --- | --- | --- |
| `shell_dialect` | a registered `CommandFrame` `type_name` | `CommandFrame` selection |
| `elevation` | `sudo` \| `su` \| `none` | `PosixPrivilege._elevate` |
| `base64` | `absent` \| the working decode spelling (`-d` \| `--decode`) | the `shell` transfer |
| `stat_size` | `stat -c %s` \| `wc -c` \| `absent` | transfer sizing |
| `timeout_style` | `coreutils` \| `dash-t` \| `absent` | the `nc` listener cap |

`shell_dialect` resolves to a **registered frame name**, not a shell name, so the
answer is directly usable and the set stays open to third-party frames. It must
probe **behaviour, not a name**: BusyBox ships two shells (`ash` and `hush`) and
`sh` may be either. `hush` resolves to the `ash` frame until a measurement shows
it needs its own — recorded as a gap if so, rather than pre-empted here.

`timeout_style` is **moved here from `transfer/nc.py`**. This is a contained
refactor, not scope creep: leaving one probe inside a transfer backend while
the next five live elsewhere recreates precisely the divergence this design
exists to prevent. `nc.py` keeps its behaviour and consults `Userland`.

`PosixPrivilege._elevate` stops hard-coding `sudo -S -p` and consults
`Userland.elevation`. This affects **all** unix hosts, not only BusyBox ones —
they resolve an elevation strategy instead of assuming sudo. That is the
intended outcome: a host without sudo currently produces `sudo: not found`
rather than anything actionable.

### Probe visibility

Probes are internal machinery and must not appear in user-facing output.

- **Probe traffic** (the commands and their output) runs with `LogMode.NEVER` —
  "redacted from every sink at every level, including session diagnostics".
- **Probe results** are emitted with `logger.debug`, which `LogMode` explicitly
  does not govern ("`LogMode` governs command I/O only"). `verbose.log`'s floor
  is INFO, DEBUG only under `--log-level DEBUG`.

So probes are invisible in normal operation and discoverable on demand. The
debug output is **paste-ready**: one line per probe result, plus a summary line
containing the exact `userland_options` JSON to copy into `lab.json` to skip
the probes on subsequent runs.

## 2. The `ash` dialect

Register `ash` in `FRAME_CLASSES` as a `BashFrame` subclass that overrides
**only what measurement shows differs**.

This is a commitment to the seam, not to a rewrite. `BashFrame`'s marker scheme
(`echo` brackets, `$?` baked into the END marker) uses only constructs ash
supports, and no measurement yet shows it failing there. Round 1 measures the
delta by running the frame's own markers under real BusyBox shells across the
version matrix; whatever comes back red becomes the override. **If nothing does,
the class stays empty and that is a finding worth recording** — it would mean
the dialect seam is cheaper than assumed.

The one known difference is history handling, and `_history_prefix()` is
already a hook on `PosixPrivilege`.

## 3. The `shell` transfer backend

`ShellFileTransfer`, registered as `"shell"` with `host_families={"unix"}`. The
menu validator resolves `valid_transfers` against `TRANSFER_BACKENDS` and the
backend's `host_families`, so this needs no validator change.

- **PUT** — chunk the file, base64 each chunk, `printf %s '<b64>' | base64 -d >>
  tmp`, then one `mv` into place. Temp-then-move is load-bearing: a failed
  transfer must not leave a truncated file where the real one was.
- **GET** — `dd bs=<n> skip=<k> count=1 | base64` per chunk, reassembled
  locally.
- **Integrity** — `md5sum` on both sides when probed present; size comparison
  otherwise.
- **Chunk size** — measured, not guessed. `ARG_MAX` is 2 MB on the dev VM, but
  telnet line handling and the command frame's per-line processing are the real
  constraints. Start conservative and pin the chosen value with a comment
  recording what bounded it.

Shape precedent: `transfer/console.py` already does chunked-over-shell transfer
for Zephyr. It is not directly reusable (it speaks the Zephyr `fs` shell in
32-byte hex chunks) but it establishes the pattern.

**Beyond BusyBox this earns otto something it lacks**: a transfer needing no
ports and no listeners, so it works over telnet and through any hop without a
port forward.

The BusyBox `nc` variant (`-l -p PORT`, and size-terminated reads to replace
the missing `-N`) is explicitly **queued for parity**, not built here.

## 4. Holes and the parity queue

Three audiences need this information and drift apart if written three times.
One source of truth: **a declared gap registry** of
`Gap(surface, reason, measured_on, queued_for)` records in the userland module,
backing all three consumers:

1. **The runtime error** — a named `UnsupportedOnUserlandError` renders its
   message from the record (surface, why, docs anchor) instead of surfacing a
   bare `sudo: not found`.
2. **The user-facing docs page** — what works and what does not on a stock
   BusyBox host.
3. **The parity queue** — `todo/busybox-parity-sweep-2026-08-11.md`, consumed by
   the later full-parity workstream.

A test asserts the docs table matches the registry, so a gap cannot be closed
in code while the docs still claim it is open, or the reverse.

### When the error fires

**Measured-broken refuses up front; unmeasured runs.**

A surface measured broken on the matrix (`daemon`'s `bash -c` on a host with no
bash) raises the named error immediately rather than emitting a command
guaranteed to fail confusingly. A surface merely untested is **not** blocked: it
runs, and the outcome is recorded. Blocking untested surfaces would convert "we
do not know" into "does not work" — a lie in the expensive direction, making
otto refuse things that work.

### Known entries at design time

From the 2026-08-10 survey, not speculation: `daemon` (`bash -c`), product
`install`/`stage`, `reboot`/`shutdown` (sudo plus coreutils `shutdown -h now`),
the `nc` backend (`-l -p`, no `-N`), `pgrep`-based test hygiene, and
`sftp`/`scp` against dropbear.

### The dropbear risk

Real BusyBox devices run **dropbear**, not OpenSSH. Old dropbear negotiates only
SHA-1-era algorithms that modern asyncssh disables by default; it ships no
`sftp-server`; and its channel limits differ from OpenSSH's `MaxSessions`.
otto's `ssh_options` already carries cipher/host-key/kex lists, so this is
configuration rather than code — but it is untested, and it would bite
immediately on a genuinely old device. Tier 3 below addresses it directly.

## 5. Testing

### Artifacts

Source: `https://busybox.net/downloads/binaries/<version>/busybox-<arch>` — the
BusyBox project's own prebuilt binaries, over HTTPS from the canonical site.

**Upstream publishes no checksums and no signatures for the binaries** (source
tarballs ship `.sha256`; the prebuilts do not). Verification is therefore:

- **Primary gate: a behavioural assertion** — the version banner and the
  expected applet set. A failure here is a real finding about interface drift,
  which is what this fixture exists to detect. A byte-level check is not.
- **Secondary: a committed SHA-256**, trust-on-first-use. Its narrow but real
  value is that CI re-fetches on every cold cache; a pin converts per-run trust
  in busybox.net into one-time trust taken at a reviewed moment. A mismatch is
  **investigated, not rubber-stamped**; if upstream legitimately rebuilt, the
  pin is updated in a reviewed commit.

State plainly in the docs: this is the one unsigned executable otto runs in CI.
The mitigation is blast radius — it executes only inside a test tmp dir and, for
Tiers 2 and 3, inside an unprivileged user namespace.

Artifacts are fetched into a gitignored cache. A cold cache with no network
fails with a named error identifying the artifact and how to prime it. It does
not skip: a silent skip is how BusyBox coverage would quietly evaporate.

### Architecture

The dev VM is **aarch64**; CI (`ubuntu-latest`) is **x86_64**. No aarch64
BusyBox build is published for any version, and this VM's ARMv8 cores have no
AArch32 EL0, so 32-bit ARM binaries fail with `ENOEXEC` (measured, not assumed —
`CONFIG_COMPAT=y` is present and irrelevant).

Therefore: **always test the x86_64 artifact** — native in CI, under
`qemu-user-static` (binfmt, so no command-line prefix) on the dev VM. Beyond
portability this means dev and CI exercise **identical bytes**; native-per-arch
would silently test different builds. `qemu-user-static` becomes a documented
dev prerequisite.

This corrects two claims in the 2026-08-10 todo: it rejected qemu-user on the
grounds that "the published binaries are already x86_64", which is false for
every version before 1.28.1 and false for this machine's architecture entirely.

### Version matrix

Chosen at known behaviour transitions, not evenly spaced:

**1.16.1** (oldest obtainable), **1.21.1**, **1.28.1** (first x86_64 build),
**1.31.0** (post-`timeout`-convention change), **1.35.0**, plus the system
**1.36.1**.

**No version floor is enforced in code.** Probe-first means a 1.0 host either
works or fails loudly on a missing capability, so no version check belongs in
the product. Below 1.16.1 is **untested, not unsupported** — nothing is
published for 1.0-1.15, and building 2004-era C on a modern toolchain is a
porting project per version. Users are asked to report holes.

### Tier 1 — applet behaviour, no container

Symlink applets into `tmp_path` and drive them from **`/bin/sh` (dash), never
`busybox sh`**.

This is not a detail. **BusyBox's ash resolves applets internally and ignores
`PATH`**, so a PATH-based shim is unreachable from a BusyBox shell. This already
produced one false pass during the `timeout` work, where a control "verified"
broken code by silently exercising BusyBox's own builtin.

Proves: argument parsing, exit codes, usage text — the `timeout` convention,
`nc` flags, `base64` presence per version. Structurally cannot prove applet
resolution.

### Tier 2 — rootless BusyBox rootfs

`unshare -r` plus `busybox --install -s` into a directory: a genuine
BusyBox-only root, ash as `sh`, no coreutils on `PATH`, no privileges and no
docker. Closes what Tier 1 structurally cannot, and is where the `ash` frame
delta is measured.

Risk to validate early: qemu-user under `unshare` needs binfmt registered with
the fix-binary (`F`) flag.

### Tier 3 — rootless loopback dropbear

`tests/integration/chaos/_sshd.py` already runs a throwaway non-root sshd on
127.0.0.1 — host key, config and logs in a tmp dir, pubkey-only, `sshd -D -e` as
the current user, "hermetic on the dev VM and on ubuntu-latest runners alike, no
sudo, no system state", with `PR_SET_PDEATHSIG` so it cannot outlive a SIGKILLed
worker.

Tier 3 is that harness with **dropbear** as the daemon and the Tier-2 rootfs as
the login environment. Dropbear from the start, not OpenSSH first: the expensive
part (rootless daemon on loopback, tmp config, pubkey auth, lifetime guard) is
shared, only the daemon binary and config syntax differ, and dropbear buys the
transport fidelity — legacy crypto, no sftp, different channel limits — that
OpenSSH structurally cannot. Dropbear is small and builds cleanly on modern
toolchains, unlike old BusyBox.

**Fallback if a static dropbear proves hard:** OpenSSH `sshd` with a
`ForceCommand` wrapper that execs the rootfs's `ash` with `PATH` scoped to the
rootfs, running `$SSH_ORIGINAL_COMMAND` for exec channels and an interactive
shell when empty — and with `Subsystem sftp` disabled, since a real device on
dropbear has no `sftp-server` and leaving OpenSSH's `internal-sftp` enabled
would let otto pass a test the real target fails.

**No docker in any tier.** Under this plan docker is optional forever, not
deferred; its only remaining marginal value is fidelity to images the world
ships (`alpine:3.4` and friends) with a real init, which the parity workstream
can argue on its own merits.

Risk to validate early: on aarch64 the rootfs is **mixed-arch** — a native
aarch64 dropbear exec'ing an x86_64 BusyBox login shell through binfmt. Expected
to work transparently, all-native in CI, but proven before it is relied upon.

### Test honesty rules

Carried from the 2026-08-11 nc fan-out work, where each was paid for:

- Every new guard is **mutation-verified**: name the production change that
  reddens it, then make that change and watch it redden.
- **Verify a RED by its message and duration, not its exit code.** A
  parametrized guard whose probe sits on a path one param never executes fails
  by *timing out*, which looks identical to a real failure in a summary line.
- **No wall-clock discriminators** (per the 2026-08-08 sweep). Runaway guards
  may be generous; discriminators must not be timing-based.
- Any PATH-shim control must be driven by dash, per Tier 1.

## Exit criteria

1. `os_type: "busybox"` resolves a profile; a minimal `lab.json` entry works.
2. The five round-1 probes resolve, cache, degrade on failure, and are
   overridable from `userland_options`; probe traffic appears in no sink;
   `--log-level DEBUG` emits paste-ready results.
3. `shell` transfer moves files both directions on the matrix, verified in
   Tier 3 over real ssh.
4. The `ash` frame delta is measured and either implemented or recorded as
   empty.
5. The gap registry, its docs page, and the docs-sync test exist.
6. `todo/busybox-parity-sweep-2026-08-11.md` exists, listing every landmine
   found with its measurement.
7. Tiers 1-3 run on the dev VM and in CI, testing identical artifacts.

## Suggested implementation order

Five phases, ordered so that each is independently verifiable and the riskiest
unknowns are proven before anything depends on them. This is a sequencing
suggestion for the implementation plan, not an eighth exit criterion.

1. **Artifact plumbing and Tier 1.** Cache, fetch, behavioural gate, hash pin,
   `qemu-user-static` prerequisite. Ends with the matrix runnable and the
   `timeout`-convention finding reproduced from the artifacts rather than from
   the 2026-08-10 notes.
2. **`Userland` and the probe set.** Options, spec, resolution, caching,
   `LogMode.NEVER` traffic, paste-ready debug output. Includes moving
   `timeout_style` out of `nc.py` and switching `_elevate` off hard-coded sudo.
3. **Profile and frame.** `os_type: "busybox"`, the `ash` registration, and the
   measured frame delta (Tier 2 needed here for real applet resolution).
4. **The `shell` transfer backend**, with chunk size measured and pinned.
5. **Tier 3, then the gap registry, docs page and parity queue** — last, because
   the registry's entries should be what the earlier phases actually measured
   rather than what this document predicted.

Phases 1 and 2 carry the two technical risks worth failing fast on: binfmt under
`unshare`, and the mixed-arch rootfs.

## Deliberate non-goals

- No BusyBox `nc` backend (queued for parity).
- No host subclass unless a measured need appears.
- No version gate in code.
- No docker.
- No attempt at 1.0-1.15 without obtainable artifacts.
