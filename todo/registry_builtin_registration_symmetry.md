# Registry hygiene — register built-ins through the public entry point

## The asymmetry

Several of otto's string registries load their **built-in** entries by directly
constructing the backing dict at import, while **third-party** entries go through
a `register_*()` function. Storage is shared, but the *insertion path* differs —
otto's own built-ins never exercise the registration entry point users rely on.

Known instances:

| Registry | Built-ins loaded by | Third-party entry point |
| --- | --- | --- |
| Command frames | `_FRAME_CLASSES = { ... }` literal ([src/otto/host/command_frame.py](../src/otto/host/command_frame.py#L348)) | `register_command_frame()` |
| Embedded filesystems | `_FILESYSTEM_CLASSES = { ... }` literal ([src/otto/host/embedded_filesystem.py](../src/otto/host/embedded_filesystem.py#L170)) | `register_filesystem()` |
| Monitor shell parsers | `DEFAULT_PARSERS = { ... }` literal ([src/otto/monitor/parsers.py](../src/otto/monitor/parsers.py#L318)) | `register_host_parsers()` |

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

## Status

Deferred — picked up after Phase A as a standalone hygiene pass (no contract
impact; the `register_*` signatures and names are unchanged, so it is
semver-internal and can land post-freeze).
