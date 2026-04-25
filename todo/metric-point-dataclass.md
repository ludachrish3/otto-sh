# Introduce `MetricPoint` dataclass for series data

## Motivation

`MetricCollector.get_series()` returns `dict[str, list[tuple[datetime, float, dict[str, Any] | None]]]` — a 3-tuple of `(ts, value, meta)`. This leaks a structural shape everywhere the data is consumed:

- `Suite.getMonitorResults()` declares `dict[str, list[tuple[datetime, float]]]` and currently strips the metadata slot in-wrapper to match. This is a runtime O(n) copy on every call.
- Tests ([test_collector_db.py](../tests/unit/monitor/test_collector_db.py), [test_monitor_import_export.py](../tests/unit/monitor/test_monitor_import_export.py)) unpack tuples positionally, which tightly couples them to the shape.
- Any future slot (e.g. a per-point flag) is a breaking change to every call site.

A named dataclass gives the data a stable identity and lets the tuple-vs-triple tension disappear.

## Proposed change

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass(slots=True)
class MetricPoint:
    ts:    datetime
    value: float
    meta:  dict[str, Any] | None = None
```

- Internal series storage in `MetricCollector` becomes `dict[str, deque[MetricPoint]]`.
- `get_series()` returns `dict[str, list[MetricPoint]]`.
- `Suite.getMonitorResults()` either returns the same shape or stays on pairs via a cheap projection.
- Tests migrate from `ts, v, meta = point` unpacking to `point.ts`, `point.value`, `point.meta`.
- The dashboard serialization path (collector → JSON for `/api/data`) updates to read named fields.

## Tradeoffs

- **Pro:** Single source of truth for point shape; adding fields is non-breaking.
- **Pro:** Code is self-documenting (`.value` vs. `[1]`).
- **Pro:** With `slots=True` the memory overhead vs. a bare tuple is modest (a few extra bytes per instance from the PyObject header, no per-instance `__dict__`).
- **Con:** For a long run — e.g. 10 hours at 5 s intervals × 20 series = ~144K points — the cumulative overhead is a few MB. Real, but small.
- **Con:** Broad ripple: every consumer of `get_series()` changes signature. External users of `Suite.getMonitorResults` would need to migrate if we propagate the change all the way.

## Affected files

- [src/otto/monitor/collector.py](../src/otto/monitor/collector.py) — define `MetricPoint`; update `_series` type and every append/read site
- [src/otto/suite/suite.py](../src/otto/suite/suite.py) — `getMonitorResults` return shape and docstring
- [tests/unit/monitor/test_collector_db.py](../tests/unit/monitor/test_collector_db.py) — unpack-by-field instead of by-index
- [tests/unit/monitor/test_monitor_import_export.py](../tests/unit/monitor/test_monitor_import_export.py) — same
- Dashboard data serialization path (wherever `/api/data` builds JSON from series) — emit named fields

## Status

Deferred. The lower-effort fix (strip metadata in the `getMonitorResults` wrapper) is already in place. Revisit if a third element gets added to the point shape, or if the pair-vs-triple drift happens again.
