# otto tunnel

`otto tunnel` creates, lists, and removes **host-resident bidirectional
tunnels** — an ordered chain of `socat` processes, tagged and spawned
directly on lab hosts, that carries a service's traffic end-to-end across
one or more hops. A tunnel rides one or more **links**: the topology edges
declared in `lab.json`, or derived from each host's management `hop` (see
{ref}`lab-links` in {doc}`../../configuration/lab-config`). Links document routes that exist;
`otto tunnel add` is what actually stands traffic up over them — one `add`
builds exactly one tunnel, so a second `add` on the same route with a
different port is a second, coexisting tunnel. Links are the static
underlay, tunnels the dynamic overlay riding it (see
{doc}`../../../architecture/subsystems/network` for the model) — for impairing a
link's traffic (delay, loss, rate, ...) rather than tunneling over it, see
{doc}`../link/index`.

Every capability is a plain callable first — `otto tunnel` is a thin CLI
wrapper over `otto.tunnel.add_tunnel` / `remove_tunnel` /
`remove_all_tunnels` / `discover_tunnels`. See the
{doc}`API reference <../../../api/tunnel>` to call them directly from an
instruction, a suite, or your own script.

```{note}
Every tunnel is **bidirectional** — a new flow can originate from either
end, each served by its own mirrored chain of processes. There is no
`--one-way` flag.
```

```{raw} html
:file: ../../../_static/generated/termynal/help-tunnel.html
```

Create, list, and remove host-resident bidirectional tunnels. Multi-hop
chains are on {doc}`add`; see also {doc}`endpoints`, {doc}`identity` and
{doc}`portability`.

```text
otto tunnel add    --hosts <h0[@if],h1[@if],...,hn-1[@if]> --port <P> [--protocol tcp|udp] [--dest <host[@if]>] [--carrier <name>]
otto tunnel list
otto tunnel remove [<id>] [--all] [-y]
```

## Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `add` | Create a bidirectional tunnel along an explicit host path (two or more hosts) |
| `list` | List every live tunnel discovery finds right now |
| `remove` | Remove a tunnel by id, or every tunnel with `--all` |

## Options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `--hosts` | `add` | Ordered `host[@iface]` path, two or more entries; `@iface` only needed when a host has more than one interface |
| `--port` | `add` | Service port, used at both endpoints |
| `--protocol` | `add` | `tcp` (default) or `udp` |
| `--dest` | `add` | Far-end delivery override; defaults to loopback on the last `--hosts` entry |
| `--all` | `remove` | Reap every otto tunnel |
| `-y, --yes` | `remove` | Skip the `--all` confirmation prompt |
| `<id>` (argument) | `remove` | Id of the tunnel to remove |

## Previewing: `--dry-run`

`--dry-run` (`-n`) is a **global** option — `otto -n --lab veggies tunnel add
…`. **A dry run contacts no device at all**, not even for the read-only probes
and not even for docker's container-liveness check. So every answer it gives
comes from `lab.json` and the options you typed, and it says plainly what it
could not check.

`otto tunnel` is one of the commands that opts into a deeper preview instead of
stopping at the CLI seam — see {doc}`../dry-run` for the contract every command
follows, what the opt-in buys, and `--probe`.

```console
$ otto -n --lab veggies tunnel add --hosts carrot_seed@eth2,pepper_seed@eth2,tomato_seed@eth2 --port 8080
dry run carrot_seed <-> tomato_seed: no device was contacted — nothing was read and nothing was changed
  would: build tun-80b8500dedcf-8080: carrot_seed@eth2 -> pepper_seed@eth2 -> tomato_seed@eth2,
    tcp:8080, delivering to 127.0.0.1 on tomato_seed
  would: carry fwd traffic on port 49152 and rev on 49153 — PROVISIONAL, see the first
    `not checked` line
  would: start each process below detached, with its argv[0] replaced by an `otto-tunnel:v1`
    sentinel …
  would: tomato_seed fwd/egress: socat TCP4-LISTEN:49152,fork,reuseaddr TCP4:127.0.0.1:8080
  would: pepper_seed fwd/relay: socat TCP4-LISTEN:49152,fork,reuseaddr TCP4:192.168.1.12:49152
  would: carrot_seed fwd/ingress: socat TCP4-LISTEN:8080,bind=192.168.1.11,fork,reuseaddr
    TCP4:192.168.1.13:49152
  … (2n lines: one per process, fwd then rev)
  not checked: which ports are already bound anywhere on the chain …
  not checked: whether this tunnel already exists …
  not checked: whether the chain hosts actually have socat and/or bash …
  not checked: the 6 launches themselves and the post-add verify …
```

Read both halves. The `would:` lines are the exact argv, but:

:::{warning}
**The two carrier ports are provisional, and every argv above names them.** A
real `add` first probes every hop with `ss -Htln` / `netstat -tln` and skips
what is already listening; a dry run has only your `--port` to go on, so it
picks 49152/49153 on every lab. If a real run finds either taken, all 2n
command lines change. `AddedTunnel.carrier_fwd` / `carrier_rev` are `None`
under a dry run for exactly this reason — the provisional pair is in the plan,
labelled, and nowhere else.
:::

What each command shows:

- **`add`** — the resolved chain with its addresses, the tunnel id (which
  hashes path + protocol + port, none of it read off a device), the 2n argv,
  and the provisional carrier pair. Refusals that need no device are still
  made: an unknown host, an ambiguous or unknown `@iface`, a chain shorter
  than two hops, a repeated host, a container in an illegal position, a
  `--dest` inside the path, an unsupported protocol, and the
  [`has_bash=False` refusal](endpoints.md#host-requirements).
- **`remove`** — the *scope* of the reap: which `has_bash` hosts would be
  scanned, and what the kill would match. It never prints
  `removed (none found)`; that line is a claim about live processes.
- **`list`** — one line saying no host was scanned. Every row of that table is
  an observed process, so there is nothing left to show; a dry run also leaves
  the `remove <TAB>` completion cache alone rather than emptying it from a scan
  that never ran. The exception is a lab that declares no `has_bash` host:
  there is nothing to scan, so `list -n` gives the same complete, empty answer
  a real run gives — banner and all — including the cache write.

**A container endpoint previews much less.** A container's tunnel address is
its docker bridge ip, read with `docker inspect` on the parent, and the hops
either side of it connect *to* that address — so a dry run has no argv to show
for any hop, not just for the container. It says so rather than guessing:

```console
$ otto -n --lab veggies tunnel add --hosts carrot_seed.repo1.api,carrot_seed@eth2,tomato_seed@eth2 --port 8080
dry run carrot_seed.repo1.api <-> tomato_seed: no device was contacted — nothing was read and nothing was changed
  would: build tun-91e5f6330d1d-8080: carrot_seed.repo1.api -> carrot_seed@eth2 -> tomato_seed@eth2,
    tcp:8080, delivering to 127.0.0.1 on tomato_seed
  would: carry fwd traffic on port 49152 and rev on 49153 — PROVISIONAL …
  not checked: every process argv, because 'carrot_seed.repo1.api' is a container endpoint …
```

Programmatically, a dry run is visible on the return value:
`AddedTunnel.plan` / `RemovedReport.plan` is a `DryRunPlan` (with `.would` and
`.unchecked`), and `TunnelDiscovery.not_measured` is `True` whenever a scan was
declined — a third state, distinct from "reachable and empty" and from
`unreachable`, because nothing was asked. `discover_tunnel_records` (the monitor's source) raises
`TunnelNotMeasuredError` rather than returning `[]`, so the dashboard keeps its
last known tunnel set instead of blanking the overlay.

## Shell completion

`--hosts` tab-completes lab host ids; once a host and a comma are typed,
the remaining candidates narrow to hosts on the same `/24` as the
last-entered one — a simple heuristic with no per-interface subnet or
container parent-adjacency awareness.

`remove <id>` tab-completes from a short-lived cache (2-minute TTL) of
tunnel ids. `otto tunnel list` populates it with a fresh live scan on its
way out; `otto tunnel add` does not touch it. `otto tunnel remove` empties
it rather than repopulating it, so a `list` right after a `remove` is what
re-warms completion for what's left. A cold or emptied cache simply offers
no suggestions until the next `list`.

```{toctree}
:caption: Subcommands
:hidden:

add
list
remove
```

```{toctree}
:caption: Topics
:hidden:

identity
endpoints
portability
```
