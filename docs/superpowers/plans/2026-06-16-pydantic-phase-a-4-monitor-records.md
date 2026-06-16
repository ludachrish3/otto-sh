# Pydantic Phase A — Plan 4: Monitor Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **STAGE ONLY — Chris commits.** The `prepare-commit-msg` hook needs `/dev/tty` and an agent self-commit mis-tags the AI-assist trailer. Each task's final step is `git add` (stage) + run tests. **Do NOT run `git commit`.** Reviewers diff with `git diff --cached`. Chris squashes into one commit at the end (as he did for Plan 3 → `565d0c5`).

**Goal:** Move the monitor subsystem's data records onto pydantic at the import/export and SNMP-descriptor boundaries: a `MetricPoint` model replaces the `(ts, value, meta)` 3-tuple, tolerant row models validate JSON/SQLite read-back, `SnmpMetric` becomes a frozen pydantic model registered through one symmetric path, and the dashboard's request bodies move to `OttoModel`.

**Architecture:** Section 5 of [docs/superpowers/specs/2026-06-14-pydantic-phase-a-design.md](../specs/2026-06-14-pydantic-phase-a-design.md). New boundary models live in `src/otto/models/monitor.py` (a pure leaf inside the models package — imports only `.base` + pydantic + stdlib, no runtime/monitor edge). The runtime objects (`MetricCollector`, `MonitorEvent`, `MetricParser`, `MetricDataPoint`) stay as they are; only their *stored point type* and *parse/serialize seams* change. `SnmpMetric` is converted in place in `monitor/snmp.py` (it is a low-volume descriptor with no runtime twin, so it becomes pydantic directly rather than via a two-type split). The series-shape flip and the row-model adoption are split into separate tasks so each leaves the suite green.

**Tech Stack:** pydantic v2 (`BaseModel`, `OttoModel`, `ConfigDict(frozen=…/extra=…)`, `Field(validation_alias=AliasChoices(...))`, `model_construct`, `model_validate`, `model_dump(mode='json', exclude_none=True)`); FastAPI request bodies; `aiosqlite`; `pytest` / `pytest-asyncio`.

---

## Design decisions locked before implementation

These were resolved against the live code + verified empirically (`python -c` probes) while writing this plan. Implementers must not re-litigate them:

1. **Row models are tolerant (`extra='ignore'`), NOT `OttoModel`.** `MetricRecord`/`EventRecord` validate *historical data read-back*, not config. The pre-pydantic parsers used `.get()`/`[]` and silently ignored unknown keys; `extra='ignore'` preserves that exactly and keeps old exports forward-importable (a newer otto adding a column must not make an older otto skip the whole row). They derive from a small private `_RowModel(BaseModel, extra='ignore')` base that documents this divergence once. **Only `MetricPoint` is `OttoModel`** (it is built solely by otto, never from a raw external dict, so `extra='forbid'` is correct and free).

2. **Field names match the JSON spelling; a `validation_alias=AliasChoices(...)` accepts the SQLite column spelling too.** JSON uses `timestamp`/`end_timestamp`; the SQLite columns are `ts`/`end_ts`. One model validates both seams: `timestamp: datetime = Field(validation_alias=AliasChoices('timestamp', 'ts'))`. Because the field's own name is one of the choices, export (`model_dump`, no `by_alias`) emits `timestamp` natively and construction via the `timestamp=` kwarg works — no `serialization_alias`, no `populate_by_name`. (Verified: JSON dict, DB dict, and `timestamp=` kwarg all validate; export emits `timestamp`; ISO string round-trips identically to `datetime.isoformat()`.)

3. **Live append uses `MetricPoint.model_construct` (trusted, fast); the import path uses full `model_validate`.** This is the spec's "hot path constructs, import validates" split, applied literally in `collector._record_point` (construct) vs `from_json`/`from_sqlite` (validate).

4. **`SnmpMetric` converts in place in `monitor/snmp.py`** (the spec's module-layout table does **not** list it under `models/monitor.py`). It becomes `OttoModel` + `frozen=True`. pydantic `BaseModel.__init__` rejects positional args, so every `SnmpMetric(oid, label, ...)` call site moves to keyword args (verified: positional raises `TypeError`). `frozen=True` merges cleanly with the inherited `extra='forbid'` (verified).

5. **Event export stays `MonitorEvent.to_dict()`.** `EventRecord` is used on the **import** side only (the untrusted read seam). `MonitorEvent` remains a mutable dataclass (it is mutated by `update_event`); the spec does not ask to convert it, and keeping `to_dict()` avoids touching `events.py`, which stays free of any `models/` import.

6. **`MetricParser` (ABC with `parse()`) and `MetricDataPoint` (NamedTuple) are NOT converted** — explicit "not converted" items in the spec.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/otto/models/monitor.py` | **NEW.** `MetricPoint` (OttoModel), `_RowModel` base, `MetricRecord`, `EventRecord` | Create |
| `src/otto/models/__init__.py` | Package exports | Add 3 names + `from .monitor import ...` |
| `src/otto/monitor/snmp.py` | `SnmpMetric` → frozen pydantic; `_register_builtin_metrics()` symmetric registration | Modify |
| `src/otto/monitor/collector.py` | `_series` → `deque[MetricPoint]`; live/import/export read named fields; row-model adoption | Modify |
| `src/otto/monitor/server.py` | `/api/data` series serialization via `MetricPoint`; `_EventBody`/`_EventUpdateBody` → `OttoModel` | Modify |
| `src/otto/suite/suite.py` | `get_monitor_results` drops the metadata-strip wrapper | Modify |
| `tests/unit/models/test_monitor.py` | **NEW.** Unit tests for the 4 new models | Create |
| `tests/unit/monitor/test_snmp.py` | Positional `SnmpMetric(...)` → keyword; built-in-registration test | Modify |
| `tests/unit/monitor/test_collector_run.py` | Series point access `[0][1]` → `.value` | Modify |
| `tests/unit/monitor/test_collector_db.py` | Helper append + `_, value, _` unpack → `MetricPoint` / `.value` | Modify |
| `tests/unit/monitor/test_monitor_import_export.py` | `_add` helper + all positional unpacks → `MetricPoint` / named | Modify |
| `tests/unit/cli/test_monitor.py` | `_, value, _` unpack → `.value` | Modify |
| `tests/unit/suite/test_plugin.py` | `_series` append tuple → `MetricPoint` | Modify |
| `tests/integration/host/test_snmp_integration.py` | `points[-1][1]` → `.value` | Modify |

**Task dependency order:** 1 → 2 → 3 → 4 → 5 → 6. Task 2 (SNMP) is independent of 1/3/4/5 and may be done any time after 1; it is placed second because it is self-contained. Task 5 depends on Task 1 (models) **and** Task 3 (`_series` is `MetricPoint`). Each task ends with a green suite.

---

### Task 1: `models/monitor.py` — the four boundary models

**Files:**
- Create: `src/otto/models/monitor.py`
- Modify: `src/otto/models/__init__.py`
- Test: `tests/unit/models/test_monitor.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/models/test_monitor.py`:

```python
"""Unit tests for the monitor boundary models (MetricPoint + import/export rows)."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from otto.models import EventRecord, MetricPoint, MetricRecord
from otto.models.monitor import _RowModel


class TestMetricPoint:
    def test_fields_round_trip(self):
        pt = MetricPoint(ts=datetime(2024, 3, 1, 10), value=42.0, meta={'a': 1})
        assert pt.ts == datetime(2024, 3, 1, 10)
        assert pt.value == 42.0
        assert pt.meta == {'a': 1}

    def test_meta_defaults_none(self):
        assert MetricPoint(ts=datetime(2024, 3, 1, 10), value=1.0).meta is None

    def test_construct_skips_validation_and_dumps(self):
        # The hot live-append path: model_construct does no coercion/validation.
        pt = MetricPoint.model_construct(ts=datetime(2024, 3, 1, 10), value=7.5, meta=None)
        assert pt.model_dump(mode='json', exclude_none=True) == {
            'ts': '2024-03-01T10:00:00',
            'value': 7.5,
        }

    def test_extra_forbidden(self):
        # MetricPoint is OttoModel — a stray key is an error, not silently dropped.
        with pytest.raises(ValidationError):
            MetricPoint(ts=datetime(2024, 3, 1, 10), value=1.0, junk=2)


class TestMetricRecord:
    def test_accepts_json_spelling(self):
        rec = MetricRecord.model_validate(
            {'timestamp': '2024-03-01T10:00:00', 'host': 'r1', 'label': 'CPU %', 'value': '33.3'}
        )
        assert rec.timestamp == datetime(2024, 3, 1, 10)
        assert rec.host == 'r1'
        assert rec.value == pytest.approx(33.3)  # string coerced to float

    def test_accepts_db_column_spelling(self):
        rec = MetricRecord.model_validate(
            {'ts': '2024-03-01T10:00:00', 'label': 'CPU %', 'value': 33.3}
        )
        assert rec.timestamp == datetime(2024, 3, 1, 10)
        assert rec.host == ''  # default for the pre-host-column schema

    def test_export_emits_json_spelling_and_omits_none_meta(self):
        rec = MetricRecord(timestamp=datetime(2024, 3, 1, 10), host='', label='CPU %', value=9.8)
        assert rec.model_dump(mode='json', exclude_none=True) == {
            'timestamp': '2024-03-01T10:00:00',
            'host': '',
            'label': 'CPU %',
            'value': 9.8,
        }

    def test_unknown_keys_ignored(self):
        # Tolerant read-back: a stray key does not reject the row.
        rec = MetricRecord.model_validate(
            {'timestamp': '2024-03-01T10:00:00', 'label': 'X', 'value': 1.0, 'future_col': 'v'}
        )
        assert rec.label == 'X'

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            MetricRecord.model_validate({'timestamp': '2024-03-01T10:00:00', 'value': 1.0})


class TestEventRecord:
    def test_accepts_json_spelling_with_defaults(self):
        rec = EventRecord.model_validate({'timestamp': '2024-03-01T10:00:00', 'label': 'start'})
        assert rec.timestamp == datetime(2024, 3, 1, 10)
        assert rec.label == 'start'
        assert rec.source == 'manual'
        assert rec.color == '#888888'
        assert rec.dash == 'dash'
        assert rec.id is None
        assert rec.end_timestamp is None

    def test_accepts_db_column_spelling(self):
        rec = EventRecord.model_validate(
            {'id': 5, 'ts': '2024-03-01T10:00:00', 'end_ts': '2024-03-01T10:05:00',
             'label': 'span', 'source': 'auto', 'color': '#2ca02c', 'dash': 'solid'}
        )
        assert rec.id == 5
        assert rec.end_timestamp == datetime(2024, 3, 1, 10, 5)

    def test_missing_timestamp_raises(self):
        with pytest.raises(ValidationError):
            EventRecord.model_validate({'label': 'no ts'})


def test_rowmodel_base_is_lenient():
    assert _RowModel.model_config['extra'] == 'ignore'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/models/test_monitor.py -q`
Expected: FAIL — `ImportError: cannot import name 'MetricPoint' from 'otto.models'`.

- [ ] **Step 3: Create `src/otto/models/monitor.py`**

```python
"""Pydantic boundary models for the monitor subsystem.

Two seams:

* :class:`MetricPoint` — the in-memory series element (replaces the old
  ``(ts, value, meta)`` 3-tuple in ``MetricCollector._series``). It is an
  :class:`~otto.models.base.OttoModel` (``extra='forbid'``) because otto is the
  only thing that builds it: the live append path uses ``model_construct`` (no
  validation, hot loop) and the import path uses ``model_validate``.

* :class:`MetricRecord` / :class:`EventRecord` — flat records at the JSON
  ``--file`` and SQLite import/export boundary. These read *historical,
  external* data, so they are deliberately **lenient** (``extra='ignore'``,
  via :class:`_RowModel`): an unknown column from a newer schema is dropped, not
  rejected, exactly as the old ``.get()``/``[]`` parsing did. Field names follow
  the JSON spelling; a ``validation_alias`` also accepts the SQLite column
  spelling (``ts``/``end_ts``) so one model validates both seams.

Leaf isolation: this module imports only :mod:`otto.models.base`, pydantic, and
the stdlib — no runtime or ``otto.monitor`` edge — so it stays a pure leaf inside
the models package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .base import OttoModel


class MetricPoint(OttoModel):
    """A single charted sample: timestamp, numeric value, optional hover meta.

    Replaces the ``(datetime, float, dict | None)`` tuple stored per series.
    Consumers read ``.ts`` / ``.value`` / ``.meta`` instead of unpacking.
    """

    ts:    datetime
    value: float
    meta:  dict[str, Any] | None = None


class _RowModel(BaseModel):
    """Lenient base for historical-data import/export rows.

    Unlike :class:`~otto.models.base.OttoModel` (``extra='forbid'``, which exists
    to turn a *config* typo into an error), data read-back is tolerant: an
    unexpected key/column from a newer schema is ignored rather than rejected.
    This matches the pre-pydantic ``.get()``/``[]`` parsing and keeps older otto
    builds able to import exports written by newer ones.
    """

    model_config = ConfigDict(extra="ignore")


class MetricRecord(_RowModel):
    """One ``metrics`` row at the JSON / SQLite import-export boundary.

    The JSON ``--file`` format spells the time key ``timestamp``; the SQLite
    ``metrics`` table column is ``ts``. The ``validation_alias`` accepts both, so
    a single model validates either seam. ``host`` is optional for the
    pre-host-column schema; ``meta`` rides only in JSON (the DB has no meta
    column). Exporting with ``model_dump(mode='json', exclude_none=True)`` emits
    the JSON spelling and omits ``meta`` when ``None``.
    """

    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host:      str = ""
    label:     str
    value:     float
    meta:      dict[str, Any] | None = None


class EventRecord(_RowModel):
    """One ``events`` row at the JSON / SQLite **import** boundary.

    Mirrors the ``MonitorEvent`` fields. Used to validate external event data
    before constructing the (unchanged, mutable) ``MonitorEvent`` dataclass —
    event *export* stays ``MonitorEvent.to_dict()``. ``timestamp`` is required
    (a row without one is skipped, as before); everything else defaults. ``id``
    is ``None`` when absent so the collector can assign its running id.
    """

    id:            int | None = None
    timestamp:     datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    end_timestamp: datetime | None = Field(
        default=None, validation_alias=AliasChoices("end_timestamp", "end_ts")
    )
    label:         str = ""
    source:        str = "manual"
    color:         str = "#888888"
    dash:          str = "dash"
```

- [ ] **Step 4: Wire the exports into `src/otto/models/__init__.py`**

Add the import block (after the `from .settings import (...)` block, before `__all__`):

```python
from .monitor import (
    EventRecord,
    MetricPoint,
    MetricRecord,
)
```

Add these three names to the `__all__` list (append after `"SettingsModel",`):

```python
    "MetricPoint",
    "MetricRecord",
    "EventRecord",
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/models/test_monitor.py -q`
Expected: PASS (all green).

- [ ] **Step 6: Verify no import cycle and the package still imports**

Run: `python -c "import otto.models; import otto.monitor.snmp; import otto.monitor.collector; print('ok')"`
Expected: prints `ok` (no `ImportError`/cycle from the new module).

- [ ] **Step 7: Lint the new/changed files**

Run: `ruff check src/otto/models/monitor.py src/otto/models/__init__.py tests/unit/models/test_monitor.py`
Expected: no new violations (clean, or only pre-existing repo debt unrelated to these files).

- [ ] **Step 8: Stage (do NOT commit)**

```bash
git add src/otto/models/monitor.py src/otto/models/__init__.py tests/unit/models/test_monitor.py
```
Suggested message (Chris commits): `feat(models): monitor boundary models — MetricPoint + MetricRecord/EventRecord rows (Phase A plan 4)`

---

### Task 2: `SnmpMetric` → frozen pydantic + symmetric built-in registration

**Files:**
- Modify: `src/otto/monitor/snmp.py:63-118` (the `SnmpMetric` dataclass + `_default_metrics()` + `_SNMP_METRICS` assignment)
- Test: `tests/unit/monitor/test_snmp.py`

- [ ] **Step 1: Update the registry tests to the keyword constructor + add a built-in-registration assertion**

In `tests/unit/monitor/test_snmp.py`, change every positional `SnmpMetric(...)` to keyword args (pydantic rejects positional). Exact edits:

- Line ~37: `SnmpMetric('1.2.3', 'CPU', chart='CPU', unit='%', scale=0.01)` → `SnmpMetric(oid='1.2.3', label='CPU', chart='CPU', unit='%', scale=0.01)`
- Line ~41: `SnmpMetric('1.2.3', 'Heap', chart='Memory', unit='B')` → `SnmpMetric(oid='1.2.3', label='Heap', chart='Memory', unit='B')`
- Line ~45: `SnmpMetric('1.2.3', 'X', chart='X', scale=1 / 3)` → `SnmpMetric(oid='1.2.3', label='X', chart='X', scale=1 / 3)`
- Line ~71: `register_snmp_metric(SnmpMetric('9.9.9', 'Custom', chart='Widgets', unit='w'))` → `register_snmp_metric(SnmpMetric(oid='9.9.9', label='Custom', chart='Widgets', unit='w'))`

Add a new test in `class TestRegistry` asserting the built-ins arrive through the public registration path (not direct dict construction):

```python
    def test_builtins_registered_through_public_path(self):
        # Every built-in descriptor must be retrievable via the same getter a
        # third-party registration would populate — i.e. _register_builtin_metrics()
        # used register_snmp_metric(), not a private dict literal.
        from otto.monitor.snmp import OID_SYS_UPTIME, _OTTO_BASE
        for oid in (OID_SYS_UPTIME, f'{_OTTO_BASE}.1.1.0', f'{_OTTO_BASE}.1.2.0',
                    f'{_OTTO_BASE}.1.3.0', f'{_OTTO_BASE}.1.4.0'):
            assert get_snmp_metric(oid) is not None, f'built-in {oid} not registered'

    def test_snmp_metric_is_frozen(self):
        m = get_snmp_metric(OID_SYS_UPTIME)
        assert m is not None
        with pytest.raises(ValidationError):
            m.scale = 2.0  # frozen → mutation rejected
```

Add the import at the top of the file (next to the existing pytest import):

```python
from pydantic import ValidationError
```

- [ ] **Step 2: Run the SNMP tests to verify they fail**

Run: `python -m pytest tests/unit/monitor/test_snmp.py -q`
Expected: FAIL — positional construction now raises `TypeError`, and `test_snmp_metric_is_frozen` fails because the dataclass raises `FrozenInstanceError` (a `dataclasses` error), not `ValidationError`.

- [ ] **Step 3: Convert `SnmpMetric` to a frozen pydantic model**

In `src/otto/monitor/snmp.py`, change the imports near the top. Replace:

```python
from dataclasses import dataclass
from typing import Literal, SupportsInt
```
with:
```python
from dataclasses import dataclass
from typing import Literal, SupportsInt

from pydantic import ConfigDict

from ..models.base import OttoModel
```
(`dataclass` is still used by `SnmpClient`/`SnmpSource`, so it stays.)

Replace the `SnmpMetric` class (currently `@dataclass(frozen=True, slots=True)` at lines ~63-88) with:

```python
class SnmpMetric(OttoModel):
    """How a single OID's value is interpreted and charted.

    Mirrors the presentation attributes :class:`~otto.monitor.parsers.MetricParser`
    already exposes (``chart``/``y_title``/``unit``/``tab``/``tab_label``) plus a
    ``scale`` factor that converts the raw integer varbind into a real value
    (e.g. sysUpTime is in hundredths of a second → ``scale=0.01`` for seconds;
    a CPU OID reported in centi-percent → ``scale=0.01`` for percent).

    These are deliberately *not* sourced from lab data — graphing stays in the
    monitor module. ``frozen=True``: a descriptor is an immutable, low-volume
    value object shared across ticks; the registry only ever replaces, never
    mutates. Built and registered through the public path
    (:func:`register_snmp_metric`) for first- and third-party descriptors alike.
    """

    model_config = ConfigDict(frozen=True)

    oid:       str
    label:     str
    chart:     str
    y_title:   str = ''
    unit:      str = ''
    tab:       str = 'metrics'
    tab_label: str = 'Metrics'
    scale:     float = 1.0

    def to_point(self, raw: float) -> MetricDataPoint:
        """Apply ``scale`` to a raw numeric varbind, returning a chartable point."""
        return MetricDataPoint(value=round(raw * self.scale, 2))
```

- [ ] **Step 4: Replace `_default_metrics()` + the module assignment with symmetric registration**

Replace the `_default_metrics()` function and the `_SNMP_METRICS: dict[str, SnmpMetric] = _default_metrics()` line (lines ~95-118) with:

```python
# ---------------------------------------------------------------------------
# Built-in descriptor registry
# ---------------------------------------------------------------------------
# otto registers its own built-ins through the SAME register_snmp_metric() entry
# point a third party uses — one validation path for first- and third-party
# descriptors, mirroring the host-class registry decision. (See the Phase A
# design, "SNMP-metric registration symmetry".)

_SNMP_METRICS: dict[str, SnmpMetric] = {}


def _register_builtin_metrics() -> None:
    """Register the built-in descriptors via the public path.

    Standard ``sysUpTime`` works against any compliant agent (net-snmp, routers,
    …); the enterprise OIDs are scalars served by otto's Zephyr agent.
    """
    for metric in (
        SnmpMetric(oid=OID_SYS_UPTIME, label='Uptime', chart='Uptime',
                   y_title='Uptime', unit='s', scale=0.01),
        SnmpMetric(oid=f'{_OTTO_BASE}.1.1.0', label='Overall CPU', chart='CPU',
                   y_title='Usage %', unit='%', tab='cpu', tab_label='CPU', scale=0.01),
        SnmpMetric(oid=f'{_OTTO_BASE}.1.2.0', label='Heap Used', chart='Memory Usage',
                   y_title='Memory', unit='B', tab='memory', tab_label='Memory'),
        SnmpMetric(oid=f'{_OTTO_BASE}.1.3.0', label='Heap Free', chart='Memory Usage',
                   y_title='Memory', unit='B', tab='memory', tab_label='Memory'),
        SnmpMetric(oid=f'{_OTTO_BASE}.1.4.0', label='Threads', chart='Threads',
                   y_title='Count', unit=''),
    ):
        register_snmp_metric(metric)
```

Then, **after** the `register_snmp_metric` function is defined (it must exist before the call runs), add the bootstrap call. The cleanest placement is immediately after `register_snmp_metric`'s definition (currently ~line 129). Add:

```python
_register_builtin_metrics()
```

Note on ordering: `_register_builtin_metrics` is *defined* above but *called* after `register_snmp_metric` is defined, so the forward reference resolves at call time. The fallback `resolve_snmp_metric` already constructs with keyword args (`SnmpMetric(oid=oid, label=oid, chart=oid)`) and needs no change.

- [ ] **Step 5: Run the SNMP tests to verify they pass**

Run: `python -m pytest tests/unit/monitor/test_snmp.py -q`
Expected: PASS.

- [ ] **Step 6: Run the collector + CLI monitor tests (SnmpMetric consumers) to confirm no positional breakage**

Run: `python -m pytest tests/unit/monitor/ tests/unit/cli/test_monitor.py -q`
Expected: PASS (collector only ever obtains `SnmpMetric` via `resolve_snmp_metric` and reads attributes — no positional construction).

- [ ] **Step 7: Lint**

Run: `ruff check src/otto/monitor/snmp.py tests/unit/monitor/test_snmp.py`
Expected: no new violations.

- [ ] **Step 8: Stage (do NOT commit)**

```bash
git add src/otto/monitor/snmp.py tests/unit/monitor/test_snmp.py
```
Suggested message: `refactor(monitor): SnmpMetric → frozen pydantic + symmetric built-in registration (Phase A plan 4)`

---

### Task 3: Flip `MetricCollector._series` to `MetricPoint` (atomic ripple)

This task changes the element type returned by `get_series()`, so **every** consumer — collector internals, the dashboard `/api/data` route, `Suite.get_monitor_results`, and all tests that read series points — must change together to stay green. The row-model adoption in `from_json`/`from_sqlite`/`to_json` is deferred to Task 5; here those methods keep their existing manual parsing but build/read `MetricPoint`.

**Files:**
- Modify: `src/otto/monitor/collector.py`
- Modify: `src/otto/monitor/server.py:71-84` (`/api/data` only)
- Modify: `src/otto/suite/suite.py:481-488`
- Modify tests: `test_collector_run.py`, `test_collector_db.py`, `test_monitor_import_export.py`, `tests/unit/cli/test_monitor.py`, `tests/unit/suite/test_plugin.py`, `tests/integration/host/test_snmp_integration.py`

- [ ] **Step 1: Update `collector.py` imports and the `_series` type**

Add the import (next to the other `from .` imports near line 28-30):

```python
from ..models import MetricPoint
```

Change the `_series` declaration (line ~174) from:

```python
        self._series: dict[str, deque[tuple[datetime, float, dict[str, Any] | None]]] = {}
```
to:
```python
        self._series: dict[str, deque[MetricPoint]] = {}
```

Update the comment block just above it (lines ~167-173) so the "(timestamp, value, metadata_or_None) triple" wording becomes "a ``MetricPoint``".

- [ ] **Step 2: Update the live append in `_record_point` (hot path → `model_construct`)**

In `_record_point` (line ~434), change:

```python
        self._series[key].append((ts, dp.value, dp.meta))
```
to:
```python
        # Hot path: model_construct skips validation (the values are otto's own).
        self._series[key].append(MetricPoint.model_construct(ts=ts, value=dp.value, meta=dp.meta))
```

- [ ] **Step 3: Update `get_series()` return type + docstring**

Change the signature + body (lines ~540-545):

```python
    def get_series(self) -> dict[str, list[MetricPoint]]:
        """Return a snapshot of all series (metrics and per-process).

        Format: ``{"hostname/label": [MetricPoint(ts, value, meta), ...]}``
        """
        return {key: list(pts) for key, pts in self._series.items()}
```

- [ ] **Step 4: Update the import builders (`from_json`, `from_sqlite`) to build `MetricPoint` via `model_validate`**

In `from_json` (line ~652), change:

```python
                collector._series[key].append((ts, value, meta))
```
to:
```python
                # Import path: full validation (untrusted historical data).
                collector._series[key].append(
                    MetricPoint.model_validate({'ts': ts, 'value': value, 'meta': meta})
                )
```

In `from_sqlite` (line ~702), change:

```python
                    collector._series[key].append((ts, value, None))
```
to:
```python
                    collector._series[key].append(
                        MetricPoint.model_validate({'ts': ts, 'value': value, 'meta': None})
                    )
```

- [ ] **Step 5: Update the export reader (`to_json`) to read named fields**

In `to_json` (line ~746), change:

```python
            for ts, value, meta in pts:
                record: dict[str, Any] = {
                    'timestamp': ts.isoformat(),
                    'host':  host,
                    'label': label,
                    'value': value,
                }
                if meta:
                    record['meta'] = meta
                metrics.append(record)
```
to:
```python
            for pt in pts:
                record: dict[str, Any] = {
                    'timestamp': pt.ts.isoformat(),
                    'host':  host,
                    'label': label,
                    'value': pt.value,
                }
                if pt.meta:
                    record['meta'] = pt.meta
                metrics.append(record)
```

(Row-model serialization replaces this manual dict in Task 5.)

- [ ] **Step 6: Update the dashboard `/api/data` route in `server.py`**

In `server.py` `data()` (lines ~74-80), change the series comprehension from positional unpacking to `MetricPoint.model_dump`:

```python
        payload: dict[str, Any] = {
            'series': {
                label: [pt.model_dump(mode='json', exclude_none=True) for pt in pts]
                for label, pts in collector.get_series().items()
            },
            'events':    [e.to_dict() for e in collector.get_events()],
            'chart_map': collector.get_chart_map(),
        }
```

This emits `{'ts': <iso>, 'value': <float>[, 'meta': {...}]}` per point — identical keys to before (`dashboard.js` reads `p.ts` / `p.value` / `p.meta`), with `meta` omitted when `None`.

- [ ] **Step 7: Update `Suite.get_monitor_results` (drop the metadata-strip wrapper)**

In `src/otto/suite/suite.py` (lines ~481-488), the wrapper previously stripped the 3rd tuple slot on every call. Replace with a direct named projection:

```python
    def get_monitor_results(self) -> 'dict[str, list[tuple[datetime, float]]]':
        """Return collected metric series after stop_monitor(). Empty dict if never started."""
        if self._monitor_collector is None:
            return {}
        return {
            key: [(pt.ts, pt.value) for pt in pts]
            for key, pts in self._monitor_collector.get_series().items()
        }
```

(The public return shape — `list[(ts, value)]` pairs — is unchanged, so external suite callers are unaffected; only the internal source moved from tuple-unpack to `.ts`/`.value`.)

- [ ] **Step 8: Migrate the test consumers of series points**

Apply these exact edits so the suite stays green:

`tests/unit/monitor/test_collector_run.py` line ~252:
```python
        assert series['sprout/Uptime'][0].value == 123.45
```

`tests/unit/monitor/test_collector_db.py`:
- Add at the top (with the other imports): `from otto.models import MetricPoint`
- Helper append, line ~66: `collector._series[key].append(MetricPoint(ts=ts, value=value, meta=None))`
- Line ~338: `value = series['router1/CPU %'][0].value`
- Line ~438: `value = series['host1/CPU %'][0].value`

`tests/unit/monitor/test_monitor_import_export.py`:
- Add at the top: `from otto.models import MetricPoint`
- `_add` helper, lines ~62-63:
  ```python
      for ts, val in values:
          collector._series[key].append(MetricPoint(ts=ts, value=val, meta=meta))
  ```
- `_assert_collectors_equal`, line ~124:
  ```python
      for op, lp in zip(orig_pts, loaded_pts):
          assert lp.ts    == op.ts,                  f'Timestamp mismatch in {key!r}'
          assert lp.value == pytest.approx(op.value), f'Value mismatch in {key!r}'
          assert lp.meta  == op.meta,                 f'Meta mismatch in {key!r}'
  ```
- Line ~170: `assert [p.value for p in pts] == pytest.approx([10.5, 12.3, 9.8])`
- Lines ~179-181:
  ```python
      assert [p.value for p in series[f'{HOST}/Load (1m)']]  == pytest.approx([0.52, 0.61, 0.48])
      assert [p.value for p in series[f'{HOST}/Load (5m)']]  == pytest.approx([0.58, 0.57, 0.55])
      assert [p.value for p in series[f'{HOST}/Load (15m)']] == pytest.approx([0.59, 0.60, 0.58])
  ```
- Line ~190: `assert [p.ts for p in pts] == [T0, T1, T2]`
- Lines ~199-200: `meta = pts[0].meta`

`tests/unit/cli/test_monitor.py`:
- Line ~250: `value = series['router1/CPU %'][0].value`
- Line ~279: `value = series['host1/CPU %'][0].value`

`tests/unit/suite/test_plugin.py` line ~388-390:
- Add `from otto.models import MetricPoint` to the imports.
- Change the append:
  ```python
                  real_collector._series.setdefault(
                      'host1/cpu', deque()
                  ).append(MetricPoint(ts=datetime.now(), value=42.0, meta=None))
  ```

`tests/integration/host/test_snmp_integration.py` line ~61:
```python
            out[key.split("/", 1)[1]] = points[-1].value
```

- [ ] **Step 9: Run the full monitor + suite + cli test surface**

Run: `python -m pytest tests/unit/monitor/ tests/unit/cli/test_monitor.py tests/unit/suite/test_plugin.py tests/unit/models/test_monitor.py -q`
Expected: PASS.

- [ ] **Step 10: Confirm the integration test at least collects (no positional unpack left)**

Run: `python -m pytest tests/integration/host/test_snmp_integration.py --collect-only -q`
Expected: collects without error (full run needs a live SNMP bed; do not force it here).

- [ ] **Step 11: Lint**

Run: `ruff check src/otto/monitor/collector.py src/otto/monitor/server.py src/otto/suite/suite.py`
Expected: no new violations. Remove the now-unused `datetime`/`Any` imports in `collector.py` **only if** ruff reports them newly unused (they are still used elsewhere in the file — check first; do not blind-delete).

- [ ] **Step 12: Stage (do NOT commit)**

```bash
git add src/otto/monitor/collector.py src/otto/monitor/server.py src/otto/suite/suite.py \
        tests/unit/monitor/test_collector_run.py tests/unit/monitor/test_collector_db.py \
        tests/unit/monitor/test_monitor_import_export.py tests/unit/cli/test_monitor.py \
        tests/unit/suite/test_plugin.py tests/integration/host/test_snmp_integration.py
```
Suggested message: `refactor(monitor): MetricCollector series → MetricPoint; drop getMonitorResults strip (Phase A plan 4)`

---

### Task 4: Dashboard request bodies → `OttoModel`

**Files:**
- Modify: `src/otto/monitor/server.py:26,45-55`
- Test: `tests/unit/monitor/` (add a server-body test if a dashboard test module exists; otherwise add to `tests/unit/cli/test_monitor.py` or a new `tests/unit/monitor/test_server_bodies.py`)

- [ ] **Step 1: Write a failing test for extra-key rejection**

Create `tests/unit/monitor/test_server_bodies.py`:

```python
"""The dashboard request bodies are OttoModel (extra='forbid')."""

import pytest
from pydantic import ValidationError

from otto.monitor.server import _EventBody, _EventUpdateBody


class TestEventBodies:
    def test_event_body_defaults(self):
        b = _EventBody(label='deploy')
        assert b.color == '#888888'
        assert b.dash == 'dash'

    def test_event_body_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            _EventBody(label='deploy', colour='#fff')  # typo'd 'color'

    def test_update_body_all_optional(self):
        b = _EventUpdateBody()
        assert b.label is None and b.color is None and b.dash is None

    def test_update_body_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            _EventUpdateBody(dashh='dot')
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/monitor/test_server_bodies.py -q`
Expected: FAIL — `_EventBody(label='deploy', colour='#fff')` is accepted (bare `BaseModel` ignores extras), so `test_event_body_rejects_unknown_field` fails.

- [ ] **Step 3: Switch the bodies to `OttoModel`**

In `src/otto/monitor/server.py`, change the import on line 26 from:

```python
from pydantic import BaseModel
```
to:
```python
from ..models.base import OttoModel
```

Change the two body classes (lines ~45-55):

```python
class _EventBody(OttoModel):
    label: str
    color: str = '#888888'
    dash:  str = 'dash'


class _EventUpdateBody(OttoModel):
    label: str | None = None
    color: str | None = None
    dash:  str | None = None
```

(`OttoModel` is itself a `BaseModel`, so FastAPI still treats these as request-body models; the only behavior change is that an unknown field now yields a 422 instead of being silently dropped — intended, and the dashboard only ever sends the declared fields.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/monitor/test_server_bodies.py -q`
Expected: PASS.

- [ ] **Step 5: Run the existing CLI/monitor server tests to confirm no regression in the POST/PATCH routes**

Run: `python -m pytest tests/unit/cli/test_monitor.py tests/unit/monitor/ -q`
Expected: PASS. If any existing test POSTs an event body with an extra field, update that test to drop the extra field (the dashboard never sends one); flag it in the task report if found.

- [ ] **Step 6: Lint + stage**

Run: `ruff check src/otto/monitor/server.py tests/unit/monitor/test_server_bodies.py`
```bash
git add src/otto/monitor/server.py tests/unit/monitor/test_server_bodies.py
```
Suggested message: `refactor(monitor): dashboard request bodies → OttoModel (extra='forbid') (Phase A plan 4)`

---

### Task 5: Adopt `MetricRecord` / `EventRecord` at the import/export boundary

Replace the hand-rolled `try/except (KeyError, ValueError)` parsing in `from_json` / `from_sqlite` with row-model validation, and serialize `to_json`'s metric records through `MetricRecord`. Event *export* stays `MonitorEvent.to_dict()`; only event *import* uses `EventRecord`.

**Files:**
- Modify: `src/otto/monitor/collector.py` (`from_json` ~640-673, `from_sqlite` ~675-729, `to_json` ~740-763)
- Test: `tests/unit/monitor/test_monitor_import_export.py`, `tests/unit/monitor/test_collector_db.py` (existing round-trip tests guard behavior; add malformed-row cases)

- [ ] **Step 1: Write failing tests for malformed-row skip + extra-key tolerance**

Add to `tests/unit/monitor/test_monitor_import_export.py` a new class:

```python
class TestImportValidation:
    """from_json validates rows via the row models: malformed rows skip,
    unknown keys are tolerated (forward-compatible read-back)."""

    def _write(self, tmp_path, payload):
        import json
        p = tmp_path / 'data.json'
        p.write_text(json.dumps(payload))
        return str(p)

    def test_malformed_metric_row_is_skipped(self, tmp_path):
        path = self._write(tmp_path, {
            'metrics': [
                {'timestamp': '2024-03-01T10:00:00', 'host': 'h', 'label': 'CPU', 'value': 10.0},
                {'timestamp': '2024-03-01T10:00:01', 'host': 'h', 'label': 'CPU'},  # no value
                {'host': 'h', 'label': 'CPU', 'value': 99.0},                        # no timestamp
            ],
            'events': [],
        })
        loaded = MetricCollector.from_json(path)
        pts = loaded.get_series()['h/CPU']
        assert [p.value for p in pts] == [10.0]  # only the valid row survives

    def test_unknown_metric_key_tolerated(self, tmp_path):
        path = self._write(tmp_path, {
            'metrics': [
                {'timestamp': '2024-03-01T10:00:00', 'host': 'h', 'label': 'CPU',
                 'value': 10.0, 'future_col': 'ignored'},
            ],
            'events': [],
        })
        loaded = MetricCollector.from_json(path)
        assert loaded.get_series()['h/CPU'][0].value == 10.0

    def test_malformed_event_row_is_skipped(self, tmp_path):
        path = self._write(tmp_path, {
            'metrics': [],
            'events': [
                {'timestamp': '2024-03-01T10:00:00', 'label': 'ok'},
                {'label': 'no timestamp'},  # required timestamp missing → skip
            ],
        })
        loaded = MetricCollector.from_json(path)
        evs = loaded.get_events()
        assert [e.label for e in evs] == ['ok']
```

- [ ] **Step 2: Run to verify the new tests pass already OR fail meaningfully**

Run: `python -m pytest "tests/unit/monitor/test_monitor_import_export.py::TestImportValidation" -q`
Expected: these likely **PASS against the current manual parser** (it already skips on `KeyError`/`ValueError` and ignores extras). That is fine — they are the behavioral contract the row-model refactor must preserve. If any fails, the current behavior differs from the spec's intent; note it and keep the test as the target.

- [ ] **Step 3: Adopt `MetricRecord`/`EventRecord` in `from_json`**

Add imports at the top of `collector.py` (extend the existing `from ..models import MetricPoint` from Task 3):

```python
from ..models import EventRecord, MetricPoint, MetricRecord
```
And import `ValidationError`:
```python
from pydantic import ValidationError
```

Rewrite the metrics loop in `from_json` (lines ~642-654) to:

```python
        for point in data.get('metrics', []):
            try:
                rec = MetricRecord.model_validate(point)
            except ValidationError:
                continue
            key = f'{rec.host}/{rec.label}' if rec.host else rec.label
            if key not in collector._series:
                collector._series[key] = deque()
            collector._series[key].append(
                MetricPoint.model_validate({'ts': rec.timestamp, 'value': rec.value, 'meta': rec.meta})
            )
```

Rewrite the events loop in `from_json` (lines ~657-672) to:

```python
        for ev in data.get('events', []):
            try:
                rec = EventRecord.model_validate(ev)
            except ValidationError:
                continue
            event = MonitorEvent(
                timestamp=rec.timestamp,
                label=rec.label,
                source=rec.source,
                color=rec.color,
                dash=rec.dash,
                id=rec.id if rec.id is not None else collector._next_event_id,
                end_timestamp=rec.end_timestamp,
            )
            collector._next_event_id = max(collector._next_event_id, event.id) + 1
            collector._events.append(event)
```

(The `meta = point.get('meta') or None` normalization is now `rec.meta`; an empty-dict meta becomes `{}` rather than `None`, but otto never writes an empty meta dict — historical exports carry `meta` only when populated.)

- [ ] **Step 4: Adopt the row models in `from_sqlite`**

In `from_sqlite`, the metrics loop (lines ~693-704) becomes — note `dict(row)` so the `ts` column reaches the `MetricRecord` alias:

```python
            async for row in await conn.execute(query):
                try:
                    rec = MetricRecord.model_validate(dict(row))
                except ValidationError:
                    continue
                key = f'{rec.host}/{rec.label}' if rec.host else rec.label
                if key not in collector._series:
                    collector._series[key] = deque()
                collector._series[key].append(
                    MetricPoint.model_validate({'ts': rec.timestamp, 'value': rec.value, 'meta': None})
                )
```

The events loop (lines ~712-726) becomes:

```python
            async for row in await conn.execute(events_query):
                try:
                    rec = EventRecord.model_validate(dict(row))
                except ValidationError:
                    continue
                collector._events.append(MonitorEvent(
                    timestamp=rec.timestamp,
                    label=rec.label,
                    source=rec.source,
                    color=rec.color,
                    dash=rec.dash,
                    id=rec.id if rec.id is not None else collector._next_event_id,
                    end_timestamp=rec.end_timestamp,
                ))
```

`dict(aiosqlite.Row)` yields `{column: value}`; the old-schema selects (no `host` / no `end_ts`) simply omit those keys, and the models default them. The `has_host` / `has_end_ts` column-probing and the differing `SELECT` lists stay exactly as they are.

- [ ] **Step 5: Serialize `to_json` metric records through `MetricRecord`**

Replace the `to_json` metrics loop (the block edited in Task 3 Step 5) with:

```python
            for pt in pts:
                metrics.append(
                    MetricRecord(
                        timestamp=pt.ts, host=host, label=label, value=pt.value, meta=pt.meta
                    ).model_dump(mode='json', exclude_none=True)
                )
```

`model_dump(mode='json', exclude_none=True)` emits `{'timestamp', 'host', 'label', 'value'[, 'meta']}` — `timestamp` from the field name, `meta` omitted when `None` — preserving the existing `--file` shape. (Event export remains `[e.to_dict() for e in self._events]`, unchanged.)

- [ ] **Step 6: Run the import/export + DB tests (round-trip is the guard)**

Run: `python -m pytest tests/unit/monitor/test_monitor_import_export.py tests/unit/monitor/test_collector_db.py -q`
Expected: PASS — the existing round-trip tests (`TestExportImportRoundTrip`, `_assert_collectors_equal`) confirm export→import is byte-equivalent through the row models; the new `TestImportValidation` cases confirm skip/tolerance.

- [ ] **Step 7: Run the full monitor suite + models tests**

Run: `python -m pytest tests/unit/monitor/ tests/unit/models/test_monitor.py -q`
Expected: PASS.

- [ ] **Step 8: Lint**

Run: `ruff check src/otto/monitor/collector.py tests/unit/monitor/test_monitor_import_export.py`
Expected: no new violations. The `Any` import and `from datetime import datetime` in `collector.py` may now have fewer uses — verify with ruff before removing; `datetime` is still used in many signatures, so likely it stays.

- [ ] **Step 9: Stage (do NOT commit)**

```bash
git add src/otto/monitor/collector.py tests/unit/monitor/test_monitor_import_export.py
```
Suggested message: `refactor(monitor): validate JSON/SQLite import-export through MetricRecord/EventRecord (Phase A plan 4)`

---

### Task 6: Full pre-merge gate

No new code — verification only. Run the same gate Plan 3 passed before Chris committed.

- [ ] **Step 1: Update the absorbed backlog note**

`todo/metric-point-dataclass.md` is now implemented. Edit its `## Status` section to:

```markdown
## Status

**Done** — implemented in Pydantic Phase A Plan 4 (`models/monitor.py` `MetricPoint`,
`MetricCollector._series: deque[MetricPoint]`, `get_series() -> dict[str, list[MetricPoint]]`,
`getMonitorResults` strip removed). Kept as a record of the motivation.
```
Stage it: `git add todo/metric-point-dataclass.md`

- [ ] **Step 2: `ty` type check (0 diagnostics on touched files)**

Run: `ty check src/otto/models/monitor.py src/otto/monitor/snmp.py src/otto/monitor/collector.py src/otto/monitor/server.py src/otto/suite/suite.py`
Expected: 0 diagnostics. (If the project uses a different `ty` invocation, match the Makefile's `ty` target.)

- [ ] **Step 3: `make coverage` (full suite incl. live VM tiers, ≥ 90% gate)**

Run: `make coverage`
Expected: PASS, coverage ≥ 90%. **Do NOT kill the run at a tight timeout** — single-client embedded consoles wedge on SIGTERM ([[feedback_live_bed_timeout_kills_wedge]]). Let it finish or use `make qemu-restart` if a bed is already wedged.

- [ ] **Step 4: `make nox` (all Pythons 3.10–3.14)**

Run: `make nox`
Expected: 5/5 Pythons green. pydantic wheels already resolve cross-version (confirmed in Plan 3).

- [ ] **Step 5: `make docs` (doc8 + sphinx -W + doctest)**

Run: `make docs`
Expected: clean. No monitor doc references the old 3-tuple shape; if a docstring/RST mentions `(ts, value, meta)`, update it to `MetricPoint`.

- [ ] **Step 6: Final review hand-off**

All tasks staged, gate green. Dispatch the subagent-driven final code review over the staged diff, then hand to Chris to commit (he squashes Plan 4 into one commit, as with `565d0c5`). **Do not commit.**

---

## Self-Review

**Spec coverage (section 5):**
- `MetricPoint(OttoModel)` with `ts`/`value`/`meta`, `model_construct` live + `model_validate` import → Tasks 1, 3. ✓
- `get_series()` → `dict[str, list[MetricPoint]]`, consumers move to `.ts`/`.value`/`.meta`, `getMonitorResults` strip removed → Task 3. ✓
- DB import/export row models for `metrics(ts,host,label,value)` + `events(ts,end_ts,label,source,color,dash)`, used at JSON import/export + `/api/data` → Tasks 1, 3 (`/api/data` via `MetricPoint`), 5 (import/export via `MetricRecord`/`EventRecord`). ✓
- `SnmpMetric` → frozen pydantic; `register_snmp_metric` first/third-party symmetry via `_register_builtin_metrics()` → Task 2. ✓
- `monitor/server.py` bare-`BaseModel` `_EventBody`/`_EventUpdateBody` → `OttoModel` → Task 4. ✓
- Not converted: `MetricParser` (ABC), `MetricDataPoint` (NamedTuple) → untouched. ✓
- Bounding principle (symmetry only where converting the registry value type to pydantic) → behavior-class registries untouched; deferred to `todo/registry_builtin_registration_symmetry.md`. ✓

**Placeholder scan:** none — every code step carries full code or an exact line edit.

**Type consistency:** `MetricPoint(ts, value, meta)`, `MetricRecord(timestamp, host, label, value, meta)`, `EventRecord(id, timestamp, end_timestamp, label, source, color, dash)`, `SnmpMetric(oid, label, chart, y_title, unit, tab, tab_label, scale)` — names are identical at every call site across tasks. `get_series()` element type (`MetricPoint`) is consistent between the collector signature (Task 3 Step 3), the server route (Step 6), the suite projection (Step 7), and every test edit (Step 8). The `validation_alias`/field-name design (decision #2) is verified to make JSON-spelling, DB-spelling, kwarg construction, and export all consistent.

**Risk notes for the executor:**
- Task 3 is the one atomic ripple — its test-migration step (Step 8) is load-bearing; the suite will not go green until every listed unpack site is converted. Do not split the production changes from the test changes.
- `extra='forbid'` on the dashboard bodies (Task 4) and on `MetricPoint` is intentional behavior tightening; the lenient `extra='ignore'` on the row models (Task 5) is intentional behavior preservation. Do not unify them.
- `SnmpMetric` positional→keyword (Task 2) touches only otto's own built-ins + tests; a third-party init module that constructed `SnmpMetric` positionally would break — acceptable pre-freeze, and the keyword form is the documented one.
```
