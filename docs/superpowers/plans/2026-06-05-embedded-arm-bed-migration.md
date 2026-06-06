# Embedded ARM bed migration (track B) — Implementation Plan / Runbook

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans for the code phases. Many phases are **host-gated** (the dev VM cannot run `vagrant` or reach the ARM bed) and are written as a runbook with "RUN ON HOST" verification gates rather than TDD steps. Checkbox (`- [ ]`) syntax tracks progress.

**Goal:** ~~Migrate the five embedded contract hosts from `qemu_x86` to ARM `mps2_an385` … Dissolve the dedicated `sprout_cov` host.~~ → **SCOPED DOWN 2026-06-06 (see "Scope decision" below):** migrate **only the `no_fs` 3.7 host** to ARM `mps2_an385` as a Cortex-M contract-surface proof. The other four contract hosts stay `qemu_x86` (FAT/LittleFS fs-variety + 2.7 legacy); coverage stays on the dedicated `sprout_cov`/`sprout_cov44` ARM beds. Preserve every existing embedded test.

**Architecture:** Hybrid transport — `-serial telnet:` bridge carries every host's shell console (`zephyr-serial` frame), a NIC rides along only on SNMP-monitored hosts. Per-host `Toolchain` in lab data (delivered by track A) lets each Zephyr-version bed resolve its own cross-gcov. The product (`cov_ext.llext`) is repo-built per version.

**Predecessor:** track A (per-host Toolchain unification) is **done and verified**. This plan builds on it.

**Tech stack:** Zephyr (2.7 / 3.7 / 4.4), `mps2_an385` Cortex-M, Zephyr SDK `arm-zephyr-eabi` (gcc 12.2 for 3.7; 4.4 SDK TBD), QEMU `qemu-system-arm`, Vagrant (zephyr VM), otto coverage pipeline.

**No-self-commit:** do not `git commit` in this repo (prepare-commit-msg hook needs /dev/tty). Report paste-able messages.

---

## Status (updated 2026-06-06)

**Phase B0 (code plumbing): DONE and live-verified. The scoped `no_fs` 3.7 x86→ARM migration (B1a → B2 → B3 → B4a + B4b): DONE and live-verified 2026-06-06** — a clean `vagrant destroy/up` reproduces the bed and `sprout_no_fs` now answers as a Cortex-M serial host (`Zephyr 3.7.2` on `192.0.2.37:2325`). B0 diverged from this plan's original end-state (it added a second dedicated coverage bed, `sprout_cov44`, instead of dissolving `sprout_cov`) — read the divergence note, it matters for B3/B4.

Shipped (commits `a4a4704` → `00275d7`, on `feature/embedded-host`):

- **B0.1 ✅** `build.sh` selects the version's venv/workspace/SDK + board (`mps2_an385` 2.7/3.7, `mps2/an385` 4.4) — `1212d6f`.
- **B0.2 ✅** per-version source roots threaded through reporter + merger; each host's `.gcda` pairs with its own `.gcno` — `80ba344`, `93d5ea6`.
- **B0.3 ✅ — but the finding recorded below (lines ~109–116) was WRONG.** gcc-14 *did* need a patch change (it added a 9th gcov counter; `GCOV_COUNTERS` 8→9, else empty `.gcda`). Fixed + patch renamed `gcc12plus` in `a4a4704`. See the corrected B0.3 below.
- **B0.4 ✅ (implemented differently — see divergence)** the suite builds a version-matched product per host — `ae09799`.

**Divergence from plan end-state (important):** instead of B0.4-Step-2's "widen `[coverage].hosts` to the contract hosts" + B3's "dissolve `sprout_cov`", I **added a second dedicated ARM coverage bed, `sprout_cov44`** (4.4, `192.0.2.34`, serial-telnet — `00275d7`) alongside the existing `sprout_cov` (3.7, `192.0.2.33`). `[coverage].hosts` stays `"sprout_cov"` (matches both via `re.search`). So **coverage-on-both-versions (Gate B4c) is proven LIVE** (3.7: 15/16, 4.4: 13/16 lines) — but via two purpose-built ARM coverage hosts, **not** by migrating the contract hosts. `sprout_cov` is therefore NOT dissolved (B3/B4f still pending).

**The five contract hosts are still `qemu_x86`** (TAP + SNMP, unchanged): `sprout`, `sprout_lfs`, `sprout_no_fs` (3.7), `sprout27` (2.7), `sprout44_lfs` (4.4). The x86 provisioner (`Vagrantfile` `for cfg in ${ZCFGS}` → `west build -b qemu_x86`) is fully intact. The only ARM firmware that exists is `tests/firmware/zephyr/configs/cov_an385` (the LLEXT-loader coverage base); the other configs (`v2_7_fat_ram`, `v3_7_fat_ram`, `v3_7_lfs`, `v3_7_no_fs`, `v4_4_lfs`) are **x86** contract overlays. There is **no** ARM contract firmware yet.

**Remaining work:** none for the scoped migration — `no_fs` 3.7 went to ARM end-to-end through B1a → (one-host) B2 → B3 → B4a + B4b, all **DONE 2026-06-06**. B1b, B1c, the B2 fan-out, the B3 flip of the other hosts, and the dissolve (B4f) were **dropped** by the scope decision. (B4c coverage-×2 is also **DONE** — re-verified live on the reproduced `sprout_cov`/`sprout_cov44` beds 2026-06-06; B4d/B4e/B4f are out of scope.)

---

## Scope decision (2026-06-06): migrate `no_fs` 3.7 only

Decided with Chris. Migrate the single `no_fs` 3.7 contract host to ARM `mps2_an385`; leave everything else as-is.

- **From otto's side the contract surface is arch-transparent** — shell commands, fs ops, SNMP OIDs, the frame retcode handshake behave the same behind x86 or Cortex-M. The only arch-sensitive capability is LLEXT coverage, and that is **already proven on ARM** by `sprout_cov` (3.7) + `sprout_cov44` (4.4).
- **Why keep `no_fs_arm` at all:** the coverage beds run `TestEmbeddedCoverage`, **not** the contract matrix (`_ZEPHYR_BACKEND_NE`), so otto's **contract** behavior is currently proven on ARM *nowhere*. `no_fs_arm` is that proof — and it's the **cheapest** one: no filesystem ⇒ no RAM disk ⇒ **Risk 1 does not apply to it.**
- **Kept on x86 deliberately:** `sprout`/`sprout_lfs` (FAT/LittleFS — real fs-variety test value), `sprout44_lfs` (4.4 fs contract), `sprout27` (2.7 — can't load LLEXT anyway; riskiest AN385 port).
- **Coverage stays on the dedicated beds.** The "dissolve `sprout_cov`" goal is **dropped** (this supersedes the earlier "dissolve later" decision).

**Dropped from the plan:** B1b (fat-ram/littlefs SPIKE — Risk 1), B1c (SNMP-over-serial SPIKE — Risk 2), the B2 per-instance fan-out, the B3 flip of the other four hosts, Gate B4d (NIC+serial+coverage co-presence — moot), Gate B4e (2.7 over serial — stays x86), Gate B4f (no special host — we keep them). **Net: the two biggest SPIKEs are avoided.**

**Kept:** B1a (author + boot `no_fs_arm`), a one-instance B2 (provision the single ARM serial instance), a one-host B3 (add the `no_fs_arm` entry, flip only it to `zephyr-serial`), and a one-host B4 (boot-from-clean-provision + the contract matrix passing for that one ARM host: B4a + B4b scoped to it).

---

## Reality check: what's doable where

| Work | Where | Verifiable by me (dev VM)? |
|------|-------|----------------------------|
| Per-version coverage plumbing (build.sh param, settings schema, 4.4 suite cell) | dev VM (code) | **Partially** — Python/shell structure + unit tests yes; actual 4.4 build/run **no** (no 4.4 SDK) |
| ARM base-image firmware (per version×fs) | zephyr VM | **No** — host-gated; **SPIKE** (memory sizing) |
| Vagrant per-instance ARM serial provisioning | zephyr VM | **No** — host-gated |
| `hosts.json` topology flip | dev VM (config) | Shape yes; behavior **no** (needs the ARM bed) |
| Live validation (tests, coverage ×2, frame) | ARM bed | **No** — host-gated |

**Implication:** I can implement + partially-test Phase B0 (code) here. Phases B1–B4 are a runbook for the host, with verification gates you run. Where I'd otherwise be guessing (firmware memory sizing, SNMP-on-ARM-serial), the plan marks a **SPIKE** with a decision point instead of fake-exact code.

---

## Top risks (read before starting)

1. **fat-ram / littlefs memory sizing on `mps2_an385`.** The x86 configs allocate a 100 MiB RAM disk inside a 128 MiB `dram0` and set `CONFIG_KERNEL_VM_SIZE` (an MMU concept). `mps2_an385` has ~16 MB SRAM and **no MMU**. These configs will not build/boot unchanged. **SPIKE B1b.**
2. **SNMP + serial shell on one ARM image.** `cov_an385` overlay disables networking. SNMP hosts need networking *and* the serial shell backend together — unproven overlay. **SPIKE B1c.**
3. **LAN9118 NIC co-present with a serial-bridge `load_hex` (coverage hosts).** The wedge was `load_hex`-over-NIC; coverage now uses serial, NIC only for SNMP — but "NIC present while load_hex runs over serial" on a coverage host (sprout, sprout44_lfs) is untested. **Gate B4d.**
4. ~~**No 4.4 SDK on the dev VM.**~~ **DONE (2026-06-05):** 4.4 build env installed locally on the dev VM — SDK **1.0.1** (`~/zephyr-sdk-1.0.1`) + `~/zephyrproject-v4_4` + `~/zephyr-venv-v4_4` (and v2_7/v3_7). Two concrete findings this surfaced, both feeding Phase B0:
   - **4.4 ships gcc 14.3.0** (`arm-zephyr-eabi-gcov` (Zephyr SDK 1.0.1) 14.3.0), vs 3.7's gcc 12.2. The embedded-gcov **gcc-12 patch will NOT fit gcc 14** — **Phase B0.3** adds a gcc-14-compatible embedded-gcov patch (extend `tests/repo3/third_party/patches/`); B0.4's 4.4 coverage depends on it. **Open B0 blocker.**
   - **SDK 1.0.1 nests toolchains under `gnu/`** (`zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/...`), unlike 0.16.8's flat layout. So the 4.4 host's `hosts.json` `toolchain` (Phase B3) is `sysroot=~/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi`, `gcov=bin/arm-zephyr-eabi-gcov` — note the extra `gnu/` segment vs 3.7.
5. **Zephyr 2.7 on `mps2_an385`.** 2.7 (2021) + the `v2_7-shell-retcode.patch` + serial backend on Cortex-M is the oldest/riskiest target. Consider migrating 2.7 **last**; if it can't build for AN385, that's a decision point (keep 2.7 on x86 as the lone legacy host, or retire it).
6. **2.7 over the serial bridge needs no new frame** — the existing `zephyr-inline` frame already handshakes `shell echo off` (see track A finding). Phase B4 *verifies* this rather than adding code.

---

## Phase dependency order

```
B0 (code, dev VM)  ──┐
                     ├─> B3 (hosts.json flip) ─> B4 (live validation)
B1 (firmware) ─> B2 (Vagrant) ──┘
```
B0 is independent and can start now. B1→B2 must precede B3→B4. Migrate **one host first** (no_fs, the simplest) through B1–B4 end-to-end before fanning out (incremental, per the spec's P1→P2).

---

## Phase B0 — Per-version coverage plumbing (dev VM, code)

Goal: the coverage pipeline can build + report a product for more than one Zephyr version. Independent of the bed; do this first.

### Task B0.1 — `build.sh` takes a Zephyr version, selects its toolchain/workspace

**Files:** Modify `tests/repo3/product/build.sh`; Test: a new `tests/unit/...` shell-invocation test is overkill — verify by `--help`/dry behavior + a real 3.7 build.

- [x] **Step 1 — Add a `--zephyr-version` (default `v3_7`) parameter** that selects the venv/workspace/SDK triple, replacing the hard-coded `ZEPHYR_VENV`/`ZEPHYR_WORKSPACE` defaults. Keep them env-overridable. Concretely, near the top of `build.sh`:

```bash
ZVER="${ZVER:-v3_7}"                       # v3_7 | v4_4 ; or pass as 2nd arg
[ "${2:-}" != "" ] && ZVER="$2"
ZEPHYR_VENV="${ZEPHYR_VENV:-$HOME/zephyr-venv-${ZVER}}"
ZEPHYR_WORKSPACE="${ZEPHYR_WORKSPACE:-$HOME/zephyrproject-${ZVER}}"
# The SDK is per-version (confirmed installed on the dev VM, 2026-06-05):
# 0.16.8 (gcc 12.2) for 2.7/3.7, 1.0.1 (gcc 14.3) for 4.4. NOTE the layout
# differs: 0.16.8 is flat, 1.0.1 nests the toolchain under gnu/ — build.sh only
# needs ZEPHYR_SDK_INSTALL_DIR (west finds the toolchain), so the gnu/ nesting is
# transparent here; it matters for the report toolchain in hosts.json (Phase B3).
case "${ZVER}" in
    v4_4) ZSDK="${ZSDK:-1.0.1}"  ;;
    *)    ZSDK="${ZSDK:-0.16.8}" ;;
esac
ZEPHYR_SDK_INSTALL_DIR="${ZEPHYR_SDK_INSTALL_DIR:-$HOME/zephyr-sdk-${ZSDK}}"
```
The `BUILD_DIR` (first arg) stays caller-supplied so each version builds into its own dir.

- [x] **Step 2 — Verify both paths build:** `build.sh ~/build/cov_ext_app_v3_7 v3_7` and `build.sh ~/build/cov_ext_app_v4_4 v4_4` each produce `cov_ext.stripped.llext`. Both SDKs + workspaces are now on the dev VM, so **both are verifiable here** (the 4.4 build also depends on B0.3's gcc-14 patch landing first).
- [x] **Step 3 — Commit** (paste-able): `feat(cov): build.sh selects the Zephyr-version toolchain/workspace/SDK`. → `1212d6f`

> **DECISION POINT (Risk 4) — RESOLVED (2026-06-05):** 4.4 builds on the dev VM. SDK 1.0.1 + `~/zephyrproject-v4_4` + `~/zephyr-venv-v4_4` are installed; the Vagrantfile provisions them for future instantiations.

### Task B0.2 — Per-version product config in `repo3/.otto/settings.toml`

**Files:** Modify `tests/repo3/.otto/settings.toml`; possibly `src/otto/cli/test.py` (report source-root selection).

- [x] **Step 1 — Decide the schema.** Two coverage products now exist (one per version), each with its own `build_dir` + `.gcno` source root. Proposed:
```toml
[coverage.embedded]
extension = "cov_ext"
# Per-Zephyr-version product builds. The report pairs each coverage host's
# osVersion with the matching build_dir (its .gcno = source root); the host's
# own lab-data `toolchain` supplies the cross-gcov (track A).
[coverage.embedded.builds."3.7"]
build_dir = "/home/vagrant/build/cov_ext_app_v3_7"
[coverage.embedded.builds."4.4"]
build_dir = "/home/vagrant/build/cov_ext_app_v4_4"
```
- [x] **Step 2 — SPIKE (read first):** confirm how the report maps each host's `.gcda` to a source root. Read `src/otto/coverage/reporter.py` + the `sut_dir`/`embedded_build_dir` use in `_run_coverage` (`src/otto/cli/test.py:665-703`). Today `embedded_build_dir` is a single value used as the source root when there are no Unix hosts. Multi-build_dir reporting may need the report to accept a per-host source root. **If single-source-root is baked in, this is a reporter change — scope it here before writing the suite.** Write a failing unit test in `tests/unit/cli/test_test.py` asserting that two embedded hosts of different `osVersion` get their respective `build_dir` recorded in the meta, then implement.
- [x] **Step 3 — Commit:** `feat(cov): per-Zephyr-version embedded product builds`. → landed as `80ba344` (meta source roots) + `93d5ea6` (reporter/merger pair each `.gcda` with its `.gcno`)

### Task B0.3 — embedded-gcov gcc-14 — **DONE (`a4a4704`). The 2026-06-05 finding was WRONG; gcc-14 needed a real patch change.**

> **CORRECTION (2026-06-06).** The finding originally recorded here — *"the existing `>=12`-gated patch already works, no change needed"* — was **falsified at runtime**. It was correct that gcc-14 *compiles* the product and that the byte-length/checksum hunks take the `>=12` branch. It **missed the breaking change:** gcc-14 added a 9th gcov counter, `GCOV_COUNTER_CONDS` (condition coverage / MC-DC), bumping the compiler's `GCOV_COUNTERS` from 8 (gcc 10–13) to 9. The runtime hard-coded `GCOV_COUNTERS=8`, so `struct gcov_info.merge[]` was one slot short and `n_functions` (read just past `merge[]`) came from `merge[8]` (NULL → 0). The 4.4 device dumped a **16-byte, header-only `.gcda` that decoded to 0% with no error** — a silent failure a clean compile cannot catch. Fixed with `#if (__GNUC__ >= 14) #define GCOV_COUNTERS 9` in `code/gcov_gcc.h`.
>
> **Lesson (carry into B4):** a clean build + matching `.gcno` format word is *necessary but not sufficient*. The `struct gcov_info` cast is a runtime ABI contract that **only a live device dump validates** — which is exactly why this plan flagged the runtime confirmation as bed-gated. That gate is what caught it. Diagnostic that localized it without gcc-internal headers: compile a trivial file `-S -fprofile-arcs` under both compilers and diff the emitted `gcov_info` struct (gcc-12 = 60 bytes / 8 merge slots, gcc-14 = 64 bytes / 9). Cross-check `gcc/gcov-counter.def` (count `DEF_GCOV_COUNTER`): gcc-13 = 8, gcc-14 = 9.

What shipped in `a4a4704`:

- [x] Added the gcc-14 `GCOV_COUNTERS=9` branch to the patch's `code/gcov_gcc.h` (alongside the existing `>=12` byte-length/checksum hunks).
- [x] Renamed `embedded-gcov-zephyr-gcc12.patch` → `embedded-gcov-zephyr-gcc12plus.patch`; updated `build.sh`'s `PATCH=` and the README "GCC coupling" note (now documents gcc≥12 *and* gcc-14: empty `.gcda` if `GCOV_COUNTERS` mismatches).
- [x] **Runtime-confirmed live (Gate B4c, 4.4 coverage bed):** `sprout_cov44` dumps a full `.gcda` decoding to 13/16 lines with a matching stamp.
- [ ] **Benign warning (no action needed):** gcc-14 flags `-Wsizeof-array-div` at pristine `gcov_public.c:266` — a deliberate `sizeof(char)`==1 byte-count in NASA's code, untouched by the patch. Optionally silence with `-Wno-sizeof-array-div`.
- [ ] **Build-staleness gotcha (carry into B1/B2):** after the patch content changes, an *incremental* `west build` can serve a stale object — the fix looked unapplied until a pristine `-p always` rebuild (verify by artifact hash, not size). `build.sh` falls back to pristine when the build dir was initialized for a different tree; whenever the third_party patch is touched, force pristine.

> **Two *other* 4.4 build prerequisites this investigation surfaced** (not gcc-related, but they block the 4.4 build and feed B0.1 / B1 / B2):
> 1. **Board rename (Zephyr HWMv2):** 4.4 has no board `mps2_an385`; it is **`mps2/an385`** (board/qualifier syntax). So `build.sh` and the firmware build loop must use the version-correct board name (`mps2_an385` for 2.7/3.7, `mps2/an385` for 4.4) — see B0.1.
> 2. **Python ≥ 3.12:** Zephyr 4.4's build system requires Python ≥ 3.12. The 4.4 venv must be created with `python3.12` (Ubuntu 24.04's system python), not whatever `python3` resolves to (on the dev VM, otto's own `.venv` is uv-managed 3.10 and shadows it). The Vagrantfile `dev-zephyr-workspace` loop runs during provisioning where `python3` is the system 3.12, so a fresh provision is fine — but pin `python3.12` for the 4.4 venv to be robust.

### Task B0.4 — 4.4 coverage suite cell

**Files:** `tests/repo3/tests/` (mirror `test_embedded_coverage.py`); `tests/repo3/.otto/settings.toml` (`[coverage].hosts` selects a 3.7 + a 4.4 target).

- [x] **Step 1** — Generalize `TestEmbeddedCoverage` so the host selector picks **all** coverage hosts (3.7 + 4.4), builds each one's version-matched product (`build.sh <build_dir> <zver>`), drains+loads per host (the track-A `_drain_unload` + "Successfully loaded" check already do this per host), and dumps. The suite is already host-loop-shaped; the change is: derive each host's `build_dir`/`zver` from its `osVersion`.
- [~] **Step 2 — `[coverage].hosts`** widens from `"sprout_cov"` to match the 3.7 + 4.4 coverage targets (e.g. `"sprout$|sprout44_lfs"` — finalize names in B3). **DIVERGED:** left as `"sprout_cov"` (matches `sprout_cov` + `sprout_cov44` via `re.search`); the contract-host widening waits on B3 since those hosts aren't yet ARM/coverage-capable.
- [x] **Step 3** — The product **build** for each version is locally verifiable (B0.3); the **live collection** from a 4.4 ARM host is bed-gated (**Gate B4c**). Commit the code: `test(cov): 4.4 embedded coverage cell`. → `ae09799`; live collection **confirmed** on `sprout_cov44` (13/16 lines).

---

## Phase B1 — ARM base-image firmware (HOST, zephyr VM) — **SPIKE-heavy**

Goal: a bootable `mps2_an385` base image per host type, with the serial shell backend, that otto can reach over the serial bridge. Build on the zephyr VM (`west build -b mps2_an385`).

### B1a — `no_fs` host (simplest; do first) — **DONE 2026-06-06**
- [x] Authored `tests/firmware/zephyr/configs/v3_7_no_fs_arm/overlay.conf` — self-contained (NOT layered on `common/otto-overlay.conf`, which hard-codes telnet + e1000 PCIe, neither of which exists on AN385). Enables `SHELL_BACKEND_SERIAL` + `KERNEL_SHELL` + the 3.7 thread-introspection set; **net-less** (serial bridge carries the shell; no_fs has no SNMP); **no fs shell** (so `fs read` → `command not found`); MPU left **on** (no LLEXT here, unlike `cov_an385`). The `zephyr-serial` frame needs only default-on builtins (`retval`, `shell echo off`) — no extra Kconfig. Sample: `samples/subsys/shell/shell_module`. Board: `mps2_an385`.
- [x] **RAN ON HOST (basil):** `west build -p always -b mps2_an385 …/shell_module -d ~/build/no_fs_arm -- -DEXTRA_CONF_FILE=<overlay>` → exit 0 (`zephyr.elf`, FLASH 60 KB / RAM 18 KB). Booted under `qemu-system-arm -machine mps2-an385 -serial telnet:127.0.0.1:5555,server`. Verified live: `kernel version` → **`Zephyr version 3.7.2`**; `retval` → `0`; `shell echo off` accepted; `fs read /foo` → **`fs: command not found`** (degradation path); `kernel threads` → full per-thread CPU%/stack columns. Test QEMU killed by PID (live cov beds untouched).

### B1b — fat-ram + littlefs hosts — **SPIKE (memory sizing)** — ~~DROPPED 2026-06-06: stay `qemu_x86` (avoids Risk 1)~~
- [ ] **Resolve Risk 1.** `mps2_an385` SRAM is ~16 MB, no MMU. Drop `CONFIG_KERNEL_VM_SIZE`; resize the RAM disk / flash-sim to fit AN385 (e.g. a few MB, not 100 MiB); rework the `app.overlay` `dram0`/ramdisk/flash-sim nodes for the AN385 devicetree. Iterate live until `fs mount` + `fs read`/`fs write` work.
- [ ] **RUN ON HOST:** transfer round-trip (`EmbeddedFileTransfer`) works on each fs variant over the serial bridge.

### B1c — SNMP-monitored hosts — **SPIKE (serial + networking)** — ~~DROPPED 2026-06-06: stay `qemu_x86` (avoids Risk 2)~~
- [ ] **Resolve Risk 2.** Compose an overlay with `SHELL_BACKEND_SERIAL` **and** networking + the SNMP agent module (`tests/firmware/zephyr/snmp_agent`) on the AN385 LAN9118. The console stays serial; SNMP rides the NIC.
- [ ] **RUN ON HOST:** the SNMP agent answers the OIDs in the host's `snmp` block via the relay, while the serial console is otto's shell.

---

## Phase B2 — Vagrant per-instance ARM serial provisioning (HOST, zephyr VM)

> **SCOPED 2026-06-06:** one instance only (`no_fs_arm`). Do **not** retire the x86 `zephyr-qemu-${cfg}` provisioner — the other four hosts stay on it. Add the `no_fs_arm` ARM serial instance alongside the existing `cov`/`cov44` instances (extend the same `COV_INSTANCES`-style loop or add a sibling unit); no fan-out, no NIC fragment.
>
> **DONE 2026-06-06.** Generalized the `COV_INSTANCES` loop → `ARM_INSTANCES` (added `sample` + `overlay-config` columns so cov/cov44 keep `shell_loader`+`cov_an385` and `no_fs_arm` gets `shell_module`+`v3_7_no_fs_arm`); added the `no_fs_arm` row to both the build and unit loops + the restart loop; `ruby -c` clean. **Verified on basil** by standing up the generated run-script + `zephyr-qemu-no_fs_arm.service` (pointing at the B1a build): unit `active`, listens on `192.0.2.37:2325`, and the shell answers over its hop-reachable address (`kernel version` → Zephyr 3.7.2, `retval` → 0, `fs read` → command not found).
> **GOTCHA (fixed):** first picked `192.0.2.35`, which is the *broadcast* of the cov `/30` (`192.0.2.32/30`, owned by the linkdown `zeth-cov` TAP) — `ip route get` sent it to the TAP, not the `/32` on `lo`, so connects failed `Network unreachable`. Telnet-addr must be a **host** address outside any zeth-owned `/30`'s network/broadcast; `no_fs_arm` uses `.37` (first host of the free `192.0.2.36/30`). Noted inline in the Vagrantfile.
> **Host-gated remainder:** the full `vagrant provision zephyr` reproduce (and the build-from-`/vagrant` path, which needs the new `v3_7_no_fs_arm` config synced into the host checkout) is Chris's to run. NIC fragment + x86-provisioner retirement are dropped (scope).

Goal: generalize the proven `zephyr-qemu-cov` serial unit (`Vagrantfile:826-926`) into a per-instance loop covering every migrated host, replacing the x86 `zephyr-qemu-${cfg}` provisioner (`Vagrantfile:655-810`).

- [ ] **Build loop:** for each `(id, zver, fs)`, `west build -b mps2_an385 <sample> -d ~/build/<id> -- -DEXTRA_CONF_FILE=<otto-overlay>;<config overlay>` (mirror the cov build at `Vagrantfile:856-859`; the x86 loop's overlay-composition at `:602-635` is the reference for layering).
- [ ] **Run-script + unit per instance:** model on `run-zephyr-qemu-cov.sh` / `zephyr-qemu-cov.service` (`Vagrantfile:886-919`): `qemu-system-arm -machine mps2-an385 -display none -monitor none -serial telnet:192.0.2.<n>:<port>,server,nowait -kernel ~/build/<id>/zephyr/zephyr.elf`, with `ExecStartPre` adding the listen IP to `lo`. Each instance gets a distinct `192.0.2.<n>:<port>`.
- [ ] **NIC fragment (SNMP hosts only):** add the LAN9118 NIC + the existing SNMP UDP relay (`Vagrantfile:928+`) for hosts with an `snmp` block. Keep it a separate, opt-in fragment so non-SNMP hosts stay pure serial.
- [ ] **Retire the x86 provisioner** and the `zephyr-qemu-cov` special case (folded into the loop). Confirm `make qemu-restart`/`make vm-health` glob `zephyr-qemu-*`.
- [ ] **RUN ON HOST:** `vagrant provision zephyr`; every `zephyr-qemu-<id>.service` is active; otto reaches each over the hop.

---

## Phase B3 — `hosts.json` topology flip (dev VM config; verify on host)

> **SCOPED 2026-06-06:** applies to **`no_fs` only**. Add/flip the single `no_fs_arm` host; the other four entries (`sprout`, `sprout_lfs`, `sprout44_lfs`, `sprout27`) and `sprout_cov`/`sprout_cov44` are **unchanged**. Do **not** remove `sprout_cov`.
>
> **DONE 2026-06-06.** Flipped `sprout_no_fs` in [hosts.json](tests/lab_data/tech1/hosts.json) **in place** (NE name kept, so `_ZEPHYR_BACKEND_NE` is unchanged): `command_frame` → `zephyr-serial`, `ip` → `192.0.2.37`, `telnet_options.port` → `2325`, and **removed its `snmp` block** (the ARM bed is net-less). No `toolchain` added — `no_fs` runs no coverage. Consequence: `sprout_no_fs` drops out of `SNMP_BACKENDS` (the SNMP suite derives that set dynamically from `snmp` blocks, so it's automatic and non-breaking); SNMP stays covered by `sprout`/`sprout_lfs`/`sprout44_lfs`. Retired the orphaned x86 `v3_7_no_fs` from the Vagrantfile (build `ZCFGS` row, IP/machine map, both run/restart loops, SNMP-relay map); left it in the over-inclusive TAP-cleanup list so the stale TAP gets removed. `ruby -c` clean. JSON valid.

**File:** `tests/lab_data/tech1/hosts.json`. For the `no_fs` host:
- [ ] `command_frame` → `"zephyr-serial"` (sprout27 → keep `"zephyr-inline"`; see Risk 6 — it already echo-offs).
- [ ] Add `telnet_options.port` = the instance's serial-bridge port; IP → the bridge listen address (the `192.0.2.<n>` the unit listens on).
- [ ] Add the per-host `toolchain` dict (track A): `sysroot` = that version's SDK `arm-zephyr-eabi`, `gcov` = `bin/arm-zephyr-eabi-gcov`, `lcov` = `/usr/bin/lcov`.
- [ ] Keep `snmp` blocks on the hosts that have them (now backed by the B1c/B2 NIC).
- [ ] `board` is a firmware detail — not an os_profile field (per the migration doc); `osType`/profile unchanged.
- [ ] **Remove the `sprout_cov` entry** (its role is absorbed by the now-coverage-capable hosts; `[coverage].hosts` from B0.4 selects the 3.7+4.4 targets).
- [ ] `_ZEPHYR_BACKEND_NE` in `tests/conftest.py` is **unchanged** (same five backends; the bed under them changes).

---

## Phase B4 — Live validation (HOST / ARM bed) — the acceptance gates

> **B4b DONE 2026-06-06 (live, full matrix on the reproduced bed).** Re-ran the **whole** embedded contract matrix — all five `_ZEPHYR_BACKEND_NE` backends, `-m "embedded and not stability"` under the suite's own `-n auto --dist loadgroup` console serialization — against the freshly `vagrant up`'d bed: **72 passed, 8 skipped, 0 failed in 46s** (`scripts/junit_failures.py reports/junit/b4b-embedded-contract.xml` → 0 problems). The 8 skips are all the by-design no-filesystem short-circuits (the 4 fs-transfer roundtrips on `no_fs`, plus the no-FS-error test deselected on the 4 fs-having backends). The migrated `no_fs` ARM host reproduces its earlier standalone result exactly: 10 passed / 4 skipped (5 impl-detail in `test_embedded_host_integration.py` — signed-errno retcode, clean multiline, `kernel uptime`, both `TestSingleConsole` concurrency tests — + 5 OS-agnostic in `test_host_contract.py`: run success/fail, oneshot cold, send/expect, `test_no_filesystem_backend_surfaces_clear_error`). otto → basil hop → telnet `192.0.2.37:2325`.
> **B4a DONE 2026-06-06 (live).** Chris ran `vagrant destroy zephyr -f && vagrant up zephyr` on the host (new Vagrantfile + the `v3_7_no_fs_arm` config + hosts.json synced into the host checkout, mounted at `/vagrant`). Verified from the dev VM against the live bed: all seven `zephyr-qemu-*` units `active running`, with **no** orphan `zephyr-qemu-v3_7_no_fs` and **no** `zephyr-snmp-relay-v3_7_no_fs`; build dirs `no_fs_arm` / `cov_base` / `cov_base_v4_4` all present; the three ARM serial consoles answer `kernel version` on their **real** ports — `no_fs_arm` `192.0.2.37:2325` → **Zephyr 3.7.2**, `sprout_cov` `:2323` → 3.7.2, `sprout_cov44` `:2324` → 4.4.1-rc1; the four x86 net beds answer with live uptimes. `make vm-health` is green (exit 0).
> **Tooling fix required to make the B4a gate trustworthy:** `scripts/lab_health.py`'s console probe **hardcoded port 23** and ignored `telnet_options.port`, so for the loopback-`/32` serial beds it connected to the hop's own `0.0.0.0:23` telnetd and reported a meaningless `up ?` — a false green that couldn't distinguish a healthy console from a wedged one. Fixed to honor `telnet_options.port` (default 23); vm-health now probes the real serial consoles (2325/2323/2324) and shows their uptimes. Uncommitted — see the paste-able message.
> **B4c DONE 2026-06-06 (live, on the reproduced bed).** `OTTO_SUT_DIRS=…/tests/repo3 OTTO_XDIR=~/b4c-xdir uv run otto --lab embedded -R test --cov --cov-report TestEmbeddedCoverage` — rebuilt both version-matched products (`cov_ext_app` v3_7, `cov_ext_app_v4_4` v4_4), drained + loaded each onto its host (`Successfully loaded extension` + `__gcov_init`), ran the ops (**12 passed, 0 failed**), then dumped + decoded **1 `.gcda` from each** of `sprout_cov` (3.7) and `sprout_cov44` (4.4) — **no stamp mismatch** (the runtime ABI gate the divergence note flagged as bed-only), each host's lab-data `toolchain` resolving its own `arm-zephyr-eabi-gcov`. Merged HTML report: product `cov_ext.c` **15/16 lines, 8/8 fns, 5/6 branches**; 44.2% overall across 4 files (the other 3 are the partially-exercised third-party embedded-gcov runtime). Report at `~/b4c-xdir/test/…_TestEmbeddedCoverage/cov_report/index.html`.
> B4d/B4e/B4f are dropped (scope: no NIC+serial+coverage co-presence, no 2.7-over-serial, keep the dedicated coverage beds).

- [x] **B4a — Reproducibility:** ✅ 2026-06-06 — `vagrant destroy zephyr -f && vagrant up zephyr`; `make vm-health` green, every `sprout*` answering with no manual steps (after the `lab_health.py` port fix — see note above).
- [x] **B4b — Parity:** ✅ 2026-06-06 — full `_ZEPHYR_BACKEND_NE` matrix green against the reproduced ARM bed (**72 passed, 8 skipped, 0 failed**; all five backends — frame contract, fs transfer across fat/lfs/no-fs, SNMP, host contract). Triaged with `scripts/junit_failures.py` → 0 problems. See note above.
- [x] **B4c — Coverage ×2:** ✅ 2026-06-06 — matching-stamp `.gcda` collected from **both** the 3.7 (`sprout_cov`) and 4.4 (`sprout_cov44`) targets and merged into an HTML report (product `cov_ext.c` 15/16 lines, 8/8 fns); each host's lab-data toolchain resolved its own cross-gcov; each version's product build supplied the matching `.gcno` — **no stamp mismatch**. See note above.
- [ ] **B4d — LAN9118 + serial + coverage co-presence (Risk 3):** confirm a coverage host that *also* has the SNMP NIC (sprout / sprout44_lfs) loads the extension over serial (`Successfully loaded extension`) without the multi-frame wedge, while SNMP still answers.
- [ ] **B4e — 2.7 over serial (Risk 6):** confirm `sprout27` with the existing `zephyr-inline` frame works over the serial bridge (its handshake already disables echo). No new frame expected; if it desyncs, add a `zephyr-serial-inline` subclass (inline-retcode parse + serial echo-off handshake) in `tests/custom_hosts`.
- [ ] **B4f — No special host:** `sprout_cov` gone; coverage runs against the normal hosts selected by `[coverage].hosts`.

---

## Verification matrix (acceptance)

| Spec goal | Gate |
|-----------|------|
| All existing embedded tests pass on ARM | B4a ✅, B4b ✅ |
| Coverage on 3.7 **and** 4.4 | B0.1–B0.4 (code) ✅ + B4c ✅ |
| `sprout_cov` dissolved | B3, B4f |
| Reproducible from clean `vagrant up` | B4a ✅ |

## Out of scope
Cross-instance coverage merge demo; `valid_labs` enforcement; any fs/version cell beyond the existing matrix + the 4.4 coverage cell. **Added 2026-06-06 (scope decision):** migrating the FAT/LittleFS hosts (`sprout`/`sprout_lfs`/`sprout44_lfs`) to ARM, migrating 2.7 (`sprout27`) to ARM, SNMP-over-serial-on-ARM, NIC+serial+coverage co-presence, and dissolving `sprout_cov`/`sprout_cov44`. All four stay `qemu_x86`; coverage stays on the dedicated ARM beds.
