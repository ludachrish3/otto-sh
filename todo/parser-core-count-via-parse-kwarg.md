# Pass `core_count` to `MetricParser.parse()` as a keyword argument

## Motivation

Currently, `MetricCollector` mutates parser instances by assigning `parser.core_count = target.core_count` at startup ([collector.py:322-325](../src/otto/monitor/collector.py#L322-L325)). Only `TopCpuParser` uses the value, but the base class carries the attribute so every parser has a harmlessly-unused field, and the collector reaches into parser state to configure it.

A cleaner contract is to make `core_count` an explicit input to the parse step: parsers do not hold per-host configuration state, and the collector passes it in when invoking `parse()`.

## Proposed change

Change the abstract signature on `MetricParser`:

```python
@abstractmethod
def parse(self, output: str, *, core_count: int = 1) -> dict[str, MetricDataPoint]: ...
```

- `TopCpuParser.parse()` consumes `core_count` directly; no instance attribute needed.
- All other subclasses ignore the kwarg.
- `MetricCollector` drops the setup-time mutation loop; instead it threads `target.core_count` into the `parser.parse(cmd_status.output, core_count=target.core_count)` call inside `_process_host_results`.
- Drop the `core_count: int = 1` field from `MetricParser` base class.

## Tradeoffs

- **Pro:** No more mutation-based coupling between collector and parser; parsers are pure functions of their input + tick-local configuration.
- **Pro:** Parsers become safely shareable across hosts even when those hosts have different core counts.
- **Con:** Adds a kwarg to the hot-path `parse()` call (every tick × every parser × every host). Overhead is nanoseconds per call — negligible in practice — but unlike the current design this is per-tick, not one-time setup.
- **Con:** Every `MetricParser` subclass's `parse()` signature must be updated, even if they ignore the new kwarg. Breaking change for any external parser.

## Affected files

- [src/otto/monitor/parsers.py](../src/otto/monitor/parsers.py) — abstract signature + `TopCpuParser.parse()` + drop base-class `core_count`
- [src/otto/monitor/collector.py](../src/otto/monitor/collector.py) — remove startup mutation loop; pass `core_count` into `parse()` inside `_process_host_results`
- Every downstream `MetricParser` subclass in user code

## Status

Deferred. The lower-effort fix (promote `core_count: int = 1` to base class and drop the `hasattr` guard) is already in place. Revisit if more per-host parser configuration gets added and the mutation pattern becomes untenable.
