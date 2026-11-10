# Chaos reboot guest: reboot chaos moves to an Ubuntu 24.04 QEMU guest, and `reboot --wait` gets a declared window

**Issues:** #985 (re-enable the chaos lanes before 0.20.0), #979 (`reboot --wait`'s down bound), #435 (the down-phase probe reuses a cached transport; spike finding, see §7.2), #978 (context only; stays open as the host-side VirtualBox bug).
**Owner decisions:** Chris, 2026-11-09 (issue #985's stance comment; this session's brainstorm: an Ubuntu 24.04 guest rather than a BusyBox one, ARM64 image on the ARM64 host, hosted on test4, a lane that takes minutes is acceptable, the journal reclaim on test4).
**Spike:** 2026-11-10 on the live test4, by hand (this document's numbers are measured there).

## 1. Intent

The chaos lane's reboot scenarios warm-rebooted test1 and test2, Ubuntu VMs under
VirtualBox. VirtualBox's ARM64 EFI firmware can lose the boot entry on a warm reset
(#978), stranding a VM that only a person at the console can recover, so one gate run
could block every session on the shared bed. The lanes were suspended as gate lanes on
2026-11-09 with the promise to re-enable them before 0.20.0.

The decision: **a test never power-cycles a hypervisor-hosted lab VM again.** The reboot
chaos moves to a purpose-built Ubuntu 24.04 guest running under QEMU behind test4, where a
reboot is a QEMU reset under a read-only firmware and cannot strand anything. The guest is
Ubuntu so that nothing the lane proves today is lost: the sudo reboot path, systemd's
real shutdown ordering with sshd still answering, OpenSSH, netem, socat and bash from the
archive. otto treats it as any other unix host; the spike drove `exec`, `tunnel add`,
`link impair`, `link repair` and `reboot --wait` at it through the hop with no product
change.

Three things ride along because the guest makes them testable deterministically:

- `reboot --wait`'s down phase (#979) gains a declared window, layered like every other
  host option, and a longer default.
- The guest can hold its shutdown for as long as a scenario asks, with sshd accepting
  logins throughout, so the slow shutdown that #979 found by accident on a VM becomes a
  knob the lane controls.
- That knob reproduced #435 on the first try: with sshd answering through the hold, the
  down phase never sees the host go down, because its first successful probe is cached
  and every later probe reads the dead cache. #435 is in scope (§7.2); without it the
  declared window is a number nobody can trust.

## 2. Goals, non-goals, anti-goals

**Goals**

1. `make chaos` and `make chaos-embedded` pass on the new bed without touching any lab
   VM's power, and the chaos lanes come back as gate lanes (#985's steps).
2. The three reboot scenarios keep their current meaning, aimed at the guest: a happy-path
   `reboot --wait`; a tunnel whose far endpoint reboots and `tunnel remove --all --yes`
   reconciles the half-chain; a link whose far endpoint reboots and `link repair --all` is
   idempotent against the half-clean state.
3. A fourth scenario pins #979 and #435 together: the guest holds its shutdown, with sshd
   accepting logins, past a short declared bound and the reboot is reported as never
   going down; within a long declared bound it passes.
4. Anyone can build this bed from the Vagrantfile. The Vagrantfile, the seed renderer, the
   lab data and the hostless drift guards change in one commit; the live bed is then
   reprovisioned from it, touching test4 only.
5. `reboot --wait`'s three numbers (`timeout`, `down_timeout`, `poll_interval`) resolve
   through one `reboot_options` table: defaults, the host's lab.json, the project's
   `[host_preferences]`, then the CLI flags or per-call keywords.
6. The down-phase probe is stateless (#435), so "down" means the host stopped answering,
   not that a cached connection object still exists.

**Non-goals**

- Fixing #978. It stays open as the host-side bug it is, medium priority, off the release's
  critical path.
- The chaos-reboot follow-ups todo file's recovery-budget and settle-window items. The
  shutdown hold makes them reproducible; they are separate work.
- Moving any other chaos module. Only `tests/e2e/chaos/test_reboot_chaos.py` changes
  target. `test_connection_drop.py`'s bb1350 arm, `test_tunnel_link_chaos.py` and the rest
  keep their hosts.
- Touching test1, the BusyBox bed, its images or units, or the Zephyr instances on test4.
- Speed. The guest runs under TCG like every other guest; a reboot cycle is about three
  minutes and the module takes what it takes.

**Anti-goals** (what would make this a failure even if it works)

- **The lane proving less than it does today.** Each scenario must assert what it asserts
  now; a weaker oracle, a skipped phase or a retry is a regression.
- A guest that can be stranded. The boot path must have nothing to lose: no firmware
  variable store, no boot entry, no state outside the disk image.
- A reboot that is not a reboot: the guest must come back from a kernel reset with its
  runtime state gone, inside the same QEMU process, with its TAPs and test4's side intact.

## 3. Constraints

- Python floor 3.10; no `from __future__ import annotations`; ONE DECISION, ONE PLACE;
  symmetry over compat (AGENTS.md).
- Every live-bed step takes the bed lease and is announced to peer sessions first.
  Reprovisioning test4 starts the new unit and restarts nothing else; the Zephyr
  instances and their relays are not touched.
- test4 (3.7 GB, 4 CPUs) had 2.6 GB available before the spike and about 1 GB with the
  guest running; the guest is capped at 1 GB. Its disk was 89 % full; the spike vacuumed
  the journal, which the Zephyr consoles had grown to 1.6 GB, to 200 MB, leaving 4.2 GB
  free. The provisioner caps the journal so it does not grow back, and requires 3 GB
  free before it downloads anything, refusing with the number otherwise.
- The dev VM cannot run `vagrant`; the lab VMs are provisioned from the owner's host
  checkout (§9).
- Breaking changes carry `!`: the `reboot` CLI flags' defaults and the Python
  `reboot()` signature change (§7).

## 4. The chaos guest

### 4.1 Identity

| Field | Value |
|---|---|
| element | `ubu2404`; host id `ubu2404-qemu` (board `qemu`, like the BusyBox guests) |
| hop | `test4` (the Zephyr VM) |
| OS | Ubuntu 24.04 server cloud image, a dated release pinned by SHA-256; **the guest's architecture follows the hop's** (`arm64` on an ARM64 host, `amd64` on x86_64), one pin per architecture |
| eth0 | QEMU user-mode NIC: egress for the archive, default route; the shape of the lab VMs' NAT `eth0` |
| eth1 (management) | `203.0.113.1/30`, TAP `ubeth-mgmt` on test4 at `203.0.113.2` |
| eth2 (data plane) | `203.0.113.5/30`, TAP `ubeth-data` on test4 at `203.0.113.6` |
| creds | `vagrant`/`vagrant` (sudo without password) and `test`/`Password1`, the lab VMs' two |
| sshd | OpenSSH, password authentication on |
| console | ttyS0 to a file under the guest's directory; no console term declared |
| firmware | ARM64: the EDK2 code image mounted read-only with **no variable store**, so the firmware boots the image's removable-media path every time and there is no entry to lose. x86_64: SeaBIOS, which has no NVRAM at all |
| RAM / CPUs | 1024 MB, 2 vCPUs, TCG with multi-threading |
| QEMU | no `-no-reboot`; virtio disk and NICs; monitor on a unix socket |
| unit | `chaos-qemu-ubu2404.service` on test4, `Restart=always`, both TAPs in `ExecStartPre`/`ExecStopPost` |

The interface names are the lab VMs' (`eth0` NAT, `eth1` management, `eth2` data) on
every architecture and machine type: the seed's `network-config` matches each NIC by the
MAC the wrapper assigns and sets its name, so no kernel naming scheme is assumed.

TEST-NET-3 (203.0.113.0/24, RFC 5737) because test4's Zephyr instances hold TEST-NET-1 and
test1's BusyBox guests hold TEST-NET-2; the three blocks can never collide. The `4n+1`
guest / `4n+2` host arithmetic continues. Two TAPs because a guest whose only NIC carries
its management path has no data plane to impair: `link impair` refuses that as a
self-lockout, by design (`ensure_not_mgmt`, `ensure_not_hop_transit`). eth2 is what the
declared link and the tunnel use; eth1 is what otto logs in over, through the hop.

### 4.2 The disk and the seed

A pristine cloud image is downloaded once into `/home/vagrant/chaos-guest/` on test4 and
checksum-checked against the release's `SHA256SUMS` (the spike left the ARM64
`release-20261017` image and its checksum list there; the provisioner reuses them); the
guest boots a qcow2 **overlay** on it (`ubu2404.qcow2`, backing file the pristine image,
virtual size 4 GB), so the pristine bytes are never written and "rebuild the guest" is
"delete the overlay". Footprint: the image (0.6 GB) plus the overlay (under 1.5 GB after
first boot and the package install).

First boot is configured by a cloud-init NoCloud seed (`cloud-localds`, from
`cloud-image-utils`, which the provisioner installs) that
`scripts/render_chaos_guest_seed.py` renders from one constant, `CHAOS_GUEST`, which holds
everything in §4.1's table that is not QEMU's business: element, host id, the MACs, both
/30s and both TAP names, the interface names, the users, the image pins. The Vagrantfile
calls the script, the lab data mirrors the constant and a hostless guard pins the mirror
(§4.5). The seed:

- creates the two users with their passwords, `vagrant` in `sudo` with `NOPASSWD`;
- turns on sshd password authentication;
- `network-config` v2: eth0 DHCP (slirp), eth1 and eth2 static from the constant, matched
  by MAC and renamed, no gateway on either TAP;
- installs `socat`; `bash`, `iproute2` (netem is a module in Ubuntu's kernel, loaded on
  first use: the spike's `link impair` placed it on the guest's eth2 with nothing
  pre-loaded) and OpenSSH are in the image;
- writes the shutdown hold unit (§4.3) and the lab VMs' `reboot.target` override
  (`JobTimeoutSec=90`, `JobTimeoutAction=reboot-force`), so a hung stop is bounded the
  same way here;
- sets the hostname to the element name.

cloud-init runs the seed on the first boot only (its instance id is fixed in `meta-data`);
later boots, including every chaos reboot, find the instance already configured.

### 4.3 The shutdown hold

`chaos-shutdown-hold.service` is a oneshot with `RemainAfterExit=yes` whose `ExecStop`
sleeps for the number of seconds in `/run/chaos/shutdown_hold` when that file exists and
returns at once otherwise (`ExecStart` creates `/run/chaos`). It is ordered
`After=systemd-user-sessions.service ssh.service ssh.socket`, so that on the way down
systemd stops it **before** all three. The user-sessions unit matters as much as sshd:
its stop creates `/run/nologin`, after which sshd still answers but PAM refuses every
login, and otto's probe (a login) reads that as "down" at once. The spike measured both
orderings: with `After=ssh.service ssh.socket` alone a 75 s hold was invisible to
`reboot --wait`; with the user-sessions unit added, logins succeeded through the hold and
`reboot --wait --down-timeout 30` reported "never went down within 30 s" after 38 s: the
#979 shape, on demand.

`TimeoutStopSec` is 600, well above any hold. The lab's `reboot.target` bound of 90 s is
copied to the guest, so a hold is effective up to about 85 s and a scenario sets 75.
`/run` is a tmpfs, so a hold never outlives the reboot it was set for. A scenario sets the
hold with `echo N | sudo tee /run/chaos/shutdown_hold` over the guest's shell before
issuing the reboot.

### 4.4 The Vagrantfile

A new provisioner, `provision_chaos_guest(vm)`, named `chaos-guest`, called from the
`zephyr` define after `provision_zephyr_vm`. It:

1. installs `cloud-image-utils` and `qemu-utils` if missing (test4 already has
   `qemu-system-aarch64`, `qemu-system-x86_64` and both firmware packages for the Zephyr
   instances; an x86_64 host gets `qemu-system-x86` the same way);
2. caps the journal (`SystemMaxUse=200M` in a `journald.conf.d` drop-in, then a vacuum
   to that size), since the QEMU consoles write to it;
3. refuses when `/home/vagrant` has less than 3 GB free, naming the number;
4. downloads the pinned image for the host's architecture once, checksum-checks it, and
   creates the overlay once;
5. renders the seed with `python3 /vagrant/scripts/render_chaos_guest_seed.py --dest …`
   and builds the ISO; a re-render after the constant changed replaces the ISO and
   deletes the overlay so the next boot re-seeds (the only way a changed identity reaches
   the guest);
6. writes `run-chaos-qemu-ubu2404.sh` (header: why there is no `-no-reboot`, why the
   firmware has no variable store, what `Restart=always` still covers) and
   `chaos-qemu-ubu2404.service` from a one-entry shell table
   (`element:mgmt_guest_ip:mgmt_tap_ip:mgmt_tap:data_guest_ip:data_tap_ip:data_tap`) with
   the same `ExecStartPre`/`ExecStopPost` TAP lifecycle the Zephyr and BusyBox wrappers
   use, two TAPs per unit; the wrapper picks the QEMU binary, machine and firmware by
   `dpkg --print-architecture`;
7. enables and starts the unit, restarting it only when the wrapper, the unit or the
   seed changed.

The spike's wrapper (`-M virt -cpu cortex-a72 -accel tcg,thread=multi -smp 2 -m 1024`,
the firmware as a read-only pflash drive, virtio disk, the seed as a second virtio disk,
three virtio NICs with fixed MACs) is the ARM64 shape to render.

The hostless guard (`tests/unit/scripts/test_render_chaos_guest_seed.py`) pins the
Vagrantfile's shell table, the wrapper template (three `-nic` lines in order: user,
`ubeth-mgmt`, `ubeth-data`, with the constant's MACs; no `-no-reboot`; no variable-store
pflash) and the unit template (both TAPs created and torn down) against `CHAOS_GUEST`,
the way the BusyBox guard pins its own table; and pins the rendered seed (users,
interfaces by MAC, no gateway on the TAPs, the hold unit and its `After=`, the
reboot.target override).

`scripts/lab_health.py` and `make qemu-restart` learn the `chaos-qemu-*.service` glob
beside the Zephyr and BusyBox ones; the reachability table gains the guest's row through
an SSH-through-hop probe (today's hop-fronted probe is telnet-only).

### 4.5 Lab data

Lab membership is derived from each element's own `labs` list, so
`tests/_fixtures/lab_data/tech1/lab.json` gains:

- an element `ubu2404` with `labs: ["chaos"]`, and `chaos` added to **test4's** `labs`
  beside `embedded`; a new lab **`chaos`** whose resources are `test4` and `ubu2404`. Not
  in `embedded` (that lab is the Zephyr matrix) and not in `unix` (the pool the lease
  draws from). The guest's host record: `ip` 203.0.113.1, `os_type` unix, `board` `qemu`,
  `hop` `test4`, `valid_terms` `["ssh"]`, `valid_transfers` `["scp", "sftp"]`,
  `is_virtual`, the two creds, `interfaces` eth1 and eth2 with their /30s. (This is the
  record the spike ran with; `otto --show-lab` resolves it as `ubu2404-qemu  UnixHost,
  address 203.0.113.1 (via test4)`.)
- `ubeth-data` on test4's `interfaces` (203.0.113.6/30). The management TAP is declared
  nowhere, like every other guest's.
- a declared link `test4:ubeth-data <-> ubu2404-qemu:eth2`, protocol udp, id
  `test4--ubu2404-qemu`.

A hostless guard (`tests/unit/host/test_chaos_guest_lab_entries.py`, the shape of
`test_busybox_bed_lab_entries.py`) pins the record, test4's interface and membership, the
link and the lab against `CHAOS_GUEST`, and that `ubu2404` is in no other lab.
`tests/unit/test_chaos_link_lab_scope.py`'s closed-unix-lab pin is unaffected;
`tests/unit/labs/test_pinned_identities.py` gains the `("tech1", "chaos")` pin.

## 5. What a reboot is on the guest

`otto host ubu2404-qemu reboot` runs `sudo -S reboot` exactly as it does on test3 today.
systemd stops units in reverse order: the hold unit first (sleeping if asked), then sshd
and user sessions, then the rest, bounded by `JobTimeoutSec=90`; the kernel resets; QEMU,
without `-no-reboot`, resets the machine and the firmware boots the overlay again. The
QEMU process, both TAPs, test4's addresses, routes and qdiscs survive. sshd comes back
with the same host key (it is on the overlay).

Measured on the spike (ARM64 image under TCG on test4):

| Event | Time |
|---|---|
| first boot, sshd answering | 141 s |
| first boot, cloud-init finished (package install) | 426 s |
| `reboot --wait --down-timeout 120` without a hold, end to end | 170 s |
| the same with a 75 s hold, command to kernel reset | 81 s |
| boot to sshd after a reset | about 130 s |

The down phase sees sshd refuse connections when systemd stops it; the up phase sees a
fresh login; `_confirm_recovered`'s shell round-trip is the existing one. Nothing in
`BaseHost.reboot` changes for this, except §7.2.

## 6. The tests

`tests/e2e/chaos/test_reboot_chaos.py` is rewritten against the guest. Its module
docstring replaces the "NO BUSYBOX GUEST ARM" argument with why an Ubuntu guest under
QEMU is the only reboot target now.

**Fixture.** `chaos_guest` (session scope, in `tests/e2e/chaos/_bed.py` beside the bb1350
target, registered in the lane's conftest): no unix lease (the guest is not in the pool
and nothing else co-runs against it; the lane holds the bed lease), proves the guest
serves a shell over SSH and that `socat`, `bash` and `tc` answer `command -v`, failing
loud and guest-named with `make qemu-restart` as the remedy, never skipping. It yields a
`ChaosTarget` whose lab is `chaos` and whose far end is `test4`. A helper reads the guest's
boot id (`/proc/sys/kernel/random/boot_id`) so a scenario can wait for the **new** boot,
not merely for a shell: during a hold the old boot still answers.

**Scenarios.** Marks as today (`chaos`, `stability`, `integration`, `hops`,
`no_hygiene_bracket`, the `chaos_lane` xdist group, `timeout(900)`; a cycle is about three
minutes, so two cycles fit one test).

1. `test_happy_path_reboot_wait_recovers`: `host ubu2404-qemu reboot --wait` returns 0,
   the boot id changed, and an independent probe answers afterwards; the wall-clock is
   printed as today.
2. `test_reboot_x_tunnel_survivors_reaped`: `tunnel add --hosts
   test4@ubeth-data,ubu2404-qemu@eth2 --port 15220` (the spike built this in 21 s); a
   guest-side daemon is observed; reboot the guest; `tunnel remove --all --yes` reconciles
   and no survivor remains on test4 or the guest. The manual hygiene bracket moves to the
   guest.
3. `test_reboot_x_link_repair_idempotent`: impair `test4--ubu2404-qemu` with loss and an
   expire backstop; netem present on test4's `ubeth-data` and the guest's eth2; reboot the
   guest; netem gone on the guest, still on test4; `link repair --all` exits 0 and both
   ends are netem-free; the `finally` repairs again. The link target is a closed lab
   holding test4, the guest and only their link (the existing `closed_unix_link_target`
   shape, generalised to an endpoint pair).
4. `test_reboot_wait_honours_the_declared_down_window` (new, #979 + #435): set a 75 s hold
   and run `reboot --wait --down-timeout 30`: it fails naming the down phase; wait for the
   new boot; set the same hold and run with the resolved default (120): it passes, which
   is only true once the probe is stateless (§7.2). Both legs print their timings.

`BUSYBOX_CHAOS_ELEMENT` and `busybox_bed` (bb1350, telnet) stay for the modules that use
them. `tunnel_target`, `closed_unix_link_target`, `assert_eth2_netem_free` and
`observe_tunnel_processes` take their endpoint pair as a parameter instead of assuming
test1/test2, with the old pair as the default so the other modules do not change.

## 7. `reboot --wait`

### 7.1 `reboot_options` (#979)

A new options table in `otto.host.options`, `RebootOptions`, with `timeout` (600),
`down_timeout` (**120**, up from 60: the bed's own forced-shutdown bound is 90 s, and a
host that drops instantly loses nothing to a longer bound) and `poll_interval` (2). It
is registered wherever the other tables are: `src/otto/host/option_specs.py`,
`HostSpec` in `src/otto/host/specs.py` (a lab-data field, so the models-side spec and
its drift guard learn it), `src/otto/host/factory.py`'s table list, the
`[host_preferences."<selector>".reboot_options]` cascade in `src/otto/models/settings.py`,
`src/otto/run_context.py`, `src/otto/init/templates.py`, per-call `reboot_options=` on
`get_host()` / `all_hosts()`, and the docs table in `docs/configuration/host-options.md`.

`BaseHost.reboot(*, hard, wait, timeout=None, down_timeout=None, poll_interval=None)`:
`None` means the host's resolved table; a value is the final word. The three go through
`_validate_timeout` like `run`/`exec`'s, with `down_timeout == 0` the one spelling of
"skip the down phase" (negatives are refused like every other timeout). The CLI's
`--timeout`, `--down-timeout` and `--poll-interval` default to unset and say so in their
help ("the host's `reboot_options`"). The lab's lab.json declares nothing for
test1–test4 or the guest: the new default covers them.

`DEFAULT_REBOOT_DOWN_TIMEOUT` and the literal defaults in the signature go away; the table
is the one place. docs/cli/host/reboot.md and the host-options page say where the numbers
come from and in what order.

### 7.2 A stateless down probe (#435)

The spike's clean long-bound leg: a 75 s hold, `--down-timeout 120`, sshd stopped at
78 s, the kernel reset at 81 s, and otto reported "never went down within 120 s" with
not one failed probe in its log. The mechanism is #435's: `reboot` rebuilds the
connections once before the down phase, the first probe during the hold logs in and
repopulates `ConnectionManager`'s cached transport, and every later probe returns that
cached object without dialling, so a dead host reads as reachable until the bound.

The fix is the one #435 proposes: the down phase's reachability probe never reuses a
cached transport. It dials a throwaway connection, verifies it and closes it, every poll.
The up phase keeps today's behaviour (it starts from rebuilt, empty caches and a success
there is the real login). A unit pin in `tests/unit/host/test_reboot_recovery.py`: a fake
manager whose first dial succeeds and whose later fresh dials fail must make
`wait_until_down` return True, not time out. Scenario 4's second leg is the live proof.

## 8. Re-enabling the lanes

Once both chaos lanes pass on the reprovisioned bed, #985's steps as written: empty
`SUSPENDED_LANES`, delete the pinning test, fix the two docs sentences, and run both
lanes once more on main's tip before the release.

## 9. Landing and the live bed

Order of work: the seed renderer, the Vagrantfile and their hostless guards; the lab data
and its guard; `reboot_options` and the stateless probe; the rewritten module; the bed;
the lanes; the un-suspension. Separate commits per logical item (the spec; `feat(lab)!`
for the bed and lab data; `feat(host)!` for `reboot_options`; `fix(host)` for #435;
`test(chaos)` for the module; `chore(gate)` for the un-suspension).

The bed is provisioned from the owner's host checkout:
`vagrant provision zephyr --provision-with chaos-guest`, against a checkout that holds
this branch. The provisioner is idempotent and touches nothing but its own files, the
journal cap and its unit. Until the owner runs it, the branch cannot be proven on the live
bed; the session asks for that run when the branch is ready for its lane runs, holding
the bed lease while the guest boots, proves the guest serves a shell afterwards, and
reports. The spike's by-hand boot (same image, same seed shape, same wrapper) is the
rehearsal of exactly that run.

## 10. Left to the builder

- The amd64 image pin (the ARM64 one is `release-20261017`, checksum in the file the
  spike left on test4; both recorded in `CHAOS_GUEST` with the date).
- The x86_64 wrapper arm (machine type, SeaBIOS is the default).
- How `closed_unix_link_target` and its siblings take an endpoint pair without disturbing
  their pins.
- Whether the guest's shell-readiness probe is the fixture's own or shared with
  `busybox_bed`'s (one function, two callers).
