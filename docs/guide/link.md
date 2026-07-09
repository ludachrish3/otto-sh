# otto link

`otto link` creates, lists, and removes **host-resident tunnels** — pairs of
`socat` processes, tagged and spawned directly on lab hosts, that carry a
service's traffic from one host to another over a direct L2 hop.  It is the
live, manageable counterpart to the *static* links declared in `lab.json`
(see {ref}`lab-links` in {doc}`lab-config`): a declared link documents a
route that exists; `otto link add` actually stands one up.

Every capability is a plain callable first — `otto link` is a thin CLI
wrapper over `otto.link.add_link` / `remove_link` / `remove_all_links` /
`discover_dynamic_links` / `all_links`.  See the
{doc}`API reference <../api/link>` to call them directly from an instruction,
a suite, or your own script.

```{note}
`otto link` is direct-L2 only right now — `--hosts` takes exactly two hosts
that can reach each other's IP directly.  Multi-hop tunnels across an SSH
jump chain are a later phase.
```

## Creating a tunnel: `otto link add`

```text
otto link add --hosts <h1[@if1],h2[@if2]> --port <P> [--protocol tcp|udp] [--dest <host[@if]>]
```

```bash
otto --lab veggies link add --hosts carrot_seed,tomato_seed --port 6001
otto --lab veggies link add --hosts carrot_seed@eth1,tomato_seed@eth1 --port 6001 --protocol udp
```

| Option | Required | Description |
| ------ | -------- | ----------- |
| `--hosts` | yes | Ordered, comma-separated `host[@iface]` path. Exactly two hosts: the first is the **ingress** host (where clients connect), the second is the **exit** host. |
| `--port` | yes | The service port, used on **both** ends — a client sends to `--port` on the ingress host, and (absent `--dest`) it arrives at `--port` on the exit host. One value keeps a tunnel traceable by port at every hop. |
| `--protocol` | no (default `tcp`) | `tcp` or `udp`. Selects the `socat` address family on both bridge legs. |
| `--dest` | no (default: the exit host) | Relay the exit host's traffic on to a **third** host instead of terminating there — see *Relaying with `--dest`*, next. |

### `@iface` interface pinning

Each `--hosts` entry may pin a specific interface with `host@iface`, where
`iface` is a key in that host's `interfaces` map in `lab.json` (see
[Network interfaces](lab-config.md#network-interfaces)). The pin is only
**required** when the host defines more than one interface — with zero or
one interface, otto resolves it automatically. Naming an interface the host
doesn't have, or omitting `@iface` on a host with more than one, is a
load-time error that lists the interfaces it does have.

### Relaying with `--dest`

By default the tunnel terminates at the exit host: traffic sent to the
ingress host on `--port` arrives at the exit host on `--port`. Passing
`--dest C` keeps the tunnel's two `socat` processes on the same ingress/exit
hosts but has the **exit host relay onward to `C`**, so the packet C receives
is sourced from the exit host's own interface — an ordinary `exit → C`
packet, not a loopback- or SSH-sourced one the way an `ssh -L` forward
would deliver it:

```bash
otto --lab veggies link add --hosts carrot_seed,tomato_seed --port 6001 --dest sprout
```

Here `carrot_seed` is ingress, `tomato_seed` is exit, and `sprout` is where
the traffic actually lands — appearing to `sprout` as if it came directly
from `tomato_seed`.

### Conflicts and preconditions

`add` resolves both endpoints (and `--dest`), computes the tunnel's id, and
refuses if a tunnel with that exact id already exists across
{func}`~otto.link.discovery.all_links` (implicit hop edges, declared links, and other
dynamic tunnels). Because the id excludes the port from its route hash but
includes it in the readable suffix, a second tunnel on the same
route+protocol+**port** is a conflict — a different port on the same route
is a separate, coexisting tunnel.

`add` spawns the tagged processes and reports where they ended up; it does
**not** pre-validate L2 reachability or guarantee end-to-end delivery — a
broken path shows up as a tunnel that carries no traffic, not an `add`
error. `add` only errors on endpoint resolution, a conflicting id, or a
failed process spawn (including a missing `socat`/`bash` — see
[Host requirements](#host-requirements) below).

## Listing tunnels: `otto link list`

```bash
otto --lab veggies link list
otto --lab veggies link list --all
```

By default `otto link list` shows **dynamic tunnels only** — the live
`otto link add` results discovered on the lab's hosts right now. Pass
`--all` to fold in implicit hop edges and declared `lab.json` links
alongside them.

## Removing tunnels: `otto link remove`

```bash
otto link remove <id>
otto link remove --all
otto link remove --all -y
```

`remove <id>` discovers the tagged processes for that id across every host
that might be running one, and kills them. `remove --all` reaps **every**
otto tunnel it finds — not just ones this invocation or this user created;
tunnel ownership isn't tracked (see [Link identity](#link-identity) below).
Because `--all` is destructive and owner-agnostic, it asks for confirmation
first; pass `-y` / `--yes` to skip the prompt (e.g. from a script or CI
cleanup step).

## Link identity

- **Dynamic tunnels** (created by `otto link add`) get an id of the form
  `lnk-<hex>-<port>` — a 12-hex-character hash of the route (endpoints +
  protocol, sorted so `a<->b` and `b<->a` match) plus a readable `-<port>`
  suffix, e.g. `lnk-0a17f76fb561-6001`. The port stays visible in `list`, in
  `remove <id>`, and in every tagged process's `argv[0]`, so two tunnels on
  the same route with different ports are visibly distinct tunnels.
- **Static links** (implicit hop edges, declared `lab.json` links) get a
  readable handle instead — the declared `name` if the link has one,
  otherwise `<a-host>--<b-host>` (endpoints sorted the same way). They never
  wear the `lnk-<hex>` form because they aren't otto tunnels.
- Dynamic and static ids live in disjoint id-spaces, so a live tunnel and
  the declared route it happens to realize coexist as two separate rows in
  `otto link list --all` rather than colliding or replacing one another.

## Host-down behavior

`otto link` is best-effort and transparent about failure, never silently
wrong:

- **`list` / discovery** shows tunnels found on every host it could reach,
  and emits a warning naming each host it couldn't. It never silently drops
  a host from the picture.
- **`remove`** kills tunnels on every host it could reach, names the hosts
  it couldn't, and **exits non-zero** when any host was unreachable — so a
  script checking the exit code learns the reap was incomplete instead of
  being told it succeeded while a stray `socat` may still be running.

## Host requirements

A host can only **host** a tunnel endpoint — be named in `--hosts`, or be
scanned by discovery — if it has a working `bash` (for the `exec -a`
argv-tagging trick tunnel processes use to stay discoverable) and `socat`
on its `PATH`. Missing either fails `add` loudly, naming the host; there is
no auto-install.

Whether a host qualifies is the
[`has_bash`](lab-config.md#common-optional) capability, not a check against
a specific host class: it defaults to `true` for Unix hosts (including the
built-in `local` host and Docker containers) and `false` for embedded
targets, and can be overridden per host in `lab.json` for a host that
defies the norm. `add` live-checks both `bash` and `socat` (`command -v`)
on its two endpoint hosts regardless; `has_bash` is the declared capability
that separately gates which hosts discovery (`list`, `remove`) bothers to
scan at all.

## Monitor compatibility

Tunnel discovery is built as a `(command, pure parser)` pair — one portable
`ps` command per host plus a pure function turning its output into
{class}`~otto.link.discovery.Observation` records — the same shape the
monitor's {class}`~otto.monitor.parsers.MetricParser` contract expects
(command, parse, interval). This phase keeps `otto.link` monitor-free;
wiring tunnel discovery into `otto monitor` as a first-class parser (with
topology/edge views) is a later phase. See
[Custom parsers](monitor.md#custom-parsers) in {doc}`monitor` for the
parser contract this is designed to plug into.

## Shell completion

`--hosts` tab-completes lab host ids; once a host and a comma are typed, the
remaining candidates narrow to hosts on the same `/24` as the last one
entered (a simple heuristic — true per-interface subnet awareness comes
later).

`remove <id>` tab-completes from a short-lived cache (2-minute TTL) of
tunnel ids. `otto link list` (default, non-`--all` mode) populates it with
a fresh live scan on its way out; `otto link add` does not touch it.
`otto link remove` empties it rather than repopulating it, so a `list`
right after a `remove` is what re-warms completion for what's left. A
cold or emptied cache simply offers no suggestions until the next `list`.

## Library API

Every command above has a callable counterpart in `otto.link` — see the
{doc}`API reference <../api/link>` for full signatures:

```python
from otto.cli.run import instruction
from otto.configmodule import get_lab
from otto.link import add_link, all_links, discover_dynamic_links, remove_link

@instruction()
async def add_tunnel():
    lab = get_lab()
    added = await add_link(lab, [("carrot_seed", None), ("tomato_seed", None)], port=6001)
    live = await discover_dynamic_links(lab)          # dynamic tunnels only
    everything = await all_links(lab)                 # + implicit + declared
    report = await remove_link(lab, added.link.id)
```
