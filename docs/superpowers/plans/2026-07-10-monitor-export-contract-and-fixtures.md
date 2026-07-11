# Monitor Export Contract + Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the versioned monitor historical-export format (`format: 1` — pydantic models → JSON schema → generated TS types) and the three committed dummy-data fixtures that the UI rebuild will run on.

**Architecture:** Additive pydantic boundary models in `src/otto/models/monitor.py` (sessions, per-session lab snapshot + presentation meta; `MetricRecord` gains `source`), wired into the existing `build_schemas()` → `otto schema export` → `gen_web_types.sh` pipeline. A deterministic, seeded generator script builds three fixture documents *through the models* and writes them to `web/fixtures/`; a drift-guard unit test keeps the committed fixtures byte-identical to regeneration.

**Tech Stack:** pydantic v2, json-schema-to-typescript (existing npx pipeline), stdlib-only generator (`random.Random`, `math`, `json`).

**Spec:** `docs/superpowers/specs/2026-07-10-monitor-export-format-and-dummy-data-phase-design.md` (data contract; §3 format, §5 fixtures). UX source of truth: `docs/superpowers/specs/2026-07-05-monitor-untitled-ui-redesign-design.md`.

**Plan series:** This is Plan 1. Plans 2+ (web scaffold + review chrome + Import; views; topology) are authored after this lands, against the real generated TS types.

## Global Constraints

- **NO `from __future__ import annotations`** — banned repo-wide (breaks the Sphinx nitpicky docs gate). Real 3.10+ annotations, module-top imports.
- **Lint is `ruff select=ALL`** minus a deny-list, plus `ruff format` (`make lint` runs both check and format-check). Run `uv run ruff format <files>` then `uv run ruff check <files>` before every commit — implementers habitually forget `format`.
- **`ty` runs only at `nox -s typecheck`**, not in `make coverage` — budget a typecheck round after src edits.
- **Fresh worktree setup:** `uv sync` (else phantom unresolved-import from ty) and `make web-install` (Task 3 needs npx). `uv sync` does not dirty `uv.lock`.
- **Worktree commit style:** self-commit allowed; use `-m` with a conventional prefix and embed the trailer `Assisted-by: Claude Fable 5` in the message (the prepare-commit-msg hook needs /dev/tty and silently defaults). Never `git add -u`; add files explicitly.
- **Air-gap:** no new runtime deps in this plan; fixtures live in `web/fixtures/` which is **outside** `src/` and therefore never packaged into the wheel (hatchling force-includes only `src/otto/monitor/static/dist/`).
- **Timestamps are `datetime` fields** emitted as ISO-8601 strings by `model_dump(mode="json")` (matches the reused v-record models). The spec's `jsonc` sketch shows illustrative epoch numbers; the schema generated from the models is normative.
- **Gate:** per-task scoped pytest; full `make coverage` + `nox -s lint typecheck` + `make web` in the final task. There is no `make test`.

---

### Task 1: Export-format pydantic models

**Files:**
- Modify: `src/otto/models/monitor.py` (append after `LogEventRecord`; extend `MetricRecord`)
- Modify: `src/otto/models/__init__.py` (exports — mirror how `MetricRecord`/`EventRecord` are exported today; check the file and add the eight new names to the same list/table)
- Test: `tests/unit/models/test_monitor.py` (append)

**Interfaces:**
- Consumes: existing `RowModel`, `OttoModel`, `ChartSpec`, `TabSpec`, `MetricRecord`, `EventRecord`, `LogEventRecord` in the same module.
- Produces (used by Tasks 2, 4): `MonitorExport(format: Literal[1], sessions: list[SessionRecord])`; `SessionRecord(id, label, note, start: datetime, end: datetime | None, lab: LabSnapshot, meta: SessionMeta, metrics, events, log_events, chart_map)`; `LabSnapshot(elements, hosts, links)`; `HostSnapshot(id, element, name, board, slot, hop, os_type, os_name, os_version, ip, interfaces: dict[str, str], labs, is_virtual)`; `LinkSnapshot(id, endpoints: list[LinkEndpointSnapshot], protocol, provenance, name, impair)`; `LinkEndpointSnapshot(host, interface, ip, port)`; `ElementRecord(id, type, description)`; `SessionMeta(interval, charts, tabs)`; `MetricRecord.source: str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_monitor.py`:

```python
class TestMetricRecordSource:
    def test_source_defaults_none_and_is_omitted(self):
        rec = MetricRecord(
            timestamp=datetime(2026, 7, 1, 8, tzinfo=_UTC), host="r1", label="CPU %", value=1.0
        )
        assert rec.source is None
        assert "source" not in rec.model_dump(mode="json", exclude_none=True)

    def test_source_round_trips(self):
        rec = MetricRecord.model_validate(
            {
                "timestamp": "2026-07-01T08:00:00+00:00",
                "host": "chassis-a_lc1",
                "label": "PSU Temp °C",
                "value": 41.5,
                "source": "mgmt-01",
            }
        )
        assert rec.source == "mgmt-01"
        assert rec.model_dump(mode="json", exclude_none=True)["source"] == "mgmt-01"


class TestExportDocument:
    def _doc(self) -> dict:
        return {
            "format": 1,
            "sessions": [
                {
                    "id": "s1",
                    "start": "2026-07-01T08:00:00+00:00",
                    "end": "2026-07-01T10:00:00+00:00",
                    "lab": {
                        "elements": [{"id": "spare-chassis", "type": "physical"}],
                        "hosts": [
                            {
                                "id": "chassis-a_lc1",
                                "element": "chassis-a",
                                "name": "chassis-a lc1",
                                "board": "lc1",
                                "slot": 1,
                                "hop": "edge-gw",
                                "os_type": "unix",
                                "os_name": "Linux",
                                "ip": "10.20.1.11",
                                "interfaces": {"eth0": "10.20.1.11"},
                                "labs": ["fixture"],
                                "is_virtual": True,
                            }
                        ],
                        "links": [
                            {
                                "id": "chassis-a_lc1--edge-gw",
                                "endpoints": [
                                    {"host": "edge-gw", "ip": "10.20.1.1"},
                                    {"host": "chassis-a_lc1", "interface": "eth0"},
                                ],
                                "protocol": "tcp",
                                "provenance": "implicit",
                            }
                        ],
                    },
                    "meta": {
                        "interval": 15.0,
                        "charts": [
                            {
                                "label": "CPU %",
                                "y_title": "CPU %",
                                "unit": "%",
                                "command": "fixture:cpu",
                                "chart": "cpu",
                                "interval": 15.0,
                            }
                        ],
                        "tabs": [
                            {"id": "overview", "label": "Overview", "metrics": ["CPU %"]}
                        ],
                    },
                    "metrics": [
                        {
                            "timestamp": "2026-07-01T08:00:00+00:00",
                            "host": "chassis-a_lc1",
                            "label": "CPU %",
                            "value": 33.3,
                        }
                    ],
                    "events": [],
                    "log_events": [],
                    "chart_map": {"CPU %": "CPU %"},
                }
            ],
        }

    def test_round_trip(self):
        doc = MonitorExport.model_validate(self._doc())
        assert doc.format == 1
        s = doc.sessions[0]
        assert s.lab.hosts[0].slot == 1
        assert s.lab.links[0].provenance == "implicit"
        assert s.meta.charts[0].chart == "cpu"
        dumped = doc.model_dump(mode="json", exclude_none=True)
        assert MonitorExport.model_validate(dumped) == doc

    def test_format_field_is_required(self):
        # A legacy (unversioned) document must fail loud, not default to format 1.
        with pytest.raises(ValidationError):
            MonitorExport.model_validate({"sessions": []})

    def test_unknown_format_rejected(self):
        with pytest.raises(ValidationError):
            MonitorExport.model_validate({"format": 2, "sessions": []})

    def test_read_back_is_lenient(self):
        # Forward compat: an unknown key from a newer otto is ignored, not rejected.
        raw = self._doc()
        raw["sessions"][0]["lab"]["hosts"][0]["future_field"] = "x"
        doc = MonitorExport.model_validate(raw)
        assert doc.sessions[0].lab.hosts[0].id == "chassis-a_lc1"

    def test_read_back_is_lenient_in_nested_meta(self):
        # The presentation-meta specs must be lenient too: chart definitions
        # drift over months exactly like lab configs (spec §2). Guards against
        # nesting the strict live-meta ChartSpec/TabSpec by accident.
        raw = self._doc()
        raw["sessions"][0]["meta"]["charts"][0]["future_style"] = "x"
        raw["sessions"][0]["meta"]["tabs"][0]["future_layout"] = "y"
        doc = MonitorExport.model_validate(raw)
        assert doc.sessions[0].meta.charts[0].chart == "cpu"
        assert doc.sessions[0].meta.tabs[0].id == "overview"

    def test_link_provenance_validated(self):
        raw = self._doc()
        raw["sessions"][0]["lab"]["links"][0]["provenance"] = "tunnel"
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)

    def test_link_needs_exactly_two_endpoints(self):
        raw = self._doc()
        raw["sessions"][0]["lab"]["links"][0]["endpoints"].append({"host": "x"})
        with pytest.raises(ValidationError):
            MonitorExport.model_validate(raw)

    def test_open_session_and_optional_fields_omitted(self):
        raw = self._doc()
        del raw["sessions"][0]["end"]
        doc = MonitorExport.model_validate(raw)
        assert doc.sessions[0].end is None
        dumped = doc.model_dump(mode="json", exclude_none=True)
        assert "end" not in dumped["sessions"][0]
        assert "label" not in dumped["sessions"][0]
```

Also extend the imports at the top of the test file:

```python
from otto.models import EventRecord, MetricPoint, MetricRecord, MonitorExport
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/models/test_monitor.py -v -k "Source or ExportDocument"`
Expected: FAIL — `ImportError: cannot import name 'MonitorExport'`

- [ ] **Step 3: Implement the models**

In `src/otto/models/monitor.py`, add `source` to `MetricRecord` (after `meta`):

```python
    source: str | None = None
    """Host id of the *reporting* host when this series came from an external
    management host (spec 2026-07-10 §3.1); ``None``/absent = self-reported.
    Rides only in JSON for now — the SQLite ``metrics`` table gains its column
    with the backend catch-up (spec §7)."""
```

Append after `LogEventRecord` (extend the `typing` import with `Literal` if not present — it is already imported):

```python
class ElementRecord(RowModel):
    """One optional ``lab.elements`` entry in the export snapshot.

    ``id`` is the element name — the same string member hosts carry in
    :attr:`HostSnapshot.element`. Elements *not* listed are derived from hosts
    (any member with a ``slot`` → physical presentation; a single member →
    singleton behavior). An explicit entry with zero member hosts renders as an
    empty element (e.g. an unpopulated chassis). ``singleton`` is always
    derived from membership count, never stored (spec 2026-07-10 §2).
    """

    id: str
    type: Literal["physical", "logical"] = "logical"
    description: str | None = None


class HostSnapshot(RowModel):
    """The view-relevant subset of a host's config, frozen into a session.

    Deliberately **never** credentials (spec 2026-07-10 §3.1). ``interfaces``
    is flattened to ``netdev -> ip`` (the frontend needs no more). Lenient
    read-back like every export row (:class:`RowModel`).
    """

    id: str
    element: str
    name: str | None = None
    board: str | None = None
    slot: int | None = None
    hop: str | None = None
    os_type: str = "unix"
    os_name: str | None = None
    os_version: str | None = None
    ip: str = ""
    interfaces: dict[str, str] = Field(default_factory=dict)
    labs: list[str] = Field(default_factory=list)
    is_virtual: bool = False


class LinkEndpointSnapshot(RowModel):
    """One end of a snapshotted link (mirrors ``otto.link.model.LinkEndpoint``)."""

    host: str
    interface: str | None = None
    ip: str = ""
    port: int | None = None


class LinkSnapshot(RowModel):
    """One static link frozen into a session's lab snapshot.

    Mirrors the runtime ``otto.link.model.Link``. Real exporters write only
    ``implicit`` + ``declared`` provenances — the snapshot is a static-config
    document and dynamic tunnels are runtime state (spec 2026-07-10 §2); the
    ``dynamic`` value stays for parity with the runtime enum (and the live
    topology view). ``impair`` is the *declared* in-path middlebox host id —
    static config, unlike applied netem parameters.
    """

    id: str
    endpoints: list[LinkEndpointSnapshot] = Field(min_length=2, max_length=2)
    protocol: str = "tcp"
    provenance: Literal["implicit", "declared", "dynamic"] = "declared"
    name: str | None = None
    impair: str | None = None


class LabSnapshot(RowModel):
    """A session's lab config as it was at run time (spec 2026-07-10 §3)."""

    elements: list[ElementRecord] = Field(default_factory=list)
    hosts: list[HostSnapshot] = Field(default_factory=list)
    links: list[LinkSnapshot] = Field(default_factory=list)


class ChartSpecRecord(ChartSpec):
    """Lenient read-back variant of :class:`ChartSpec` for export documents.

    Same fields; ``extra="ignore"`` so an older otto can read exports written
    by a newer one whose chart specs carry new fields (the :class:`RowModel`
    boundary philosophy). :class:`ChartSpec` itself stays ``extra="forbid"``
    as the otto-built live ``/api/meta`` contract.
    """

    model_config = ConfigDict(extra="ignore")


class TabSpecRecord(TabSpec):
    """Lenient read-back variant of :class:`TabSpec` (see :class:`ChartSpecRecord`)."""

    model_config = ConfigDict(extra="ignore")


class SessionMeta(RowModel):
    """Presentation meta frozen at run time: chart/tab specs + intervals.

    Client-side Import has no parser catalog to rebuild specs from, derived
    health needs per-series cadences, and chart definitions drift over months
    exactly like lab configs (spec 2026-07-10 §2, §4) — hence the lenient
    ``*Record`` spec variants, not the strict live-meta classes.
    """

    interval: float | None = None
    charts: list[ChartSpecRecord] = Field(default_factory=list)
    tabs: list[TabSpecRecord] = Field(default_factory=list)


class SessionRecord(RowModel):
    """One self-contained monitoring session: config snapshot + data.

    ``end=None`` means a still-open session. ``chart_map`` maps bare series
    labels to chart keys (:attr:`ChartSpec.label`), as ``/api/data`` does today.
    """

    id: str
    label: str | None = None
    note: str | None = None
    start: datetime
    end: datetime | None = None
    lab: LabSnapshot = Field(default_factory=LabSnapshot)
    meta: SessionMeta = Field(default_factory=SessionMeta)
    metrics: list[MetricRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    log_events: list[LogEventRecord] = Field(default_factory=list)
    chart_map: dict[str, str] = Field(default_factory=dict)


class MonitorExport(RowModel):
    """The versioned historical-export document (spec 2026-07-10 §3).

    ``format`` is **required with no default**: a legacy unversioned document
    (the field's absence is its marker) must fail loud here, never validate as
    an empty modern one. ``Literal[1]`` rejects future formats loud too.
    """

    format: Literal[1]
    sessions: list[SessionRecord]
```

Then export the ten new names from `src/otto/models/__init__.py`. Replace the existing `from .monitor import (...)` block with:

```python
from .monitor import (
    ChartSpec,
    ChartSpecRecord,
    ElementRecord,
    EventRecord,
    HostSnapshot,
    LabSnapshot,
    LinkEndpointSnapshot,
    LinkSnapshot,
    LogEventRecord,
    MetricPoint,
    MetricRecord,
    MonitorExport,
    MonitorMeta,
    SessionMeta,
    SessionRecord,
    TabSpec,
    TabSpecRecord,
)
```

and insert into `__all__` (it is sorted alphabetically — keep it that way): `"ChartSpecRecord"` after `"ChartSpec"`; `"ElementRecord"` after `"DockerSettingsSpec"`; `"HostSnapshot"` before `"HostSpec"`; `"LabSnapshot"`, `"LinkEndpointSnapshot"`, `"LinkSnapshot"` between `"HostSpec"` and `"LogEventRecord"`; `"MonitorExport"` before `"MonitorMeta"`; `"SessionMeta"`, `"SessionRecord"` before `"SettingsModel"`; `"TabSpecRecord"` after `"TabSpec"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/models/test_monitor.py -v`
Expected: ALL PASS (new and pre-existing).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/otto/models/monitor.py src/otto/models/__init__.py tests/unit/models/test_monitor.py
uv run ruff check src/otto/models/monitor.py src/otto/models/__init__.py tests/unit/models/test_monitor.py
git add src/otto/models/monitor.py src/otto/models/__init__.py tests/unit/models/test_monitor.py
git commit -m "feat(monitor): versioned export-format models (format: 1)

Sessions with per-session lab snapshot (hosts/links/optional elements) and
frozen presentation meta; MetricRecord gains optional 'source' (mgmt-host
provenance). Spec: docs/superpowers/specs/2026-07-10-monitor-export-format-
and-dummy-data-phase-design.md §3.

Assisted-by: Claude Fable 5"
```

---

### Task 2: JSON schema emission

**Files:**
- Modify: `src/otto/models/jsonschema.py`
- Test: `tests/unit/models/test_jsonschema.py`

**Interfaces:**
- Consumes: `MonitorExport` (Task 1).
- Produces: `build_schemas()` gains a `"monitor-export"` stem → `otto schema export` / `make schema` writes `schemas/monitor-export.schema.json` (consumed by Task 3).

- [ ] **Step 1: Write the failing test**

In `tests/unit/models/test_jsonschema.py`, find the existing stems assertion (the list containing `"monitor-meta"`, around line 19) and add `"monitor-export"` to the expected stems. Then append:

```python
def test_monitor_export_schema_shape():
    docs = build_schemas(builtins_only=True)
    doc = docs["monitor-export"]
    assert doc["title"] == "Monitor historical export document"
    assert set(doc["required"]) == {"format", "sessions"}
    assert doc["properties"]["format"]["const"] == 1
```

(Match the existing file's import of `build_schemas` — it is already imported for the other tests.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_jsonschema.py -v`
Expected: FAIL — `KeyError: 'monitor-export'` (and the stems assertion).

- [ ] **Step 3: Implement**

In `src/otto/models/jsonschema.py`:
- extend the import: `from .monitor import MonitorExport, MonitorMeta`
- in `build_schemas()`, directly after the `docs["monitor-meta"] = ...` block, add:

```python
    docs["monitor-export"] = _decorate(
        MonitorExport.model_json_schema(),
        "monitor-export",
        "Monitor historical export document",
    )
```

- update the module docstring's "Emitted documents" list with one line:

```text
- ``monitor-export`` — the versioned historical export document
  (:class:`~otto.models.monitor.MonitorExport`, ``format: 1``); not
  user-edited, it feeds the web dashboard's generated TS types like
  ``monitor-meta``.
```

- [ ] **Step 4: Run tests, then the real exporter**

Run: `uv run pytest tests/unit/models/test_jsonschema.py -v`
Expected: PASS.

Run: `make schema && python -c "import json; d=json.load(open('schemas/monitor-export.schema.json')); print(d['title'])"`
Expected: `Monitor historical export document` (file appears; `schemas/` is git-ignored).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format src/otto/models/jsonschema.py tests/unit/models/test_jsonschema.py
uv run ruff check src/otto/models/jsonschema.py tests/unit/models/test_jsonschema.py
git add src/otto/models/jsonschema.py tests/unit/models/test_jsonschema.py
git commit -m "feat(schema): emit monitor-export.schema.json from MonitorExport

Assisted-by: Claude Fable 5"
```

---

### Task 3: Generated TS types for the export document

**Files:**
- Modify: `scripts/gen_web_types.sh`
- Modify: `Makefile` (the `web` target's drift diff)
- Create (generated, committed): `web/src/api/export.gen.ts`

**Interfaces:**
- Consumes: `schemas/monitor-export.schema.json` (Task 2).
- Produces: `web/src/api/export.gen.ts` — the TS contract Plans 2+ import (`MonitorHistoricalExportDocument` et al., names as json-schema-to-typescript derives them from the schema title).

- [ ] **Step 1: Extend the generation script**

In `scripts/gen_web_types.sh`, inside the existing `( cd web ... )` subshell, add a second conversion after the first:

```bash
    npx json-schema-to-typescript \
        ../schemas/monitor-export.schema.json \
        -o src/api/export.gen.ts \
        --bannerComment "/* AUTO-GENERATED from monitor-export.schema.json — run scripts/gen_web_types.sh; do not edit. */"
```

Also update the script's header comment: it currently narrates only `monitor-meta.schema.json` → mention both generated files.

- [ ] **Step 2: Extend the Makefile drift gate**

In the `web` target, change the diff line to cover both generated files:

```make
	git diff --exit-code web/src/api/types.gen.ts web/src/api/export.gen.ts
```

- [ ] **Step 3: Generate and inspect**

Run: `make web-install` (once per fresh worktree) then `scripts/gen_web_types.sh`
Expected: `web/src/api/export.gen.ts` created; open it and confirm it declares interfaces for the export document (session, lab snapshot, host/link snapshot shapes) with no `any`-typed placeholder for known fields.

Run: `scripts/gen_web_types.sh && git diff --stat web/src/api/`
Expected: second run is byte-identical (only the first run shows the new file).

- [ ] **Step 4: Verify the drift gate catches staleness**

Run: `make web`
Expected: passes (regenerates identically, vite builds, air-gap checks pass — no runtime asset changed in this plan).

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_web_types.sh Makefile web/src/api/export.gen.ts
git commit -m "build(web): generate export.gen.ts from monitor-export schema

make web now drift-checks both generated TS contract files.

Assisted-by: Claude Fable 5"
```

---

### Task 4: Fixture generator

**Files:**
- Create: `scripts/gen_monitor_fixtures.py`
- Test: `tests/unit/scripts/test_gen_monitor_fixtures.py`

**Interfaces:**
- Consumes: the Task 1 models; `otto.link.model.LinkEndpoint` / `make_static_link_id` (real id derivation — and deliberately **nothing from `otto.configmodule`**, which the extraction branch renames; spec §5/§10).
- Produces: `build_all() -> dict[str, MonitorExport]` with stems `kitchen-sink`, `minimal`, `drift`; `main(out_dir: str) -> None` writing `<stem>.json` minified + trailing newline (consumed by Task 5's make target and drift guard).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/scripts/test_gen_monitor_fixtures.py`:

```python
"""Invariants of the committed monitor fixtures (spec 2026-07-10 §5).

The generator builds documents THROUGH the export models, so schema
conformance is by construction; these tests pin the *content* contracts the
UI phase relies on: determinism, subject resolvability, the outage window,
drift across sessions, and the fixture-only status of the dynamic link.
"""

import json

from otto.models import MonitorExport

from scripts.gen_monitor_fixtures import OUTAGE_S, build_all, dumps


def _subjects(doc):
    hosts = {h.id for s in doc.sessions for h in s.lab.hosts}
    elements = {e.id for s in doc.sessions for e in s.lab.elements}
    elements |= {h.element for s in doc.sessions for h in s.lab.hosts}
    return hosts, elements

def test_deterministic():
    a = {k: dumps(v) for k, v in build_all().items()}
    b = {k: dumps(v) for k, v in build_all().items()}
    assert a == b

def test_documents_round_trip_and_stems():
    docs = build_all()
    assert set(docs) == {"kitchen-sink", "minimal", "drift"}
    for doc in docs.values():
        assert MonitorExport.model_validate(json.loads(dumps(doc))) is not None

def test_every_metric_subject_resolves():
    for doc in build_all().values():
        hosts, elements = _subjects(doc)
        for s in doc.sessions:
            for m in s.metrics:
                assert m.host in hosts or m.host in elements, m.host
                if m.source is not None:
                    assert m.source in hosts, m.source

def test_kitchen_sink_shapes():
    doc = build_all()["kitchen-sink"]
    (s,) = doc.sessions
    # the empty chassis is explicit; the populated explicit element merges
    assert {e.id for e in s.lab.elements} == {"spare-chassis", "workers"}
    assert all(h.element != "spare-chassis" for h in s.lab.hosts)
    # exactly one dynamic link, fixture-only (spec §2)
    assert sum(1 for ln in s.lab.links if ln.provenance == "dynamic") == 1
    assert any(ln.impair for ln in s.lab.links)
    assert any(ln.provenance == "implicit" for ln in s.lab.links)
    # metadata holes for edge-case rendering
    assert any(h.hop is None for h in s.lab.hosts)
    assert any(h.slot is None and h.board is not None for h in s.lab.hosts)
    assert any(h.os_version is None for h in s.lab.hosts)
    # element-targeted + mgmt-sourced series exist
    assert any(m.host == "chassis-a" for m in s.metrics)
    assert any(m.source == "mgmt-01" for m in s.metrics)
    assert s.events and s.log_events and s.meta.charts and s.chart_map

def test_kitchen_sink_outage_window():
    doc = build_all()["kitchen-sink"]
    (s,) = doc.sessions
    lo = s.start.timestamp() + OUTAGE_S[0]
    hi = s.start.timestamp() + OUTAGE_S[1]
    down = [m for m in s.metrics if m.host == "workers_w2"]
    assert down, "outage host must still have samples outside the gap"
    assert not [m for m in down if lo <= m.timestamp.timestamp() < hi]

def test_drift_sessions_evolve():
    doc = build_all()["drift"]
    assert len(doc.sessions) == 3
    labs = [s.lab.model_dump(mode="json") for s in doc.sessions]
    assert labs[0] != labs[1] != labs[2]
    s1, s2, s3 = doc.sessions
    assert len(s2.lab.hosts) > len(s1.lab.hosts)  # host added
    ids2 = {h.id for h in s2.lab.hosts}
    ids3 = {h.id for h in s3.lab.hosts}
    assert "workers_w2" in ids2  # host removed
    assert "workers_w2" not in ids3
    assert "edge-gw" not in ids2  # gateway added
    assert "edge-gw" in ids3
    slot = {
        s: next(h.slot for h in sess.lab.hosts if h.board == "lc2")
        for s, sess in (("s2", s2), ("s3", s3))
    }
    assert slot["s2"] != slot["s3"]  # board slot moved
    assert any(ln.impair for ln in s3.lab.links)  # impairment added
    assert not any(ln.impair for ln in s2.lab.links)

def test_no_dynamic_links_outside_kitchen_sink():
    docs = build_all()
    for stem in ("minimal", "drift"):
        for s in docs[stem].sessions:
            assert not [ln for ln in s.lab.links if ln.provenance == "dynamic"]

def test_no_credentials_anywhere():
    for doc in build_all().values():
        text = dumps(doc)
        for needle in ("password", "creds", "login"):
            assert needle not in text, needle

def test_size_caps():
    for stem, doc in build_all().items():
        assert len(dumps(doc)) < 3_500_000, stem
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/scripts/test_gen_monitor_fixtures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.gen_monitor_fixtures'`

- [ ] **Step 3: Write the generator**

Create `scripts/gen_monitor_fixtures.py`:

```python
"""Generate the committed monitor dummy-data fixtures (spec 2026-07-10 §5).

Builds the three review-mode fixture documents THROUGH the pydantic export
models — conformance-checked by construction — and writes them minified to
``web/fixtures/``. Deterministic: fixed base timestamps + seeded
``random.Random``, so regeneration is byte-identical (drift-guarded by
``tests/unit/scripts/test_monitor_fixture_files.py``). Regenerate via
``make monitor-fixtures`` whenever the schema or the scenarios change.

Import discipline (spec §5): only :mod:`otto.models` and the leaf
:mod:`otto.link.model` — deliberately nothing from ``otto.configmodule``,
which the library-extraction branch renames.

Series shapes are realistic on purpose (diurnal CPU, sawtooth memory leak,
a spike under an event span): chart UX cannot be judged against noise.
"""

import json
import math
import random
import sys
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto.link.model import LinkEndpoint, make_static_link_id
from otto.models import (
    ChartSpecRecord,
    ElementRecord,
    EventRecord,
    HostSnapshot,
    LabSnapshot,
    LinkEndpointSnapshot,
    LinkSnapshot,
    LogEventRecord,
    MetricRecord,
    MonitorExport,
    SessionMeta,
    SessionRecord,
    TabSpecRecord,
)

BASE = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
"""Fixed session-start epoch — never wall clock (determinism)."""

OUTAGE_S = (3600.0, 4800.0)
"""workers_w2 goes silent in this window (seconds from session start)."""

_DURATION_S = 7200.0  # kitchen-sink session length: 2 h
_CADENCE_S = 15.0  # base cadence; spec §12 allows trimming from the 5 s sketch


# --- lab building blocks -----------------------------------------------------


def _host(
    host_id: str,
    element: str,
    ip: str,
    *,
    board: str | None = None,
    slot: int | None = None,
    hop: str | None = None,
    os_version: str | None = "24.04",
    interfaces: dict[str, str] | None = None,
) -> HostSnapshot:
    """One fixture host with the common defaults filled in."""
    return HostSnapshot(
        id=host_id,
        element=element,
        name=host_id.replace("_", " "),
        board=board,
        slot=slot,
        hop=hop,
        os_type="unix",
        os_name="Linux",
        os_version=os_version,
        ip=ip,
        interfaces=interfaces or {"eth0": ip},
        labs=["fixture"],
        is_virtual=True,
    )


def _link(
    a: tuple[str, str | None, str],
    b: tuple[str, str | None, str],
    *,
    protocol: str = "tcp",
    provenance: str = "declared",
    name: str | None = None,
    impair: str | None = None,
    ports: tuple[int | None, int | None] = (None, None),
) -> LinkSnapshot:
    """One fixture link; the id comes from the real static-id derivation."""
    ea = LinkEndpoint(host=a[0], interface=a[1], ip=a[2])
    eb = LinkEndpoint(host=b[0], interface=b[1], ip=b[2])
    return LinkSnapshot(
        id=make_static_link_id(ea, eb, name),
        endpoints=[
            LinkEndpointSnapshot(host=a[0], interface=a[1], ip=a[2], port=ports[0]),
            LinkEndpointSnapshot(host=b[0], interface=b[1], ip=b[2], port=ports[1]),
        ],
        protocol=protocol,
        provenance=provenance,  # type: ignore[arg-type]  # Literal narrowed by callers
        name=name,
        impair=impair,
    )


def _implicit_links(hosts: Iterable[HostSnapshot]) -> list[LinkSnapshot]:
    """Hop-chain edges: one implicit link per host with a ``hop`` set."""
    by_id = {h.id: h for h in hosts}
    return [
        _link((h.hop, None, by_id[h.hop].ip), (h.id, None, h.ip), provenance="implicit")
        for h in by_id.values()
        if h.hop is not None and h.hop in by_id
    ]


# --- series synthesis ---------------------------------------------------------


def _series(
    rng: random.Random,
    host: str,
    label: str,
    fn: Callable[[float], float],
    *,
    start: datetime,
    duration_s: float,
    cadence_s: float,
    source: str | None = None,
    gaps: tuple[tuple[float, float], ...] = (),
) -> list[MetricRecord]:
    """Sample ``fn(t_seconds)`` on a fixed cadence, skipping outage gaps."""
    del rng  # cadence jitter deliberately omitted: keeps health derivation exact
    records = []
    for tick in range(int(duration_s // cadence_s) + 1):
        t = tick * cadence_s
        if any(lo <= t < hi for lo, hi in gaps):
            continue
        records.append(
            MetricRecord(
                timestamp=start + timedelta(seconds=t),
                host=host,
                label=label,
                value=round(min(max(fn(t), 0.0), 1e9), 3),
                source=source,
            )
        )
    return records


def _diurnal(rng: random.Random, *, base: float, amp: float, period_s: float = 5400.0
             ) -> Callable[[float], float]:
    """Slow sine + noise, clamped to [0, 100] — a plausible CPU% curve."""
    phase = rng.uniform(0.0, 2 * math.pi)
    return lambda t: min(
        100.0, base + amp * math.sin(2 * math.pi * t / period_s + phase) + rng.uniform(-amp / 8, amp / 8)
    )


def _sawtooth(*, lo: float, hi: float, period_s: float) -> Callable[[float], float]:
    """Linear climb + reset — the classic leak-then-restart memory shape."""
    return lambda t: lo + (hi - lo) * ((t % period_s) / period_s)


def _noisy(rng: random.Random, *, base: float, jitter: float) -> Callable[[float], float]:
    """Flat line with noise — network/disk background chatter."""
    return lambda t: base + rng.uniform(-jitter, jitter)


def _with_spike(fn: Callable[[float], float], *, center_s: float, width_s: float,
                height: float) -> Callable[[float], float]:
    """Overlay a gaussian bump (aligned with an event span in the fixture)."""
    return lambda t: fn(t) + height * math.exp(-((t - center_s) ** 2) / (2 * width_s**2))


# --- presentation meta ---------------------------------------------------------


def _chart(label: str, unit: str, chart: str, interval: float) -> ChartSpecRecord:
    """One fixture chart spec (``command`` is an honest fixture marker)."""
    return ChartSpecRecord(
        label=label, y_title=label, unit=unit, command=f"fixture:{chart}",
        chart=chart, interval=interval,
    )


_HOST_CHARTS = [
    ("CPU %", "%", "cpu", _CADENCE_S),
    ("Memory MB", "MB", "mem", _CADENCE_S),
    ("Net kB/s", "kB/s", "net", 30.0),
    ("Disk io/s", "io/s", "disk", 30.0),
]
_MGMT_CHARTS = [
    ("PSU Temp °C", "°C", "psu-temp", 60.0),
    ("Fan RPM", "rpm", "fan", 60.0),
    ("Ambient °C", "°C", "ambient", 60.0),
]


def _meta(charts: list[tuple[str, str, str, float]], *, tables: bool) -> SessionMeta:
    """Session presentation meta for the given chart set."""
    specs = [_chart(*c) for c in charts]
    tabs = [TabSpecRecord(id="overview", label="Overview", metrics=[c.label for c in specs])]
    if tables:
        tabs.append(
            TabSpecRecord(id="kernel", label="Kernel", metrics=[], kind="table",
                          columns=["level", "facility", "message"])
        )
    return SessionMeta(interval=_CADENCE_S, charts=specs, tabs=tabs)


def _chart_map(meta: SessionMeta) -> dict[str, str]:
    """Bare series label -> chart key; fixtures use label == chart label."""
    return {c.label: c.label for c in meta.charts}


# --- host data bundles ----------------------------------------------------------


def _host_metrics(
    rng: random.Random,
    host_ids: list[str],
    *,
    start: datetime,
    duration_s: float,
    gaps_for: dict[str, tuple[tuple[float, float], ...]] | None = None,
    cpu_extra: dict[str, Callable[[Callable[[float], float]], Callable[[float], float]]] | None = None,
) -> list[MetricRecord]:
    """The four standard per-host series for each host id."""
    gaps_for = gaps_for or {}
    cpu_extra = cpu_extra or {}
    out: list[MetricRecord] = []
    for hid in host_ids:
        gaps = gaps_for.get(hid, ())
        cpu = _diurnal(rng, base=35.0, amp=20.0)
        if hid in cpu_extra:
            cpu = cpu_extra[hid](cpu)
        args = {"start": start, "duration_s": duration_s, "gaps": gaps}
        out += _series(rng, hid, "CPU %", cpu, cadence_s=_CADENCE_S, **args)
        out += _series(
            rng, hid, "Memory MB",
            _sawtooth(lo=900.0, hi=3100.0, period_s=2700.0), cadence_s=_CADENCE_S, **args,
        )
        out += _series(rng, hid, "Net kB/s", _noisy(rng, base=420.0, jitter=180.0),
                       cadence_s=30.0, **args)
        out += _series(rng, hid, "Disk io/s", _noisy(rng, base=55.0, jitter=25.0),
                       cadence_s=30.0, **args)
    return out


# --- the three documents ---------------------------------------------------------


def kitchen_sink() -> MonitorExport:
    """Every UI feature in one lab (spec §5 table)."""
    rng = random.Random(20260710)
    hosts = [
        _host("edge-gw", "edge-gw", "10.20.0.1",
              interfaces={"eth0": "10.20.0.1", "eth1": "10.20.1.1"}),
        _host("chassis-a_lc1", "chassis-a", "10.20.1.11", board="lc1", slot=1, hop="edge-gw"),
        _host("chassis-a_lc2", "chassis-a", "10.20.1.12", board="lc2", slot=2, hop="edge-gw",
              os_version=None),                       # metadata hole: no os_version
        _host("chassis-a_sup", "chassis-a", "10.20.1.15", board="sup", slot=5, hop="edge-gw"),
        _host("workers_w1", "workers", "10.20.2.21", board="w1"),   # board, no slot
        _host("workers_w2", "workers", "10.20.2.22", board="w2"),
        _host("workers_w3", "workers", "10.20.2.23", board="w3"),
        _host("db-01", "db-01", "10.20.3.31"),        # singleton, no hop
        _host("mgmt-01", "mgmt-01", "10.20.4.41"),    # the external source
    ]
    elements = [
        ElementRecord(id="spare-chassis", type="physical",
                      description="Spare 8-slot chassis, unpopulated"),
        ElementRecord(id="workers", type="logical", description="Load-generator cluster"),
    ]
    links = [
        *_implicit_links(hosts),
        _link(("workers_w1", "eth0", "10.20.2.21"), ("db-01", "eth0", "10.20.3.31"),
              name="app-db"),
        _link(("workers_w3", "eth0", "10.20.2.23"), ("db-01", "eth0", "10.20.3.31"),
              protocol="udp", name="metrics-udp", impair="edge-gw"),
        # FIXTURE-ONLY (spec §2): real exporters never write dynamic links; this
        # one exists purely so the provenance styling can be seen and tested.
        _link(("edge-gw", None, "10.20.0.1"), ("db-01", None, "10.20.3.31"),
              provenance="dynamic", name="tun-demo", ports=(15001, 22)),
    ]
    spike = {"chassis-a_lc1": lambda cpu: _with_spike(cpu, center_s=5400.0,
                                                      width_s=180.0, height=45.0)}
    metrics = _host_metrics(
        rng, [h.id for h in hosts],
        start=BASE, duration_s=_DURATION_S,
        gaps_for={"workers_w2": (OUTAGE_S,)},
        cpu_extra=spike,
    )
    for board in ("chassis-a_lc1", "chassis-a_lc2", "chassis-a_sup"):
        metrics += _series(rng, board, "PSU Temp °C", _noisy(rng, base=41.0, jitter=2.5),
                           start=BASE, duration_s=_DURATION_S, cadence_s=60.0, source="mgmt-01")
        metrics += _series(rng, board, "Fan RPM", _noisy(rng, base=7200.0, jitter=400.0),
                           start=BASE, duration_s=_DURATION_S, cadence_s=60.0, source="mgmt-01")
    for element in ("chassis-a", "spare-chassis"):     # element-targeted series
        metrics += _series(rng, element, "Ambient °C", _noisy(rng, base=24.0, jitter=1.0),
                           start=BASE, duration_s=_DURATION_S, cadence_s=60.0, source="mgmt-01")
    events = [
        EventRecord(id=1, timestamp=BASE + timedelta(minutes=20), label="config reload",
                    source="manual", color="#7c5cff"),
        EventRecord(id=2, timestamp=BASE + timedelta(minutes=85),
                    end_timestamp=BASE + timedelta(minutes=95), label="stress run",
                    source="manual", color="#ff6b6b"),   # aligns with the CPU spike
        EventRecord(id=3, timestamp=BASE + timedelta(minutes=90),
                    end_timestamp=BASE + timedelta(minutes=100), label="log capture",
                    source="manual", color="#2f9e6e"),   # overlaps the stress span
        EventRecord(id=4, timestamp=BASE + timedelta(minutes=60), label="w2 lost",
                    source="watchdog", color="#e8a13c"),
    ]
    log_events = [
        LogEventRecord(
            timestamp=BASE + timedelta(minutes=3 * i + (0 if hid == "db-01" else 1)),
            host=hid, tab="kernel",
            fields={"level": rng.choice(["info", "warn", "err"]),
                    "facility": "kern",
                    "message": f"fixture kernel message {i} on {hid}"},
        )
        for hid in ("chassis-a_lc1", "db-01")
        for i in range(40)
    ]
    meta = _meta(_HOST_CHARTS + _MGMT_CHARTS, tables=True)
    session = SessionRecord(
        id="2026-07-01T08-00-00-kitchen-sink", label="kitchen sink",
        note="Synthetic fixture exercising every monitor UI feature.",
        start=BASE, end=BASE + timedelta(seconds=_DURATION_S),
        lab=LabSnapshot(elements=elements, hosts=hosts, links=links),
        meta=meta, metrics=metrics, events=events, log_events=log_events,
        chart_map=_chart_map(meta),
    )
    return MonitorExport(format=1, sessions=[session])


def minimal() -> MonitorExport:
    """The degenerate case: one singleton host, two series, nothing else."""
    rng = random.Random(11)
    host = _host("solo", "solo", "10.30.0.5")
    meta = _meta(_HOST_CHARTS[:2], tables=False)
    start = BASE
    metrics = _series(rng, "solo", "CPU %", _diurnal(rng, base=20.0, amp=10.0),
                      start=start, duration_s=1800.0, cadence_s=30.0)
    metrics += _series(rng, "solo", "Memory MB", _sawtooth(lo=400.0, hi=900.0, period_s=1200.0),
                       start=start, duration_s=1800.0, cadence_s=30.0)
    session = SessionRecord(
        id="2026-07-01T08-00-00-minimal", label="minimal", start=start,
        end=start + timedelta(seconds=1800.0),
        lab=LabSnapshot(hosts=[host]), meta=meta, metrics=metrics,
        chart_map=_chart_map(meta),
    )
    return MonitorExport(format=1, sessions=[session])


def drift() -> MonitorExport:
    """Three sessions across months over one evolving lab (spec §5)."""
    rng = random.Random(77)

    def lab_v1() -> LabSnapshot:
        hosts = [
            _host("chassis-a_lc1", "chassis-a", "10.20.1.11", board="lc1", slot=1),
            _host("chassis-a_sup", "chassis-a", "10.20.1.15", board="sup", slot=5),
            _host("db-01", "db-01", "10.20.3.31"),
        ]
        return LabSnapshot(hosts=hosts)

    def lab_v2() -> LabSnapshot:
        v1 = lab_v1()
        hosts = [
            *v1.hosts,
            _host("chassis-a_lc2", "chassis-a", "10.20.1.12", board="lc2", slot=2),
            _host("workers_w1", "workers", "10.20.2.21", board="w1"),
            _host("workers_w2", "workers", "10.20.2.22", board="w2"),
        ]
        links = [_link(("workers_w1", "eth0", "10.20.2.21"), ("db-01", "eth0", "10.20.3.31"),
                       name="app-db")]
        return LabSnapshot(hosts=hosts, links=links)

    def lab_v3() -> LabSnapshot:
        v2 = lab_v2()
        gw = _host("edge-gw", "edge-gw", "10.20.0.1")
        hosts = [gw] + [
            h.model_copy(update={
                "slot": 3 if h.board == "lc2" else h.slot,      # board slot moved
                "hop": "edge-gw" if h.element == "chassis-a" else h.hop,
            })
            for h in v2.hosts
            if h.id != "workers_w2"                             # host removed
        ]
        links = [
            *_implicit_links(hosts),
            _link(("workers_w1", "eth0", "10.20.2.21"), ("db-01", "eth0", "10.20.3.31"),
                  name="app-db", impair="edge-gw"),             # impairment added
        ]
        return LabSnapshot(hosts=hosts, links=links)

    meta = _meta(_HOST_CHARTS[:2], tables=False)
    sessions = []
    for sid, label, start, lab in (
        ("2026-03-01T08-00-00-baseline", "baseline", datetime(2026, 3, 1, 8, tzinfo=timezone.utc), lab_v1()),
        ("2026-05-01T08-00-00-expanded", "expanded", datetime(2026, 5, 1, 8, tzinfo=timezone.utc), lab_v2()),
        ("2026-07-01T08-00-00-rewired", "rewired", datetime(2026, 7, 1, 8, tzinfo=timezone.utc), lab_v3()),
    ):
        metrics: list[MetricRecord] = []
        for h in lab.hosts:
            metrics += _series(rng, h.id, "CPU %", _diurnal(rng, base=30.0, amp=15.0),
                               start=start, duration_s=1200.0, cadence_s=30.0)
            metrics += _series(rng, h.id, "Memory MB",
                               _sawtooth(lo=800.0, hi=2400.0, period_s=900.0),
                               start=start, duration_s=1200.0, cadence_s=30.0)
        sessions.append(SessionRecord(
            id=sid, label=label, start=start, end=start + timedelta(seconds=1200.0),
            lab=lab, meta=meta, metrics=metrics, chart_map=_chart_map(meta),
        ))
    return MonitorExport(format=1, sessions=sessions)


# --- output ----------------------------------------------------------------------


def dumps(doc: MonitorExport) -> str:
    """Minified JSON + trailing newline — the committed on-disk form."""
    return json.dumps(doc.model_dump(mode="json", exclude_none=True),
                      separators=(",", ":"), ensure_ascii=False) + "\n"


def build_all() -> dict[str, MonitorExport]:
    """All fixture documents, keyed by file stem."""
    return {"kitchen-sink": kitchen_sink(), "minimal": minimal(), "drift": drift()}


def main(out_dir: str) -> None:
    """Write every fixture to ``<out_dir>/<stem>.json``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for stem, doc in build_all().items():
        (out / f"{stem}.json").write_text(dumps(doc), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "web/fixtures")
```

Note on `test_no_credentials_anywhere`: the fixture deliberately contains no `login`/`creds`/`password` strings anywhere (including log messages) so the guard stays a plain substring check. Keep it that way when editing scenarios.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/scripts/test_gen_monitor_fixtures.py -v`
Expected: ALL PASS. If `test_size_caps` fails, reduce `_DURATION_S` (not the host set — coverage of shapes beats data length).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format scripts/gen_monitor_fixtures.py tests/unit/scripts/test_gen_monitor_fixtures.py
uv run ruff check scripts/gen_monitor_fixtures.py tests/unit/scripts/test_gen_monitor_fixtures.py
git add scripts/gen_monitor_fixtures.py tests/unit/scripts/test_gen_monitor_fixtures.py
git commit -m "feat(monitor): deterministic dummy-data fixture generator

kitchen-sink / minimal / drift documents built through the export models
(spec 2026-07-10 §5); seeded + fixed-epoch for byte-identical regeneration.

Assisted-by: Claude Fable 5"
```

---

### Task 5: Committed fixtures + make target + freshness guard

**Files:**
- Modify: `Makefile` (new `monitor-fixtures` target + `.PHONY`)
- Create (generated, committed): `web/fixtures/kitchen-sink.json`, `web/fixtures/minimal.json`, `web/fixtures/drift.json`
- Test: `tests/unit/scripts/test_monitor_fixture_files.py`

**Interfaces:**
- Consumes: `build_all()` / `dumps()` / `main()` from Task 4.
- Produces: the committed fixture files Plans 2+ load via Import; `make monitor-fixtures` as the regeneration entry point.

- [ ] **Step 1: Write the failing freshness-guard test**

Create `tests/unit/scripts/test_monitor_fixture_files.py`:

```python
"""Drift guard: committed web/fixtures/ must match regeneration exactly.

If this fails, the generator or the export models changed without
re-stamping the fixtures — run ``make monitor-fixtures`` and commit the
result (the byte-identical guarantee is what makes the fixtures a reliable
contract artifact for the web tests).
"""

from pathlib import Path

import pytest

from scripts.gen_monitor_fixtures import build_all, dumps

_FIXTURE_DIR = Path(__file__).parents[3] / "web" / "fixtures"


@pytest.mark.parametrize("stem", ["kitchen-sink", "minimal", "drift"])
def test_committed_fixture_is_fresh(stem: str):
    committed = (_FIXTURE_DIR / f"{stem}.json").read_text(encoding="utf-8")
    assert committed == dumps(build_all()[stem]), (
        f"web/fixtures/{stem}.json is stale — run 'make monitor-fixtures' and commit"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_monitor_fixture_files.py -v`
Expected: FAIL — `FileNotFoundError` (fixtures not generated yet).

- [ ] **Step 3: Add the make target and generate**

In the `Makefile`: add `monitor-fixtures` to the `.PHONY` line, and add the target near `schema` (it is the same "regenerate a contract artifact" family):

```make
monitor-fixtures: ## (Dev) Regenerate the committed monitor dummy-data fixtures in web/fixtures/ (spec 2026-07-10)
	uv run python scripts/gen_monitor_fixtures.py web/fixtures
```

Run: `make monitor-fixtures && ls -la web/fixtures/ && git check-ignore web/fixtures/kitchen-sink.json; echo "ignored=$?"`
Expected: three `.json` files; `git check-ignore` exits **1** (`ignored=1` — NOT ignored). If it exits 0, find the matching ignore rule (`git check-ignore -v`) and add a negation for `web/fixtures/` in the relevant `.gitignore`.

- [ ] **Step 4: Run the guard + eyeball one fixture**

Run: `uv run pytest tests/unit/scripts/test_monitor_fixture_files.py -v`
Expected: 3 PASS.

Run: `python -c "import json; d=json.load(open('web/fixtures/kitchen-sink.json')); s=d['sessions'][0]; print(d['format'], len(s['lab']['hosts']), len(s['lab']['links']), len(s['metrics']), len(s['log_events']))"`
Expected: `1 9 <n≥6> <tens of thousands> 80` — sane counts, no traceback.

- [ ] **Step 5: Commit**

```bash
git add Makefile web/fixtures/kitchen-sink.json web/fixtures/minimal.json web/fixtures/drift.json tests/unit/scripts/test_monitor_fixture_files.py
git commit -m "feat(monitor): commit dummy-data fixtures + make monitor-fixtures

Freshness drift-guard keeps web/fixtures byte-identical to regeneration.

Assisted-by: Claude Fable 5"
```

---

### Task 6: Full gate

**Files:** none new — verification only (fix-forward anything it surfaces).

- [ ] **Step 1: Python gate (hostless — another agent owns the lab right now)**

Run: `make coverage-hostless`
Expected: green, coverage ≥ the CI gate. (No heavy parallel loops beyond the single run — dev-VM rule.) This plan touches no host/lab runtime code, so the hostless gate is sufficient; the full `make coverage` (lab VMs + dashboard lane) runs at merge time when the lab is free.

- [ ] **Step 2: Lint + typecheck + docs-relevant gates**

Run: `uv run nox -s lint typecheck`
Expected: green. `ty` sees the new models here for the first time — fix any annotation nits now.

- [ ] **Step 3: Web gate**

Run: `make web`
Expected: green — regenerated TS types byte-identical (drift diff passes), vite builds, both air-gap checks pass.

- [ ] **Step 4: Import-budget sanity**

Run: `make import-snapshot` and diff against baseline expectations.
Expected: no regression — the new models are additive to an already-imported module and the generator script is never imported by `otto`.

- [ ] **Step 5: Commit any gate fixes**

Each fix as its own conventional commit (`fix(...)`/`test(...)`), same trailer convention.

---

## Self-review notes (done at authoring time)

- **Spec coverage:** §3 format → Task 1; §3.1 records → Task 1; §4 models/schema/TS → Tasks 1–3; §5 fixtures/generator/make target/import discipline → Tasks 4–5; §6 health is *client-side* (Plans 2+) — only its data prerequisites (intervals in meta, outage gap) land here; §9 air-gap → fixtures outside `src/`, `make web` air-gap checks in Tasks 3/6; §7 backend catch-up + §8 scaffold/views deliberately NOT in this plan (plan series).
- **Type consistency:** `dumps`/`build_all`/`main`/`OUTAGE_S` names match between Tasks 4 and 5; model names match between Tasks 1, 2, and 4.
- **The `# type: ignore[arg-type]` in `_link`** narrows a `str` parameter onto the `Literal` provenance field — acceptable at a fixture-only seam; if `ty` flags differently, change the parameter type to the same `Literal` and drop the ignore.
