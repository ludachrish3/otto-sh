# Generic `EmbeddedHost` + host-class registry (`ZephyrHost`) — design

**Date:** 2026-06-09
**Status:** Approved for planning
**TODO addressed:** top item of `todo/TODO.md` — *"Neutralize `EmbeddedHost`'s class-level Zephyr defaults."*

## Problem

`EmbeddedHost` hard-codes Zephyr assumptions as class-level defaults:

- `osName: Optional[str] = 'Zephyr'` ([embeddedHost.py:77](../../../src/otto/host/embeddedHost.py#L77))
- `command_frame: CommandFrame = field(default_factory=ZephyrFrame)` ([embeddedHost.py:121](../../../src/otto/host/embeddedHost.py#L121))

So a bare `embedded` host with no declared framing **silently inherits Zephyr** framing. A misconfigured non-Zephyr embedded target produces wrong-but-quiet behavior instead of failing loudly. The OS-profile layer ([os_profile.py](../../../src/otto/host/os_profile.py)) already lets a profile bundle OS-specific defaults, and a built-in `zephyr` profile carries the Zephyr bundle — but the class defaults still make `embedded` mean "Zephyr."

This work makes a bare `EmbeddedHost` **OS-agnostic** (fails loud without a frame) and moves the Zephyr-isms into a concrete, **registerable** `ZephyrHost(EmbeddedHost)` subclass — establishing the pattern by which **external repos register their own host subclasses**, the way they already register command frames and filesystems.

## Goals

1. A bare `EmbeddedHost` carries no OS-specific assumptions; constructing one without a `command_frame` **fails loud** with an actionable error.
2. Zephyr defaults live on a concrete `ZephyrHost(EmbeddedHost)` subclass.
3. otto provides a **host-class registry** so `ZephyrHost` — and external subclasses — are selectable from lab data by name, mirroring `register_command_frame` / `register_filesystem` / `register_os_profile`.
4. A **data bundle** (an `OsProfile`'s `defaults`) can be layered over a **custom host class**, so variants compose (e.g. `zephyr-3.7-fat32` = `ZephyrHost` + a defaults bundle).
5. No data duplication: a single `osType` field is the selector/discriminator; no parallel `osProfile` field.

## Non-goals

- No deprecation window or runtime migration shim. Blast radius is **repo fixtures + in-repo tests only** (confirmed); there is no un-migrated production lab data.
- No new file-transfer backends, command frames, or filesystems.
- `transfer='console'` stays the `EmbeddedHost` default — it is the only implemented backend and is a backend selector, not Zephyr *framing*; neutralizing it is out of scope for this TODO.

## Forward-compatibility note (validated)

`ZephyrHost(EmbeddedHost)` redefines two inherited dataclass field defaults in a `@dataclass(slots=True)` subclass. This idiom was tested against the real `EmbeddedHost` shape (required fields, `default_factory`, `field(init=False)`, `field(repr=False)`) on CPython **3.10, 3.11, 3.12, 3.13, 3.14, and 3.15.0b1**:

| Python | subclass constructs | base fails loud | slots intact (no `__dict__`) | subclass `__slots__` |
|---|---|---|---|---|
| 3.10 | ✅ | ✅ | ✅ | all fields re-listed (cosmetic) |
| 3.11–3.15b1 | ✅ | ✅ | ✅ | `()` (reuses parent slots) |

Correctness, the default override, and fail-loud are identical across versions. The only difference is cosmetic: on 3.10 a `slots=True` subclass re-lists inherited slot names (minor per-instance memory waste); 3.11+ emits an empty `__slots__`. No version-specific branching is needed. (Captured to the wiki inbox for future reference.)

## Section 1 — Field taxonomy & selection

### Fields (on `RemoteHost`)

| Field | Role | Type / values | Source |
|---|---|---|---|
| `osType` | **the selector & stored discriminator** | profile name: `"unix"`, `"embedded"`, `"zephyr"`, `"zephyr-3.7-fat32"`, `"ubuntu-22.04"`, `"myrtos"` | lab data; stamped on host |
| `osName` | OS name (coarse, informational) | `"Linux"`, `"Zephyr"`, … | profile/class default or host |
| `osVersion` | version (informational metadata) | str / None | host or profile default |

- `OsType` widens from `Literal['unix', 'embedded']` to `str` ([remoteHost.py:40](../../../src/otto/host/remoteHost.py#L40)). The registry is extensible, so the type cannot be a closed literal.
- The **base family** (`unix` / `embedded`) is **derived from the Python class** via `isinstance` / `issubclass` wherever needed (only the factory and validation need it, and both hold the class). It is never stored and never duplicated.
- Rationale for `osType`-as-discriminator (vs. a new `osProfile` field): `osName` is too coarse to tell variants apart (one `osName: "Linux"` spans Ubuntu/RHEL/Yocto; one `"Zephyr"` spans 3.7-fat32/4.4-lfs). `osType` already selects the profile, so it is the natural precise discriminator. No second field, no duplication.

### Selection rule (factory)

1. `selector = host_data.get('osType', 'unix')`.
2. `profile = build_os_profile(selector)` → `cls = build_host_class(profile.base)`.
3. Construct via the family builder, dispatched on `issubclass(cls, EmbeddedHost)` vs `issubclass(cls, UnixHost)`, passing the concrete `cls`.
4. Stamp `kwargs['osType'] = selector` (the precise profile name — so a `zephyr-3.7-fat32` host records `osType == 'zephyr-3.7-fat32'`).

Existing `osType: "unix"` / `osType: "embedded"` data keeps working unchanged. A bare `osType: "embedded"` now correctly resolves to the **generic** `EmbeddedHost` and fails loud if no `command_frame` is supplied.

## Section 2 — `EmbeddedHost` goes generic; add `ZephyrHost`

### `EmbeddedHost` ([embeddedHost.py](../../../src/otto/host/embeddedHost.py))

- `osName`: default `'Zephyr'` → `None`.
- `command_frame`: default `field(default_factory=ZephyrFrame)` → `None` (type becomes `CommandFrame | None`). The string→instance coercion in `__post_init__` ([embeddedHost.py:212-214](../../../src/otto/host/embeddedHost.py#L212-L214)) is guarded by `is not None`.
- `__post_init__`: after coercion, if `command_frame is None`, raise `ValueError` — fail loud. The message names the host and points to the fix:

  > `EmbeddedHost {name!r} has no command_frame. A bare 'embedded' host carries no shell-framing dialect. Set osType to a profile that supplies one (e.g. "zephyr"), or pass an explicit command_frame.`

- `osType` default stays `'embedded'`; `transfer='console'` stays.

### `ZephyrHost(EmbeddedHost)` (new, in `embeddedHost.py`)

```python
@dataclass(slots=True)
class ZephyrHost(EmbeddedHost):
    """Concrete Zephyr RTOS host. The worked example for registering a
    host subclass; external repos mirror this for their own RTOS/OS."""
    osType: OsType = 'zephyr'
    osName: Optional[str] = 'Zephyr'
    command_frame: CommandFrame = field(default_factory=ZephyrFrame)
```

Exported from `otto.host` ([`__init__.py`](../../../src/otto/host/__init__.py)).

## Section 3 — Host-class registry + factory

### Registry ([os_profile.py](../../../src/otto/host/os_profile.py))

- New `_HOST_CLASSES: dict[str, type[RemoteHost]]` and `register_host_class(name, cls)`, mirroring `register_command_frame`. Validates `cls` is a `RemoteHost` subclass. Registering a class **also** auto-registers a trivial same-named profile (`OsProfile(name, base=name, defaults={})`), so `osType: "<name>"` works with zero extra config.
- `build_host_class(name)` / `get_host_class(name)` resolve a name to a class (raising / non-raising, matching `build_os_profile` / `get_os_profile`).
- `OsProfile.base` widens from `Literal['unix','embedded']` to "the name of a registered host class." `register_os_profile`'s `base` is validated against `_HOST_CLASSES` instead of the removed `_VALID_BASES`. `_slots_for_base` becomes "union of `__slots__` across the registered class's MRO" (so inherited fields validate).
- Built-ins replace the three current `register_os_profile(...)` calls ([os_profile.py:188-194](../../../src/otto/host/os_profile.py#L188-L194)):
  ```python
  register_host_class('unix', UnixHost)
  register_host_class('embedded', EmbeddedHost)
  register_host_class('zephyr', ZephyrHost)
  ```
  The old **data-only** `zephyr` profile is **deleted** — its defaults now live on the `ZephyrHost` class. `_BUILTIN_NAMES` stays `{'unix','embedded','zephyr'}` for the override-warning logic.
- Import-cycle care: the registry lazily imports the host classes (as `_slots_for_base` already does), or the built-in registrations live at the bottom of the module after a local import, matching the current structure.

### Factory ([factory.py](../../../src/otto/storage/factory.py))

- `create_host_from_dict`: resolve `selector → profile → cls = build_host_class(profile.base)`, then dispatch:
  ```python
  if issubclass(cls, EmbeddedHost):
      return _create_embedded_host(host_data, defaults, profile, cls)
  if issubclass(cls, UnixHost):
      return _create_unix_host(host_data, defaults, profile, cls)
  raise ValueError(f"osType {selector!r} → {cls.__name__} is neither a Unix nor an embedded host")
  ```
  (`EmbeddedHost` and `UnixHost` are disjoint `RemoteHost` siblings, so the two `issubclass` checks are unambiguous.)
- `_create_unix_host` / `_create_embedded_host` take a `cls` parameter and instantiate `cls(**kwargs)` instead of hard-coding `UnixHost` / `EmbeddedHost`. The kwargs filter uses the union of `cls.__slots__` across the MRO (so a subclass's inherited fields are kept). Existing per-family coercion (`*_options`, `toolchain`, `filesystem`, `command_frame`, `snmp`) is unchanged.
- Both builders stamp `kwargs['osType'] = selector` (the profile name) rather than `profile.base`.

## Section 4 — Migration (in scope)

### Lab fixtures — [tests/lab_data/tech1/hosts.json](../../../tests/lab_data/tech1/hosts.json)

The 7 embedded hosts (`sprout`, `sprout_lfs`, `sprout_no_fs`, `sprout27`, `sprout44_lfs`, `sprout_cov`, `sprout_cov44`):

- `osType: "embedded"` → `osType: "zephyr"`.
- Drop the now-redundant inline `osName: "Zephyr"` (supplied by the `ZephyrHost` class).
- Hosts with an explicit `command_frame` (`zephyr-serial`, `zephyr-inline`) keep it — host fields win over class/profile defaults.
- Other fields (`transfer`, `filesystem`, `toolchain`, etc.) unchanged.

### Direct constructions — tests

- ~15 direct `EmbeddedHost(...)` call sites that rely on the implicit Zephyr default ([test_embeddedHost.py](../../../tests/unit/host/test_embeddedHost.py), [test_test.py](../../../tests/unit/cli/test_test.py)) → `ZephyrHost(...)` where Zephyr framing is intended.
- `osType` / `osName` asserts updated to expect the profile name (e.g. `test_repo.py:268` "base family, not the profile name" comment + assert; `test_factory.py` osType asserts; `test_embeddedHost.py:50-51`).
- **New focused test:** a bare generic `EmbeddedHost(ip=..., ne=..., log=False)` with no `command_frame` raises `ValueError` (fail-loud), and the message names the host.
- **New focused test:** `register_host_class` round-trips — a dummy `class FooHost(EmbeddedHost)` registered as `"foo"` is selectable via `osType: "foo"` through the factory, and a data-bundle profile (`register_os_profile('foo-v2', base='foo', defaults={...})`) layers over it.

## Section 5 — Errors, validation, docs

- `validate_host_dict` ([factory.py:286](../../../src/otto/storage/factory.py#L286)) generalizes: resolve the profile from `osType`, derive the family from `issubclass(cls, EmbeddedHost/UnixHost)`, and apply the existing family-specific required-field/type checks against the effective dict. The unknown-`osType` error already lists registered profiles.
- Docstrings: update the `os_profile.py` module doc (selector is `osType`, profiles select a registered class); `EmbeddedHost` class doc (now OS-agnostic; fails loud without a frame); new `ZephyrHost` doc; and a short **"Registering a custom host class"** subsection documenting the external pattern (`class MyHost(EmbeddedHost)` + `register_host_class('myos', MyHost)` from a settings.toml init module, optionally `register_os_profile('myos-variant', base='myos', defaults={...})`).

## Testing strategy

- Unit: registry round-trip (register/build/get/auto-profile); `EmbeddedHost` fail-loud; `ZephyrHost` carries Zephyr defaults; factory dispatch for `unix`/`embedded`/`zephyr`/custom; data-bundle-over-custom-class composition; `validate_host_dict` for each family and unknown `osType`.
- Migration: the existing embedded/Zephyr suites (`test_embeddedHost`, `test_zephyr`, `test_embedded_transfer`, the coverage tests in `test_test.py`, `test_factory`, `test_repo`) pass against `ZephyrHost`.
- Forward-compat: covered by the validated 3.10→3.15 matrix above; no per-version test added.

## Risks & mitigations

- **Hidden direct constructions** beyond the ~15 found → grep `EmbeddedHost(` across `src`/`tests` during implementation; any that intend Zephyr move to `ZephyrHost`.
- **`OsProfile.base` widening** could let a profile name a non-host class → `register_host_class` validates `issubclass(cls, RemoteHost)`; `register_os_profile` validates `base in _HOST_CLASSES`.
- **Cosmetic 3.10 duplicate slots** → accepted; vanishes when 3.10 support is dropped. No action.
