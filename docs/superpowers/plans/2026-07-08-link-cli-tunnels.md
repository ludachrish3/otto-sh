# `otto link` CLI + live tunnels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `otto link` command group (add / list / remove) and the live
discovery + host-resident socat tunnels behind it, turning the merged foundation's
static link model into a manageable live one.

**Architecture:** A pure command-builder layer (`socat.py`), an async
orchestration layer (`manage.py`), and the wired discovery layer (`discovery.py`),
all callable as plain library functions; a thin Typer CLI (`cli/link.py`) consumes
them. Dynamic tunnels are host-resident, sentinel-tagged socat processes discovered
live via a portable `ps`; nothing is persisted.

**Tech Stack:** Python 3.10+, Typer, asyncssh (via existing `host.oneshot`),
`socat`/`bash` on lab hosts, the existing `otto.link` package + completion cache.

## Global Constraints

- **Python 3.10+ real annotations.** NEVER `from __future__ import annotations`
  (breaks the Sphinx nitpicky `-W` docs gate). Module-top imports only.
- **No new Python dependencies.** Runtime host tools (`socat`, `bash`) are required
  on tunnel-hosting hosts; fail loud + name the host if missing (no auto-install).
- **Old-OS portability (down to Linux 2.6.32 / procps 3.2.8):** discovery uses
  `ps -eo pid=,etime=,args=` (formatted `etime`, NOT `etimes`); NEVER `pgrep -a`.
  socat addresses stay old-stable (`UDP4-LISTEN`, `TCP4-LISTEN`, `fork`,
  `reuseaddr`). Tagging is `bash -c 'exec -a "$1" socat "${@:2}"' …` (bash required;
  not dash/busybox).
- **Sentinel wire format v1 is frozen** — no version bump, no segment change. The
  id *value* now carries a `-<port>` suffix; the segment layout is untouched.
- **`make_link_id` route hash is frozen** — do not alter its algorithm. Dynamic ids
  append the port suffix; static ids use a separate readable builder.
- **Logging:** internal host I/O runs at `LogMode.QUIET`; only warnings/errors reach
  the console. `otto link` commands create NO per-invocation output directory.
- **Library-first:** every capability is a plain callable in `otto.link`; the CLI is
  a thin consumer.
- **Commits (worktree self-commit):** conventional prefix + a trailing
  `Assisted-by: Claude Opus 4.8` line. NEVER `git add -u`; stage exact paths.
- **Gate per task:** `make coverage` (the scoped task gate). A typecheck round
  (`nox -s typecheck`) after any `src/` edit, since `ty` runs only there.

---

## File Structure

- **Create `src/otto/link/socat.py`** — pure command/argv builders (socat ingress
  /egress, the `bash exec -a` launch string, the discovery `ps` command, the
  free-port probe command + pure parser + picker). Zero I/O.
- **Create `src/otto/link/manage.py`** — async `add_link` / `remove_link` /
  `remove_all_links` + the `AddedTunnel` / `RemovedReport` report dataclasses.
- **Modify `src/otto/link/model.py`** — provenance-aware id assignment
  (`make_dynamic_link_id`, `make_static_link_id`, `Link.__post_init__`).
- **Modify `src/otto/link/discovery.py`** — the `Observation` record +
  `parse_process_discovery` + `etime` parser; wire `discover_dynamic_links`;
  update `all_links` docstring.
- **Modify `src/otto/link/__init__.py`** — re-export the public API.
- **Modify `src/otto/configmodule/completion_cache.py`** — the `__dynamic_links__`
  reserved key (read / record / warm).
- **Create `src/otto/cli/link.py`** — the `otto link` Typer group + completers.
- **Modify `src/otto/cli/builtin_commands.py`** — register `link`.
- **Create `tests/unit/link/test_socat.py`, `test_link_id.py`,
  `test_discovery_parse.py`, `test_manage.py`, `test_link_cli.py`,
  `tests/unit/configmodule/test_completion_cache_links.py`** — hostless units.
- **Create `tests/e2e/test_link_tunnels_e2e.py`** — live-bed e2e.
- **Modify `docs/`** — `otto.link` API page + a new `otto link` CLI guide.

---

## Task 1: Provenance-aware link ids

**Files:**
- Modify: `src/otto/link/model.py`
- Test: `tests/unit/link/test_link_id.py`
- Modify (fixup existing expectations): `tests/unit/link/` foundation tests that
  assert `lnk-` ids for implicit/declared links.

**Interfaces:**
- Consumes: existing `make_link_id(a, b, protocol) -> str`, `LinkEndpoint`,
  `Provenance`, `Link`.
- Produces:
  - `make_dynamic_link_id(a: LinkEndpoint, b: LinkEndpoint, protocol: str, port: int) -> str`
    → `"{make_link_id(a,b,protocol)}-{port}"` (e.g. `lnk-ab12cd34ef56-161`).
  - `make_static_link_id(a: LinkEndpoint, b: LinkEndpoint, name: str | None) -> str`
    → `name` if truthy, else `"{lo.host}--{hi.host}"` (endpoints sorted by
    `_endpoint_key`).
  - `Link.__post_init__` assigns the id by provenance when `id` is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_link_id.py
from otto.link.model import (
    Link, LinkEndpoint, Provenance, make_dynamic_link_id, make_static_link_id,
    make_link_id,
)


def test_dynamic_id_appends_port_suffix():
    a = LinkEndpoint(host="test1", interface="eth0", port=161)
    b = LinkEndpoint(host="test2", interface="eth0", port=161)
    route = make_link_id(a, b, "udp")
    assert make_dynamic_link_id(a, b, "udp", 161) == f"{route}-161"


def test_dynamic_link_computes_suffixed_id():
    a = LinkEndpoint(host="test1", interface="eth0", port=161)
    b = LinkEndpoint(host="test2", interface="eth0", port=161)
    link = Link(a=a, b=b, protocol="udp", provenance=Provenance.DYNAMIC)
    assert link.id == f"{make_link_id(a, b, 'udp')}-161"


def test_static_declared_id_uses_name_when_present():
    a = LinkEndpoint(host="test1")
    b = LinkEndpoint(host="test2")
    link = Link(a=a, b=b, protocol="tcp", provenance=Provenance.DECLARED, name="mgmt")
    assert link.id == "mgmt"


def test_static_id_falls_back_to_sorted_endpoints():
    a = LinkEndpoint(host="test2")
    b = LinkEndpoint(host="test1")
    link = Link(a=a, b=b, protocol="ssh", provenance=Provenance.IMPLICIT)
    assert link.id == "test1--test2"  # sorted, so a<->b == b<->a


def test_explicit_id_is_preserved():
    a = LinkEndpoint(host="test1", port=161)
    b = LinkEndpoint(host="test2", port=161)
    link = Link(a=a, b=b, protocol="udp", provenance=Provenance.DYNAMIC, id="lnk-x-161")
    assert link.id == "lnk-x-161"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_link_id.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_dynamic_link_id'`.

- [ ] **Step 3: Write minimal implementation**

In `src/otto/link/model.py`, add after `make_link_id`:

```python
def make_dynamic_link_id(a: LinkEndpoint, b: LinkEndpoint, protocol: str, port: int) -> str:
    """Id for a dynamic tunnel: the route hash plus a readable ``-<port>`` suffix.

    The suffix keeps the port visible in the id, in ``otto link list``, in
    ``remove <id>``, and in every tagged process's ``argv[0]``. Distinct ports
    on the same route are therefore distinct tunnels.
    """
    return f"{make_link_id(a, b, protocol)}-{port}"


def make_static_link_id(a: LinkEndpoint, b: LinkEndpoint, name: str | None) -> str:
    """Readable handle for a static link — the declared ``name`` or ``a--b``.

    No hash: static links (implicit hop edges, declared routes) are described
    connectivity, not otto tunnels, so they never wear the ``lnk-<hex>`` form.
    Endpoints are sorted so ``a<->b`` and ``b<->a`` yield the same handle.
    """
    if name:
        return name
    lo, hi = sorted((a, b), key=_endpoint_key)
    return f"{lo.host}--{hi.host}"
```

Then replace `Link.__post_init__`:

```python
    def __post_init__(self) -> None:
        if self.id:
            return
        if self.provenance is Provenance.DYNAMIC:
            port = self.a.port if self.a.port is not None else self.b.port
            new_id = make_dynamic_link_id(self.a, self.b, self.protocol, port or 0)
        else:
            new_id = make_static_link_id(self.a, self.b, self.name)
        object.__setattr__(self, "id", new_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_link_id.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Fix foundation tests that expected `lnk-` for static links**

Run the existing link suite to find breakages:
Run: `uv run pytest tests/unit/link/ -v`
For each failure asserting a `lnk-<hex>` id on an IMPLICIT or DECLARED link,
update the expectation to the readable handle (`local--test1`, the declared
`name`, or `a--b`). Do NOT weaken any dynamic-id or sentinel assertion.

- [ ] **Step 6: Commit**

```bash
git add src/otto/link/model.py tests/unit/link/test_link_id.py tests/unit/link/
git commit -m "$(printf 'feat(link): provenance-aware ids (dynamic -<port> suffix, readable static handles)\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 2: socat command builders + discovery/probe commands (pure)

**Files:**
- Create: `src/otto/link/socat.py`
- Test: `tests/unit/link/test_socat.py`

**Interfaces:**
- Consumes: `Link`, `encode_sentinel(link) -> str` (from `otto.link.sentinel`).
- Produces:
  - `ingress_socat_args(protocol: str, service_port: int, exit_ip: str, carrier_port: int) -> list[str]`
  - `egress_socat_args(protocol: str, service_port: int, dest_ip: str, carrier_port: int) -> list[str]`
  - `launch_command(sentinel: str, socat_args: list[str]) -> str` — a detached,
    argv[0]-tagged shell command for `host.oneshot`.
  - `DISCOVERY_PS_COMMAND: str`
  - `FREE_PORT_PROBE_COMMAND: str`
  - `parse_listening_ports(output: str) -> set[int]`
  - `pick_free_port(used: set[int], lo: int = 49152, hi: int = 65535) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_socat.py
from otto.link.socat import (
    ingress_socat_args, egress_socat_args, launch_command,
    DISCOVERY_PS_COMMAND, FREE_PORT_PROBE_COMMAND, parse_listening_ports,
    pick_free_port,
)


def test_ingress_udp_bridges_udp_listen_to_tcp_carrier():
    assert ingress_socat_args("udp", 161, "10.0.0.2", 50001) == [
        "socat", "UDP4-LISTEN:161,fork,reuseaddr", "TCP4:10.0.0.2:50001",
    ]


def test_egress_udp_bridges_tcp_carrier_to_udp_dest():
    assert egress_socat_args("udp", 161, "10.0.0.9", 50001) == [
        "socat", "TCP4-LISTEN:50001,fork,reuseaddr", "UDP4:10.0.0.9:161",
    ]


def test_tcp_tunnel_uses_tcp_on_both_legs():
    assert ingress_socat_args("tcp", 8080, "10.0.0.2", 50002) == [
        "socat", "TCP4-LISTEN:8080,fork,reuseaddr", "TCP4:10.0.0.2:50002",
    ]
    assert egress_socat_args("tcp", 8080, "10.0.0.9", 50002) == [
        "socat", "TCP4-LISTEN:50002,fork,reuseaddr", "TCP4:10.0.0.9:8080",
    ]


def test_launch_command_tags_argv0_and_detaches():
    cmd = launch_command("otto-link:v1:lnk-x-161:udp:test1::161:test2::161",
                         ["socat", "UDP4-LISTEN:161,fork,reuseaddr", "TCP4:10.0.0.2:50001"])
    assert cmd.startswith("setsid bash -c 'exec -a \"$1\" socat \"${@:2}\"' _ ")
    assert "otto-link:v1:lnk-x-161:udp:test1::161:test2::161" in cmd
    assert cmd.rstrip().endswith("</dev/null >/dev/null 2>&1 &")


def test_discovery_command_is_portable():
    # portable etime (not etimes), no pgrep -a, and tolerant of no matches
    assert "ps -eo pid=,etime=,args=" in DISCOVERY_PS_COMMAND
    assert "etimes" not in DISCOVERY_PS_COMMAND
    assert "pgrep" not in DISCOVERY_PS_COMMAND
    assert DISCOVERY_PS_COMMAND.rstrip().endswith("|| true")


def test_parse_listening_ports_extracts_from_ss_and_netstat():
    ss = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:6010 0.0.0.0:*"
    assert parse_listening_ports(ss) == {22, 6010}


def test_pick_free_port_skips_used():
    assert pick_free_port({49152, 49153}, lo=49152, hi=49155) == 49154


def test_pick_free_port_raises_when_exhausted():
    import pytest
    with pytest.raises(RuntimeError):
        pick_free_port({49152, 49153, 49154}, lo=49152, hi=49154)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_socat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.link.socat'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/otto/link/socat.py
"""Pure command/argv builders for host-resident socat tunnels — no I/O.

Every value here is a string or list of strings destined for ``host.oneshot``;
running nothing keeps the whole module unit-testable (assert exact argv).
"""

import re
import shlex

# Old-stable socat address keywords only (compatible down to procps/socat on
# Linux 2.6.32). ``fork`` lets one listener serve repeated datagrams/connections;
# ``reuseaddr`` avoids TIME_WAIT bind failures on teardown+re-add.
_LISTEN = {"udp": "UDP4-LISTEN", "tcp": "TCP4-LISTEN"}
_DELIVER = {"udp": "UDP4", "tcp": "TCP4"}

# Discovery: portable ``ps`` (formatted ``etime`` — ``etimes`` is procps>=3.3,
# too new for 2.6.32-era userland); ``|| true`` so a no-match grep (exit 1) is
# not treated as a command failure.
DISCOVERY_PS_COMMAND = "ps -eo pid=,etime=,args= 2>/dev/null | grep -a ' otto-link:' || true"

# Free-port probe on the exit host: ss preferred, netstat fallback (both exist on
# CentOS 6). Parsed by parse_listening_ports.
FREE_PORT_PROBE_COMMAND = "ss -Htln 2>/dev/null || netstat -tln 2>/dev/null || true"

_PORT_RE = re.compile(r":(\d{1,5})\b")


def ingress_socat_args(protocol: str, service_port: int, exit_ip: str, carrier_port: int) -> list[str]:
    """Ingress socat (runs on the source host): accept client traffic on the
    service port, ship it over the TCP carrier to the exit host."""
    listen = _LISTEN[protocol]
    return [
        "socat",
        f"{listen}:{service_port},fork,reuseaddr",
        f"TCP4:{exit_ip}:{carrier_port}",
    ]


def egress_socat_args(protocol: str, service_port: int, dest_ip: str, carrier_port: int) -> list[str]:
    """Egress socat (runs on the exit host): accept the TCP carrier, deliver to
    the destination on the service port (``dest_ip`` = the exit host itself, or a
    relay target for ``--dest``)."""
    deliver = _DELIVER[protocol]
    return [
        "socat",
        f"TCP4-LISTEN:{carrier_port},fork,reuseaddr",
        f"{deliver}:{dest_ip}:{service_port}",
    ]


def launch_command(sentinel: str, socat_args: list[str]) -> str:
    """A detached, argv[0]-tagged launch line for ``host.oneshot``.

    ``bash -c 'exec -a "$1" socat "${@:2}"' _ <sentinel> <socat args…>`` sets the
    process's ``argv[0]`` to the sentinel (``exec -a`` — a bash builtin; bash is
    required on tunnel hosts). ``setsid`` + stdio-to-/dev/null + ``&`` detach it so
    it outlives the ``otto link add`` invocation.
    """
    inner = shlex.quote("exec -a \"$1\" socat \"${@:2}\"")
    tagged = " ".join(shlex.quote(a) for a in (sentinel, *socat_args))
    return f"setsid bash -c {inner} _ {tagged} </dev/null >/dev/null 2>&1 &"


def parse_listening_ports(output: str) -> set[int]:
    """Every port appearing as ``:<port>`` in ss/netstat output (a safe superset
    of used ports — we only need to avoid them)."""
    return {int(m) for m in _PORT_RE.findall(output) if 0 < int(m) < 65536}


def pick_free_port(used: set[int], lo: int = 49152, hi: int = 65535) -> int:
    """First port in ``[lo, hi]`` not in ``used``. Raises when exhausted."""
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    raise RuntimeError(f"no free TCP port in [{lo}, {hi}]")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_socat.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/socat.py tests/unit/link/test_socat.py
git commit -m "$(printf 'feat(link): pure socat argv + launch/discovery/probe command builders\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 3: Process-discovery parser (per-host observation)

**Files:**
- Modify: `src/otto/link/discovery.py`
- Test: `tests/unit/link/test_discovery_parse.py`

**Interfaces:**
- Consumes: `parse_sentinel(token) -> Link | None` (from `otto.link.sentinel`),
  `Link`.
- Produces:
  - `Observation` frozen dataclass: `pid: int`, `age_seconds: int`, `link: Link`.
  - `parse_etime(text: str) -> int` — procps `[[DD-]HH:]MM:SS` → seconds.
  - `parse_process_discovery(ps_output: str) -> list[Observation]` — one per
    tagged process line; non-otto lines ignored.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_discovery_parse.py
from otto.link.discovery import Observation, parse_etime, parse_process_discovery


def test_parse_etime_formats():
    assert parse_etime("05") == 5            # SS (rare) — treat as seconds
    assert parse_etime("01:05") == 65        # MM:SS
    assert parse_etime("02:01:05") == 7265   # HH:MM:SS
    assert parse_etime("1-02:01:05") == 93665  # DD-HH:MM:SS


def test_parse_process_discovery_extracts_pid_age_link():
    out = (
        "  4021    01:05 otto-link:v1:lnk-abc-161:udp:test1::161:test2::161 "
        "UDP4-LISTEN:161,fork,reuseaddr TCP4:10.0.0.2:50001\n"
    )
    obs = parse_process_discovery(out)
    assert len(obs) == 1
    assert obs[0].pid == 4021
    assert obs[0].age_seconds == 65
    assert obs[0].link.id == "lnk-abc-161"
    assert obs[0].link.protocol == "udp"


def test_parse_process_discovery_excludes_non_otto():
    out = (
        "  777    10:00 socat UDP4-LISTEN:53,fork TCP4:1.2.3.4:53\n"
        "  778    00:30 otto-link:v1:lnk-x-161:udp:a::161:b::161 socat ...\n"
    )
    obs = parse_process_discovery(out)
    assert [o.link.id for o in obs] == ["lnk-x-161"]


def test_parse_process_discovery_ignores_garbage_lines():
    assert parse_process_discovery("\n   \nnot a ps line\n") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_discovery_parse.py -v`
Expected: FAIL — `ImportError: cannot import name 'Observation'`.

- [ ] **Step 3: Write minimal implementation**

Add to the top of `src/otto/link/discovery.py` (after the existing imports):

```python
from dataclasses import dataclass

from .sentinel import SENTINEL_PREFIX, parse_sentinel


@dataclass(frozen=True, slots=True)
class Observation:
    """One tagged tunnel process seen on one host."""

    pid: int
    age_seconds: int
    link: Link


def parse_etime(text: str) -> int:
    """procps ``etime`` (``[[DD-]HH:]MM:SS`` or bare ``SS``) → seconds."""
    days = 0
    if "-" in text:
        d, _, text = text.partition("-")
        days = int(d)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return days * 86400 + h * 3600 + m * 60 + s


def parse_process_discovery(ps_output: str) -> list[Observation]:
    """Reconstruct per-process observations from ``ps -eo pid=,etime=,args=``.

    Each matched line is ``<pid> <etime> <argv…>`` where the sentinel is a word
    in argv (``exec -a`` put it at ``argv[0]``). Non-otto lines are ignored.
    """
    out: list[Observation] = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        token = next((w for w in fields[2:] if w.startswith(f"{SENTINEL_PREFIX}:")), None)
        if token is None:
            continue
        link = parse_sentinel(token)
        if link is None:
            continue
        out.append(Observation(pid=int(fields[0]), age_seconds=parse_etime(fields[1]), link=link))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_discovery_parse.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/discovery.py tests/unit/link/test_discovery_parse.py
git commit -m "$(printf 'feat(link): per-host process-discovery parser (pid, etime, sentinel)\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 4: Wire `discover_dynamic_links` + `all_links` coexistence

**Files:**
- Modify: `src/otto/link/discovery.py`
- Test: `tests/unit/link/test_manage.py` (discovery half)

**Interfaces:**
- Consumes: `Observation`, `parse_process_discovery`, `DISCOVERY_PS_COMMAND`
  (from `otto.link.socat`), `Lab`, `UnixHost`/`LocalHost`, `LogMode`,
  `HostAddressing`/`addressing_from_dict` is NOT used here (hosts are live
  objects; read `.ip`/`.interfaces` off them).
- Produces:
  - `discover_observations(lab: "Lab") -> list[tuple[str, Observation]]` — async;
    `(origin_host_id, Observation)` across tunnel-hosting hosts; best-effort.
  - `discover_dynamic_links(lab) -> list[Link]` — async; groups observations by
    id, fills endpoint ips from lab hosts.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_manage.py
import asyncio
from types import SimpleNamespace

import pytest

from otto.link.discovery import discover_dynamic_links, discover_observations


class FakeHost:
    """Minimal stand-in: has .id/.ip/.interfaces and an async .oneshot."""

    def __init__(self, host_id, ip, ps_output="", *, unreachable=False):
        self.id = host_id
        self.ip = ip
        self.interfaces = {"eth0": ip}
        self._ps = ps_output
        self._unreachable = unreachable

    async def oneshot(self, cmd, timeout=None, log=None):
        if self._unreachable:
            raise ConnectionError(f"{self.id} down")
        return SimpleNamespace(output=self._ps, exit_code=0)


def _lab(*hosts):
    return SimpleNamespace(hosts={h.id: h for h in hosts})


PS_A = ("  10    00:30 otto-link:v1:lnk-abc-161:udp:test1::161:test2::161 socat "
        "UDP4-LISTEN:161,fork,reuseaddr TCP4:10.0.0.2:50001\n")
PS_B = ("  20    00:29 otto-link:v1:lnk-abc-161:udp:test1::161:test2::161 socat "
        "TCP4-LISTEN:50001,fork,reuseaddr UDP4:10.0.0.2:161\n")


def test_discover_groups_processes_across_hosts_by_id():
    a = FakeHost("test1", "10.0.0.1", PS_A)
    b = FakeHost("test2", "10.0.0.2", PS_B)
    links = asyncio.run(discover_dynamic_links(_lab(a, b)))
    assert [l.id for l in links] == ["lnk-abc-161"]
    assert {links[0].a.host, links[0].b.host} == {"test1", "test2"}


def test_discover_is_best_effort_on_host_down(caplog):
    a = FakeHost("test1", "10.0.0.1", PS_A)
    down = FakeHost("test2", "10.0.0.2", unreachable=True)
    links = asyncio.run(discover_dynamic_links(_lab(a, down)))
    assert [l.id for l in links] == ["lnk-abc-161"]  # still returns what it found
    assert any("test2" in r.message for r in caplog.records)  # named loudly
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_manage.py -k discover -v`
Expected: FAIL — `discover_dynamic_links` still raises `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation**

Replace the `discover_dynamic_links` stub body and add `discover_observations`:

```python
async def discover_observations(lab: "Lab") -> list[tuple[str, "Observation"]]:
    """Every tagged tunnel process across the lab's tunnel-hosting hosts.

    Best-effort + transparent (spec §10): an unreachable host is warned about by
    name and skipped, never silently dropped and never fatal to the scan.
    """
    import asyncio

    from ..host.local_host import LocalHost
    from ..host.unix_host import UnixHost
    from ..logger.mode import LogMode
    from .socat import DISCOVERY_PS_COMMAND

    hosts = [h for h in lab.hosts.values() if isinstance(h, (UnixHost, LocalHost))]

    async def scan(host: "Any") -> list[tuple[str, Observation]]:
        try:
            result = await host.oneshot(DISCOVERY_PS_COMMAND, log=LogMode.QUIET)
        except Exception as e:  # noqa: BLE001 — best-effort scan; name + skip
            logger.warning(f"otto link: could not scan host {host.id!r} for tunnels: {e}")
            return []
        return [(host.id, obs) for obs in parse_process_discovery(result.output)]

    gathered = await asyncio.gather(*(scan(h) for h in hosts))
    return [pair for host_pairs in gathered for pair in host_pairs]


async def discover_dynamic_links(lab: "Lab") -> list[Link]:
    """Discover live otto tunnels across the lab's Unix hosts (spec §8).

    Groups per-host observations by id into one ``Link`` per tunnel, filling
    endpoint ips from the live lab hosts.
    """
    by_id: dict[str, Link] = {}
    for _origin, obs in await discover_observations(lab):
        by_id.setdefault(obs.link.id, obs.link)
    return [_resolve_link_ips(link, lab) for link in by_id.values()]


def _resolve_link_ips(link: Link, lab: "Lab") -> Link:
    """Fill each endpoint's ip from the lab host it names (sentinel carries ids
    + ifaces but empty ips)."""
    from dataclasses import replace

    def ip_for(ep: LinkEndpoint) -> LinkEndpoint:
        host = lab.hosts.get(ep.host)
        if host is None:
            return ep
        ifaces = getattr(host, "interfaces", {}) or {}
        raw = ifaces.get(ep.interface) if ep.interface else None
        ip = (raw if isinstance(raw, str) else getattr(raw, "ip", None)) or getattr(host, "ip", "")
        return replace(ep, ip=ip)

    return replace(link, a=ip_for(link.a), b=ip_for(link.b))
```

Add the needed imports to `discovery.py`'s header: `from typing import Any` and
`from .model import Link, LinkEndpoint`. Update `all_links`'s docstring: remove
the "dynamic entry wins on a shared id" language — dynamic ids
(`lnk-<hex>-<port>`) and static ids (`name`/`a--b`) are disjoint, so links
coexist; the merge only dedups a genuine same-id duplicate.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_manage.py -k discover -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/discovery.py tests/unit/link/test_manage.py
git commit -m "$(printf 'feat(link): wire live discover_dynamic_links (gather+group, best-effort)\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 5: `add_link`

**Files:**
- Create: `src/otto/link/manage.py`
- Test: `tests/unit/link/test_manage.py` (add half)

**Interfaces:**
- Consumes: `discover_observations`, `all_links` (conflict check), `LinkEndpoint`,
  `Link`, `Provenance`, `encode_sentinel`, the `socat.py` builders,
  `derive._resolve_endpoint` (via `HostAddressing` built from live hosts),
  `LogMode`.
- Produces:
  - `EndpointSpec = tuple[str, str | None]` (host id, interface or None).
  - `AddedTunnel` frozen dataclass: `link: Link`, `ingress_host: str`,
    `exit_host: str`, `carrier_port: int`.
  - `add_link(lab, hosts: list[EndpointSpec], *, port: int, protocol: str = "tcp", dest: EndpointSpec | None = None) -> AddedTunnel`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_manage.py  (append)
from otto.link.manage import add_link, AddedTunnel


class SpawnHost(FakeHost):
    def __init__(self, host_id, ip, ps_output=""):
        super().__init__(host_id, ip, ps_output)
        self.commands = []

    async def oneshot(self, cmd, timeout=None, log=None):
        self.commands.append(cmd)
        # ss probe → no listeners; ps discovery/spawn → empty
        return SimpleNamespace(output="", exit_code=0)


def test_add_link_spawns_ingress_and_egress_and_returns_ids():
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    added = asyncio.run(add_link(lab, [("test1", "eth0"), ("test2", "eth0")],
                                 port=161, protocol="udp"))
    assert isinstance(added, AddedTunnel)
    assert added.link.id.endswith("-161")
    assert added.ingress_host == "test1" and added.exit_host == "test2"
    # egress launched on B, ingress on A, both carry the sentinel
    assert any("otto-link:v1:" in c and "TCP4-LISTEN" in c for c in b.commands)
    assert any("otto-link:v1:" in c and "UDP4-LISTEN:161" in c for c in a.commands)


def test_add_link_relay_dest_targets_third_host():
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    c = SpawnHost("test3", "10.0.0.3")
    lab = _lab(a, b, c)
    added = asyncio.run(add_link(lab, [("test1", "eth0"), ("test2", "eth0")],
                                 port=161, protocol="udp", dest=("test3", "eth0")))
    # logical endpoints are ingress + dest; exit host is still B
    assert {added.link.a.host, added.link.b.host} == {"test1", "test3"}
    assert added.exit_host == "test2"
    assert any("UDP4:10.0.0.3:161" in c2 for c2 in b.commands)  # egress → C


def test_add_link_rejects_more_than_two_hosts():
    a = SpawnHost("test1", "10.0.0.1")
    lab = _lab(a)
    with pytest.raises(ValueError, match="multi-hop"):
        asyncio.run(add_link(lab, [("a", None), ("b", None), ("c", None)], port=161))


def test_add_link_conflict_when_id_exists(monkeypatch):
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    added = asyncio.run(add_link(lab, [("test1", "eth0"), ("test2", "eth0")],
                                 port=161, protocol="udp"))

    async def fake_all(_lab, **_):
        return [added.link]

    monkeypatch.setattr("otto.link.manage.all_links", fake_all)
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(add_link(lab, [("test1", "eth0"), ("test2", "eth0")],
                             port=161, protocol="udp"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_manage.py -k add -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.link.manage'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/otto/link/manage.py
"""Async orchestration for dynamic tunnels — the callable library API (spec §5).

The CLI is a thin consumer of ``add_link`` / ``remove_link`` /
``remove_all_links``; each is usable standalone from any Python code.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..logger import get_logger
from ..logger.mode import LogMode
from .discovery import all_links, discover_observations
from .model import Link, LinkEndpoint, Provenance, make_dynamic_link_id
from .sentinel import encode_sentinel
from .socat import (
    FREE_PORT_PROBE_COMMAND, egress_socat_args, ingress_socat_args,
    launch_command, parse_listening_ports, pick_free_port,
)

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

logger = get_logger()

EndpointSpec = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class AddedTunnel:
    link: Link
    ingress_host: str
    exit_host: str
    carrier_port: int


def _resolve_endpoint(lab: "Lab", spec: EndpointSpec, port: int) -> LinkEndpoint:
    """Resolve ``(host_id, iface)`` to a ``LinkEndpoint`` with ip + port off the
    live lab host, applying the single-interface auto-resolution rule."""
    host_id, iface = spec
    host = lab.hosts.get(host_id)
    if host is None:
        raise ValueError(f"unknown host {host_id!r}")
    ifaces = getattr(host, "interfaces", {}) or {}
    if iface is not None:
        raw = ifaces.get(iface)
        if raw is None:
            known = ", ".join(sorted(ifaces)) or "<none>"
            raise ValueError(f"host {host_id!r} has no interface {iface!r} (known: {known})")
        ip = raw if isinstance(raw, str) else getattr(raw, "ip", "")
        return LinkEndpoint(host=host_id, interface=iface, ip=ip, port=port)
    if len(ifaces) > 1:
        raise ValueError(f"host {host_id!r}: ambiguous interface, specify one of: "
                         f"{', '.join(sorted(ifaces))}")
    if len(ifaces) == 1:
        ((name, raw),) = ifaces.items()
        ip = raw if isinstance(raw, str) else getattr(raw, "ip", "")
        return LinkEndpoint(host=host_id, interface=name, ip=ip, port=port)
    return LinkEndpoint(host=host_id, interface=None, ip=getattr(host, "ip", ""), port=port)


async def _alloc_carrier_port(host: "Any") -> int:
    result = await host.oneshot(FREE_PORT_PROBE_COMMAND, log=LogMode.QUIET)
    return pick_free_port(parse_listening_ports(result.output))


async def add_link(
    lab: "Lab",
    hosts: list[EndpointSpec],
    *,
    port: int,
    protocol: str = "tcp",
    dest: EndpointSpec | None = None,
) -> AddedTunnel:
    """Build a host-resident tunnel and return where it runs (spec §7).

    Spawns the tagged processes and reports which started; it does not
    pre-validate reachability or guarantee delivery (see spec §7.5).
    """
    if len(hosts) != 2:
        raise ValueError("multi-hop paths arrive with the hop-aware phase; give exactly 2 hosts")
    ingress_spec, exit_spec = hosts[0], hosts[-1]
    dest_spec = dest or exit_spec

    a = _resolve_endpoint(lab, ingress_spec, port)          # ingress (logical a)
    b = _resolve_endpoint(lab, dest_spec, port)             # destination (logical b)
    exit_ep = _resolve_endpoint(lab, exit_spec, port)       # tunnel exit host

    link = Link(a=a, b=b, protocol=protocol, provenance=Provenance.DYNAMIC,
                id=make_dynamic_link_id(a, b, protocol, port))

    existing = {l.id for l in await all_links(lab)}
    if link.id in existing:
        raise ValueError(f"a tunnel {link.id!r} already exists on this route+port")

    ingress_host = lab.hosts[ingress_spec[0]]
    exit_host = lab.hosts[exit_spec[0]]
    for tool_host in (ingress_host, exit_host):
        await _require_tools(tool_host)

    carrier = await _alloc_carrier_port(exit_host)
    sentinel = encode_sentinel(link)

    # Egress first (so the carrier is listening before ingress connects).
    await exit_host.oneshot(
        launch_command(sentinel, egress_socat_args(protocol, port, b.ip, carrier)),
        log=LogMode.QUIET,
    )
    await ingress_host.oneshot(
        launch_command(sentinel, ingress_socat_args(protocol, port, exit_ep.ip, carrier)),
        log=LogMode.QUIET,
    )
    return AddedTunnel(link=link, ingress_host=a.host, exit_host=exit_spec[0], carrier_port=carrier)


async def _require_tools(host: "Any") -> None:
    """Fail loud + name the host when socat or bash is missing."""
    result = await host.oneshot(
        "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 && echo ok || echo no",
        log=LogMode.QUIET,
    )
    if "ok" not in result.output:
        raise RuntimeError(f"host {host.id!r} is missing socat and/or bash (required for tunnels)")
```

Add `from typing import Any` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_manage.py -k add -v`
Expected: PASS (4 tests). Note the `SpawnHost` stub returns `"ok"`-free output for
`_require_tools`; update the stub's `oneshot` to return `"ok"` when the command
contains `command -v socat` so `add` proceeds. (Add that branch to `SpawnHost`.)

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/manage.py tests/unit/link/test_manage.py
git commit -m "$(printf 'feat(link): add_link — resolve, conflict-check, spawn tagged socats\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 6: `remove_link` / `remove_all_links`

**Files:**
- Modify: `src/otto/link/manage.py`
- Test: `tests/unit/link/test_manage.py` (remove half)

**Interfaces:**
- Consumes: `discover_observations`, `LogMode`.
- Produces:
  - `RemovedReport` frozen dataclass: `removed_ids: list[str]`,
    `killed: dict[str, list[int]]` (host id → pids), `unreachable: list[str]`.
  - `remove_link(lab, link_id: str) -> RemovedReport`
  - `remove_all_links(lab) -> RemovedReport`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_manage.py  (append)
from otto.link.manage import remove_link, remove_all_links, RemovedReport


class KillHost(FakeHost):
    def __init__(self, host_id, ip, ps_output=""):
        super().__init__(host_id, ip, ps_output)
        self.killed = []

    async def oneshot(self, cmd, timeout=None, log=None):
        if cmd.startswith("kill "):
            self.killed.append(cmd)
            return SimpleNamespace(output="", exit_code=0)
        return SimpleNamespace(output=self._ps, exit_code=0)


def test_remove_link_kills_matching_pids_across_hosts():
    a = KillHost("test1", "10.0.0.1", PS_A)  # pid 10
    b = KillHost("test2", "10.0.0.2", PS_B)  # pid 20
    report = asyncio.run(remove_link(_lab(a, b), "lnk-abc-161"))
    assert report.removed_ids == ["lnk-abc-161"]
    assert report.killed == {"test1": [10], "test2": [20]}
    assert any("kill 10" in c for c in a.killed)
    assert any("kill 20" in c for c in b.killed)


def test_remove_all_reaps_every_tunnel():
    a = KillHost("test1", "10.0.0.1", PS_A)
    b = KillHost("test2", "10.0.0.2", PS_B)
    report = asyncio.run(remove_all_links(_lab(a, b)))
    assert report.removed_ids == ["lnk-abc-161"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_manage.py -k remove -v`
Expected: FAIL — `ImportError: cannot import name 'remove_link'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/otto/link/manage.py

@dataclass(frozen=True, slots=True)
class RemovedReport:
    removed_ids: list[str]
    killed: dict[str, list[int]]
    unreachable: list[str]


async def _reap(lab: "Lab", predicate) -> RemovedReport:
    """Discover, then kill the pids of tunnels matching *predicate*, per host."""
    observations = await discover_observations(lab)
    killed: dict[str, list[int]] = {}
    ids: set[str] = set()
    by_host: dict[str, list[int]] = {}
    for origin, obs in observations:
        if predicate(obs.link):
            ids.add(obs.link.id)
            by_host.setdefault(origin, []).append(obs.pid)

    unreachable: list[str] = []
    for host_id, pids in by_host.items():
        host = lab.hosts[host_id]
        try:
            await host.oneshot(f"kill {' '.join(str(p) for p in sorted(pids))}", log=LogMode.QUIET)
        except Exception as e:  # noqa: BLE001 — transparent partial reap (spec §10)
            logger.warning(f"otto link: could not reap on host {host_id!r}: {e}")
            unreachable.append(host_id)
            continue
        killed[host_id] = sorted(pids)
    return RemovedReport(removed_ids=sorted(ids), killed=killed, unreachable=unreachable)


async def remove_link(lab: "Lab", link_id: str) -> RemovedReport:
    """Reap the tunnel with *link_id* (its ``-<port>`` suffix targets one tunnel)."""
    return await _reap(lab, lambda link: link.id == link_id)


async def remove_all_links(lab: "Lab") -> RemovedReport:
    """Reap every otto tunnel (owner-agnostic)."""
    return await _reap(lab, lambda _link: True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_manage.py -k remove -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/manage.py tests/unit/link/test_manage.py
git commit -m "$(printf 'feat(link): remove_link/remove_all_links — discover, kill by id, report\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 7: Public API re-exports

**Files:**
- Modify: `src/otto/link/__init__.py`
- Test: `tests/unit/link/test_public_api.py`

**Interfaces:**
- Produces: `otto.link.{add_link, remove_link, remove_all_links, discover_dynamic_links,
  all_links, AddedTunnel, RemovedReport, Link, LinkEndpoint, Provenance}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_public_api.py
import otto.link as link


def test_public_callables_exported():
    for name in ("add_link", "remove_link", "remove_all_links",
                 "discover_dynamic_links", "all_links",
                 "AddedTunnel", "RemovedReport", "Link", "LinkEndpoint", "Provenance"):
        assert hasattr(link, name), name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_public_api.py -v`
Expected: FAIL — `AssertionError: add_link`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/otto/link/__init__.py`:

```python
from .discovery import all_links, discover_dynamic_links
from .manage import AddedTunnel, RemovedReport, add_link, remove_all_links, remove_link
from .model import Link, LinkEndpoint, Provenance

__all__ = [
    "AddedTunnel", "Link", "LinkEndpoint", "Provenance", "RemovedReport",
    "add_link", "all_links", "discover_dynamic_links", "remove_all_links", "remove_link",
]
```

(Preserve any existing `__init__` exports; merge into the `__all__`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_public_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/__init__.py tests/unit/link/test_public_api.py
git commit -m "$(printf 'feat(link): re-export the callable tunnel API from otto.link\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 8: `__dynamic_links__` completion-cache key

**Files:**
- Modify: `src/otto/configmodule/completion_cache.py`
- Test: `tests/unit/configmodule/test_completion_cache_links.py`

**Interfaces:**
- Consumes: `_cache_path`, `_atomic_write_json`, `compute_fingerprint`,
  `time.time()`.
- Produces:
  - `DYNAMIC_LINKS_KEY = "__dynamic_links__"`, `DYNAMIC_LINKS_TTL_SECONDS = 120`,
    `DYNAMIC_LINKS_SCHEMA_VERSION = 1`.
  - `record_dynamic_link_ids(repos, ids: list[str]) -> None`
  - `read_dynamic_link_ids(repos) -> list[str] | None` (None = cold/expired).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/configmodule/test_completion_cache_links.py
import time
from types import SimpleNamespace

import otto.configmodule.completion_cache as cc


def _repos(tmp_path):
    # one repo whose fingerprint sources exist under tmp_path/.otto
    (tmp_path / ".otto").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".otto" / "settings.toml").write_text("")
    return [SimpleNamespace(sut_dir=tmp_path, init=[], libs=[], tests=[], labs=[])]


def test_record_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repos = _repos(tmp_path)
    cc.record_dynamic_link_ids(repos, ["lnk-a-161", "lnk-b-53"])
    assert cc.read_dynamic_link_ids(repos) == ["lnk-a-161", "lnk-b-53"]


def test_read_expired_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repos = _repos(tmp_path)
    cc.record_dynamic_link_ids(repos, ["lnk-a-161"])
    monkeypatch.setattr(cc.time, "time", lambda: time.time() + cc.DYNAMIC_LINKS_TTL_SECONDS + 1)
    assert cc.read_dynamic_link_ids(repos) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache_links.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'record_dynamic_link_ids'`.

- [ ] **Step 3: Write minimal implementation**

Append to `completion_cache.py` (modeled on the `__collected_tests__` block):

```python
DYNAMIC_LINKS_KEY = "__dynamic_links__"
DYNAMIC_LINKS_SCHEMA_VERSION = 1
DYNAMIC_LINKS_TTL_SECONDS = 120  # link state is volatile; short TTL (spec §11.2)


def record_dynamic_link_ids(repos: list["Repo"], ids: list[str]) -> None:
    """Cache the freshly-discovered tunnel ids for ``remove <id>`` completion."""
    if not repos:
        return
    cache_path = _cache_path()
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass
    namespace = existing.get(DYNAMIC_LINKS_KEY)
    if not isinstance(namespace, dict):
        namespace = {}
    namespace[compute_fingerprint(repos)] = {
        "schema_version": DYNAMIC_LINKS_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "ids": list(ids),
    }
    existing[DYNAMIC_LINKS_KEY] = namespace
    _atomic_write_json(cache_path, existing)


def read_dynamic_link_ids(repos: list["Repo"]) -> list[str] | None:
    """Fresh cached tunnel ids, or ``None`` (cold / expired / malformed)."""
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(DYNAMIC_LINKS_KEY, {}).get(compute_fingerprint(repos)) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or entry.get("schema_version") != DYNAMIC_LINKS_SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)) or time.time() - generated_at > DYNAMIC_LINKS_TTL_SECONDS:
        return None
    ids = entry.get("ids")
    return ids if isinstance(ids, list) else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache_links.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/otto/configmodule/completion_cache.py tests/unit/configmodule/test_completion_cache_links.py
git commit -m "$(printf 'feat(completion): __dynamic_links__ cache key for otto link remove ids\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 9: `otto link` CLI group + registration

**Files:**
- Create: `src/otto/cli/link.py`
- Modify: `src/otto/cli/builtin_commands.py`
- Test: `tests/unit/link/test_link_cli.py`

**Interfaces:**
- Consumes: `add_link`, `discover_dynamic_links`, `all_links`, `remove_link`,
  `remove_all_links` (from `otto.link`), `get_lab` (from `otto.configmodule`),
  `async_typer_command` (from `otto.utils`), `record_dynamic_link_ids` +
  `get_repos`.
- Produces: a `link_app` Typer group registered as the `link` builtin;
  `_parse_hosts(value: str) -> list[EndpointSpec]` and
  `_parse_endpoint(token: str) -> EndpointSpec` helpers.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_link_cli.py
import pytest

from otto.cli.link import _parse_endpoint, _parse_hosts


def test_parse_endpoint_plain_and_pinned():
    assert _parse_endpoint("test1") == ("test1", None)
    assert _parse_endpoint("test1@eth1") == ("test1", "eth1")


def test_parse_hosts_splits_comma_list():
    assert _parse_hosts("test1@eth0,test2") == [("test1", "eth0"), ("test2", None)]


def test_parse_hosts_rejects_empty():
    with pytest.raises(ValueError):
        _parse_hosts("")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_link_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.cli.link'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/otto/cli/link.py
"""``otto link`` — manage host-resident tunnels (spec §7/§9).

Thin consumer of the ``otto.link`` library API. Reservation-group shaped
(Typer group + callback + command leaves). Runs no per-invocation output dir and
keeps internal host I/O quiet (only warnings/errors surface).
"""

import typer
from rich import print as rprint

from ..configmodule import get_lab, get_repos
from ..configmodule.completion_cache import read_dynamic_link_ids, record_dynamic_link_ids
from ..link import add_link, all_links, discover_dynamic_links, remove_all_links, remove_link
from ..utils import async_typer_command, complete_comma_list

link_app = typer.Typer(help="Create, list, and remove host-resident tunnels.", no_args_is_help=True)


@link_app.callback()
def link_callback(ctx: typer.Context) -> None:
    """Tunnel management. Discovery/teardown touch hosts but create no output dir."""
    if ctx.resilient_parsing:
        return


def _parse_endpoint(token: str) -> "tuple[str, str | None]":
    host, sep, iface = token.partition("@")
    if not host:
        raise ValueError(f"empty host in {token!r}")
    return (host, iface if sep else None)


def _parse_hosts(value: str) -> "list[tuple[str, str | None]]":
    parts = [p for p in value.split(",") if p]
    if not parts:
        raise ValueError("--hosts must name at least one host")
    return [_parse_endpoint(p) for p in parts]


def _hosts_completer(ctx: typer.Context, incomplete: str) -> "list[str]":  # noqa: ARG001
    from ..configmodule.completion_cache import collect_host_ids
    try:
        ids = collect_host_ids(get_repos())
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        ids = []
    return complete_comma_list(sorted(ids), incomplete)


def _link_id_completer(ctx: typer.Context, incomplete: str) -> "list[str]":  # noqa: ARG001
    try:
        ids = read_dynamic_link_ids(get_repos()) or []
    except Exception:  # noqa: BLE001
        ids = []
    return sorted(i for i in ids if i.startswith(incomplete))


@link_app.command()
@async_typer_command
async def add(
    hosts: str = typer.Option(..., "--hosts", help="Ordered host path h1[@if],h2[@if].",
                              autocompletion=_hosts_completer),
    port: int = typer.Option(..., "--port", help="Service port (both ends)."),
    protocol: str = typer.Option("tcp", "--protocol", help="tcp or udp."),
    dest: "str | None" = typer.Option(None, "--dest", help="Relay delivery target host[@if]."),
) -> None:
    """Create a tunnel. See spec §7."""
    lab = get_lab()
    dest_spec = _parse_endpoint(dest) if dest else None
    added = await add_link(lab, _parse_hosts(hosts), port=port, protocol=protocol, dest=dest_spec)
    rprint(f"[green]added[/green] {added.link.id} "
           f"({added.ingress_host} -> {added.exit_host}, carrier {added.carrier_port})")


@link_app.command(name="list")
@async_typer_command
async def list_links(
    all_: bool = typer.Option(False, "--all", help="Include implicit + declared links."),
) -> None:
    """List tunnels (default: dynamic only). See spec §9.2."""
    lab = get_lab()
    links = await (all_links(lab) if all_ else discover_dynamic_links(lab))
    if not all_:
        record_dynamic_link_ids(get_repos(), [l.id for l in links])
    for link in links:
        rprint(f"{link.id}  {link.a.host}@{link.a.interface or '-'} <-> "
               f"{link.b.host}@{link.b.interface or '-'}  {link.protocol}")


@link_app.command()
@async_typer_command
async def remove(
    link_id: "str | None" = typer.Argument(None, autocompletion=_link_id_completer),
    all_: bool = typer.Option(False, "--all", help="Reap every otto tunnel."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the --all confirmation."),
) -> None:
    """Remove a tunnel by id, or all tunnels. See spec §9.3."""
    lab = get_lab()
    if all_:
        if not yes and not typer.confirm("Reap ALL otto tunnels?"):
            raise typer.Exit(1)
        report = await remove_all_links(lab)
    elif link_id:
        report = await remove_link(lab, link_id)
    else:
        rprint("[red]give a link id or --all[/red]")
        raise typer.Exit(2)
    record_dynamic_link_ids(get_repos(), [])  # invalidate; next scan refreshes
    rprint(f"[green]removed[/green] {report.removed_ids or '(none found)'}")
    if report.unreachable:
        rprint(f"[yellow]could not reach:[/yellow] {report.unreachable}")
        raise typer.Exit(1)
```

Register it in `builtin_commands.py` alongside `reservation`:

```python
    register_cli_command(
        "link",
        "otto.cli.link:link_app",
        help="Create, list, and remove host-resident tunnels.",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_link_cli.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify the command registers**

Run: `uv run otto link --help`
Expected: shows `add`, `list`, `remove`.

- [ ] **Step 6: Commit**

```bash
git add src/otto/cli/link.py src/otto/cli/builtin_commands.py tests/unit/link/test_link_cli.py
git commit -m "$(printf 'feat(cli): otto link add/list/remove group + registration\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 10: Context-aware `--hosts` completion (L2 stretch)

**Files:**
- Modify: `src/otto/cli/link.py`
- Test: `tests/unit/link/test_link_cli.py` (append)

**Interfaces:**
- Consumes: `complete_comma_list`, `collect_host_ids`.
- Produces: `_reachable_from(last_host: str, repos) -> list[str]` used to narrow
  `_hosts_completer` candidates once a host is already typed.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/link/test_link_cli.py  (append)
from otto.cli.link import _l2_reachable


def test_l2_reachable_shares_24_prefix():
    hosts = {"a": "10.0.0.1", "b": "10.0.0.9", "c": "192.168.5.5"}
    assert set(_l2_reachable("a", hosts)) == {"b"}  # same /24, excludes self + far net
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/link/test_link_cli.py -k l2 -v`
Expected: FAIL — `ImportError: cannot import name '_l2_reachable'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/otto/cli/link.py
def _l2_reachable(host_id: str, ip_by_host: "dict[str, str]") -> "list[str]":
    """Simple-L2 heuristic (spec §11.3): hosts sharing the /24 of ``host_id``.

    Refined to true per-interface subnets in a later phase.
    """
    def net24(ip: str) -> str:
        return ip.rsplit(".", 1)[0] if ip.count(".") == 3 else ""

    mine = net24(ip_by_host.get(host_id, ""))
    if not mine:
        return []
    return sorted(h for h, ip in ip_by_host.items() if h != host_id and net24(ip) == mine)
```

Wire it into `_hosts_completer`: parse the already-typed prefix
(`head, sep, _frag = incomplete.rpartition(",")`); when `sep`, take the last
host, build `ip_by_host` from `collect_host_ids`-backed lab data, narrow to
`_l2_reachable(last, ip_by_host)` before calling `complete_comma_list`; on any
error fall back to the full host list.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/link/test_link_cli.py -k l2 -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/link.py tests/unit/link/test_link_cli.py
git commit -m "$(printf 'feat(cli): context-aware --hosts completion (simple-L2 reachability)\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 11: Live-bed e2e

**Files:**
- Create: `tests/e2e/test_link_tunnels_e2e.py`

**Interfaces:**
- Consumes: the peer VMs (test1/test2/test3 on 10.10.200.0/24) via the standard
  e2e lab fixtures; `otto.link.{add_link, discover_dynamic_links, remove_link}`.

- [ ] **Step 1: Write the tests (they gate on real hosts, so no pre-fail step)**

```python
# tests/e2e/test_link_tunnels_e2e.py
import asyncio
import socket

import pytest

from otto.link import add_link, discover_dynamic_links, remove_link

pytestmark = [pytest.mark.e2e, pytest.mark.hops]


@pytest.fixture
def reap_tunnels(e2e_lab):
    """Guaranteed teardown: reap every otto tunnel created during the test."""
    created: list[str] = []
    yield created
    for link_id in created:
        asyncio.run(remove_link(e2e_lab, link_id))


def test_udp_tunnel_delivers_and_lists_and_removes(e2e_lab, reap_tunnels):
    # start a UDP echo listener on test2, add a tunnel test1->test2:P, send from
    # test1's ingress, assert receipt; then list shows it; remove; assert gone.
    ...  # full body: spawn socat UDP listener on test2 via host.run; use
         # add_link; send a datagram; assert the listener received it; assert the
         # id appears in discover_dynamic_links; remove_link; assert it's gone.


def test_relay_dest_appears_sourced_from_exit(e2e_lab, reap_tunnels):
    # add_link test1->test2 --dest test3; assert test3's listener sees the
    # datagram with source == test2's ip.
    ...


def test_non_otto_socat_is_excluded(e2e_lab, reap_tunnels):
    # spawn a plain (untagged) socat on test2; assert discover_dynamic_links
    # never lists it.
    ...
```

> The `...` bodies are filled during execution against the live bed, following
> the exact assertions named in each comment. Keep every spawned helper tagged or
> tracked so `reap_tunnels` + a bed-wide `remove_all_links` leave nothing behind.
> Honor the dev-VM rules: single pass, never power VMs, fail loud on host-down.

- [ ] **Step 2: Run on the bed**

Run: `uv run pytest tests/e2e/test_link_tunnels_e2e.py -m "e2e and hops" -v`
Expected: PASS against the live peers (or a clear host-named failure if a VM is
down — never a skip).

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_link_tunnels_e2e.py
git commit -m "$(printf 'test(link): live-bed e2e for add/list/remove, relay, non-otto exclusion\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Task 12: Docs

**Files:**
- Modify: the `otto.link` API doc page (add the `manage` callables + `discovery`).
- Create: an `otto link` CLI guide page (add/list/remove, `--hosts`/`--port`/
  `--protocol`/`--dest`, host-down transparency, the old-OS/bash requirement).
- Modify: note the monitor-compatibility seam (§12) where the monitor collector
  docs live.

- [ ] **Step 1: Write the docs**

Document: the callable API (`add_link`/`remove_link`/`remove_all_links`/
`discover_dynamic_links`/`all_links` with signatures), the CLI surface, the
`lnk-<hex>-<port>` id scheme + readable static handles, the `--dest` relay
semantics, and the bash/socat host requirement. Add a short "monitor
compatibility" paragraph pointing at the per-host parser (§8/§12).

- [ ] **Step 2: Build the docs**

Run: `make docs`
Expected: builds clean under `-W` (no nitpicky warnings; every referenced symbol
resolves).

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "$(printf 'docs(link): otto link CLI guide + otto.link tunnel API reference\n\nAssisted-by: Claude Opus 4.8')"
```

---

## Final Gate

- [ ] `nox -s typecheck` — clean (`ty` runs only here; run after all `src/` edits).
- [ ] `make coverage` — full unit/integration suite green, coverage not regressed.
- [ ] `make docs` — clean under `-W`.
- [ ] Live-bed e2e (Task 11) green against the peers.
- [ ] `ruff check` + `ruff format --check` clean (implementers often miss format).

---

## Self-Review

**Spec coverage:**
- §4 module layout → Tasks 2 (socat), 5–6 (manage), 4 (discovery), 9 (cli). ✓
- §5 library API → Tasks 5, 6, 7. ✓
- §6 identity → Task 1. ✓
- §7 add / bridge / relay / tagging / conflict → Tasks 2, 5. ✓
- §8 two-layer discovery → Tasks 3, 4. ✓
- §9 all_links/list/remove → Tasks 4, 6, 9. ✓
- §10 host-down transparency → Tasks 4, 6, 9. ✓
- §11 logging + completion cache + `--hosts` completion → Tasks 5/6/9 (QUIET), 8, 9, 10. ✓
- §12 monitor compatibility → Tasks 3/4 (pure parser + aggregator) + 12 (docs). ✓
- §13 old-OS portability → Task 2 (portable ps/probe), Task 5 (`_require_tools`), Task 11 (bed check). ✓
- §14 testing → Tasks 1–10 units, Task 11 e2e. ✓
- §15 foundation revisions → Task 1 (ids), Task 4 (all_links docstring). ✓

**Type consistency:** `EndpointSpec = tuple[str, str | None]` used uniformly in
Tasks 5, 9; `AddedTunnel(link, ingress_host, exit_host, carrier_port)` and
`RemovedReport(removed_ids, killed, unreachable)` referenced consistently;
`Observation(pid, age_seconds, link)` used in Tasks 3, 4, 6;
`discover_observations -> list[tuple[str, Observation]]` consumed the same way in
Tasks 4 and 6.

**Placeholder scan:** the only deferred bodies are the Task 11 e2e `...` blocks,
which are gated on live hosts and specify exact assertions in-line — filled during
bed execution, not shippable as-is. All hostless tasks carry complete code.
