# otto link

`otto link` inspects and impairs the lab's **static links** — the topology
edges declared in `lab.json`'s `links` section, or derived from each host's
management `hop` (see {ref}`lab-links` in {doc}`../../configuration/lab-config`). A link is
where `tc qdisc` actually attaches; tunnels (see {doc}`../tunnel/index`) ride *over*
links but are never impaired directly — impairing a link a tunnel happens to
ride affects that tunnel realistically, for free. For why links and tunnels are
modelled as separate underlay and overlay layers, see
{doc}`../../../architecture/subsystems/network`.

Every capability below is a plain callable first — `otto link` is a thin CLI
wrapper over `otto.link.impair_link` / `repair_link` / `repair_all` /
`read_link_states`. See
[the link Python API](../../../library/network-api.md#the-link-python-api)
and the {doc}`API reference <../../../api/link>` to call them directly.

```{note}
Impairment state lives in the kernel, not in otto (see
{doc}`../../../architecture/subsystems/network`) — `otto link list` reads it back
live from `tc`. Both directions of a link are impaired by default; `--from`
narrows to one.
```

```{raw} html
:file: ../../../_static/generated/termynal/help-link.html
```

Inspect and impair the lab's static links (the topology edges `otto tunnel`
rides). Units and merge semantics are on {doc}`impair`; see also
{doc}`in-path`, {doc}`port-scoped` and {doc}`safety`.

```text
otto link impair <link> [--delay <time>] [--jitter <time>] [--loss <percent>] [--rate <rate>]
                         [--corrupt <percent>] [--duplicate <percent>] [--reorder <percent>]
                         [--from <host>] [--expire <seconds>]
otto link repair [<link>] [--all]
otto link list
```

## Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `impair` | Merge-apply netem parameters to a link's resolved placement(s) |
| `repair` | Clear a link's impairment(s) and cancel its timers, or every link with `--all` |
| `list` | List every static link's current impairment state |

## Options

| Option | Applies to | Description |
| ------ | ---------- | ----------- |
| `<link>` (argument) | `impair`, `repair` | Link id or name |
| `--delay` | `impair` | Delay; bare number = ms, or an explicit `us`/`ms`/`s` suffix |
| `--jitter` | `impair` | Jitter; requires a delay (given now or already applied) |
| `--loss` | `impair` | Packet loss; bare number = percent, or a `%` suffix |
| `--rate` | `impair` | Rate limit; an explicit tc unit is required (e.g. `10mbit`) |
| `--corrupt` | `impair` | Corruption; bare number = percent, or a `%` suffix |
| `--duplicate` | `impair` | Duplication; bare number = percent, or a `%` suffix |
| `--reorder` | `impair` | Reorder; requires a delay (given now or already applied) |
| `--from` | `impair` | Narrow to the direction originating at this host (both by default) |
| `--expire` | `impair` | Auto-clear this impairment after N seconds |
| `--all` | `repair` | Repair every static link in the lab |

## Previewing: `--dry-run`

`--dry-run` (`-n`) is a **global** option — `otto -n --lab veggies link impair
edge --delay 50ms`. **A dry run contacts no device at all**, not even for the
read-only commands. So every answer it gives comes from lab data and the
options you typed, and it says plainly what it could not check.

`otto link` is one of the commands that opts into a deeper preview instead of
stopping at the CLI seam — see {doc}`../dry-run` for the contract every command
follows, what the opt-in buys, and `--probe`.

```console
$ otto -n --lab veggies link impair edge --delay 50ms
dry run edge: no device was contacted — nothing was read and nothing was changed
  would: a->b on carrot_seed/eth1.100: tc qdisc replace dev eth1.100 root netem delay 50ms
  would: b->a on tomato_seed/eth1.200: tc qdisc replace dev eth1.200 root netem delay 50ms
  not checked: what is CURRENTLY applied to the netdev. A real run merges the given
    parameters over it per-param, so any command line above is the one a CLEAN netdev
    would get and nothing else …
  not checked: the two self-lockout refusals …
  not checked: the netdev's current SHAPE …
  not checked: live expire timers … and the post-apply verify …
```

Read both halves. The `would:` lines are exact command strings, but they are
built as though the netdev were clean — a re-impair [merges](impair.md#re-impairing-merge-per-param-last-one-wins)
over what is already applied and produces a different command. The
`not checked:` lines are the ones that change what you should conclude:

:::{warning}
**A dry run cannot tell you an impairment is safe to apply.** Both
[self-lockout refusals](safety.md#safety-rules) — the management interface and hop transit —
fire only on a *positive* match against the placement host's live
`ip -o addr show`, and a dry run does not run it. An impair that a real run
would refuse outright, because it would cut otto off from the bed, previews
here as an ordinary `tc` command line with a `not checked:` note beside it.
:::

What each command shows:

- **`impair`** — the placement (host and netdev) per direction and the exact
  `tc` line, for an *endpoint-mode* link. Refusals that need no device are
  still made, and made *early*: the local-host refusal, an unknown host, a
  `--port` against an impairer that has no selector support, and the
  `--expire` [bash refusal](impair.md#--expire-auto-clearing) — which in a real run only
  fires *after* that placement's qdisc mutation has been applied and rolled
  back.
- **`repair`** — the same placements, with each clear marked `only if`: whether
  a netdev carries anything to clear is a device read. It never prints
  `cleared …, timers cancelled N`; that line is three measurements.
- **`list`** — every row, with both direction cells reading `not read`. This is
  a distinct state from `-` (clean), `?` (host unreachable) and `!` (host
  answered, read failed), because it is distinct news: nothing was asked.

**In-path links preview much less.** A middlebox's facing netdev per direction
is resolved by subnet-matching its live address table, so a dry run cannot name
a single placement — and therefore has no command line, no current state and no
refusal to show for the link. It says so rather than guessing:

```console
$ otto -n --lab veggies link impair dataplane --delay 50ms
dry run dataplane: no device was contacted — nothing was read and nothing was changed
  not checked: every placement. 'pepper_seed' is this link's in-path middlebox, and which
    of its interfaces faces each endpoint is resolved by subnet-matching the middlebox's
    live `ip -o addr show`, which was not run …
```

Programmatically, a dry run is visible on the return value:
`ImpairReport.plan` / `RepairReport.plan` is a `DryRunPlan` (with `.would` and
`.unchecked`) and `applied` / `cleared` are empty; `LinkState.not_measured` is
`True`; and `repair_all` files previews under `RepairAllReport.planned` rather
than `repaired`, with `RepairAllReport.dry_run` recording the run itself —
check that rather than `planned`, since a lab whose links are all refused
previews nothing.

```{toctree}
:caption: Subcommands
:hidden:

impair
repair
list
```

```{toctree}
:caption: Topics
:hidden:

in-path
port-scoped
safety
```
