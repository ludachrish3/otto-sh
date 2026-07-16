# Dynamic Tunnels in the Monitor Topology — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live otto tunnels appear in the monitor GUI topology view as overlays riding the links their hop paths traverse, streamed over SSE, with last-known-state persistence.

**Architecture:** A new `TunnelRecord` rides format:1 as `SessionRecord.tunnels` (+ replace-semantics fragment field). A `_tunnel_loop` inside `MetricCollector` polls an injected discovery callable on the collection interval, publishing on change and upserting `sessions.tunnels_json`. The web maps each tunnel's consecutive hop-pairs onto underlay `TopoEdge`s (riding segments recompute the underlay's exact `routeEdge` geometry) or bare `dynamic` segments, with whole-tunnel selection and ok/degraded/uncertain styling.

**Tech Stack:** pydantic v2 models, aiosqlite, asyncio, React 19 + React Flow (`@xyflow/react`), zustand, vitest, Playwright (pytest + nox).

**Spec:** `docs/superpowers/specs/2026-07-16-dynamic-tunnels-topology-design.md` — read it first.

## Global Constraints

- Work on a worktree branch (create via superpowers:using-git-worktrees). Fresh worktree needs `uv sync` and `npm ci` in `web/` before gates run.
- Worktree branch ⇒ self-commit OK: conventional prefix + `Assisted-by:` trailer on every commit.
- NO `from __future__ import annotations` anywhere (Sphinx nitpicky `-W`).
- Prefer lists over tuples in API signatures/returns.
- `ty` runs only at `nox -s typecheck` — run it after every Python src change. `nox -s lint` = ruff check **+ ruff format --check**.
- Browser gate is `nox -s dashboard` (chromium+firefox+webkit); a bare `pytest tests/e2e/monitor/dashboard` is chromium-only and does NOT count.
- `pytest` never rebuilds the web dist; run `make web` before any browser test after web/src changes.
- Monitor e2e stays docker-free; no heavy parallel test load on the dev VM.
- Every new guard must be PROVEN able to fail (mutate the production code, watch it go red) — steps below say where.
- `make web` (type-drift gate) will FAIL between Task 2 and Task 8 (regenerated types, consumers not yet fixed). That window is expected; don't run web/doc gates inside it. The branch lands atomically via squash-merge, so the "format:1 pieces move together" rule holds at the merge boundary.
- Final gate for the whole branch: `make coverage` + `nox -s dashboard` + `make web` + `nox -s typecheck` + `nox -s lint`. Scoped pytest green is NOT the gate.

---

### Task 1: `TunnelRecord` model; `tunnels` on SessionRecord + fragment; LinkSnapshot narrows

**Files:**
- Modify: `src/otto/models/monitor.py` (LinkSnapshot :263-279, SessionRecord :322-340, MonitorSessionFragment :355-381)
- Test: `tests/unit/models/test_monitor_tunnels.py` (create)
- Modify: `tests/unit/models/test_jsonschema.py` (schema-shape pins)

**Interfaces:**
- Produces: `otto.models.monitor.TunnelRecord` with fields `id: str`, `protocol: str`, `service_port: int`, `hops: list[str]` (min 2), `status: Literal["ok","degraded","uncertain"]`, `carriers_present: int`, `carriers_expected: int`, `age_seconds: float | None`. `SessionRecord.tunnels: list[TunnelRecord]` (default `[]`). `MonitorSessionFragment.tunnels: list[TunnelRecord] | None` (default `None`). `LinkSnapshot.provenance: Literal["implicit","declared"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/models/test_monitor_tunnels.py
"""TunnelRecord + the tunnels fields (spec 2026-07-16 §1)."""

import pytest
from pydantic import ValidationError

from otto.models.monitor import (
    LinkSnapshot,
    MonitorSessionFragment,
    SessionRecord,
    TunnelRecord,
)


def _record(**overrides: object) -> TunnelRecord:
    base: dict[str, object] = {
        "id": "tun-abc123def456-15001",
        "protocol": "udp",
        "service_port": 15001,
        "hops": ["edge-gw", "core-01", "db-01"],
        "status": "ok",
        "carriers_present": 6,
        "carriers_expected": 6,
        "age_seconds": 120.0,
    }
    base.update(overrides)
    return TunnelRecord.model_validate(base)


def test_tunnel_record_round_trips() -> None:
    rec = _record()
    assert rec.hops == ["edge-gw", "core-01", "db-01"]
    assert TunnelRecord.model_validate(rec.model_dump(mode="json")) == rec


def test_tunnel_record_rejects_single_hop() -> None:
    with pytest.raises(ValidationError):
        _record(hops=["edge-gw"])


def test_tunnel_record_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _record(status="down")


def test_session_record_tunnels_default_empty() -> None:
    from datetime import datetime, timezone

    s = SessionRecord(id="s1", start=datetime(2026, 7, 16, tzinfo=timezone.utc))
    assert s.tunnels == []


def test_fragment_tunnels_absent_is_none_and_empty_is_empty() -> None:
    """Replace semantics on the wire: None = no update, [] = now empty."""
    assert MonitorSessionFragment(session="s1").tunnels is None
    frag = MonitorSessionFragment.model_validate({"session": "s1", "tunnels": []})
    assert frag.tunnels == []


def test_link_snapshot_rejects_dynamic_provenance() -> None:
    """'dynamic' left the snapshot contract — tunnels are first-class now."""
    with pytest.raises(ValidationError):
        LinkSnapshot.model_validate(
            {
                "id": "l1",
                "endpoints": [
                    {"host": "a", "ip": "10.0.0.1"},
                    {"host": "b", "ip": "10.0.0.2"},
                ],
                "provenance": "dynamic",
            }
        )
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/unit/models/test_monitor_tunnels.py -v`
Expected: FAIL — `ImportError: cannot import name 'TunnelRecord'`.

- [ ] **Step 3: Implement in `src/otto/models/monitor.py`**

Narrow `LinkSnapshot.provenance` (line 277) and rewrite its docstring's provenance sentence:

```python
    provenance: Literal["implicit", "declared"] = "declared"
```

Docstring replacement for the "Real exporters write only..." sentence:

```
    Mirrors the runtime ``otto.link.model.Link``. The snapshot is a
    static-config document: ``implicit`` + ``declared`` only. Dynamic tunnels
    are runtime state and ride ``SessionRecord.tunnels`` as first-class
    :class:`TunnelRecord` rows instead (spec 2026-07-16) — the runtime
    ``Provenance.DYNAMIC`` enum value survives for the link-conflict rules,
    but it never reaches this wire. ``impair`` is the *declared* in-path
    middlebox host id — static config, unlike applied netem parameters.
```

Add after `LabSnapshot` (line 288):

```python
class TunnelRecord(RowModel):
    """One live tunnel's last known state (spec 2026-07-16 §1).

    ``hops`` is the ordered host-id chain of ``otto.tunnel.model.Tunnel.path``
    — ``hops[0]`` the entry end, ``hops[-1]`` the exit end; the topology view
    consumes consecutive pairs. Host ids share the id space of
    :class:`LinkEndpointSnapshot.host`. ``status`` is derived from discovery
    fields, never parsed from the human ``DiscoveredTunnel.status`` string.
    """

    id: str
    protocol: str = "udp"
    service_port: int
    hops: list[str] = Field(min_length=2)
    status: Literal["ok", "degraded", "uncertain"] = "ok"
    carriers_present: int = 0
    carriers_expected: int = 0
    age_seconds: float | None = None
```

In `SessionRecord`, after `chart_map` (line 340):

```python
    tunnels: list[TunnelRecord] = Field(default_factory=list)
```

In `MonitorSessionFragment`, after `meta` (line 381), with a docstring
addition to the class body's trailing paragraph:

```python
    tunnels: list[TunnelRecord] | None = None
```

Append to the fragment docstring (after the ``deleted_event_ids`` paragraph):

```
    ``tunnels`` is the one REPLACE-semantics payload field (the ``meta``
    precedent, not the append rule): ``None`` means "no tunnel update in this
    fragment"; a list — including ``[]`` — replaces the session's set
    wholesale. That is "last known state" expressed on the wire.
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/unit/models/test_monitor_tunnels.py -v`
Expected: 6 passed.

- [ ] **Step 5: Update the jsonschema shape pins**

Run: `uv run pytest tests/unit/models/test_jsonschema.py -v` — expect failures where the export schema's `$defs`/properties are pinned. Update `test_monitor_export_schema_shape` (line ~214) and the fragment-def test (~222) to include `TunnelRecord` in `$defs` and `tunnels` in the session/fragment properties, following each test's existing style. Add one assertion: `assert "TunnelRecord" in schema["$defs"]`.

- [ ] **Step 6: Typecheck + lint, commit**

```bash
uv run nox -s typecheck lint
git add src/otto/models/monitor.py tests/unit/models/test_monitor_tunnels.py tests/unit/models/test_jsonschema.py
git commit -m "feat(monitor): TunnelRecord + tunnels on SessionRecord/fragment; LinkSnapshot drops dynamic"
```

---

### Task 2: Fixture generator migration + regenerated types and fixtures

**Files:**
- Modify: `scripts/gen_monitor_fixtures.py` (kitchen-sink :321-329, isp-core :622-628, sprawl :839-845)
- Regenerate: `web/fixtures/*.json` (`make monitor-fixtures`), `web/src/api/export.gen.ts` (`bash scripts/gen_web_types.sh`), `schemas/` if committed
- Test: `tests/unit/scripts/test_monitor_fixture_files.py` (existing drift test re-passes), `tests/unit/scripts/test_gen_monitor_fixtures.py` (update any dynamic-link pins)

**Interfaces:**
- Consumes: `TunnelRecord` (Task 1).
- Produces: fixtures whose sessions carry `tunnels` — kitchen-sink: `tun-demo` 2-hop bare `ok`; isp-core: `tun-app-path` 3-hop riding `degraded` + `tun-jump-zephyr` 2-hop `ok`; sprawl: `tun-jump-acc07` 2-hop `uncertain`. Zero `provenance="dynamic"` links anywhere.

- [ ] **Step 1: Migrate kitchen-sink.** Delete the `_link(... provenance="dynamic", name="tun-demo" ...)` entry (gen_monitor_fixtures.py:321-329, including the FIXTURE-ONLY comment). Find the fixture's `SessionRecord(...)` construction (grep `SessionRecord(` in the same function) and add:

```python
        tunnels=[
            # Bare 2-hop tunnel: no declared/implicit link joins edge-gw⇄db-01,
            # so this renders as a bare segment — the old chord look, pinned.
            TunnelRecord(
                id="tun-00000000demo-15001",
                protocol="udp",
                service_port=15001,
                hops=["edge-gw", "db-01"],
                status="ok",
                carriers_present=4,
                carriers_expected=4,
                age_seconds=3600.0,
            ),
        ],
```

Import `TunnelRecord` alongside the script's existing `otto.models.monitor` imports.

- [ ] **Step 2: Migrate isp-core.** Delete the `tun-jump-zephyr` dynamic link (:622-628). Add to that fixture's `SessionRecord(...)`:

```python
        tunnels=[
            # 3-hop path riding two DECLARED links (app01-tor, app01-db):
            # the riding-overlay vector. Degraded: 4 of 6 carriers alive.
            TunnelRecord(
                id="tun-000000a9db01-15002",
                protocol="tcp",
                service_port=15002,
                hops=["tor-sw-a", "app-01", "db-01"],
                status="degraded",
                carriers_present=4,
                carriers_expected=6,
                age_seconds=900.0,
            ),
            # The old tun-jump-zephyr, kept as a 2-hop ok tunnel.
            TunnelRecord(
                id="tun-0000jumpzeph-15004",
                protocol="tcp",
                service_port=15004,
                hops=["jump-01", "zephyr-01"],
                status="ok",
                carriers_present=4,
                carriers_expected=4,
                age_seconds=7200.0,
            ),
        ],
```

- [ ] **Step 3: Migrate sprawl.** Delete the `tun-jump-acc07` dynamic link (:839-845). Add:

```python
        tunnels=[
            # Uncertain: a hop host was unreachable during the last scan.
            TunnelRecord(
                id="tun-0000jumpacc7-15003",
                protocol="udp",
                service_port=15003,
                hops=["jump-01", "acc-07"],
                status="uncertain",
                carriers_present=2,
                carriers_expected=4,
                age_seconds=300.0,
            ),
        ],
```

- [ ] **Step 4: Purge the dead provenance plumbing.** `grep -n 'provenance="dynamic"\|"dynamic"' scripts/gen_monitor_fixtures.py` — must return nothing. If `_link`'s `provenance` parameter now has no non-default caller, leave the parameter (implicit links use it) but delete any dynamic-specific branch.

- [ ] **Step 5: Regenerate + verify drift tests**

```bash
make monitor-fixtures
bash scripts/gen_web_types.sh
uv run pytest tests/unit/scripts/ tests/unit/models/test_jsonschema.py -v
```

Expected: all pass. `git diff web/fixtures/` shows `tunnels` arrays and no `"provenance": "dynamic"`. `git diff web/src/api/export.gen.ts` shows `TunnelRecord` and the narrowed `Provenance`.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_monitor_fixtures.py web/fixtures/ web/src/api/export.gen.ts schemas/ tests/unit/scripts/
git commit -m "feat(monitor): fixtures carry first-class tunnels; regen format:1 types"
```

(Web build is now red until Task 8 — expected.)

---

### Task 3: Discovery→record adapter in the tunnel package

**Files:**
- Create: `src/otto/tunnel/records.py`
- Test: `tests/unit/tunnel/test_records.py` (create; `tests/unit/tunnel/` exists)

**Interfaces:**
- Consumes: `DiscoveredTunnel`, `TunnelDiscovery`, `discover_tunnels` (`src/otto/tunnel/discovery.py:96-158`); `Tunnel.expected_processes()` (`src/otto/tunnel/model.py:96`).
- Produces: `tunnel_record(d: DiscoveredTunnel) -> TunnelRecord`; `async discover_tunnel_records(lab: Lab) -> list[TunnelRecord]` (sorted by id); `TunnelScanFailed(RuntimeError)` raised when every scannable host was unreachable. Callers: Task 7 (CLI).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/tunnel/test_records.py
"""DiscoveredTunnel -> TunnelRecord adapter (spec 2026-07-16 §2)."""

import pytest

from otto.tunnel.discovery import DiscoveredTunnel
from otto.tunnel.model import Tunnel, TunnelHop
from otto.tunnel.records import TunnelScanFailed, tunnel_record

TUNNEL = Tunnel(
    protocol="udp",
    service_port=15001,
    path=(TunnelHop(host="edge-gw"), TunnelHop(host="core-01"), TunnelHop(host="db-01")),
)


def _discovered(missing: frozenset = frozenset(), uncertain: bool = False) -> DiscoveredTunnel:
    expected = TUNNEL.expected_processes()
    return DiscoveredTunnel(
        tunnel=TUNNEL,
        present=expected - missing,
        missing=set(missing),
        age_seconds=120,
        uncertain=uncertain,
    )


def test_ok_tunnel_maps_ok_with_ordered_hops() -> None:
    rec = tunnel_record(_discovered())
    assert rec.status == "ok"
    assert rec.hops == ["edge-gw", "core-01", "db-01"]
    assert rec.carriers_present == 6
    assert rec.carriers_expected == 6
    assert rec.age_seconds == 120.0
    assert rec.id == TUNNEL.id
    assert rec.protocol == "udp"
    assert rec.service_port == 15001


def test_missing_carriers_map_degraded() -> None:
    some = frozenset(list(TUNNEL.expected_processes())[:2])
    rec = tunnel_record(_discovered(missing=some))
    assert rec.status == "degraded"
    assert rec.carriers_present == 4


def test_uncertain_wins_over_degraded() -> None:
    some = frozenset(list(TUNNEL.expected_processes())[:2])
    assert tunnel_record(_discovered(missing=some, uncertain=True)).status == "uncertain"


def test_all_unreachable_scan_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-unreachable is a FAILED scan, not an empty lab — it must raise so
    the collector keeps the last known set (guard what you emit)."""
    import asyncio

    from otto.tunnel import records as mod
    from otto.tunnel.discovery import TunnelDiscovery

    class _Host:
        has_bash = True
        id = "h1"

    class _Lab:
        hosts = {"h1": _Host()}

    async def fake_discover(lab: object) -> TunnelDiscovery:
        return TunnelDiscovery(tunnels=[], unreachable=["h1"])

    monkeypatch.setattr(mod, "discover_tunnels", fake_discover)
    with pytest.raises(TunnelScanFailed):
        asyncio.run(mod.discover_tunnel_records(_Lab()))


def test_no_scannable_hosts_is_a_successful_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from otto.tunnel import records as mod
    from otto.tunnel.discovery import TunnelDiscovery

    class _Lab:
        hosts: dict = {}

    async def fake_discover(lab: object) -> TunnelDiscovery:
        return TunnelDiscovery(tunnels=[], unreachable=[])

    monkeypatch.setattr(mod, "discover_tunnels", fake_discover)
    assert asyncio.run(mod.discover_tunnel_records(_Lab())) == []
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/unit/tunnel/test_records.py -v`
Expected: FAIL — `ModuleNotFoundError: otto.tunnel.records`.

- [ ] **Step 3: Implement `src/otto/tunnel/records.py`**

```python
"""Adapter: tunnel discovery -> monitor ``TunnelRecord`` rows.

Lives tunnel-side so the monitor package never imports ``otto.tunnel`` —
the collector consumes these through an injected callable composed in
``otto.cli.monitor`` (spec 2026-07-16 §2).
"""

from typing import TYPE_CHECKING

from ..models.monitor import TunnelRecord
from .discovery import DiscoveredTunnel, discover_tunnels

if TYPE_CHECKING:
    from ..config.lab import Lab


class TunnelScanFailed(RuntimeError):
    """A discovery pass that reached no host at all.

    Raised instead of returning ``[]`` so a dead scan can never masquerade as
    an empty lab and blank the topology's tunnel layer.
    """


def tunnel_record(discovered: DiscoveredTunnel) -> TunnelRecord:
    """Map one discovery result to its wire record.

    Status derives from the discovery FIELDS (``uncertain``/``missing``),
    never from parsing the human ``status`` string.
    """
    if discovered.uncertain:
        status = "uncertain"
    elif discovered.missing:
        status = "degraded"
    else:
        status = "ok"
    tunnel = discovered.tunnel
    return TunnelRecord(
        id=tunnel.id,
        protocol=tunnel.protocol,
        service_port=tunnel.service_port,
        hops=[hop.host for hop in tunnel.path],
        status=status,
        carriers_present=len(discovered.present),
        carriers_expected=len(tunnel.expected_processes()),
        age_seconds=float(discovered.age_seconds),
    )


async def discover_tunnel_records(lab: "Lab") -> list[TunnelRecord]:
    """One full-lab scan as sorted wire records; raises on a dead scan."""
    discovery = await discover_tunnels(lab)
    scannable = [h for h in lab.hosts.values() if getattr(h, "has_bash", False)]
    if scannable and len(discovery.unreachable) == len(scannable):
        raise TunnelScanFailed(
            f"tunnel scan reached none of the lab's {len(scannable)} scannable hosts"
        )
    return sorted((tunnel_record(d) for d in discovery.tunnels), key=lambda r: r.id)
```

Note: `status` is typed by inference as `str`; if `ty` complains about the
`Literal` field, annotate `status: Literal["ok", "degraded", "uncertain"]`
with the import from `typing`.

- [ ] **Step 4: Run tests + typecheck, commit**

```bash
uv run pytest tests/unit/tunnel/test_records.py -v && uv run nox -s typecheck lint
git add src/otto/tunnel/records.py tests/unit/tunnel/test_records.py
git commit -m "feat(tunnel): discovery->TunnelRecord adapter with fail-loud dead-scan rule"
```

---

### Task 4: Collector `_tunnel_loop`

**Files:**
- Modify: `src/otto/monitor/collector.py` (constructor :145, run() :448, publish section)
- Test: `tests/unit/monitor/test_collector_tunnels.py` (create)

**Interfaces:**
- Consumes: `TunnelRecord` (Task 1). `MetricDB.write_tunnels` (Task 5 — this task fakes it; the real method lands next).
- Produces: `MetricCollector(..., tunnel_source: Callable[[], Awaitable[list[TunnelRecord]]] | None = None)`; `collector.get_tunnel_records() -> list[TunnelRecord]`; `_tunnel_pass()` publishing `{"format": 1, "session": ..., "tunnels": [...]}` on change. Consumed by Tasks 6, 7, 12.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/monitor/test_collector_tunnels.py
"""The collector's tunnel loop: change-detection, failure containment
(spec 2026-07-16 §2). Every guard here is mutation-proven — see Step 5."""

import asyncio
import logging
from typing import Any

import pytest

from otto.models.monitor import TunnelRecord
from otto.monitor.collector import MetricCollector


def _rec(tid: str, status: str = "ok") -> TunnelRecord:
    return TunnelRecord(
        id=tid,
        protocol="udp",
        service_port=15001,
        hops=["a", "b"],
        status=status,  # type: ignore[arg-type]
        carriers_present=4,
        carriers_expected=4,
    )


class _Script:
    """A tunnel_source scripted per call: a list -> that set; an Exception -> raise."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> list[TunnelRecord]:
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _SpyDB:
    """Records write ordering; quacks just enough of MetricDB."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def write_tunnels(self, tunnels_json: str) -> None:
        self._log.append(f"db:{tunnels_json}")


def _collector(source: _Script, log: list[str] | None = None) -> MetricCollector:
    c = MetricCollector(hosts=[], tunnel_source=source)
    c.session_id = "s1"
    if log is not None:
        c._db = _SpyDB(log)  # type: ignore[assignment]
        c._publish = lambda frag: log.append(f"sse:{sorted(frag)}")  # type: ignore[method-assign]
    return c


def test_first_pass_with_tunnels_publishes_and_stores() -> None:
    source = _Script([_rec("tun-a-1")])
    log: list[str] = []
    c = _collector(source, log)
    asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == [_rec("tun-a-1")]
    assert len(log) == 2
    assert log[0].startswith("db:")  # db BEFORE publish
    assert log[1].startswith("sse:")


def test_unchanged_set_does_not_republish() -> None:
    source = _Script([_rec("tun-a-1")], [_rec("tun-a-1")])
    log: list[str] = []
    c = _collector(source, log)
    asyncio.run(c._tunnel_pass())
    asyncio.run(c._tunnel_pass())
    assert len(log) == 2  # one db + one sse total


def test_status_flip_republishes() -> None:
    source = _Script([_rec("tun-a-1")], [_rec("tun-a-1", status="degraded")])
    log: list[str] = []
    c = _collector(source, log)
    asyncio.run(c._tunnel_pass())
    asyncio.run(c._tunnel_pass())
    assert len(log) == 4


def test_successful_empty_scan_publishes_empty_exactly_once() -> None:
    source = _Script([_rec("tun-a-1")], [], [])
    log: list[str] = []
    c = _collector(source, log)
    for _ in range(3):
        asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == []
    assert len(log) == 4  # initial pair + the [] pair; third pass is a no-op


def test_failed_scan_keeps_last_state_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _Script(
        [_rec("tun-a-1")], RuntimeError("net down"), RuntimeError("still down"), [_rec("tun-a-1")]
    )
    log: list[str] = []
    c = _collector(source, log)
    with caplog.at_level(logging.DEBUG, logger="otto.monitor.collector"):
        for _ in range(4):
            asyncio.run(c._tunnel_pass())
    assert c.get_tunnel_records() == [_rec("tun-a-1")]  # never blanked
    assert len(log) == 2  # failures wrote/published NOTHING; recovery set is unchanged
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # first failure + recovery, not one per failing tick
    assert "tunnel scan" in warnings[0].message


def test_initial_run_spawns_tunnel_loop() -> None:
    """run() wires the loop when a source is present. Uses a shell-less
    target so the collection side idles; duration=0 -> exactly the initial
    passes."""
    from datetime import timedelta

    from otto.monitor.collector import MonitorTarget

    class _Host:
        name = "h1"
        id = "h1"

        async def run(self, commands: list[str], **kwargs: Any) -> Any:
            raise RuntimeError("no shell in this test")

    source = _Script([], [])
    c = MetricCollector(targets=[MonitorTarget(host=_Host(), parsers={})], tunnel_source=source)  # type: ignore[arg-type]
    asyncio.run(c.run(interval=timedelta(milliseconds=10), duration=timedelta(0)))
    assert source.calls >= 1
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/unit/monitor/test_collector_tunnels.py -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'tunnel_source'`.

- [ ] **Step 3: Implement in `src/otto/monitor/collector.py`**

Constructor (line 145): add the parameter and state. Imports: extend the
existing `collections.abc`/`typing` imports with `Awaitable`, `Callable`;
add `TunnelRecord` to the existing `otto.models.monitor` import (check what
the module already imports; it has `Any`, `json`, `logging`, `datetime`).

```python
    def __init__(
        self,
        hosts: "Sequence[RemoteHost] | None" = None,
        parsers: list[MetricParser] | None = None,
        db: MetricDB | None = None,
        targets: "list[MonitorTarget] | None" = None,
        tunnel_source: "Callable[[], Awaitable[list[TunnelRecord]]] | None" = None,
    ) -> None:
```

In the body, near `self.session_id` (line 232):

```python
        # Tunnel layer (spec 2026-07-16): an injected full-lab discovery
        # callable — the monitor package never imports otto.tunnel. None
        # (suite, plugin, scripted collectors) means no tunnel loop at all.
        self._tunnel_source = tunnel_source
        # Last known tunnel set, sorted by id. NEVER blanked by a failed
        # scan — only a successful scan may change it (guard what you emit).
        self._tunnels: list[TunnelRecord] = []
        # Edge-triggered failure logging: warn on the 0->1 transition and on
        # recovery, debug in between (mirrors the parser-health pattern).
        self._tunnel_scan_failing = False
```

Public accessor, near `get_chart_map`/`subscribe`:

```python
    def get_tunnel_records(self) -> list[TunnelRecord]:
        """Last known tunnel set (sorted by id) — the export producer's input."""
        return list(self._tunnels)
```

The pass + loop, placed after `_bucket_loop`'s enclosing `run()` (module
level of the class, near `_record_point`):

```python
    async def _tunnel_pass(self) -> None:
        """One discovery pass: scan, diff, persist-then-publish on change."""
        assert self._tunnel_source is not None  # noqa: S101 — loop only spawns with a source
        try:
            records = sorted(await self._tunnel_source(), key=lambda r: r.id)
        except Exception as err:  # noqa: BLE001 — any scan failure keeps last state
            if not self._tunnel_scan_failing:
                logger.warning(
                    "Monitor: tunnel scan failed (keeping last known set): %s", err
                )
                self._tunnel_scan_failing = True
            else:
                logger.debug("Monitor: tunnel scan still failing: %s", err)
            return
        if self._tunnel_scan_failing:
            logger.warning("Monitor: tunnel scan recovered")
            self._tunnel_scan_failing = False
        if records == self._tunnels:
            return
        self._tunnels = records
        payload = [r.model_dump(mode="json") for r in records]
        # DB first, then broadcast: a crash between the two can only make
        # hydrate FRESHER than the stream, never staler (spec §2).
        if self._db:
            await self._db.write_tunnels(json.dumps(payload))
        self._publish({"format": 1, "session": self.session_id, "tunnels": payload})
```

In `run()`, replace the final gather (line 448):

```python
        loops = [_bucket_loop(s, e) for s, e in buckets.items()]
        if self._tunnel_source is not None:
            loops.append(self._tunnel_loop(secs, start, duration))
        await asyncio.gather(*loops)
```

And add the loop next to `_tunnel_pass` (same `gather(sleep, work)` cadence
primitive as `_bucket_loop` — period is max(interval, scan_time), so passes
serialize by construction):

```python
    async def _tunnel_loop(
        self, secs: float, start: datetime, duration: "timedelta | None"
    ) -> None:
        await self._tunnel_pass()
        while duration is None or datetime.now(tz=timezone.utc) - start < duration:
            await asyncio.gather(asyncio.sleep(secs), self._tunnel_pass())
```

- [ ] **Step 4: Run tests, verify pass**

Run: `uv run pytest tests/unit/monitor/test_collector_tunnels.py -v`
Expected: 7 passed. (If `_SpyDB` trips `ty` on the `_db` assignment, keep the
`# type: ignore[assignment]` — the fake is deliberate.)

- [ ] **Step 5: Mutation-prove the guards**

Temporarily, one at a time (revert each):
1. Delete the `if records == self._tunnels: return` lines → `test_unchanged_set_does_not_republish` must FAIL.
2. In the `except` branch, add `self._tunnels = []` before `return` → `test_failed_scan_keeps_last_state_and_warns_once` must FAIL.
3. Swap the db write and `_publish` lines → `test_first_pass_with_tunnels_publishes_and_stores` must FAIL.

All three red? Revert the mutations, re-run green.

- [ ] **Step 6: Typecheck + commit**

```bash
uv run nox -s typecheck lint
git add src/otto/monitor/collector.py tests/unit/monitor/test_collector_tunnels.py
git commit -m "feat(monitor): collector tunnel loop — change-detected, fail-contained, db-first"
```

---

### Task 5: `tunnels_json` persistence

**Files:**
- Modify: `src/otto/monitor/db.py` (_SCHEMA :40-49, _check_session_columns :107-123, SessionRow :139-160, write methods :309, read_sessions :430-445)
- Test: `tests/unit/monitor/test_db_tunnels.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `sessions.tunnels_json` column (default `'[]'`); `MetricDB.write_tunnels(tunnels_json: str)`; `SessionRow.tunnels_json: str`. Consumed by Tasks 4 (already faked) and 6.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/monitor/test_db_tunnels.py
"""tunnels_json persistence: in-place v2 column, chart_map_json precedent."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from otto.monitor.db import MetricDB, UnsupportedDBError, read_sessions
from otto.monitor.session import SessionFrame

START = datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)


def _frame() -> SessionFrame:
    return SessionFrame(id="s1", label=None, note=None, start=START, end=None)


def _open_write_close(path: str, tunnels_json: str) -> None:
    async def go() -> None:
        db = MetricDB(path, _frame(), lab_json="{}", meta_json="{}")
        await db.open()
        await db.write_tunnels(tunnels_json)
        await db.close()

    asyncio.run(go())


def test_write_tunnels_round_trips(tmp_path: Path) -> None:
    path = str(tmp_path / "m.db")
    _open_write_close(path, '[{"id": "tun-a-1"}]')
    rows = read_sessions(path)
    assert rows[0].tunnels_json == '[{"id": "tun-a-1"}]'


def test_fresh_session_defaults_to_empty_list(tmp_path: Path) -> None:
    path = str(tmp_path / "m.db")

    async def go() -> None:
        db = MetricDB(path, _frame(), lab_json="{}", meta_json="{}")
        await db.open()
        await db.close()

    asyncio.run(go())
    assert read_sessions(path)[0].tunnels_json == "[]"


def test_pre_column_v2_database_is_refused_loud(tmp_path: Path) -> None:
    """The chart_map_json precedent: same version, old shape, loud refusal
    naming the missing column."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, label TEXT, note TEXT, start TEXT NOT NULL,
            end TEXT, lab_json TEXT NOT NULL DEFAULT '{}',
            meta_json TEXT NOT NULL DEFAULT '{}',
            chart_map_json TEXT NOT NULL DEFAULT '{}'
        );
        PRAGMA user_version = 2;
        """
    )
    conn.close()
    with pytest.raises(UnsupportedDBError, match="tunnels_json"):
        read_sessions(path)
```

Check `SessionFrame`'s actual constructor (`src/otto/monitor/session.py`) and
adjust `_frame()` if its signature differs (it may be `new_frame(...)` — use
whatever the existing db tests use; `grep -rn "SessionFrame(" tests/unit/monitor/`).

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/unit/monitor/test_db_tunnels.py -v`
Expected: FAIL — `AttributeError: ... 'write_tunnels'`.

- [ ] **Step 3: Implement in `src/otto/monitor/db.py`**

In `_SCHEMA`, extend the `sessions` CREATE (line 48) and its header comment:

```sql
    chart_map_json TEXT    NOT NULL DEFAULT '{}',
    tunnels_json   TEXT    NOT NULL DEFAULT '[]'
```

Comment addition (after the chart_map_json paragraph):

```
-- tunnels_json was likewise added to v2 in place (spec 2026-07-16): the last
-- known tunnel set as a JSON list of TunnelRecord dumps, overwritten on
-- change by the collector's tunnel loop — last-known-state only, no history.
```

Generalize `_check_session_columns` (line 107):

```python
def _check_session_columns(columns: set[str], path: str) -> None:
    """Refuse a ``sessions`` table missing an in-place v2 column.

    ``chart_map_json`` and ``tunnels_json`` were both added to v2 in place,
    pre-release (see ``_SCHEMA``) — the version number cannot distinguish the
    shapes, only the columns can. Refused loud, no migration, naming the
    missing column.
    """
    for required in ("chart_map_json", "tunnels_json"):
        if columns and required not in columns:
            raise UnsupportedDBError(
                f"'{path}' uses an early development build of schema v2 (its "
                f"sessions table has no {required} column); otto provides no "
                "migration — use a fresh --db file (not supported: converting "
                "pre-column captures)."
            )
```

`SessionRow`: add `tunnels_json: str` after `chart_map_json` (line 155).

New writer after `write_chart_map` (line 324):

```python
    async def write_tunnels(self, tunnels_json: str) -> None:
        """Overwrite this session's last-known tunnel set. No-op if not open.

        Called by the collector's tunnel loop on every CHANGE (not per tick,
        not at finalize) — same crash-tolerance rationale as
        :meth:`write_chart_map`: a crashed session keeps its last known set.
        """
        if not self._conn:
            return
        await self._conn.execute(
            "UPDATE sessions SET tunnels_json = ? WHERE id = ?",
            (tunnels_json, self._frame.id),
        )
        await self._conn.commit()
```

`read_sessions`: add `tunnels_json` to the SELECT (line 432) and the
unpacking (line 435), and pass it through to the `SessionRow(...)`
construction at the end of the loop.

- [ ] **Step 4: Run tests, verify pass — then the full db suite**

```bash
uv run pytest tests/unit/monitor/test_db_tunnels.py -v
uv run pytest tests/unit/monitor/ -v -x
```

Existing db tests that construct `SessionRow` literals or pin the refusal
message will fail — update them to carry `tunnels_json="[]"` and the
generalized message.

- [ ] **Step 5: Typecheck + commit**

```bash
uv run nox -s typecheck lint
git add src/otto/monitor/db.py tests/unit/monitor/
git commit -m "feat(monitor): tunnels_json column — in-place v2, last-known-state upsert"
```

---

### Task 6: Export producers carry tunnels

**Files:**
- Modify: `src/otto/monitor/export.py` (build_live_export :197-210, _session_record :276-288)
- Test: `tests/unit/monitor/test_export_tunnels.py` (create)

**Interfaces:**
- Consumes: `collector.get_tunnel_records()` (Task 4), `SessionRow.tunnels_json` (Task 5), `TunnelRecord` (Task 1).
- Produces: `SessionRecord.tunnels` populated in both live and db exports — what `/api/monitor_sessions` hydrates.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/monitor/test_export_tunnels.py
"""Both export producers carry the last known tunnel set (spec 2026-07-16 §3)."""

import json
from datetime import datetime, timezone

from otto.models.monitor import LabSnapshot, TunnelRecord
from otto.monitor.collector import MetricCollector
from otto.monitor.db import SessionRow
from otto.monitor.export import _session_record, build_live_export
from otto.monitor.session import SessionFrame

REC = TunnelRecord(
    id="tun-a-1",
    protocol="udp",
    service_port=15001,
    hops=["a", "b"],
    status="ok",
    carriers_present=4,
    carriers_expected=4,
)
START = datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)


def test_live_export_carries_collector_tunnels() -> None:
    collector = MetricCollector(hosts=[])
    collector._tunnels = [REC]
    frame = SessionFrame(id="s1", label=None, note=None, start=START, end=None)
    doc = build_live_export(frame, collector, LabSnapshot())
    assert doc.sessions[0].tunnels == [REC]


def test_db_export_parses_tunnels_json() -> None:
    row = SessionRow(
        id="s1",
        label=None,
        note=None,
        start=START.isoformat(),
        end=None,
        lab_json="{}",
        meta_json="{}",
        chart_map_json="{}",
        tunnels_json=json.dumps([REC.model_dump(mode="json")]),
        metrics=[],
        events=[],
        log_events=[],
    )
    assert _session_record(row).tunnels == [REC]
```

(Adjust `SessionFrame` construction as in Task 5 if its signature differs.)

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/unit/monitor/test_export_tunnels.py -v`
Expected: FAIL — `tunnels == []` in both.

- [ ] **Step 3: Implement**

`build_live_export` — add to the `SessionRecord(...)` construction (after
`chart_map=`, line 208):

```python
        tunnels=collector.get_tunnel_records(),
```

`_session_record` — add `TunnelRecord` to the module's
`otto.models.monitor` imports, and to the `SessionRecord(...)` construction
(after `chart_map=`, line 287):

```python
        tunnels=[
            TunnelRecord.model_validate(t) for t in json.loads(row.tunnels_json)
        ],
```

- [ ] **Step 4: Run tests + typecheck, commit**

```bash
uv run pytest tests/unit/monitor/test_export_tunnels.py tests/unit/monitor/ -v
uv run nox -s typecheck lint
git add src/otto/monitor/export.py tests/unit/monitor/test_export_tunnels.py
git commit -m "feat(monitor): live + archive exports carry the tunnel set"
```

---

### Task 7: CLI composition — full-lab discovery on the collector

**Files:**
- Modify: `src/otto/monitor/factory.py` (:20-68), `src/otto/cli/monitor.py` (:215)
- Test: `tests/unit/monitor/test_factory.py` (extend or create)

**Interfaces:**
- Consumes: `discover_tunnel_records` (Task 3), `MetricCollector.tunnel_source` (Task 4).
- Produces: `build_monitor_collector(hosts, db=None, tunnel_source=None)` pass-through; `otto monitor --live` wires `lambda: discover_tunnel_records(lab)` over the FULL lab (decision 3 — not the `selected` subset).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/monitor/test_factory.py (create with imports if absent)
def test_factory_passes_tunnel_source_through() -> None:
    from otto.models.monitor import TunnelRecord
    from otto.monitor.factory import build_monitor_collector

    async def source() -> list[TunnelRecord]:
        return []

    collector = build_monitor_collector(hosts=[], tunnel_source=source)
    assert collector._tunnel_source is source


def test_factory_defaults_to_no_tunnel_source() -> None:
    from otto.monitor.factory import build_monitor_collector

    assert build_monitor_collector(hosts=[])._tunnel_source is None
```

- [ ] **Step 2: Run, verify fail** — `uv run pytest tests/unit/monitor/test_factory.py -v` → `TypeError: unexpected keyword argument`.

- [ ] **Step 3: Implement**

`factory.py` — signature and pass-through (imports: `Awaitable`, `Callable`
from `collections.abc`; `TunnelRecord` from `..models.monitor`):

```python
def build_monitor_collector(
    hosts: Sequence[RemoteHost],
    db: MetricDB | None = None,
    tunnel_source: Callable[[], Awaitable[list[TunnelRecord]]] | None = None,
) -> MetricCollector:
```

…and at the end:

```python
    return MetricCollector(
        targets=targets,
        db=db,
        tunnel_source=tunnel_source,
    )
```

Extend the factory docstring Args with:

```
        tunnel_source: Optional full-lab tunnel discovery callable for the
            collector's tunnel loop (spec 2026-07-16). The CLI composes this
            over the WHOLE lab — tunnels may traverse hosts that metric
            collection was never pointed at.
```

`cli/monitor.py` — at line 215, replace the collector construction:

```python
    # Tunnel discovery scans the WHOLE lab, not `selected`: stats gathering
    # may target a few hosts while tunnels traverse hosts outside that set
    # (spec 2026-07-16, decision 3). Deferred import, matching this module's
    # convention — and keeping otto.tunnel out of CLI startup (import budget).
    from ..tunnel.records import discover_tunnel_records

    active_lab = get_lab()
    collector = build_monitor_collector(
        hosts=selected,
        db=monitor_db,
        tunnel_source=lambda: discover_tunnel_records(active_lab),
    )
```

- [ ] **Step 4: Run + gates, commit**

```bash
uv run pytest tests/unit/monitor/test_factory.py tests/unit/cli/ -v
uv run pytest tests/unit/import_budget/ -v   # the CLI-startup import guard
uv run nox -s typecheck lint
git add src/otto/monitor/factory.py src/otto/cli/monitor.py tests/unit/monitor/test_factory.py
git commit -m "feat(monitor): otto monitor wires full-lab tunnel discovery into the collector"
```

If a `tests/unit/cli/` monitor test constructs the collector path with a
mocked `get_lab`, it may need the mock extended — follow the failure.

---

### Task 8: Web — types, normalize, fragment replace-merge

**Files:**
- Modify: `web/src/data/exportDoc.ts` (NormalizedSession :44-66, normalizeSession :162-237, sessionToRecord :252-274)
- Modify: `web/src/data/fragment.ts` (applyFragment :46-134)
- Modify: `web/src/data/topology.ts` (:304, :401 — dead `"dynamic"` comparisons)
- Test: `web/src/data/fragment.test.ts` — first run `grep -rln "applyFragment" web/src --include="*.test.ts*"`; extend the existing file if one exists, else create.

**Interfaces:**
- Consumes: regenerated `TunnelRecord` type in `web/src/api/export.gen.ts` (Task 2).
- Produces: `NormalizedSession.tunnels: TunnelRecord[]`; `applyFragment` replace rule. Consumed by Tasks 9-12.

- [ ] **Step 1: Write the failing tests**

```typescript
// web/src/data/fragment.test.ts (or append to the existing applyFragment suite)
import { describe, expect, it } from "vitest";

import type { MonitorSessionFragment, TunnelRecord } from "../api/export.gen";
import { applyFragment } from "./fragment";
import { parseExportDocument } from "./exportDoc";

const REC: TunnelRecord = {
  id: "tun-a-1",
  protocol: "udp",
  service_port: 15001,
  hops: ["a", "b"],
  status: "ok",
  carriers_present: 4,
  carriers_expected: 4,
  age_seconds: 60,
};

function freshSession() {
  const doc = {
    format: 1,
    sessions: [{ id: "s1", start: "2026-07-16T10:00:00Z", lab: { hosts: [], links: [] } }],
  };
  return parseExportDocument(JSON.stringify(doc)).sessions[0];
}

function frag(extra: Partial<MonitorSessionFragment>): MonitorSessionFragment {
  return { format: 1, session: "s1", ...extra } as MonitorSessionFragment;
}

describe("tunnel fragments", () => {
  it("normalizes a session without tunnels to an empty list", () => {
    expect(freshSession().tunnels).toEqual([]);
  });

  it("replaces the set wholesale", () => {
    const s1 = applyFragment(freshSession(), frag({ tunnels: [REC] }));
    expect(s1.tunnels).toEqual([REC]);
    const s2 = applyFragment(s1, frag({ tunnels: [{ ...REC, status: "degraded" }] }));
    expect(s2.tunnels).toHaveLength(1);
    expect(s2.tunnels[0].status).toBe("degraded");
  });

  it("[] empties; absent leaves untouched — the wire's None/[] distinction", () => {
    const s1 = applyFragment(freshSession(), frag({ tunnels: [REC] }));
    const untouched = applyFragment(s1, frag({ metrics: [] }));
    expect(untouched.tunnels).toEqual([REC]);
    const emptied = applyFragment(s1, frag({ tunnels: [] }));
    expect(emptied.tunnels).toEqual([]);
  });

  it("a tunnels-only fragment is not treated as a no-op heartbeat", () => {
    const s0 = freshSession();
    const s1 = applyFragment(s0, frag({ tunnels: [REC] }));
    expect(s1).not.toBe(s0);
  });
});
```

- [ ] **Step 2: Run, verify fail**

Run: `cd web && npx vitest run src/data/fragment.test.ts`
Expected: FAIL — `tunnels` undefined on NormalizedSession.

- [ ] **Step 3: Implement**

`exportDoc.ts`:
- Import `TunnelRecord` in the existing type import from `../api/export.gen`.
- `NormalizedSession` gains (after `chartMap`): `tunnels: TunnelRecord[];`
- `normalizeSession` return gains: `tunnels: raw.tunnels ?? [],`
- `sessionToRecord` return gains: `tunnels: session.tunnels,`

`fragment.ts` — in `applyFragment`, after `hasChartMapPatch` (line 65):

```typescript
  // REPLACE semantics (the meta precedent): null/undefined = no update,
  // a list — including [] — replaces the set wholesale. Last known state,
  // expressed on the wire (spec 2026-07-16 §1).
  const hasTunnels = frag.tunnels != null;
```

Extend the no-op guard (line 77-84) with `&& !hasTunnels`, and the return
(line 133):

```typescript
  const tunnels = hasTunnels ? (frag.tunnels as TunnelRecord[]) : session.tunnels;
  return { ...session, events, endMs, meta, chartMap, tunnels };
```

(Import `TunnelRecord` type at the top.)

`topology.ts` — lines 304 and 401: `link.provenance === "dynamic" ? "dynamic" : "declared"` becomes `"declared"` (the narrowed wire type makes the comparison dead). Leave `TopoEdge.provenance`'s `"dynamic"` union member — Task 9 feeds it from tunnels.

- [ ] **Step 4: Run tests + web checks, commit**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check src && cd ..
make web   # drift gate goes green again from here
git add web/src/data/ web/src/api/export.gen.ts
git commit -m "feat(web): sessions carry tunnels; fragments replace the set wholesale"
```

If other vitest suites pinned `NormalizedSession` literals, add `tunnels: []` to them.

---

### Task 9: Web — hop-pairs to riding/bare tunnel segments

**Files:**
- Modify: `web/src/data/topology.ts` (TopoEdge :49-58, buildTopoGraph :222-426)
- Test: extend the suite found by `grep -rln "buildTopoGraph" web/src --include="*.test.ts*"` (create `web/src/data/topology.tunnels.test.ts` if the existing file is huge)

**Interfaces:**
- Consumes: `NormalizedSession.tunnels` (Task 8), `pairKey`, `assignParallelIndices` (topology.ts:198).
- Produces: `TopoEdge.tunnel?: TunnelRecord` and `TopoEdge.tunnelGroupSize?: number`; segment edges with ids `` `${tunnel.id}:${i}` ``. Geometry contract for Task 10: a riding segment carries the underlay's `parallelIndex` and `tunnelGroupSize` = the pair's static-edge count, so `routeEdge` reproduces the underlay path exactly; non-riding segments get fan slots after the static group.

- [ ] **Step 1: Write the failing tests**

```typescript
// web/src/data/topology.tunnels.test.ts
import { describe, expect, it } from "vitest";

import type { TunnelRecord } from "../api/export.gen";
import { parseExportDocument } from "./exportDoc";
import { buildTopoGraph } from "./topology";

function session(tunnels: TunnelRecord[], links: object[] = []) {
  const doc = {
    format: 1,
    sessions: [
      {
        id: "s1",
        start: "2026-07-16T10:00:00Z",
        lab: {
          hosts: [
            { id: "gw", element: "gw", ip: "10.0.0.1" },
            { id: "mid", element: "mid", ip: "10.0.0.2" },
            { id: "db", element: "db", ip: "10.0.0.3" },
          ],
          links,
        },
        tunnels,
      },
    ],
  };
  return parseExportDocument(JSON.stringify(doc)).sessions[0];
}

const TUN: TunnelRecord = {
  id: "tun-x-1",
  protocol: "udp",
  service_port: 15001,
  hops: ["gw", "mid", "db"],
  status: "ok",
  carriers_present: 6,
  carriers_expected: 6,
  age_seconds: 1,
};

const LINK = (id: string, a: string, b: string, provenance = "declared") => ({
  id,
  endpoints: [
    { host: a, ip: "10.0.0.1" },
    { host: b, ip: "10.0.0.2" },
  ],
  provenance,
});

describe("tunnel segments", () => {
  it("emits one segment per consecutive hop pair", () => {
    const g = buildTopoGraph(session([TUN]), new Map(), { sources: false });
    const segs = g.edges.filter((e) => e.provenance === "dynamic");
    expect(segs.map((e) => e.id)).toEqual(["tun-x-1:0", "tun-x-1:1"]);
    expect(segs[0].tunnel).toEqual(TUN);
  });

  it("a riding segment adopts its underlay's geometry basis", () => {
    const g = buildTopoGraph(session([TUN], [LINK("l-gw-mid", "gw", "mid")]), new Map(), {
      sources: false,
    });
    const underlay = g.edges.find((e) => e.id === "l-gw-mid");
    const seg = g.edges.find((e) => e.id === "tun-x-1:0");
    expect(seg?.parallelIndex).toBe(underlay?.parallelIndex);
    expect(seg?.tunnelGroupSize).toBe(1); // the pair's static count
  });

  it("prefers declared over implicit underlays", () => {
    const g = buildTopoGraph(
      session([TUN], [LINK("l-imp", "gw", "mid", "implicit"), LINK("l-dec", "gw", "mid")]),
      new Map(),
      { sources: false },
    );
    const declared = g.edges.find((e) => e.id === "l-dec");
    const seg = g.edges.find((e) => e.id === "tun-x-1:0");
    expect(seg?.parallelIndex).toBe(declared?.parallelIndex);
  });

  it("underlay-less pairs get bare segments; underlays never re-fan", () => {
    const bare = buildTopoGraph(session([TUN]), new Map(), { sources: false });
    expect(bare.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(2);
    // static parallelIndex assignment must not count tunnel edges:
    const withLink = buildTopoGraph(session([TUN], [LINK("l-gw-mid", "gw", "mid")]), new Map(), {
      sources: false,
    });
    expect(withLink.edges.find((e) => e.id === "l-gw-mid")?.parallelIndex).toBe(0);
  });

  it("a hop host missing from the SESSION warns and drops the segment", () => {
    const ghost = { ...TUN, hops: ["gw", "nope"] };
    const g = buildTopoGraph(session([ghost]), new Map(), { sources: false });
    expect(g.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(0);
    expect(g.warnings.some((w) => w.includes("tun-x-1") && w.includes("nope"))).toBe(true);
  });
});
```

- [ ] **Step 2: Run, verify fail** — `cd web && npx vitest run src/data/topology.tunnels.test.ts` → no `dynamic` edges emitted.

- [ ] **Step 3: Implement in `topology.ts`**

`TopoEdge` gains:

```typescript
  /** The tunnel this segment belongs to (`dynamic` edges only). */
  tunnel?: TunnelRecord;
  /** Geometry basis for `routeEdge`: a riding segment carries its underlay's
   * group size so it reproduces the underlay path exactly; a fanned segment
   * carries static+fanned count. TopologyPage passes this as `groupSize`
   * INSTEAD of counting (tunnel edges are excluded from the pair counts so
   * underlays never re-fan when tunnels churn). */
  tunnelGroupSize?: number;
```

(Import `TunnelRecord` in the type import at line 11.)

New function after `assignParallelIndices` (line 212):

```typescript
/** Tunnel overlay segments (spec 2026-07-16 §4). Called AFTER
 * assignParallelIndices(staticEdges): the static graph's slots are frozen
 * first, so tunnel churn can never re-fan an underlay. `nodeOf` maps a hop
 * HOST id to a node at this view level (element id / host id), undefined if
 * out of view. `knownHost` distinguishes "out of this view" (silent skip)
 * from "not in the session at all" (data error -> warning). */
function buildTunnelEdges(
  tunnels: TunnelRecord[],
  nodeOf: (hostId: string) => string | undefined,
  knownHost: (hostId: string) => boolean,
  staticEdges: TopoEdge[],
  warnings: string[],
): TopoEdge[] {
  const byPair = new Map<string, TopoEdge[]>();
  for (const e of staticEdges) {
    if (e.provenance === "reports-for") continue; // never an underlay
    const key = pairKey(e.source, e.target);
    const list = byPair.get(key);
    if (list) list.push(e);
    else byPair.set(key, [e]);
  }
  // Underlay preference: declared over anything else, then stable by id.
  const underlayOf = (key: string): TopoEdge | undefined =>
    [...(byPair.get(key) ?? [])].sort(
      (a, b) =>
        (a.provenance === "declared" ? 0 : 1) - (b.provenance === "declared" ? 0 : 1) ||
        a.id.localeCompare(b.id),
    )[0];

  const out: TopoEdge[] = [];
  const fanned = new Map<string, TopoEdge[]>(); // non-riding segments per pair
  const ridden = new Set<string>(); // pairs whose underlay already carries one rider
  for (const t of [...tunnels].sort((a, b) => a.id.localeCompare(b.id))) {
    for (let i = 0; i + 1 < t.hops.length; i++) {
      const [ha, hb] = [t.hops[i], t.hops[i + 1]];
      if (!knownHost(ha) || !knownHost(hb)) {
        warnings.push(`tunnel ${t.id}: hop host "${knownHost(ha) ? hb : ha}" not in lab snapshot`);
        continue;
      }
      const [a, b] = [nodeOf(ha), nodeOf(hb)];
      if (!a || !b) continue; // out of this view level
      if (a === b) continue; // both hops inside one element at this level
      const key = pairKey(a, b);
      const underlay = underlayOf(key);
      const seg: TopoEdge = {
        id: `${t.id}:${i}`,
        source: a,
        target: b,
        provenance: "dynamic",
        tunnel: t,
        impair: null,
        parallelIndex: 0,
        tunnelGroupSize: 0,
      };
      const staticSize = byPair.get(key)?.length ?? 0;
      if (underlay && !ridden.has(key)) {
        // First rider reproduces the underlay path exactly.
        ridden.add(key);
        seg.parallelIndex = underlay.parallelIndex;
        seg.tunnelGroupSize = staticSize;
      } else {
        const list = fanned.get(key) ?? [];
        seg.parallelIndex = staticSize + list.length;
        list.push(seg);
        fanned.set(key, list);
      }
      out.push(seg);
    }
  }
  // Fanned segments learn their final group size once the pair is complete.
  for (const [key, list] of fanned) {
    const staticSize = byPair.get(key)?.length ?? 0;
    for (const seg of list) seg.tunnelGroupSize = staticSize + list.length;
  }
  return out;
}
```

Wire it into `buildTopoGraph`, replacing the single `assignParallelIndices(edges)` call (line 424):

```typescript
  assignParallelIndices(edges); // static slots FIRST — frozen before tunnels
  const knownHost = (id: string): boolean => byId.has(id);
  const nodeOf =
    opts.expand === undefined
      ? (hostId: string): string | undefined => elementOf.get(hostId)
      : (hostId: string): string | undefined => (includeIds.has(hostId) ? hostId : undefined);
  edges.push(...buildTunnelEdges(session.tunnels, nodeOf, knownHost, edges, warnings));
```

For the intra branch, hoist the `include` map's key set to a variable
`includeIds` visible at the call site (`const includeIds = new Set(include.keys())`
after `include` is fully built, around line 361; in the inter branch declare
`const includeIds = new Set<string>()` so the identifier exists — or simply
build `nodeOf` inside each branch and store it in a `let nodeOf` declared
before the `if`; pick whichever reads cleaner in place).

- [ ] **Step 4: Run tests + checks, commit**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check src && cd ..
git add web/src/data/
git commit -m "feat(web): map tunnels onto riding/bare topology segments"
```

---

### Task 10: Web — rendering: status styles, geometry reuse, whole-tunnel emphasis

**Files:**
- Modify: `web/src/topo/edgeStyles.ts`, `web/src/topo/LinkEdge.tsx`, `web/src/topo/TopologyPage.tsx` (:200-232, :304-311), `web/src/topo/linkText.ts`
- Modify: the stylesheet defining `--topo-edge-*` (find it: `grep -rn "topo-edge-tunnel-casing" web/src --include="*.css"`) — add `--topo-edge-tunnel-degraded` in BOTH light and dark blocks
- Test: `web/src/topo/edgeStyles.test.ts` (extend or create beside it)

**Interfaces:**
- Consumes: `TopoEdge.tunnel`, `TopoEdge.tunnelGroupSize` (Task 9).
- Produces: `tunnelEdgeStyle(status, emphasized)` in edgeStyles.ts; `LinkEdgeData.tunnelEmphasized?: boolean`; DOM contract for Task 12: each segment `<g>` keeps `data-testid="topo-link-<segId>"`, gains `data-tunnel="<tunnelId>"` and `data-tunnel-status="<status>"`; emphasis raises stroke width by `EMPHASIS_WIDTH`.

- [ ] **Step 1: Failing style test**

```typescript
// web/src/topo/edgeStyles.test.ts (extend if present)
import { describe, expect, it } from "vitest";

import { EDGE_STYLES, EMPHASIS_WIDTH, tunnelEdgeStyle } from "./edgeStyles";

describe("tunnelEdgeStyle", () => {
  it("ok keeps the shipped tunnel stroke", () => {
    const s = tunnelEdgeStyle("ok", false);
    expect(s.stroke).toBe(EDGE_STYLES.tunnel.stroke);
    expect(s.strokeDasharray).toBe("7 4");
    expect(s.opacity).toBeUndefined();
  });

  it("degraded takes the warning accent, same geometry", () => {
    const s = tunnelEdgeStyle("degraded", false);
    expect(s.stroke).toBe("var(--topo-edge-tunnel-degraded)");
    expect(s.strokeDasharray).toBe("7 4");
  });

  it("uncertain ghosts", () => {
    expect(tunnelEdgeStyle("uncertain", false).opacity).toBeLessThan(1);
  });

  it("emphasis widens and restores opacity", () => {
    const s = tunnelEdgeStyle("uncertain", true);
    expect(s.strokeWidth).toBe(EDGE_STYLES.tunnel.strokeWidth + EMPHASIS_WIDTH);
    expect(s.opacity).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run, verify fail** — `cd web && npx vitest run src/topo/edgeStyles.test.ts`.

- [ ] **Step 3: Implement**

`edgeStyles.ts` — after `edgeStyle` (line 122):

```typescript
export type TunnelStatus = "ok" | "degraded" | "uncertain";

/** Ghost opacity for a tunnel whose last scan couldn't reach a hop host. */
const UNCERTAIN_OPACITY = 0.4;

/** Status variants over the ONE tunnel class (spec 2026-07-16 §4): ok = the
 * shipped stroke, degraded = warning accent on identical geometry, uncertain
 * = ghosted. One tunnel, one status — callers apply this to every segment.
 * Colour values exist in BOTH theme blocks (resolve the values, not the
 * vars — the dark-mode-only collision lesson). */
export function tunnelEdgeStyle(
  status: TunnelStatus,
  emphasized: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string; opacity?: number } {
  const base = edgeStyle("dynamic", emphasized);
  if (status === "degraded") return { ...base, stroke: "var(--topo-edge-tunnel-degraded)" };
  if (status === "uncertain" && !emphasized) return { ...base, opacity: UNCERTAIN_OPACITY };
  return base;
}
```

Stylesheet — add beside the existing `--topo-edge-tunnel-casing`
definitions, in the light block and the dark override block respectively
(match the file's exact selector conventions):

```css
  --topo-edge-tunnel-degraded: #b54708; /* warning-700 on light */
```
```css
  --topo-edge-tunnel-degraded: #f79009; /* warning-500 on dark */
```

`LinkEdge.tsx` — three changes:

```typescript
export interface LinkEdgeData {
  edge: TopoEdge;
  groupSize: number;
  hovered?: boolean;
  /** Another segment of the same tunnel is hovered/selected. */
  tunnelEmphasized?: boolean;
  [key: string]: unknown;
}
```

In the component body (line 64):

```typescript
  const tunnel = edge.tunnel;
  const emphasized = (selected ?? false) || hovered || (data.tunnelEmphasized ?? false);
  const style = tunnel !== undefined ? tunnelEdgeStyle(tunnel.status, emphasized) : edgeStyle(edge.provenance, emphasized);
  const casing = EDGE_STYLES[edgeClass(edge.provenance)].casing;
```

…render `<BaseEdge id={id} path={geom.path} style={style} />` and extend the `<g>`:

```typescript
    <g
      data-testid={`topo-link-${edge.id}`}
      data-provenance={edge.provenance}
      data-tunnel={tunnel?.id}
      data-tunnel-status={tunnel?.status}
    >
```

(Import `tunnelEdgeStyle`; the old `emphasized` const is replaced.)

`TopologyPage.tsx` — in the `flow` memo (line 200), count only non-tunnel
edges and hand tunnel edges their carried size:

```typescript
    const groupSizes = new Map<string, number>();
    for (const e of graph.edges) {
      if (e.tunnel !== undefined) continue; // frozen static slots only
      const key = pairKey(e.source, e.target);
      groupSizes.set(key, (groupSizes.get(key) ?? 0) + 1);
    }
    const edges: Edge[] = graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      type: "link",
      data: {
        edge: e,
        groupSize:
          e.tunnel !== undefined
            ? (e.tunnelGroupSize ?? 1)
            : (groupSizes.get(pairKey(e.source, e.target)) ?? 1),
      },
    }));
```

Whole-tunnel emphasis — replace the hover-decoration memo (line 218):

```typescript
  const hoveredTunnel = useMemo(() => {
    const hit = flow.edges.find((e) => e.id === hoveredEdge);
    return ((hit?.data as { edge?: TopoEdge } | undefined)?.edge?.tunnel?.id ?? null) as
      | string
      | null;
  }, [flow.edges, hoveredEdge]);
  const selectedTunnel = selectedEdge?.tunnel?.id ?? null;
  const edges = useMemo(
    () =>
      flow.edges.map((e) => {
        const edge = (e.data as { edge: TopoEdge }).edge;
        const sameTunnel =
          edge.tunnel !== undefined &&
          (edge.tunnel.id === hoveredTunnel || edge.tunnel.id === selectedTunnel);
        return { ...e, data: { ...e.data, hovered: e.id === hoveredEdge, tunnelEmphasized: sameTunnel } };
      }),
    [flow.edges, hoveredEdge, hoveredTunnel, selectedTunnel],
  );
```

Selection gate (line 310) — tunnels have no `link`, so admit them explicitly:

```typescript
                if (data?.edge && (primaryLink(data.edge) !== null || data.edge.tunnel !== undefined))
                  onSelectEdge(data.edge);
```

`linkText.ts` — tunnel-aware naming:

```typescript
export function edgeTitle(edge: TopoEdge): string {
  if (edge.tunnel !== undefined) return edge.tunnel.id;
  // ...existing branches unchanged...
}

export function edgeSubtitle(edge: TopoEdge): string {
  if (edge.tunnel !== undefined) {
    return `tunnel · ${edge.tunnel.status} · ${edge.tunnel.protocol}`;
  }
  // ...existing branches unchanged...
}
```

- [ ] **Step 4: Run all web checks, commit**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check src && cd ..
git add web/src/topo/ web/src/data/ $(grep -rl "topo-edge-tunnel-degraded" web/src)
git commit -m "feat(web): tunnel status styling, underlay geometry reuse, whole-tunnel emphasis"
```

---

### Task 11: Web — inspector tunnel block

**Files:**
- Modify: `web/src/topo/LinkInspector.tsx` (:76-119)
- Test: `web/src/topo/linkText.test.ts` (extend/create — the inspector itself is e2e-covered in Task 12)

**Interfaces:**
- Consumes: `edge.tunnel` (Task 9), `edgeTitle` (Task 10).
- Produces: inspector rows with testids `inspector-tunnel-status`, `inspector-tunnel-carriers`, `inspector-tunnel-port`, `inspector-tunnel-age`, `inspector-tunnel-path` (Task 12 asserts these).

- [ ] **Step 1: Failing test (title/subtitle already covered; pin the path text helper)**

```typescript
// web/src/topo/linkText.test.ts — append (create with imports if absent)
import { describe, expect, it } from "vitest";

import { tunnelPathText } from "./linkText";

describe("tunnelPathText", () => {
  it("joins hops in order", () => {
    expect(tunnelPathText(["gw", "mid", "db"])).toBe("gw → mid → db");
  });
});
```

- [ ] **Step 2: Run, verify fail** — missing export.

- [ ] **Step 3: Implement**

`linkText.ts`:

```typescript
export function tunnelPathText(hops: string[]): string {
  return hops.join(" → ");
}
```

`LinkInspector.tsx` — the title line (line 78) becomes tunnel-aware and a
tunnel block renders before the NetEm placeholder:

```typescript
  const tunnel = edge.tunnel;
  const title = tunnel?.id ?? primary?.name ?? primary?.id ?? edge.id;
```

Inside `<SlideoutMenu.Content>`, after the `{primary && (...)}` block
(line 111):

```tsx
        {tunnel && (
          <div className="flex flex-col gap-2" data-testid="inspector-tunnel">
            <Row label="Status" testId="inspector-tunnel-status">
              {tunnel.status}
            </Row>
            <Row label="Carriers" testId="inspector-tunnel-carriers">
              {tunnel.carriers_present}/{tunnel.carriers_expected}
            </Row>
            <Row label="Protocol" testId="inspector-tunnel-protocol">
              {tunnel.protocol}
            </Row>
            <Row label="Service port" testId="inspector-tunnel-port">
              {tunnel.service_port}
            </Row>
            {tunnel.age_seconds != null && (
              <Row label="Age" testId="inspector-tunnel-age">
                {formatOutage(tunnel.age_seconds * 1000)}
              </Row>
            )}
            <Row label="Path" testId="inspector-tunnel-path">
              {tunnelPathText(tunnel.hops)}
            </Row>
          </div>
        )}
```

Imports: `tunnelPathText` from `./linkText`, `formatOutage` from
`../data/time` (verify the export name: `grep -n "formatOutage" web/src/data/time.ts`;
if it's not exported, export it — the e2e helper `_format_outage` mirrors it,
so it exists).

- [ ] **Step 4: Run checks, commit**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check src && cd ..
git add web/src/topo/
git commit -m "feat(web): link inspector tunnel block — status, carriers, port, age, path"
```

---

### Task 12: Dashboard e2e — fixtures, live SSE, node stability, all engines

**Files:**
- Create: `tests/e2e/monitor/dashboard/test_topology_tunnels.py`
- Modify: `tests/_fixtures/_fake_collector.py` (a tunnel-publish helper)

**Interfaces:**
- Consumes: fixture tunnels (Task 2), DOM contract (Tasks 10-11: `data-tunnel`, `data-tunnel-status`, `inspector-tunnel-*` testids), `MonitorSessionFragment.tunnels` wire shape (Task 1), FakeCollector/`DashboardHarness`/`live_stream_dash` (existing — mirror `test_live_shell.py`'s imports and `pytestmark`).
- Produces: the browser-lane certification of the feature.

- [ ] **Step 1: FakeCollector helper.** Read `tests/_fixtures/_fake_collector.py`; add a method following `push()`'s async style:

```python
    async def push_tunnels(self, tunnels: "list[dict[str, Any]]") -> None:
        """Replace the live tunnel set — mirrors the real _tunnel_pass publish.

        Takes wire dicts (TunnelRecord dumps) so specs read like the SSE
        payloads they assert on.
        """
        from otto.models.monitor import TunnelRecord

        self._tunnels = [TunnelRecord.model_validate(t) for t in tunnels]
        self._publish(
            {"format": 1, "session": self.session_id, "tunnels": tunnels}
        )
```

(Adapt to the class's actual attribute names — it subclasses or wraps
`MetricCollector`; if it wraps, delegate to the inner collector's fields.)

- [ ] **Step 2: Rebuild the dist, write the spec file**

```bash
make web
```

```python
# tests/e2e/monitor/dashboard/test_topology_tunnels.py
"""Tunnels in the topology view (spec 2026-07-16): riding geometry, bare
fallback, whole-tunnel selection, live churn without node movement."""

from datetime import datetime, timezone
from typing import Any

import pytest
from playwright.sync_api import Locator, Page

from tests._fixtures._fake_collector import FakeCollector
from tests.e2e.monitor.dashboard.conftest import DashboardHarness  # match test_live_shell.py's import

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _path_d(page: Page, edge_testid: str) -> str:
    """The BaseEdge path's `d` — the drawn geometry of one edge."""
    return page.eval_on_selector(
        f'[data-testid="{edge_testid}"] path.react-flow__edge-path',
        "el => el.getAttribute('d')",
    )


# ── Review mode (fixtures) ────────────────────────────────────────────────


def test_bare_tunnel_renders_as_a_dynamic_edge(page: Page, review_dash: Any) -> None:
    """kitchen-sink's tun-demo: no underlay -> one bare segment."""
    # Import kitchen-sink and open /topology, following test_review_shell.py's
    # existing import-and-navigate helper for this fixture.
    seg = page.locator('[data-tunnel="tun-00000000demo-15001"]')
    assert seg.count() == 1
    assert seg.first.get_attribute("data-tunnel-status") == "ok"


def test_riding_segments_reproduce_their_underlay_geometry(page: Page, review_dash: Any) -> None:
    """isp-core's tun-...a9db01: both segments ride declared links — the drawn
    path `d` of each segment equals its underlay's exactly."""
    # (import isp-core, open /topology)
    seg0 = _path_d(page, "topo-link-tun-000000a9db01-15002:0")
    seg1 = _path_d(page, "topo-link-tun-000000a9db01-15002:1")
    # Underlay edge ids come from the fixture's declared links between the
    # same element pairs (app01-tor, app01-db).
    assert seg0 == _path_d(page, "topo-link-app01-tor") or seg0 == _path_d(
        page, "topo-link-app01-db"
    )
    assert seg1 in (_path_d(page, "topo-link-app01-tor"), _path_d(page, "topo-link-app01-db"))
    assert seg0 != seg1


def test_clicking_any_segment_selects_the_whole_tunnel(page: Page, review_dash: Any) -> None:
    # (isp-core, /topology)
    page.locator('[data-tunnel="tun-000000a9db01-15002"]').first.click(force=True)
    assert _tid(page, "inspector-tunnel-status").inner_text().endswith("degraded")
    assert "4/6" in _tid(page, "inspector-tunnel-carriers").inner_text()
    assert "tor-sw-a → app-01 → db-01" in _tid(page, "inspector-tunnel-path").inner_text()
    # Every segment of the tunnel is emphasized, not just the clicked one:
    for i in range(2):
        seg = page.locator(f'[data-testid="topo-link-tun-000000a9db01-15002:{i}"]')
        width = seg.locator("path.react-flow__edge-path").evaluate(
            "el => parseFloat(getComputedStyle(el).strokeWidth)"
        )
        assert width > 2  # base tunnel width 2 + EMPHASIS_WIDTH


def test_uncertain_tunnel_ghosts(page: Page, review_dash: Any) -> None:
    # (sprawl, /topology)
    seg = page.locator('[data-tunnel="tun-0000jumpacc7-15003"]').first
    assert seg.get_attribute("data-tunnel-status") == "uncertain"
    opacity = seg.locator("path.react-flow__edge-path").evaluate(
        "el => parseFloat(getComputedStyle(el).opacity)"
    )
    assert opacity < 1


# ── Live mode (SSE) ───────────────────────────────────────────────────────

REC = {
    "id": "tun-live0000001-15009",
    "protocol": "udp",
    "service_port": 15009,
    "hops": ["h1", "h2"],  # use two host ids that exist in live_stream_dash's lab
    "status": "ok",
    "carriers_present": 4,
    "carriers_expected": 4,
    "age_seconds": 5.0,
}


def _node_positions(page: Page) -> dict[str, str]:
    return page.eval_on_selector_all(
        ".react-flow__node",
        "els => Object.fromEntries(els.map(e => [e.dataset.id, e.style.transform]))",
    )


def test_tunnel_churn_never_moves_nodes(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    # open /topology in live mode
    before = _node_positions(page)
    live_stream_dash.run(live_stream_dash.collector.push_tunnels([REC]))
    seg = page.locator(f'[data-tunnel="{REC["id"]}"]')
    seg.first.wait_for(state="visible")
    assert _node_positions(page) == before
    live_stream_dash.run(live_stream_dash.collector.push_tunnels([]))
    page.wait_for_function(
        f'() => document.querySelectorAll(\'[data-tunnel="{REC["id"]}"]\').length === 0'
    )
    assert _node_positions(page) == before
```

**Adapt the scaffolding, keep the assertions:** the exact fixture-import
helper (`review_dash` vs an import step), the `DashboardHarness` import path,
and live host ids (`h1`/`h2`) must be copied from `test_review_shell.py` /
`test_live_shell.py` — those are harness idioms, not design decisions. The
assertions (path-`d` equality, whole-tunnel emphasis, node-position equality,
inspector text) are the contract; do not weaken them.

- [ ] **Step 3: Run chromium first for iteration, then the REAL gate**

```bash
uv run pytest tests/e2e/monitor/dashboard/test_topology_tunnels.py -v   # chromium-only smoke
uv run nox -s dashboard                                                  # THE gate: all 3 engines
```

WebKit is where edge clicking historically breaks (unpainted-stroke
hit-testing). If the segment click fails on webkit only: click the CASING
path (it has painted stroke) via its bounding box midpoint instead of
`Locator.click` on the group, and note the workaround in the test docstring.

- [ ] **Step 4: Prove one guard.** Temporarily make `applyFragment` ignore `frag.tunnels` → `test_tunnel_churn_never_moves_nodes` must fail on the visibility wait. Revert.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/monitor/dashboard/test_topology_tunnels.py tests/_fixtures/_fake_collector.py
git commit -m "test(monitor): tunnel overlay e2e — geometry, selection, live churn, all engines"
```

---

### Task 13: Docs (after the toctree restructure merges)

**Precondition:** `docs/guide/network/tunnel.md` exists on the branch's base
(the restructure merged). If not, STOP this task and flag it — do not write
to the old paths.

**Files:**
- Modify: `docs/guide/monitor.md` (Web dashboard section; the monitor-free note near :389)
- Modify: `docs/guide/network/tunnel.md` (:274), `docs/guide/network/link.md` (:476)
- Modify: `docs/architecture/subsystems/network.md` (Tunnels + where-the-code-lives)

**Rules:** behavior in the guide, mechanism in architecture, cross-link
don't restate; no roadmap/phasing language; `make docs` must pass (nitpicky
Sphinx + Playwright captures — kitchen-sink's tunnel is bare, so existing
screenshots regenerate unchanged).

- [ ] **Step 1: guide/monitor.md.** Add a `### Topology view` subsection under `## Web dashboard` (sibling of Fleet grid), covering: the map's element/host levels, the legend's static/tunnel/reports-for classes, and tunnels — live overlay riding the links a tunnel's path traverses, dashed-with-casing, status styling (ok / degraded warning accent / uncertain ghost), click-for-inspector (carriers n/m, service port, age, hop path). State the cadence fact: tunnel discovery runs on the collection interval across the whole lab, regardless of which hosts are monitored. REWRITE the monitor-free note (:389) to its true converse, e.g.:

```markdown
> `otto tunnel` needs no monitor to function — `otto tunnel list` is the
> CLI's own live view. When `otto monitor` IS running, the collector also
> scans the whole lab for tunnels on each collection interval and streams
> them into the topology view as overlays; see the Topology view above.
```

- [ ] **Step 2: guide/network/tunnel.md.** Replace the "(tunnels do not appear in topology or edge views)" clause (:274) with a cross-link: "tunnels appear live in the monitor's topology view, riding the links their path traverses — see the monitor guide's Topology view section."

- [ ] **Step 3: guide/network/link.md.** Flip ":476 a future monitor/GUI topology overlay" to present tense: "the single API the CLI, the monitor's topology overlay, and any direct importer all call".

- [ ] **Step 4: architecture/subsystems/network.md.** Extend `## Tunnels` with a "Tunnels in the monitor" paragraph: `TunnelRecord` (wire, `SessionRecord.tunnels`, replace-semantics fragments), the collector's `_tunnel_loop` fed by an injected `discover_tunnel_records` callable (monitor never imports otto.tunnel; the adapter lives tunnel-side), last-known-state-only persistence (`tunnels_json`, in-place v2 column), and the failure rule (a dead scan raises and the collector keeps the last known set). Update the "Discovery reconstructs from a single survivor" paragraph's "same shape the monitor's parser contract expects" to note the seam is now WIRED, not just shaped. Add `otto/tunnel/records.py` to "Where the code lives".

- [ ] **Step 5: Build + commit**

```bash
make docs
git add docs/guide/ docs/architecture/
git commit -m "docs: tunnels in the monitor topology — guide + architecture"
```

---

### Task 14: Branch-wide verification + follow-up bookkeeping

- [ ] **Step 1: The full gates, in order**

```bash
uv run nox -s typecheck lint
make web
uv run pytest tests/unit -x -q
make coverage
uv run nox -s dashboard
```

All green, no skips-on-host-down (fail loud is the policy). `make coverage | tail` eats make's exit code — check failures via `uv run python scripts/junit_failures.py reports/junit/*/*.xml` if anything looks off.

- [ ] **Step 2: Close the loop in the repo's own trackers.** `todo/monitor-topology-followups.md` item 7 (tunnels as overlays) is now SHIPPED — mark it with a pointer to the spec, following the file's strike-through convention for items 5/6. Check `todo/TODO.md` for a dynamic-tunnels line and mark it too.

- [ ] **Step 3: Commit, then run the finishing skill**

```bash
git add todo/
git commit -m "chore: mark topology follow-up 7 shipped (tunnels as overlays)"
```

Invoke superpowers:finishing-a-development-branch for the merge decision.

---

## Self-review notes (kept for the executor)

- Task ordering is load-bearing: 1→2 regenerate the wire (web red until 8); 3→7 are pure-Python and gate-clean throughout; 8 restores `make web`; 9→11 are vitest-verified; 12 is the only browser task; 13 depends on an EXTERNAL merge (restructure) — it may be reordered last without harm, and must be skipped loudly if the restructure hasn't landed.
- `LinkSnapshot` narrowing (Task 1) is what breaks fixture generation until Task 2 — run `pytest tests/unit/scripts` only after both.
- Anywhere a test constructs `SessionRow`/`NormalizedSession` literals, the new field must be added — Tasks 5 and 8 call this out; expect a few more in `make coverage` and fix mechanically.
