# Registry hygiene — register built-ins through the public entry point

## The asymmetry

Several of otto's string registries load their **built-in** entries by directly
constructing the backing dict at import, while **third-party** entries go through
a `register_*()` function. Storage is shared, but the *insertion path* differs —
otto's own built-ins never exercise the registration entry point users rely on.

Known instances (remaining):

| Registry | Built-ins loaded by | Third-party entry point |
| --- | --- | --- |
| Monitor shell parsers | `DEFAULT_PARSERS = { ... }` literal ([src/otto/monitor/parsers.py](../src/otto/monitor/parsers.py#L318)) | `register_host_parsers()` |

The monitor-parser case has a **different shape**: `register_host_parsers` is
host-scoped and instance-valued (no `register_X(type_name, cls)` path), so
converting it would require a NEW public function — widening the frozen public
surface — and is intentionally deferred.

### Resolved (WS#4)

The three `register_X(type_name, cls)` class registries now register their
built-ins through their own public path (empty seed dict + a
`_register_builtin_*()` bootstrap call at module end):

| Registry | Built-ins now loaded by | Third-party entry point |
| --- | --- | --- |
| Command frames | `_register_builtin_frames()` → `register_command_frame()` ([src/otto/host/command_frame.py](../src/otto/host/command_frame.py)) | `register_command_frame()` |
| Embedded filesystems | `_register_builtin_filesystems()` → `register_filesystem()` ([src/otto/host/embedded_filesystem.py](../src/otto/host/embedded_filesystem.py)) | `register_filesystem()` |
| Binary loaders | `_register_builtin_loaders()` → `register_binary_loader()` ([src/otto/host/binary_loader.py](../src/otto/host/binary_loader.py)) | `register_binary_loader()` |

## Why fix it

First-party and third-party code should travel the **same** path, so otto's own
built-ins are the proof the mechanism works — no divergence, no duplicated logic,
and any validation/normalization added to the registration function applies
uniformly. This is the same symmetry already adopted for `register_host_class`
(host class + spec) and `register_snmp_metric` (SNMP descriptors) in Pydantic
Phase A.

## Scope

Make otto register each built-in through its own `register_*()` function, e.g.:

```python
_FRAME_CLASSES: dict[str, type[CommandFrame]] = {}

def _register_builtin_frames() -> None:
    register_command_frame('bash', BashFrame)
    register_command_frame('zephyr', ZephyrFrame)
    ...

_register_builtin_frames()
```

Mind the registration-order / import-cycle constraints (some registries are read
during settings parse, before init modules import). Keep last-writer-wins
override semantics intact.

## Why it is *not* in Pydantic Phase A

These registries hold **behavior classes** (`CommandFrame.parse()`,
`EmbeddedFileSystem`, `MetricParser.parse()`), not data/boundary types, so they
are not pydantic candidates. Phase A applied the registration symmetry only where
it was *already* converting a registry's value type to pydantic (host specs, SNMP
descriptors). Folding these behavior-class registries in would blur a focused
"pydantic boundary" workstream into a general registry refactor.

## Public read accessor

Plan 6 (JSON Schema export) added `os_profile.registered_host_specs(builtins_only=False)`
as the public read accessor over the private `_HOST_SPECS` registry. A future
registry-hygiene pass should keep and extend this function rather than re-expose
the private dict directly.

## Status

Partially resolved (WS#4): the three `register_X(type_name, cls)` class
registries (command frames, embedded filesystems, binary loaders) now register
built-ins through their public path; only the host-scoped monitor-parser case
remains.

**2026-07-02:** the remaining monitor-parser case should be settled by the
monitor revamp Phase 1 plan
(`docs/superpowers/plans/2026-07-02-monitor-phase1-backend-contract.md`),
which introduces project-level `register_parsers()` — check whether built-in
`DEFAULT_PARSERS` travel that same path when Phase 1 lands, then close this
file.
