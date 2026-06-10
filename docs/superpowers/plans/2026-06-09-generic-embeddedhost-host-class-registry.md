# Generic `EmbeddedHost` + host-class registry (`ZephyrHost`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Commits are run by Chris, not the agent.** This repo's `prepare-commit-msg` hook needs `/dev/tty`; agent-run commits mis-tag AI attribution. Each task ends with a paste-able commit command — surface it and let Chris run it. Do **not** run `git commit` yourself.

**Goal:** Make a bare `EmbeddedHost` OS-agnostic (fails loud without a `command_frame`), move Zephyr defaults onto a concrete `ZephyrHost(EmbeddedHost)`, and add a host-class registry so `ZephyrHost` and external subclasses are selectable from lab data — with data-bundle `OsProfile`s composable over any registered class.

**Architecture:** A new name→class registry (`_HOST_CLASSES` / `register_host_class`) sits beside the existing `OsProfile` registry; `OsProfile.base` widens from `Literal['unix','embedded']` to "a registered host-class name." The storage factory resolves `osType → profile → class`, dispatches construction by `issubclass(cls, EmbeddedHost/UnixHost)`, and stamps the selector onto `host.osType`. `osType` becomes the single profile selector/discriminator (open `str`); the base family is derived from the Python class, never stored.

**Tech Stack:** Python 3.10+ (`@dataclass(slots=True)`), pytest, the otto host/storage modules.

**Spec:** [docs/superpowers/specs/2026-06-09-generic-embeddedhost-host-class-registry-design.md](../specs/2026-06-09-generic-embeddedhost-host-class-registry-design.md)

---

## File Structure

- `src/otto/host/os_profile.py` — **modify.** Add the host-class registry (`_HOST_CLASSES`, `register_host_class`, `build_host_class`, `get_host_class`); widen `OsProfile.base`; replace `_VALID_BASES`/`BaseFamily`; add an MRO-union slots helper; re-do built-in registrations.
- `src/otto/host/embeddedHost.py` — **modify.** Make `EmbeddedHost` generic + fail-loud; add `ZephyrHost`.
- `src/otto/host/remoteHost.py` — **modify.** Widen `OsType` to `str`.
- `src/otto/host/__init__.py` — **modify.** Export `ZephyrHost`, `register_host_class`, `build_host_class`, `get_host_class`.
- `src/otto/storage/factory.py` — **modify.** Class-based dispatch; parameterize builders by `cls`; stamp `osType=selector`; class-derived family in `validate_host_dict`.
- `src/otto/configmodule/repo.py` — **modify (docstrings only).** `[os_profiles].base` may now name any registered class.
- `tests/lab_data/tech1/hosts.json` — **modify.** Embedded hosts `osType: "embedded"` → `"zephyr"`, drop inline `osName`.
- `tests/unit/host/test_os_profile.py` — **create/modify.** Registry round-trip + compose tests. (If a registry test module already exists for os_profile, add to it; otherwise create.)
- `tests/unit/host/test_embeddedHost.py` — **modify.** Generic fail-loud + `ZephyrHost`; migrate direct constructions; fix asserts.
- `tests/unit/storage/test_factory.py` — **modify.** Zephyr dispatch, fail-loud, custom-class registration; fix asserts.
- `tests/unit/configmodule/test_repo.py` — **modify.** Fix the `osType` assert (selector, not family).
- `tests/unit/cli/test_test.py` — **modify.** Coverage-test `EmbeddedHost(...)` → `ZephyrHost(...)`.

> Before starting, capture a baseline: `make test` or `pytest tests/unit -q` should be green. Note any pre-existing failures so they aren't blamed on this work.

---

## Task 1: Host-class registry (additive; behavior-preserving)

Add the registry and generalize `OsProfile.base` validation. No selection behavior changes yet — built-in bases stay `unix`/`embedded`.

**Files:**
- Modify: `src/otto/host/os_profile.py`
- Modify: `src/otto/host/__init__.py`
- Test: `tests/unit/host/test_os_profile.py`

- [ ] **Step 1: Write failing tests for the registry**

Add to `tests/unit/host/test_os_profile.py` (create the file with this header if it doesn't exist):

```python
import pytest

from otto.host.os_profile import (
    build_host_class,
    build_os_profile,
    get_host_class,
    register_host_class,
    register_os_profile,
)
from otto.host.embeddedHost import EmbeddedHost
from otto.host.unixHost import UnixHost


def test_builtin_host_classes_registered():
    assert build_host_class('unix') is UnixHost
    assert build_host_class('embedded') is EmbeddedHost


def test_register_host_class_round_trips_and_autoregisters_profile():
    class FooHost(EmbeddedHost):
        pass

    register_host_class('foo', FooHost)
    try:
        assert build_host_class('foo') is FooHost
        # registering a class also makes osType:"foo" resolvable as a profile
        prof = build_os_profile('foo')
        assert prof.base == 'foo'
        assert prof.defaults == {}
    finally:
        # registries are module-global; clean up so other tests are unaffected
        from otto.host import os_profile as _m
        _m._HOST_CLASSES.pop('foo', None)
        _m._OS_PROFILES.pop('foo', None)


def test_get_host_class_missing_returns_none():
    assert get_host_class('does-not-exist') is None


def test_register_host_class_rejects_non_remotehost():
    with pytest.raises(ValueError, match='RemoteHost'):
        register_host_class('bad', dict)  # type: ignore[arg-type]


def test_register_os_profile_base_must_be_registered_class():
    with pytest.raises(ValueError, match='base'):
        register_os_profile('bogus', base='not-a-class', defaults={})


def test_profile_defaults_validated_against_subclass_inherited_fields():
    # max_filename_len is an EmbeddedHost field; a profile over 'embedded'
    # must accept it (MRO-union slots), not reject it as unknown.
    register_os_profile('emb-variant', base='embedded',
                        defaults={'max_filename_len': 32})
    try:
        assert build_os_profile('emb-variant').defaults['max_filename_len'] == 32
    finally:
        from otto.host import os_profile as _m
        _m._OS_PROFILES.pop('emb-variant', None)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/unit/host/test_os_profile.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_host_class'` (and friends).

- [ ] **Step 3: Add the registry to `os_profile.py`**

In `src/otto/host/os_profile.py`, replace the `BaseFamily` alias + `_VALID_BASES` block (lines 36–45) with a registry. Replace:

```python
BaseFamily = Literal['unix', 'embedded']
"""... (existing docstring) ..."""

_VALID_BASES: frozenset[str] = frozenset(('unix', 'embedded'))
```

with:

```python
BaseFamily = str
"""The name of a registered host class an :class:`OsProfile` builds.

Built-ins: ``unix`` (:class:`~otto.host.unixHost.UnixHost`), ``embedded``
(:class:`~otto.host.embeddedHost.EmbeddedHost`), ``zephyr``
(:class:`~otto.host.embeddedHost.ZephyrHost`). Register more with
:func:`register_host_class`.
"""

# Registry of host-class name -> class, mirroring ``_OS_PROFILES`` /
# ``command_frame._FRAME_CLASSES``. Populated for built-ins at module load.
_HOST_CLASSES: dict[str, type] = {}
```

Change `OsProfile.base`'s docstring (line 62–63) to:

```python
    base: BaseFamily
    """Name of the registered host class the profile builds (e.g. ``unix``,
    ``embedded``, ``zephyr``, or a custom class registered via
    :func:`register_host_class`)."""
```

Replace `_slots_for_base` (lines 74–86) with an MRO-aware pair:

```python
def _all_slots(cls: type) -> frozenset[str]:
    """All settable field names of *cls*, gathered across its MRO.

    A ``@dataclass(slots=True)`` subclass reports an *empty* ``__slots__`` on
    Python 3.11+ (it reuses the parent's slots), so a single-class lookup would
    miss inherited fields. The union over the MRO is what the storage factory
    filters host/profile dicts against.
    """
    names: set[str] = set()
    for klass in cls.__mro__:
        names.update(getattr(klass, '__slots__', ()))
    return frozenset(names)


def _slots_for_base(base: str) -> frozenset[str]:
    """Return the settable field names for the host class named *base*."""
    return _all_slots(build_host_class(base))
```

Add the registration/lookup functions (place after `register_os_profile`, before `build_os_profile`):

```python
def register_host_class(name: str, cls: type) -> None:
    """Register a host class so lab data can select it by ``osType``.

    Mirrors :func:`otto.host.command_frame.register_command_frame`. Call from
    an init module listed in ``.otto/settings.toml`` to ship a custom host
    subclass. Registering a class also registers a trivial same-named
    :class:`OsProfile` (``base=name``, empty ``defaults``), so ``osType: name``
    resolves with no extra config. Re-registering replaces the prior class.

    Raises
    ------
    ValueError
        If *cls* is not a :class:`~otto.host.remoteHost.RemoteHost` subclass.
    """
    from .remoteHost import RemoteHost
    if not (isinstance(cls, type) and issubclass(cls, RemoteHost)):
        raise ValueError(
            f"register_host_class({name!r}): cls must be a RemoteHost "
            f"subclass, got {cls!r}"
        )
    _HOST_CLASSES[name] = cls
    # Auto-register a selector profile so osType:<name> works immediately.
    _OS_PROFILES[name] = OsProfile(name=name, base=name, defaults={})


def build_host_class(name: str) -> type:
    """Return the host class registered under *name* (raising on miss)."""
    try:
        return _HOST_CLASSES[name]
    except KeyError:
        known = ', '.join(sorted(_HOST_CLASSES))
        raise ValueError(
            f"Unknown host class {name!r}. Registered: {known}. "
            f"Add one via register_host_class()."
        ) from None


def get_host_class(name: str) -> type | None:
    """Return the host class for *name*, or ``None`` (non-raising)."""
    return _HOST_CLASSES.get(name)
```

In `register_os_profile`, replace the base check (lines 117–121):

```python
    if base not in _VALID_BASES:
        raise ValueError(
            f"register_os_profile({name!r}): base must be one of "
            f"{sorted(_VALID_BASES)}, got {base!r}"
        )
```

with:

```python
    if base not in _HOST_CLASSES:
        known = ', '.join(sorted(_HOST_CLASSES))
        raise ValueError(
            f"register_os_profile({name!r}): base must name a registered "
            f"host class (one of {known}), got {base!r}"
        )
```

Remove the now-unused `cast`/`BaseFamily` narrowing comment at lines 137–139 and simplify the final line to:

```python
    _OS_PROFILES[name] = OsProfile(name=name, base=base, defaults=defaults)
```

Finally, replace the built-in registration block (lines 188–194):

```python
register_os_profile('unix', base='unix')
register_os_profile('embedded', base='embedded')
register_os_profile(
    'zephyr',
    base='embedded',
    defaults={'osName': 'Zephyr', 'command_frame': 'zephyr', 'transfer': 'console'},
)
```

with (Task 1 registers only `unix`/`embedded`; `zephyr` becomes a class in Task 3):

```python
def _register_builtin_host_classes() -> None:
    """Register the built-in host classes. Imported lazily to avoid an import
    cycle (the host modules do not import this one at module top)."""
    from .unixHost import UnixHost
    from .embeddedHost import EmbeddedHost
    register_host_class('unix', UnixHost)
    register_host_class('embedded', EmbeddedHost)


_register_builtin_host_classes()
```

Keep `_BUILTIN_NAMES` as `frozenset(('unix', 'embedded', 'zephyr'))`. Drop the now-unused `cast` import if nothing else uses it (check first).

- [ ] **Step 4: Export the new functions**

In `src/otto/host/__init__.py`, after line 31 (`register_os_profile`), add:

```python
from .os_profile import register_host_class as register_host_class
from .os_profile import build_host_class as build_host_class
from .os_profile import get_host_class as get_host_class
```

- [ ] **Step 5: Run the registry tests + the import smoke check**

Run: `python -c "import otto.host"` (Expected: no error — confirms no import cycle.)
Run: `pytest tests/unit/host/test_os_profile.py -q`
Expected: PASS.

- [ ] **Step 6: Run the existing host/storage suites to confirm no regression**

Run: `pytest tests/unit/host tests/unit/storage tests/unit/configmodule -q`
Expected: PASS (no selection behavior changed yet).

- [ ] **Step 7: Commit (Chris runs)**

```bash
git add src/otto/host/os_profile.py src/otto/host/__init__.py tests/unit/host/test_os_profile.py && \
git commit -m "feat(host): add host-class registry; OsProfile.base names a class"
```

---

## Task 2: Factory resolves a class & dispatches by `issubclass` (behavior-preserving refactor)

Parameterize the builders by the concrete class, derive the family from the class, and stamp `osType = selector`. Bases are still `unix`/`embedded`, so the only observable change is that a host built via a *profile whose name differs from its base* now records `osType = <profile name>` (the `zephyr-2.7` data-profile test).

**Files:**
- Modify: `src/otto/storage/factory.py`
- Test: `tests/unit/storage/test_factory.py`, `tests/unit/configmodule/test_repo.py`

- [ ] **Step 1: Update the data-profile test to expect the selector (failing assert first)**

In `tests/unit/configmodule/test_repo.py`, change lines 268 from:

```python
        assert host.osType == 'embedded'      # base family, not the profile name
```

to:

```python
        assert host.osType == 'zephyr-2.7'    # the profile selector is recorded
        assert isinstance(host, EmbeddedHost)  # family derived from the class
```

- [ ] **Step 2: Run it, verify it fails**

Run: `pytest tests/unit/configmodule/test_repo.py -q -k profile`
Expected: FAIL — `assert 'embedded' == 'zephyr-2.7'`.

- [ ] **Step 3: Refactor `create_host_from_dict` dispatch**

In `src/otto/storage/factory.py`, update the import line (line 19) to include the class resolver:

```python
from ..host.os_profile import (
    OsProfile,
    build_host_class,
    build_os_profile,
    get_host_class,
    get_os_profile,
    registered_profile_names,
)
```

Replace the body of `create_host_from_dict` (lines 161–165):

```python
    os_type = host_data.get('osType', 'unix')
    profile = build_os_profile(os_type)
    if profile.base == 'unix':
        return _create_unix_host(host_data, defaults, profile)
    return _create_embedded_host(host_data, defaults, profile)
```

with:

```python
    selector = host_data.get('osType', 'unix')
    profile = build_os_profile(selector)
    cls = build_host_class(profile.base)
    if issubclass(cls, EmbeddedHost):
        return _create_embedded_host(host_data, defaults, profile, cls, selector)
    if issubclass(cls, UnixHost):
        return _create_unix_host(host_data, defaults, profile, cls, selector)
    raise ValueError(
        f"osType {selector!r} resolves to {cls.__name__}, which is neither a "
        f"Unix nor an embedded host"
    )
```

- [ ] **Step 4: Parameterize `_create_unix_host` by class + selector**

Change its signature (line 168–172) to:

```python
def _create_unix_host(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
    profile: OsProfile | None = None,
    cls: type[UnixHost] = UnixHost,
    selector: str = 'unix',
) -> UnixHost:
```

Change the kwargs filter (line 185) from `if k in UnixHost.__slots__` to use the MRO union:

```python
    from ..host.os_profile import _all_slots
    kwargs = { k: v for k, v in effective.items() if k in _all_slots(cls) }
```

Change the stamp + construct (lines 213–214) from:

```python
    kwargs['osType'] = profile.base if profile else 'unix'
    return UnixHost(**kwargs)
```

to:

```python
    kwargs['osType'] = selector
    return cls(**kwargs)
```

- [ ] **Step 5: Parameterize `_create_embedded_host` by class + selector**

Change its signature (line 217–221) to:

```python
def _create_embedded_host(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
    profile: OsProfile | None = None,
    cls: type[EmbeddedHost] = EmbeddedHost,
    selector: str = 'embedded',
) -> EmbeddedHost:
```

Change the kwargs filter (line 244) from `if k in EmbeddedHost.__slots__` to:

```python
    from ..host.os_profile import _all_slots
    kwargs = { k: v for k, v in effective.items() if k in _all_slots(cls) }
```

Change the stamp + construct (lines 282–283) from:

```python
    kwargs['osType'] = profile.base if profile else 'embedded'
    return EmbeddedHost(**kwargs)
```

to:

```python
    kwargs['osType'] = selector
    return cls(**kwargs)
```

- [ ] **Step 6: Class-derive the family in `validate_host_dict`**

Replace lines 313–321 (the `os_type`/`profile`/`base` resolution):

```python
    os_type = host_data.get('osType', 'unix')
    profile = get_os_profile(os_type)
    if profile is None:
        known = ', '.join(registered_profile_names())
        raise ValueError(
            f"Field 'osType' {os_type!r} is not a registered profile. "
            f"Registered profiles: {known}"
        )
    base = profile.base
```

with:

```python
    os_type = host_data.get('osType', 'unix')
    profile = get_os_profile(os_type)
    if profile is None:
        known = ', '.join(registered_profile_names())
        raise ValueError(
            f"Field 'osType' {os_type!r} is not a registered profile. "
            f"Registered profiles: {known}"
        )
    cls = get_host_class(profile.base)
    base = 'embedded' if (cls is not None and issubclass(cls, EmbeddedHost)) else 'unix'
```

(The rest of `validate_host_dict` keys off `base == 'embedded'`, unchanged.)

- [ ] **Step 7: Run factory + repo + host suites**

Run: `pytest tests/unit/storage/test_factory.py tests/unit/configmodule/test_repo.py tests/unit/host -q`
Expected: PASS (the `zephyr-2.7` assert now holds; everything else unchanged).

- [ ] **Step 8: Commit (Chris runs)**

```bash
git add src/otto/storage/factory.py tests/unit/configmodule/test_repo.py && \
git commit -m "refactor(storage): class-based host dispatch; stamp osType selector"
```

---

## Task 3: Add `ZephyrHost`; register it; retire the data-only `zephyr` profile

`EmbeddedHost` still has the `ZephyrFrame` default here — so existing `osType: "embedded"` data keeps working. This task only adds the concrete class and makes `osType: "zephyr"` build it.

**Files:**
- Modify: `src/otto/host/embeddedHost.py`
- Modify: `src/otto/host/os_profile.py`
- Modify: `src/otto/host/__init__.py`
- Test: `tests/unit/storage/test_factory.py`

- [ ] **Step 1: Write a failing test for Zephyr dispatch**

Add to `tests/unit/storage/test_factory.py` (in the dispatch test class near line 320):

```python
    def test_zephyr_ostype_builds_zephyr_host(self):
        from otto.host.embeddedHost import ZephyrHost
        from otto.host.command_frame import ZephyrFrame
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'ne': 'sprout', 'osType': 'zephyr',
        })
        assert isinstance(host, ZephyrHost)
        assert isinstance(host, EmbeddedHost)       # family still embedded
        assert host.osType == 'zephyr'              # selector recorded
        assert host.osName == 'Zephyr'              # from the class default
        assert isinstance(host.command_frame, ZephyrFrame)
```

- [ ] **Step 2: Run it, verify it fails**

Run: `pytest tests/unit/storage/test_factory.py -q -k zephyr_ostype_builds`
Expected: FAIL — `ImportError: cannot import name 'ZephyrHost'`.

- [ ] **Step 3: Add `ZephyrHost` to `embeddedHost.py`**

At the end of `src/otto/host/embeddedHost.py`, after the `EmbeddedHost` class, add:

```python
@dataclass(slots=True)
class ZephyrHost(EmbeddedHost):
    """A Zephyr RTOS host — the concrete, registered embedded host.

    This is the worked example for shipping a host subclass: it re-declares the
    Zephyr-specific field defaults that :class:`EmbeddedHost` no longer assumes,
    and is registered under ``osType: "zephyr"`` via
    :func:`otto.host.os_profile.register_host_class`. External repositories
    register their own ``EmbeddedHost``/``UnixHost`` subclasses the same way
    (from an init module listed in ``.otto/settings.toml``), and may layer
    per-build ``OsProfile`` data bundles over them.
    """

    osType: OsType = 'zephyr'
    """Profile selector recorded on the host. ``zephyr`` for this class."""

    osName: Optional[str] = 'Zephyr'
    """Kernel/OS name — ``Zephyr`` for this class."""

    command_frame: CommandFrame = field(default_factory=ZephyrFrame)
    """Stock Zephyr ``retval`` shell framing (3.7 / 4.4 LTS)."""
```

(`OsType`, `CommandFrame`, `ZephyrFrame`, `Optional`, `dataclass`, `field` are all already imported at the top of this module.)

- [ ] **Step 4: Register `ZephyrHost`; drop the data `zephyr` profile**

In `src/otto/host/os_profile.py`, update `_register_builtin_host_classes` to also register Zephyr:

```python
def _register_builtin_host_classes() -> None:
    """Register the built-in host classes. Imported lazily to avoid an import
    cycle (the host modules do not import this one at module top)."""
    from .unixHost import UnixHost
    from .embeddedHost import EmbeddedHost, ZephyrHost
    register_host_class('unix', UnixHost)
    register_host_class('embedded', EmbeddedHost)
    register_host_class('zephyr', ZephyrHost)
```

(There is no longer a separate `register_os_profile('zephyr', ...)` call — `register_host_class('zephyr', ZephyrHost)` auto-registers the `zephyr` selector profile, and the Zephyr defaults live on the class.)

- [ ] **Step 5: Export `ZephyrHost`**

In `src/otto/host/__init__.py`, after line 14 (`EmbeddedHost`), add:

```python
from .embeddedHost import ZephyrHost as ZephyrHost
```

- [ ] **Step 6: Run the new test + smoke import + regression**

Run: `python -c "import otto.host; from otto.host import ZephyrHost; print(ZephyrHost.__mro__[1].__name__)"`
Expected: prints `EmbeddedHost`.
Run: `pytest tests/unit/storage/test_factory.py tests/unit/host tests/unit/configmodule -q`
Expected: PASS (existing `osType:"embedded"` tests still pass — EmbeddedHost still defaults to ZephyrFrame at this stage).

- [ ] **Step 7: Commit (Chris runs)**

```bash
git add src/otto/host/embeddedHost.py src/otto/host/os_profile.py src/otto/host/__init__.py tests/unit/storage/test_factory.py && \
git commit -m "feat(host): add ZephyrHost; register zephyr as a host class"
```

---

## Task 4: Migrate lab fixtures to `osType: "zephyr"`

Safe now that `osType: "zephyr"` builds `ZephyrHost` (frame from the class). Do this *before* flipping the `EmbeddedHost` default so the fixtures never pass through a frame-less window.

**Files:**
- Modify: `tests/lab_data/tech1/hosts.json`
- Test: `tests/unit/storage/test_factory.py`, `tests/unit/configmodule/test_repo.py`, and any integration test loading `tech1`.

- [ ] **Step 1: Edit the 7 embedded hosts in `tests/lab_data/tech1/hosts.json`**

For each host currently `"osType": "embedded"` (the `sprout*` entries at lines ~83, 113, 143, 167, 186, 216, 245): change `"osType": "embedded"` → `"osType": "zephyr"` and **delete** the adjacent `"osName": "Zephyr",` line (now supplied by `ZephyrHost`). Leave `transfer`, `filesystem`, `command_frame` (where present), `toolchain`, and all other fields untouched.

Example — the `sprout` entry becomes:

```json
        "ne": "sprout",
        "osType": "zephyr",
        "is_virtual": true,
        "transfer": "console",
        "filesystem": "fat-ram",
```

- [ ] **Step 2: Find tests asserting these hosts' `osType`/`osName`**

Run: `grep -rn "osType == 'embedded'\|osName == 'Zephyr'" tests --include=*.py`
For any assertion that targets a `tech1` `sprout*` host, change `'embedded'` → `'zephyr'`. (Asserts on *directly-constructed* generic `EmbeddedHost`es are handled in Task 5 — only update lab-data-loaded ones here.)

- [ ] **Step 3: Run the suites that load lab data**

Run: `pytest tests/unit/storage tests/unit/configmodule -q`
Expected: PASS.
Run (if present): `pytest tests/integration -q -k tech1` or the project's lab-data integration target.
Expected: PASS.

- [ ] **Step 4: Commit (Chris runs)**

```bash
git add tests/lab_data/tech1/hosts.json tests/unit && \
git commit -m "test(lab): migrate tech1 embedded hosts to osType zephyr"
```

---

## Task 5: Make `EmbeddedHost` generic + fail-loud; migrate direct constructions

The breaking flip. Drop the Zephyr class defaults, fail loud without a frame, and move every direct `EmbeddedHost(...)` that wanted Zephyr framing to `ZephyrHost(...)`.

**Files:**
- Modify: `src/otto/host/embeddedHost.py`
- Modify: `src/otto/host/remoteHost.py`
- Test: `tests/unit/host/test_embeddedHost.py`, `tests/unit/cli/test_test.py`, `tests/unit/storage/test_factory.py`

- [ ] **Step 1: Write the fail-loud test (and the generic-with-frame test)**

Add to `tests/unit/host/test_embeddedHost.py`:

```python
from otto.host.command_frame import ZephyrFrame  # add to imports if absent


class TestGenericEmbeddedFailsLoud:
    def test_no_command_frame_raises(self):
        with pytest.raises(ValueError, match='command_frame'):
            EmbeddedHost(ip='192.0.2.1', ne='sprout', log=False)

    def test_explicit_frame_builds_generic_embedded(self):
        h = EmbeddedHost(
            ip='192.0.2.1', ne='sprout', log=False,
            command_frame=ZephyrFrame(),
        )
        h._connections = None  # type: ignore[assignment]
        assert h.osName is None          # generic: no implicit OS name
        assert h.osType == 'embedded'
        assert isinstance(h.command_frame, ZephyrFrame)
```

- [ ] **Step 2: Run them, verify the fail-loud test fails**

Run: `pytest tests/unit/host/test_embeddedHost.py -q -k GenericEmbeddedFailsLoud`
Expected: FAIL — `test_no_command_frame_raises` does NOT raise (default frame still present).

- [ ] **Step 3: Widen `OsType`**

In `src/otto/host/remoteHost.py`, change line 40:

```python
OsType = Literal['unix', 'embedded']
```

to:

```python
OsType = str
```

and update its docstring (lines 41–46) to:

```python
"""Profile selector recorded on a host (the ``osType`` field).

Built-ins: ``unix`` (:class:`UnixHost`), ``embedded`` (generic
:class:`EmbeddedHost`), ``zephyr`` (:class:`ZephyrHost`). Custom profiles add
more names. The base *family* (unix vs embedded) is derived from the host
class, not from this string.
"""
```

Drop the now-unused `Literal` import from `remoteHost.py` only if nothing else there uses it (check first).

- [ ] **Step 4: Make `EmbeddedHost` generic + fail-loud**

In `src/otto/host/embeddedHost.py`:

Change `osName` (line 77):

```python
    osName: Optional[str] = 'Zephyr'
    """Kernel/OS name. Defaults to ``Zephyr``, the first supported RTOS."""
```

to:

```python
    osName: Optional[str] = None
    """Kernel/OS name, or None. A bare ``embedded`` host carries no OS name;
    a concrete subclass (e.g. :class:`ZephyrHost`) sets it."""
```

Change `command_frame` (line 121):

```python
    command_frame: CommandFrame = field(default_factory=ZephyrFrame)
```

to:

```python
    command_frame: Optional[CommandFrame] = None
```

and trim its docstring's "Defaults to the stock Zephyr ..." sentence to note there is **no** default — a frame is required (via a profile/subclass or an explicit value).

In `__post_init__`, the `command_frame` string-coercion block (lines 211–214) is currently:

```python
        # Same for ``command_frame`` — lab JSON declares the dialect by name.
        if isinstance(self.command_frame, str):
            from .command_frame import build_command_frame
            self.command_frame = build_command_frame(self.command_frame)
```

Add the fail-loud guard immediately after it:

```python
        # A bare 'embedded' host carries no shell-framing dialect. Fail loud
        # rather than silently inheriting one, so a misconfigured non-Zephyr
        # host is caught at construction, not at first command.
        if self.command_frame is None:
            raise ValueError(
                f"EmbeddedHost {self.name!r} has no command_frame. A bare "
                f"'embedded' host carries no shell-framing dialect. Set osType "
                f"to a profile that supplies one (e.g. \"zephyr\"), or pass an "
                f"explicit command_frame."
            )
```

(`self.name` is assigned earlier in `__post_init__` at line 201–202, so it is set here.)

The `_session_mgr` construction (line 247, `command_frame=self.command_frame`) is unchanged — by this point `command_frame` is guaranteed non-None.

- [ ] **Step 5: Run the fail-loud tests**

Run: `pytest tests/unit/host/test_embeddedHost.py -q -k GenericEmbeddedFailsLoud`
Expected: PASS.

- [ ] **Step 6: Migrate `test_embeddedHost.py` direct constructions**

The shared `host` fixture (lines 21–29) and the standalone constructions now fail loud (no frame). Repoint those that want a working Zephyr host to `ZephyrHost`.

Change the fixture (lines 21–24):

```python
@pytest.fixture
def host():
    """Bare EmbeddedHost, no connections established."""
    h = EmbeddedHost(ip='192.0.2.1', ne='sprout', log=False)
```

to:

```python
@pytest.fixture
def host():
    """Bare Zephyr host, no connections established."""
    h = ZephyrHost(ip='192.0.2.1', ne='sprout', log=False)
```

Add `ZephyrHost` to the import at line 15:

```python
from otto.host import EmbeddedHost, RemoteHost, ZephyrHost
```

Update `test_os_schema_defaults` (lines 49–52):

```python
    def test_os_schema_defaults(self, host: EmbeddedHost):
        assert host.osType == 'embedded'
        assert host.osName == 'Zephyr'
        assert host.osVersion is None
```

to:

```python
    def test_os_schema_defaults(self, host: ZephyrHost):
        assert host.osType == 'zephyr'
        assert host.osName == 'Zephyr'
        assert host.osVersion is None
```

For the remaining standalone `EmbeddedHost(ip='192.0.2.1', ne=..., log=False, ...)` constructions in this file (the id/name/hop/telnet/transfer/default_dest_dir tests at lines ~55, 68, 82, 87, 92, 107, 160, 204, 212, 220, 230), replace `EmbeddedHost(` with `ZephyrHost(` **unless** the test already passes an explicit `command_frame`. These tests assert id/name/transfer/path behavior that `ZephyrHost` inherits unchanged, so only the constructor name changes.

Run: `grep -n 'EmbeddedHost(' tests/unit/host/test_embeddedHost.py` and convert each remaining frame-less site to `ZephyrHost(`. Keep `test_os_schema_overrides` (line 55) explicit `osName='Zephyr'` — it still passes on `ZephyrHost`.

- [ ] **Step 7: Migrate `test_test.py` coverage constructions**

In `tests/unit/cli/test_test.py`, the coverage hosts at lines ~736, 776, 825, 945, 951 construct `EmbeddedHost(... transfer='console', toolchain=...)` with no frame. Change each `EmbeddedHost(` → `ZephyrHost(` and update imports:

- Line 773 `from otto.host.embeddedHost import EmbeddedHost` → `from otto.host.embeddedHost import ZephyrHost` (and any other local import of `EmbeddedHost` in this file used only for these constructions; if `EmbeddedHost` is still referenced elsewhere in the file, import both).

Run: `grep -n 'EmbeddedHost' tests/unit/cli/test_test.py` to confirm every construction site is converted and imports resolve.

- [ ] **Step 8: Fix the generic-embedded factory test**

In `tests/unit/storage/test_factory.py`, `test_embedded_ostype_builds_embedded_host` (lines 337–344) now fails loud (`osType:"embedded"` with no frame). Change it to supply a frame and assert the generic host:

```python
    def test_embedded_ostype_builds_embedded_host(self):
        host = create_host_from_dict({
            'ip': '192.0.2.1', 'ne': 'sprout', 'osType': 'embedded',
            'command_frame': 'zephyr',
        })
        assert isinstance(host, EmbeddedHost)
        assert not isinstance(host, ZephyrHost)   # the generic base, not Zephyr
        assert host.osType == 'embedded'
        assert host.osName is None                # generic: no implicit OS name

    def test_embedded_ostype_without_frame_fails_loud(self):
        with pytest.raises(ValueError, match='command_frame'):
            create_host_from_dict({
                'ip': '192.0.2.1', 'ne': 'sprout', 'osType': 'embedded',
            })
```

Add `ZephyrHost` to this file's imports (alongside `EmbeddedHost`). Check `test_embedded_creds_are_optional` (line 346) and any other `osType:"embedded"` construction in this file — add `'command_frame': 'zephyr'` where the test needs a *built* host (not a validation-only check), or switch it to `osType:"zephyr"` if the intent is just "an embedded host."

- [ ] **Step 9: Run the full host/storage/cli suites**

Run: `pytest tests/unit/host tests/unit/storage tests/unit/cli/test_test.py tests/unit/configmodule -q`
Expected: PASS.

- [ ] **Step 10: Commit (Chris runs)**

```bash
git add src/otto/host/embeddedHost.py src/otto/host/remoteHost.py tests/unit/host/test_embeddedHost.py tests/unit/cli/test_test.py tests/unit/storage/test_factory.py && \
git commit -m "feat(host)!: make EmbeddedHost OS-agnostic; fail loud without a frame"
```

---

## Task 6: Docs, custom-class round-trip test, and full sweep

**Files:**
- Modify: `src/otto/host/os_profile.py` (module docstring), `src/otto/host/embeddedHost.py` (class docstring), `src/otto/configmodule/repo.py` (docstrings)
- Test: `tests/unit/host/test_os_profile.py`

- [ ] **Step 1: Add a compose/round-trip test proving the external pattern**

Add to `tests/unit/host/test_os_profile.py`:

```python
def test_custom_subclass_with_data_bundle_composes():
    """External pattern: register a subclass, then layer a data bundle over it."""
    from otto.storage.factory import create_host_from_dict
    from otto.host.embeddedHost import EmbeddedHost

    class MyRtosHost(EmbeddedHost):
        pass

    register_host_class('myrtos', MyRtosHost)
    register_os_profile('myrtos-v2', base='myrtos',
                        defaults={'osName': 'MyRTOS', 'command_frame': 'zephyr',
                                  'max_filename_len': 12})
    try:
        host = create_host_from_dict({
            'ip': '192.0.2.9', 'ne': 'widget', 'osType': 'myrtos-v2',
        })
        assert isinstance(host, MyRtosHost)
        assert host.osType == 'myrtos-v2'      # selector recorded
        assert host.osName == 'MyRTOS'         # from the data bundle
        assert host.max_filename_len == 12     # from the data bundle
    finally:
        from otto.host import os_profile as _m
        _m._HOST_CLASSES.pop('myrtos', None)
        _m._OS_PROFILES.pop('myrtos', None)
        _m._OS_PROFILES.pop('myrtos-v2', None)
```

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/host/test_os_profile.py -q -k composes`
Expected: PASS.

- [ ] **Step 3: Update docstrings**

- `src/otto/host/os_profile.py` module docstring (lines 1–26): state that `osType` is the profile selector recorded on the host, a profile selects a *registered host class* (`base`), and add a short **"Registering a custom host class"** paragraph: `class MyHost(EmbeddedHost): ...` + `register_host_class('myos', MyHost)` from a settings.toml init module, optionally `register_os_profile('myos-v1', base='myos', defaults={...})` to layer a data bundle. Reference `ZephyrHost` as the in-tree example.
- `src/otto/host/embeddedHost.py` `EmbeddedHost` class docstring (lines 64–66) and module docstring (lines 22–29): say `EmbeddedHost` is now OS-agnostic — it requires a `command_frame` (from a profile/subclass or explicitly) and raises if none is given; Zephyr specifics live on `ZephyrHost`.
- `src/otto/configmodule/repo.py` (lines 498, 532): change "`'unix'` or `'embedded'`" to "the name of a registered host class (e.g. `unix`, `embedded`, `zephyr`)".

- [ ] **Step 4: Final sweep — no stray frame-less constructions, full suite, lint/type**

Run: `grep -rn 'EmbeddedHost(' src tests --include=*.py | grep -v 'class EmbeddedHost' | grep -v command_frame`
Inspect each remaining hit; any that builds a host for use (not validation) needs a frame or should be `ZephyrHost`.
Run: `pytest tests/unit -q` (Expected: PASS — full unit suite.)
Run the project's lint/type targets if present: `make lint` / `make typecheck` (or `ruff check src tests` and `mypy src`). Expected: clean. Fix any `OsType`/`Literal` fallout the type-checker surfaces.

- [ ] **Step 5: Commit (Chris runs)**

```bash
git add src/otto/host/os_profile.py src/otto/host/embeddedHost.py src/otto/configmodule/repo.py tests/unit/host/test_os_profile.py && \
git commit -m "docs(host): document host-class registration; add compose test"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (osType selector, family from class, OsType→str) → Tasks 2, 5.
- Section 2 (EmbeddedHost generic + fail-loud; ZephyrHost) → Tasks 3, 5.
- Section 3 (registry, OsProfile.base widening, factory dispatch, built-ins) → Tasks 1, 2, 3.
- Section 4 (fixture + direct-construction migration) → Tasks 4, 5.
- Section 5 (validate_host_dict family derivation; docstrings) → Tasks 2, 6.
- Forward-compat (3.10→3.15) → validated in the spec; `_all_slots` MRO-union (Task 1) is the concrete guard for the empty-`__slots__` behavior. No gaps.

**Placeholder scan:** No TBD/TODO; every code step shows full content; migration steps name exact files/lines and the grep to find the rest.

**Type/name consistency:** `register_host_class` / `build_host_class` / `get_host_class` / `_all_slots` / `_slots_for_base` used consistently across Tasks 1–3, 6. Builders take `(host_data, defaults, profile, cls, selector)` consistently (Task 2) and are called with that arity (Task 2 dispatch). `OsType = str` (Task 5) is consistent with `ZephyrHost.osType = 'zephyr'` (Task 3). `command_frame: Optional[CommandFrame] = None` (Task 5) is consistent with the `is None` fail-loud guard and with `ZephyrHost` re-supplying a default (Task 3).

**Ordering safety:** Each task leaves the suite green — additive registry (1), behavior-preserving refactor (2), additive ZephyrHost (3), fixture migration while the old default still cushions (4), then the breaking flip with its test migration (5). The frame-less window is never reachable by lab data.
