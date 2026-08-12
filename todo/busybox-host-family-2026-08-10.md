# BusyBox as a host family (2026-08-10) — findings and a testing approach

Starting point for a workstream, not a plan to execute as written. Everything
under "Findings" was measured on 2026-08-10 against `busybox 1.36.1`
(`Ubuntu 1:1.36.1-6ubuntu3.1`) on the dev VM; everything under "Approach" is a
proposal.

The question that opened this: otto's Unix interfaces assume a GNU/coreutils
userland with bash. BusyBox diverges in enough places that a BusyBox target is
plausibly its own **host family** rather than a hardening pass on the existing
one. The trigger was narrow — a `timeout` calling-convention bug — but the
survey around it found a backend that cannot work at all.

## Why this is not already covered

otto has no BusyBox target. The bed is four Ubuntu VMs plus a Zephyr board, so
nothing in any lane exercises an ash shell or a BusyBox applet. The only
in-tree BusyBox usage is `tests/unit/host/test_history_suppression_portability.py`,
which shells out to `busybox sh` as one of several *shell dialects* — valuable,
and unrelated to applet behaviour.

## Findings

### 1. The netcat transfer backend cannot work on BusyBox at all (blocking)

`busybox nc` usage is:

    nc [-iN] [-wN] [-l] [-p PORT] [-f FILE|IPADDR PORT] [-e PROG]

Against what otto emits from `src/otto/host/transfer/nc.py`:

| otto sends | BusyBox | consequence |
| --- | --- | --- |
| `nc -l PORT` (positional port) | needs `-l -p PORT` | measured: bare `9077` is read as IPADDR → `bind: Cannot assign requested address` |
| `nc -Nl …` (GET listener) | no `-N` | `invalid option -- 'N'` |
| `nc -N HOST PORT` (PUT sender, nc.py:928) | no `-N` | same |
| `-w SEC` | present | accepted, but documented as *connect* timeout |

`NcOptions.exec_name` already says so out loud — *"Listener syntax is assumed
to be OpenBSD-style (`nc -l PORT`)"* — so this is a known assumption rather
than a discovery, but nothing enforces or tests it. Note `-N` (shutdown on EOF)
is load-bearing for the GET direction, so this is not a flag-spelling fix.

### 2. Applets otto depends on that BusyBox does not provide

Checked against `busybox --list`:

| applet | present | impact |
| --- | --- | --- |
| `ss` | **no** | none — the port-strategy cascade already falls through `ss → netstat → python → proc`. This is the model for how the rest should behave. |
| `bash` | **no** | `CommandFrame.type_name = "bash"` is the default dialect for SSH/telnet/local unix, and `src/otto/host/daemon.py:103` hard-wires `bash -c {quoted}` |
| `sudo` | **no** | `src/otto/host/privilege.py:92` builds `sudo -S -p …`; user elevation and `unix_host.py:842`'s `shutdown -h now` both depend on it |
| `pgrep` | **no** | test-side only (`tests/_fixtures/bed_hygiene.py` uses `pgrep -af`), but a BusyBox bed would need a different hygiene probe |
| `stat` | yes | `stat -c %s` verified working |
| `netstat`, `ps`, `mktemp`, `readlink`, `base64`, `md5sum`, `tar`, `find`, `xargs`, `stty` | yes | not individually flag-checked — **do this before trusting them** |

### 3. Already fixed, for context

The `timeout` calling convention (BusyBox `-t SECS PROG` up to 1.28.1 vs
coreutils/BusyBox-from-1.31.0 `SECS PROG`) is handled by convention rather than
by binary name. See `todo/nc-listener-leak-2026-08-10.md`. That fix is what
surfaced everything above.

`_nc_listener_prefix` no longer probes for it. The probe was correct but
private, so it has been retired into `Userland.timeout_style` — one mechanism
alongside the four sibling capabilities — and the prefix is now a mapping from
the resolved answer. Two consequences for anyone working here: the convention
can be declared in lab data's `[userland_options]` to skip the probe, and until
a host wires its `Userland` through `TransferContext` the backend receives
`None` and its listeners run **uncapped**.

### 4. The trap that will bite anyone testing this

**BusyBox's `ash` resolves applets internally and ignores `PATH`.** A shim
placed on `PATH` is unreachable from `busybox sh`:

    PATH=/nonexistent busybox sh -c 'command -v timeout'   ->  timeout

This produced a false pass during the `timeout` work — a control that
"verified" the broken code worked, because it had silently exercised BusyBox's
own built-in applet instead of the shim on `PATH`. Any PATH-based fake must be
driven by a **non-BusyBox** shell (`/bin/sh` → dash) to mean anything, and
conversely, anything testing *applet resolution* must use a real BusyBox shell.

## Proposed approach to testing

Deliberately tiered, because the cheap tiers answer most questions and the
expensive one is the only thing that answers the host-family question.

### Tier 1 — static BusyBox binary on PATH (cheap, no container)

Fetch or vendor a per-release static BusyBox from busybox.net, `ln -s busybox
timeout` etc. in `tmp_path`, and drive it from `/bin/sh`. Exercises the real
argument parsing, real usage text, real exit codes — everything the current
hand-written shims assert by hand.

Open question to settle first: a checked-in ~1 MB binary is a supply-chain
artifact; a fetch is a network dependency in a lane that may not have one.
Pick one deliberately.

### Tier 2 — rootless namespace rootfs (cheap, high fidelity, no docker)

    unshare -r  +  busybox --install -s  into a directory

Gives a genuine BusyBox-only root — ash as the shell, real applet resolution,
no coreutils anywhere on `PATH` — with no privileges and no container. This is
the tier that closes finding 4, which Tier 1 structurally cannot.

### Tier 3 — a real BusyBox host with sshd (the host-family tier)

Only this answers "can otto drive a BusyBox target". Needs a bootable image
with an ssh server, i.e. docker (`busybox:1.28` exists, so old syntax is
reachable) or a VM.

**Constraint:** docker is deliberately restricted in this project to one or two
old-OS e2e tests. A BusyBox lane has to either justify displacing that budget
or fit inside the existing exception. Worth noting Tiers 1 and 2 do *not* need
it, so the docker argument should be made on the strength of Tier 3 alone.

### Rejected

- **Build old BusyBox from source** — 2–5 min and a toolchain per version, buys
  nothing over the published static binaries.
- **qemu-user** — the published binaries are already x86_64, and argument
  parsing is architecture-independent.
- **chroot / initramfs** — needs root for what Tier 2 gives without it.

## Where the seams already are

The work looks less like new architecture than like populating seams that are
already cut:

- `CommandFrame.type_name` — `bash`, `zephyr`, `zephyr-serial` today. An
  `ash`/`busybox` dialect slots in beside them, and `command_frame.py` already
  carries ash-specific reasoning in its comments.
- The transfer registry dispatches per backend, so a BusyBox nc variant
  (`-l -p PORT`, no `-N`) is a backend concern, not a rewrite. Whether it is
  worth writing at all versus documenting "install netcat-openbsd" is a real
  decision — `-N` has no BusyBox equivalent, so the GET direction needs a
  different EOF strategy.
- The port-strategy cascade is the pattern the rest should copy: probe, fall
  through, cache the answer. Nothing about it needed changing for BusyBox.

## Suggested first step

Not the docker image. Start by deciding **what otto should claim**: does a
BusyBox target need to work with the stock userland, or is "BusyBox userland
plus `netcat-openbsd` plus `bash`" an acceptable documented requirement? The
second is a much smaller job and may be the honest answer — the host class the
recent `timeout` fix rescues is exactly that shape (Alpine <= 3.8, OpenWrt
<= 18.06, with a real netcat added). The first is the host family.

Answering that question changes which tiers are worth building, so it should
come before any of them.
