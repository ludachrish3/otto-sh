# Endpoints & host requirements

## Docker container endpoints

A Docker container host may be a tunnel **endpoint** — the first or last
`--hosts` entry — but never an intermediate relay hop, and its chain
neighbor must be **its own parent host** (the docker-capable host that runs
it). `add` rejects any other placement or neighbor at add time, naming the
parent it expected.

Docker is a **testing aid, never a requirement**: no tunnel command ever
starts a container. `add` requires a container endpoint to already be
running (start it with `otto docker up` first) and fails loudly when it
isn't; `list` and `remove` probe a declared-but-down container read-only
and treat it as carrying no tunnel processes — scanning a lab never
composes a docker stack as a side effect.

A container entry never takes `@iface` — containers have no modeled
`interfaces` — its data-plane IP is instead resolved through its parent via
`docker inspect` at add time. The container's two tagged `socat` processes
launch through the **container's own** command execution (a `docker exec`
by way of the parent), and because containers have no systemd user
manager, the launch always falls back to the `setsid`-detached path (see
*Old-OS portability* below) rather than `systemd-run --user`.

```bash
otto --lab veggies tunnel add --hosts sprout,carrot_seed,carrot_seed.compose.web --port 8080
```

Here `carrot_seed.compose.web` is a container whose parent is `carrot_seed`
— a valid chain because the container neighbors its own parent.

## Host requirements

A host can only carry a tunnel process — appear in `--hosts`, or be scanned
by discovery/removal — if it has a working `bash` (for the `exec -a`
argv-tagging trick tunnel processes use to stay discoverable) and `socat`
on its `PATH`. Missing either fails `add` loudly, naming the host; there is
no auto-install. This applies to every hop in the chain, not just the
endpoints.

Whether a host qualifies is the
[`has_bash`](../../configuration/lab-config.md#common-optional) capability, not a check against
a specific host class: it defaults to `true` for Unix hosts (including the
built-in `local` host and Docker containers) and `false` for embedded
targets, and can be overridden per host in `lab.json` for a host that
defies the norm. `add` live-checks both `bash` and `socat` (`command -v`)
on every chain host regardless; `has_bash` is the declared capability that
separately gates which hosts discovery (`list`, `remove`) bothers to scan
at all.

