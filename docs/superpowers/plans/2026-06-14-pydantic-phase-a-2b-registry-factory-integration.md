# Pydantic Phase A — Plan 2b: Registry + Factory Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the runtime actually *use* the committed `*HostSpec` pydantic models — carry the spec on the host registry, collapse the hand-rolled `storage/factory.py` onto `merge → model_validate → spec.to_host()`, promote `command_frame` to a common host field, and add the `interfaces` / `address_for` multi-interface seam.

**Architecture:** The committed Plan 2a left `UnixHostSpec` / `EmbeddedHostSpec` as a parallel, unused boundary layer guarded by parity tests (`*_matches_factory`) against the *old* factory. Plan 2b deletes the old factory's family-branching and dict→dataclass plumbing and routes lab-host construction through the specs, so the specs become the single source of truth. Three runtime-touching schema changes ride along (all deferred out of 2a because they touch behavior-bearing classes): `command_frame` promoted to a common field with a `UnixHost` runtime field, an additive `interfaces: dict[str, str]` map plus `address_for()` on every remote host, and `register_host_class(name, cls, spec=None)` so otto dogfoods the same registration path third parties use.

**Tech Stack:** pydantic v2 (`model_validate`, `model_fields_set`, `field_validator`), Python `@dataclass(slots=True)`, the existing string registries in `otto.host.os_profile` / `command_frame` / `embedded_filesystem`.

---

## Context the engineer needs (read once before Task 1)

You are working on branch `phase-a-pydantic-1-foundation-options` (Plan 1 + 2a are already committed there — `308af53`, `b8882e4`). **Stage only — do NOT `git commit`.** The repo's `prepare-commit-msg` hook needs `/dev/tty` and an agent commit mis-tags the AI-assist trailer; Chris commits manually. Each task below ends with a `git add` step, not a commit.

**Do NOT run `make test` (it spins up live VM tiers — never kill those mid-run) or `make coverage` (the 90% gate is Chris's pre-merge run).** Run the *targeted* `pytest` commands each task specifies. They are fast and hermetic.

The two-type split (already shipped in 2a): a pydantic `*Spec` validates the JSON boundary and builds the **unchanged** runtime dataclass via `to_runtime()` / `to_host()`. Two hard-won rules from 2a that this plan depends on:

1. **Omit-unset parity.** `to_host` / `_common_host_kwargs` build constructor kwargs **only from `self.model_fields_set`**, so a field absent from the source dict is omitted and the runtime class's own default (including subclass overrides like `UnixHost.os_name='Linux'`, `ZephyrHost.os_name='Zephyr'`) applies. Never pass a field unconditionally — it clobbers subclass defaults and breaks factory parity.
2. **Drift guards are bidirectional.** `tests/unit/models/test_host_specs.py::test_host_spec_fields_match_runtime_init` asserts `set(spec.model_fields) - {"labs"} == {runtime init fields}`. Any field you add to a spec MUST get a matching runtime constructor field **in the same task**, or that test goes red.

**Why the task ordering matters:** Tasks 1 and 2 each add a field to *both* the spec and the runtime class together, so the bidirectional drift guard stays green at every commit. Task 3 (registry carries the spec) must precede Task 5 (factory reads the spec from the registry). Task 4 (spec membership validators) precedes Task 5 so `validate_host_dict` keeps its registry-name guarantees.

### Files this plan touches

| File | Change |
| --- | --- |
| `src/otto/host/remote_host.py` | add bare `interfaces` annotation + `address_for()` method (behavior, shared) |
| `src/otto/host/unix_host.py` | add `interfaces` field; add `command_frame` field + thread it to both `SessionManager(...)` sites + `__post_init__` str-coercion |
| `src/otto/host/embedded_host.py` | add `interfaces` field |
| `src/otto/models/host.py` | add `interfaces` (+ IP validator) and promote `command_frame` to `HostSpec` base; resolve both in `_common_host_kwargs`; drop `EmbeddedHostSpec.command_frame`; add `filesystem`/`command_frame` registry-membership validators |
| `src/otto/host/os_profile.py` | `register_host_class(name, cls, spec=None)` + `_HOST_SPECS` + `build_host_spec()` + `_nearest_registered_spec()` MRO default; built-ins register their specs |
| `src/otto/storage/factory.py` | collapse `create_host_from_dict` / `validate_host_dict` onto `_merge_host_dict → model_validate → spec.to_host()`; delete `_create_*` / `_build_*` / `_OPTIONS_BUILDERS`; keep `OPTIONS_KEYS` |
| `src/otto/monitor/factory.py` | SNMP `address` resolves via `host.address_for(...)` |
| `tests/unit/host/test_remote_host*.py` | new: `interfaces` / `address_for` |
| `tests/unit/host/test_unix_host*.py` | new: `command_frame` threading |
| `tests/unit/host/test_os_profile.py` | extend `restore_registry` for `_HOST_SPECS`; spec-registry tests |
| `tests/unit/models/test_host_specs.py` | interfaces/command_frame/validator tests; update unknown-filesystem test |
| `tests/unit/storage/test_factory.py` | adapt assertions from bespoke `ValueError`/`TypeError` to pydantic `ValidationError` |
| `tests/unit/monitor/test_monitor_factory.py` | SNMP address resolution tests |

---

## Task 1: `interfaces` field + `address_for()` on the runtime hosts (and `HostSpec`)

**Why first:** smallest self-contained schema field; adds to spec + both runtime classes together so the bidirectional drift guard stays green.

**Files:**
- Modify: `src/otto/host/remote_host.py` (bare annotation + method, near the "Naming" section ~line 188)
- Modify: `src/otto/host/unix_host.py` (dataclass field near `resources`, ~line 205)
- Modify: `src/otto/host/embedded_host.py` (dataclass field near `resources`, ~line 211)
- Modify: `src/otto/models/host.py` (`HostSpec.interfaces` + validator; `_common_host_kwargs` handling)
- Test: `tests/unit/host/test_remote_host_addressing.py` (new), `tests/unit/models/test_host_specs.py` (extend)

- [ ] **Step 1: Write the failing runtime test**

Create `tests/unit/host/test_remote_host_addressing.py`:

```python
from otto.host.command_frame import ZephyrFrame
from otto.host.embedded_host import EmbeddedHost
from otto.host.unix_host import UnixHost


def _unix(**kw):
    return UnixHost(ip="10.0.0.1", creds={"u": "p"}, element="e", **kw)


def test_unix_interfaces_default_empty():
    assert _unix().interfaces == {}


def test_address_for_returns_literal_unchanged():
    h = _unix()
    assert h.address_for("10.0.0.1") == "10.0.0.1"
    assert h.address_for("203.0.113.9") == "203.0.113.9"


def test_address_for_resolves_named_interface():
    h = _unix(interfaces={"mgmt": "10.9.9.9", "data": "192.168.5.5"})
    assert h.address_for("mgmt") == "10.9.9.9"
    assert h.address_for("data") == "192.168.5.5"
    assert h.address_for("10.0.0.1") == "10.0.0.1"  # literal still passes through


def test_embedded_interfaces_field_and_address_for():
    h = EmbeddedHost(ip="192.0.2.1", element="dut", command_frame=ZephyrFrame())
    assert h.interfaces == {}
    assert h.address_for("192.0.2.1") == "192.0.2.1"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/unit/host/test_remote_host_addressing.py -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'interfaces'` / `AttributeError: 'UnixHost' object has no attribute 'address_for'`.

- [ ] **Step 3: Add the `interfaces` contract + `address_for()` to `RemoteHost`**

In `src/otto/host/remote_host.py`, in the "Shared instance-attribute contract" block (after the `max_filename_len` annotation, ~line 144), add the bare annotation:

```python
    interfaces: dict[str, str]
    """Named secondary interface addresses, keyed by interface name (e.g.
    ``{"mgmt": "10.0.0.5", "data": "192.168.1.5"}``). The *primary* address
    stays :attr:`ip`; this map is additive and optional (empty by default).
    Resolve a name (or pass a literal through) with :meth:`address_for`."""
```

Then add the method in the "Naming" section (after `_generate_id` / the `_slot_str` property, ~line 222):

```python
    ####################
    #  Addressing
    ####################

    def address_for(self, name_or_literal: str) -> str:
        """Resolve an interface *name* to its address, or pass a literal through.

        If *name_or_literal* is a key in :attr:`interfaces`, return that
        interface's address; otherwise return the value unchanged (it is taken
        to be a literal address such as :attr:`ip` or an explicit IP). This lets
        a host's ``snmp.address`` name a secondary interface without otto having
        to distinguish names from literals.
        """
        return self.interfaces.get(name_or_literal, name_or_literal)
```

- [ ] **Step 4: Add the `interfaces` dataclass field to `UnixHost` and `EmbeddedHost`**

In `src/otto/host/unix_host.py`, immediately after the `resources` field (~line 205):

```python
    interfaces: dict[str, str] = field(default_factory=dict, repr=False)
    """Named secondary interface addresses (see :attr:`RemoteHost.interfaces`).
    Resolve with :meth:`address_for`."""
```

In `src/otto/host/embedded_host.py`, immediately after the `resources` field (~line 211):

```python
    interfaces: dict[str, str] = field(default_factory=dict, repr=False)
    """Named secondary interface addresses (see :attr:`RemoteHost.interfaces`).
    Resolve with :meth:`address_for`."""
```

- [ ] **Step 5: Run the runtime test to confirm it passes**

Run: `pytest tests/unit/host/test_remote_host_addressing.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Write the failing spec test**

Append to `tests/unit/models/test_host_specs.py`:

```python
def test_hostspec_interfaces_default_empty_and_passes_to_host():
    spec = UnixHostSpec(ip="10.0.0.1", element="lab", creds={"u": "p"})
    assert spec.interfaces == {}
    assert spec.to_host().interfaces == {}


def test_hostspec_interfaces_resolve_on_built_host():
    spec = UnixHostSpec(
        ip="10.0.0.1", element="lab", creds={"u": "p"},
        interfaces={"mgmt": "10.9.9.9"},
    )
    host = spec.to_host()
    assert host.interfaces == {"mgmt": "10.9.9.9"}
    assert host.address_for("mgmt") == "10.9.9.9"


def test_hostspec_interfaces_rejects_non_ip_value():
    with pytest.raises(ValidationError) as exc:
        HostSpec(ip="10.0.0.1", element="lab", interfaces={"mgmt": "not-an-ip"})
    assert "mgmt" in str(exc.value)
```

- [ ] **Step 7: Run it to confirm it fails**

Run: `pytest tests/unit/models/test_host_specs.py -q -k interfaces`
Expected: FAIL — `ValidationError: Extra inputs are not permitted [interfaces]` (spec has no such field yet).

- [ ] **Step 8: Add `interfaces` to `HostSpec` + validator + `_common_host_kwargs`**

In `src/otto/models/host.py`:

Add the import at the top (with the other stdlib imports):

```python
from ipaddress import ip_address
```

and add `field_validator` to the pydantic import (it is not yet imported in this module):

```python
from pydantic import field_validator
```

In `class HostSpec`, after the `resources` field (~line 74), add:

```python
    interfaces: dict[str, str] = {}
```

and, after the field block (before `_common_host_kwargs`), add the validator:

```python
    @field_validator("interfaces")
    @classmethod
    def _validate_interface_addresses(cls, v: dict[str, str]) -> dict[str, str]:
        for name, addr in v.items():
            try:
                ip_address(addr)
            except ValueError:
                raise ValueError(
                    f"interface {name!r} address {addr!r} is not a valid IP"
                ) from None
        return v
```

In `_common_host_kwargs`, add an `interfaces` clause alongside `resources` (mirrors the copy-the-mutable pattern):

```python
        if "interfaces" in s:
            kw["interfaces"] = dict(self.interfaces)
```

- [ ] **Step 9: Run the spec + drift-guard tests to confirm green**

Run: `pytest tests/unit/models/test_host_specs.py -q`
Expected: PASS — including `test_host_spec_fields_match_runtime_init[UnixHostSpec-UnixHost]` and `[EmbeddedHostSpec-EmbeddedHost]` (both spec and runtime gained `interfaces`, so the bidirectional guard balances).

- [ ] **Step 10: Stage**

```bash
git add src/otto/host/remote_host.py src/otto/host/unix_host.py \
        src/otto/host/embedded_host.py src/otto/models/host.py \
        tests/unit/host/test_remote_host_addressing.py \
        tests/unit/models/test_host_specs.py
```

---

## Task 2: promote `command_frame` to a common host field (`UnixHost` runtime + `HostSpec` base)

**Why:** `command_frame` is generic to all hosts (Unix uses `BashFrame`, embedded passes a `ZephyrFrame`) but today only `EmbeddedHost` exposes it. Promoting it pre-freeze turns a free generalization into a breaking schema addition later. `SessionManager` already accepts and threads `command_frame`, so the runtime change is small.

**Files:**
- Modify: `src/otto/host/unix_host.py` (new `command_frame` field; import `CommandFrame`/`build_command_frame`; `__post_init__` str-coercion; thread to both `SessionManager(...)` sites — `__post_init__` ~line 271 and `rebuild_connections` ~line 319)
- Modify: `src/otto/models/host.py` (`HostSpec.command_frame`; resolve in `_common_host_kwargs`; drop `EmbeddedHostSpec.command_frame` field + its `to_host` block)
- Test: `tests/unit/host/test_unix_host_command_frame.py` (new), `tests/unit/models/test_host_specs.py` (extend)

- [ ] **Step 1: Write the failing runtime test**

Create `tests/unit/host/test_unix_host_command_frame.py`:

```python
from otto.host.command_frame import BashFrame
from otto.host.unix_host import UnixHost


def _unix(**kw):
    return UnixHost(ip="10.0.0.1", creds={"u": "p"}, element="e", **kw)


def test_unix_command_frame_defaults_none_preserves_bash_behavior():
    # None means "let SessionManager apply its built-in BashFrame" — the exact
    # historical behavior (UnixHost never passed a frame before).
    h = _unix()
    assert h.command_frame is None
    assert h._session_mgr._command_frame is None


def test_unix_accepts_command_frame_instance_and_threads_it():
    f = BashFrame()
    h = _unix(command_frame=f)
    assert h.command_frame is f
    assert h._session_mgr._command_frame is f


def test_unix_coerces_command_frame_string():
    h = _unix(command_frame="bash")
    assert isinstance(h.command_frame, BashFrame)
    assert isinstance(h._session_mgr._command_frame, BashFrame)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/unit/host/test_unix_host_command_frame.py -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'command_frame'`.

- [ ] **Step 3: Add the `command_frame` field + threading to `UnixHost`**

In `src/otto/host/unix_host.py`, extend the command-frame import (the module imports from `.command_frame`? No — add it). Add near the other `.host`/`.session` imports (~line 90, after `from .telnet import TelnetClient`):

```python
from .command_frame import CommandFrame, build_command_frame
```

Add the field immediately after the `nc_options` field (~line 194), before `snmp`:

```python
    command_frame: Optional[CommandFrame] = None
    """Shell-framing dialect for this host's bash console. ``None`` (the
    default) lets the :class:`~otto.host.session.SessionManager` use its
    built-in :class:`~otto.host.command_frame.BashFrame`, preserving the
    historical behavior exactly. Lab data may name a registered frame by string
    (resolved in ``__post_init__``); a profile or subclass may supply an
    instance. Promoted to a common field in Phase A so any host can declare its
    dialect — see :attr:`EmbeddedHost.command_frame`."""
```

In `__post_init__`, add a str-coercion clause (mirroring `EmbeddedHost`) — place it just after the `default_dest_dir` coercion (~line 253), before `hop_transport = ...`:

```python
        # Lab JSON declares the frame dialect by name; coerce a string to the
        # registered instance. None is left as-is (SessionManager applies bash).
        if isinstance(self.command_frame, str):
            self.command_frame = build_command_frame(self.command_frame)
```

Thread it to the `SessionManager(...)` call in `__post_init__` (~line 271) by adding the kwarg:

```python
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
        )
```

Do the **same** to the second `SessionManager(...)` call in `rebuild_connections` (~line 319):

```python
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
        )
```

(`SessionManager.__init__` already accepts `command_frame: CommandFrame | None = None` and defaults to `BashFrame` when `None`, so passing `None` reproduces today's behavior byte-for-byte.)

- [ ] **Step 4: Run the runtime test to confirm it passes**

Run: `pytest tests/unit/host/test_unix_host_command_frame.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Write the failing spec test**

Append to `tests/unit/models/test_host_specs.py`:

```python
def test_unix_spec_accepts_command_frame_string():
    from otto.host.command_frame import BashFrame
    spec = UnixHostSpec(
        ip="10.0.0.1", element="lab", creds={"u": "p"}, command_frame="bash",
    )
    host = spec.to_host()
    assert isinstance(host.command_frame, BashFrame)


def test_unix_spec_omits_command_frame_when_unset():
    spec = UnixHostSpec(ip="10.0.0.1", element="lab", creds={"u": "p"})
    # unset -> not passed -> UnixHost default (None -> SessionManager BashFrame)
    assert "command_frame" not in spec._common_host_kwargs()
    assert spec.to_host().command_frame is None
```

- [ ] **Step 6: Run it to confirm it fails**

Run: `pytest tests/unit/models/test_host_specs.py -q -k command_frame`
Expected: FAIL — `ValidationError: Extra inputs are not permitted [command_frame]` (the field lives only on `EmbeddedHostSpec` today, not on the base, so `UnixHostSpec` rejects it).

- [ ] **Step 7: Promote `command_frame` to `HostSpec` base; resolve it in `_common_host_kwargs`; drop it from `EmbeddedHostSpec`**

In `src/otto/models/host.py`:

Add to `class HostSpec`, after the `toolchain` field (~line 79):

```python
    command_frame: str | None = None
```

In `_common_host_kwargs`, add a resolution clause at the end (just before `return kw`):

```python
        if "command_frame" in s and self.command_frame is not None:
            kw["command_frame"] = build_command_frame(self.command_frame)
```

In `class EmbeddedHostSpec`, **delete** its own `command_frame: str | None = None` field declaration (~line 140) — it now inherits from the base.

In `EmbeddedHostSpec.to_host`, **delete** the now-redundant block (~lines 150-151):

```python
        if "command_frame" in s and self.command_frame is not None:
            kw["command_frame"] = build_command_frame(self.command_frame)
```

(The base `_common_host_kwargs` now resolves it for both families.)

- [ ] **Step 8: Run spec + drift-guard + the 2a embedded parity tests to confirm green**

Run: `pytest tests/unit/models/test_host_specs.py -q`
Expected: PASS — `test_host_spec_fields_match_runtime_init` still balances (`UnixHostSpec`↔`UnixHost` both now carry `command_frame`; `EmbeddedHostSpec`↔`EmbeddedHost` unchanged), and `test_embedded_to_host_matches_factory` still passes (frame now resolved in `_common_host_kwargs`, same result).

- [ ] **Step 9: Stage**

```bash
git add src/otto/host/unix_host.py src/otto/models/host.py \
        tests/unit/host/test_unix_host_command_frame.py \
        tests/unit/models/test_host_specs.py
```

---

## Task 3: `register_host_class(name, cls, spec=None)` — carry the spec on the registry

**Why:** the factory (Task 5) needs `(host_class, host_spec)` per `os_type`. otto registers its **own** built-ins through the same call a third party uses, with `spec=None` defaulting to the nearest base's spec via the MRO.

**Files:**
- Modify: `src/otto/host/os_profile.py` (`_HOST_SPECS`; new `register_host_class` signature; `_nearest_registered_spec`; `build_host_spec`; built-ins pass their specs)
- Test: `tests/unit/host/test_os_profile.py` (extend `restore_registry`; new tests)

**Import-direction note (no cycle):** `os_profile` already imports the host classes lazily inside `_register_builtin_host_classes`. It will now also lazily import `UnixHostSpec` / `EmbeddedHostSpec` from `otto.models.host`. `models.host` imports the runtime *data* modules (`unix_host`, `embedded_host`, `options`, `command_frame`, …) but **not** `os_profile`, so the dependency runs one way (`os_profile → models.host → runtime data`). The membership check inside `register_host_class` also imports `HostSpec` lazily. If you introduce an import cycle, the first `pytest` run will surface it immediately as an `ImportError`.

- [ ] **Step 1: Extend `restore_registry` to also snapshot `_HOST_SPECS`**

In `tests/unit/host/test_os_profile.py`, update the `restore_registry` fixture (~lines 27-35) to cover the new registry so a test registration can't leak:

```python
    saved_profiles = dict(os_profile._OS_PROFILES)
    saved_classes = dict(os_profile._HOST_CLASSES)
    saved_specs = dict(os_profile._HOST_SPECS)
    try:
        yield
    finally:
        os_profile._OS_PROFILES.clear()
        os_profile._OS_PROFILES.update(saved_profiles)
        os_profile._HOST_CLASSES.clear()
        os_profile._HOST_CLASSES.update(saved_classes)
        os_profile._HOST_SPECS.clear()
        os_profile._HOST_SPECS.update(saved_specs)
```

- [ ] **Step 2: Write the failing registry tests**

Append to `tests/unit/host/test_os_profile.py` (inside the module, a new test class or top-level functions — match the file's existing style):

```python
class TestHostSpecRegistry:
    def test_builtins_carry_their_specs(self):
        from otto.host.os_profile import build_host_spec
        from otto.models.host import EmbeddedHostSpec, UnixHostSpec
        assert build_host_spec("unix") is UnixHostSpec
        assert build_host_spec("embedded") is EmbeddedHostSpec
        assert build_host_spec("zephyr") is EmbeddedHostSpec  # adds no fields

    def test_register_with_explicit_spec(self):
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.os_profile import build_host_spec, register_host_class
        from otto.models.host import EmbeddedHostSpec

        class MyHost(EmbeddedHost):
            pass

        register_host_class("myos", MyHost, EmbeddedHostSpec)
        assert build_host_spec("myos") is EmbeddedHostSpec

    def test_register_defaults_spec_via_mro(self):
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.os_profile import build_host_spec, register_host_class
        from otto.models.host import EmbeddedHostSpec

        class MyHost(EmbeddedHost):
            pass

        register_host_class("myos2", MyHost)  # no spec -> nearest base spec
        assert build_host_spec("myos2") is EmbeddedHostSpec

    def test_register_rejects_non_hostspec_spec(self):
        from otto.host.unix_host import UnixHost
        from otto.host.os_profile import register_host_class
        with pytest.raises(ValueError, match="HostSpec"):
            register_host_class("bad", UnixHost, dict)  # dict is not a HostSpec

    def test_build_host_spec_unknown_raises(self):
        from otto.host.os_profile import build_host_spec
        with pytest.raises(ValueError, match="No host spec"):
            build_host_spec("nope")
```

(`pytest` is already imported at the top of this test file.)

- [ ] **Step 3: Run them to confirm they fail**

Run: `pytest tests/unit/host/test_os_profile.py::TestHostSpecRegistry -q`
Expected: FAIL — `ImportError`/`AttributeError` (`build_host_spec` and `_HOST_SPECS` don't exist; `register_host_class` takes no `spec`).

- [ ] **Step 4: Implement the spec-carrying registry**

In `src/otto/host/os_profile.py`:

Add the parallel registry next to `_HOST_CLASSES` (~line 73):

```python
# Registry of host-class name -> its boundary HostSpec subclass, populated for
# built-ins at module load alongside ``_HOST_CLASSES``.
_HOST_SPECS: dict[str, type] = {}
```

Replace `register_host_class` (~lines 118-144) with the spec-carrying version:

```python
def register_host_class(name: str, cls: type, spec: type | None = None) -> None:
    """Register a host class (and its boundary spec) so lab data can select it
    by ``os_type``.

    Mirrors :func:`otto.host.command_frame.register_command_frame`. Call from an
    init module listed in ``.otto/settings.toml`` to ship a custom host
    subclass. otto registers its own built-ins through this same call.

    Parameters
    ----------
    name : str
        The ``os_type`` selector to register under.
    cls : type
        A :class:`~otto.host.remote_host.RemoteHost` subclass.
    spec : type | None
        The :class:`~otto.models.host.HostSpec` subclass that validates this
        class's lab-dict shape. When ``None``, defaults to the spec registered
        for the nearest base class in *cls*'s MRO — so a subclass that adds no
        fields needs none; add fields → register a ``HostSpec`` subclass.

    Registering a class also registers a trivial same-named :class:`OsProfile`
    (``base=name``, empty ``defaults``), so ``os_type: name`` resolves with no
    extra config. Re-registering replaces the prior class and spec.

    Raises
    ------
    ValueError
        If *cls* is not a ``RemoteHost`` subclass; if *spec* is given but is not
        a ``HostSpec`` subclass; or if *spec* is ``None`` and no base class of
        *cls* has a registered spec.
    """
    from .remote_host import RemoteHost
    if not (isinstance(cls, type) and issubclass(cls, RemoteHost)):
        raise ValueError(
            f"register_host_class({name!r}): cls must be a RemoteHost "
            f"subclass, got {cls!r}"
        )
    if spec is None:
        spec = _nearest_registered_spec(cls)
        if spec is None:
            raise ValueError(
                f"register_host_class({name!r}): no spec given and no base "
                f"class of {cls.__name__} has a registered spec. Pass spec=."
            )
    else:
        from ..models.host import HostSpec
        if not (isinstance(spec, type) and issubclass(spec, HostSpec)):
            raise ValueError(
                f"register_host_class({name!r}): spec must be a HostSpec "
                f"subclass, got {spec!r}"
            )
    if name in _BUILTIN_NAMES and name in _HOST_CLASSES:
        logger.warning(
            f"register_host_class: overriding built-in host class {name!r}"
        )
    _HOST_CLASSES[name] = cls
    _HOST_SPECS[name] = spec
    # Auto-register a selector profile so os_type:<name> works immediately.
    _OS_PROFILES[name] = OsProfile(name=name, base=name, defaults={})


def _nearest_registered_spec(cls: type) -> type | None:
    """Return the spec registered for the nearest base of *cls* in its MRO."""
    by_class = {_HOST_CLASSES[n]: _HOST_SPECS[n] for n in _HOST_SPECS}
    for base in cls.__mro__:
        if base in by_class:
            return by_class[base]
    return None


def build_host_spec(name: str) -> type:
    """Return the :class:`~otto.models.host.HostSpec` subclass registered under
    host-class *name* (raising on miss)."""
    try:
        return _HOST_SPECS[name]
    except KeyError:
        known = ', '.join(sorted(_HOST_SPECS))
        raise ValueError(
            f"No host spec registered for {name!r}. Registered: {known}."
        ) from None
```

Update `_register_builtin_host_classes` (~lines 273-280) to pass the specs:

```python
def _register_builtin_host_classes() -> None:
    """Register the built-in host classes and their boundary specs. Imported
    lazily to avoid an import cycle (the host/spec modules do not import this
    one at module top)."""
    from .unix_host import UnixHost
    from .embedded_host import EmbeddedHost, ZephyrHost
    from ..models.host import EmbeddedHostSpec, UnixHostSpec
    register_host_class('unix', UnixHost, UnixHostSpec)
    register_host_class('embedded', EmbeddedHost, EmbeddedHostSpec)
    register_host_class('zephyr', ZephyrHost, EmbeddedHostSpec)
```

- [ ] **Step 5: Run the registry tests to confirm they pass**

Run: `pytest tests/unit/host/test_os_profile.py -q`
Expected: PASS (the new `TestHostSpecRegistry` plus all existing os_profile tests — the `restore_registry` change keeps them isolated).

- [ ] **Step 6: Stage**

```bash
git add src/otto/host/os_profile.py tests/unit/host/test_os_profile.py
```

---

## Task 4: spec registry-membership validators (`filesystem`, `command_frame`)

**Why:** the old `validate_host_dict` rejected an unregistered `filesystem` name and a bad `command_frame` at validate-time. When Task 5 collapses validation onto `model_validate`, those guarantees must come from `field_validator`s so the error fires at validation (and shows up in the JSON Schema's allowed values later), not only at build time.

**Files:**
- Modify: `src/otto/models/host.py` (`command_frame` validator on `HostSpec`; `filesystem` validator on `EmbeddedHostSpec`)
- Test: `tests/unit/models/test_host_specs.py` (new validator tests; **update** the existing `test_embedded_spec_rejects_unknown_filesystem`)

- [ ] **Step 1: Write the failing validator tests + update the unknown-filesystem test**

In `tests/unit/models/test_host_specs.py`, **replace** the existing `test_embedded_spec_rejects_unknown_filesystem` (it currently expects `to_host()` to raise; the validator now rejects at construction):

```python
def test_embedded_spec_rejects_unknown_filesystem():
    # Now caught at validate-time by the field_validator, not at to_host().
    with pytest.raises(ValidationError) as exc:
        EmbeddedHostSpec(ip="192.0.2.1", element="dut", filesystem="bogusfs")
    assert "bogusfs" in str(exc.value)
```

Append the new tests:

```python
def test_embedded_spec_accepts_registered_filesystem():
    # A registered filesystem name validates (resolved to an instance at build).
    spec = EmbeddedHostSpec(
        ip="192.0.2.1", element="dut", command_frame="zephyr", filesystem="none",
    )
    assert spec.filesystem == "none"


def test_hostspec_rejects_unregistered_command_frame():
    with pytest.raises(ValidationError) as exc:
        UnixHostSpec(
            ip="10.0.0.1", element="lab", creds={"u": "p"},
            command_frame="nonesuch",
        )
    assert "nonesuch" in str(exc.value)
```

> Note: confirm `"none"` is a registered filesystem name via `otto.host.embedded_filesystem._FILESYSTEM_CLASSES` before finalizing this test; if the registered no-op name differs (e.g. `"nofs"`), use the real key. `build_filesystem`'s error message lists the valid names.

- [ ] **Step 2: Run to confirm the new ones fail (and the rewritten one fails for the right reason)**

Run: `pytest tests/unit/models/test_host_specs.py -q -k "filesystem or command_frame"`
Expected: FAIL — the unregistered names currently pass validation (no validator yet), so construction does **not** raise.

- [ ] **Step 3: Add the validators**

In `src/otto/models/host.py`, extend the top imports to expose the membership sets (the module already imports `build_command_frame` and `build_filesystem`):

```python
from ..host.command_frame import _FRAME_CLASSES, build_command_frame
from ..host.embedded_filesystem import _FILESYSTEM_CLASSES, build_filesystem
```

Add to `class HostSpec`, next to the `interfaces` validator:

```python
    @field_validator("command_frame")
    @classmethod
    def _validate_command_frame_name(cls, v: str | None) -> str | None:
        if v is not None and v not in _FRAME_CLASSES:
            known = ", ".join(sorted(_FRAME_CLASSES))
            raise ValueError(
                f"command_frame {v!r} is not a registered frame. Known: {known}"
            )
        return v
```

Add to `class EmbeddedHostSpec`:

```python
    @field_validator("filesystem")
    @classmethod
    def _validate_filesystem_name(cls, v: str | None) -> str | None:
        if v is not None and v not in _FILESYSTEM_CLASSES:
            known = ", ".join(sorted(_FILESYSTEM_CLASSES))
            raise ValueError(
                f"filesystem {v!r} is not a registered filesystem. Known: {known}"
            )
        return v
```

(`_FRAME_CLASSES` / `_FILESYSTEM_CLASSES` are the same dict objects mutated in place by `register_command_frame` / `register_filesystem`, so a third-party name registered before host-load validates correctly.)

- [ ] **Step 4: Run to confirm green**

Run: `pytest tests/unit/models/test_host_specs.py -q`
Expected: PASS (validators reject the bad names; `test_embedded_spec_builds_with_command_frame` and the parity tests still pass — their names are registered).

- [ ] **Step 5: Stage**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_specs.py
```

---

## Task 5: collapse `storage/factory.py` onto merge → `model_validate` → `spec.to_host()`

**Why:** this is the integration payoff — delete the hand-rolled family-branching, `_all_slots` filtering, and dict→dataclass `_build_*` conversions, and route construction through the validated specs.

**Files:**
- Rewrite: `src/otto/storage/factory.py`
- Test: `tests/unit/storage/test_factory.py` (adapt assertions to pydantic `ValidationError`)

**Behavior to preserve exactly (the 2a `*_matches_factory` tests are the safety net; they now compare the spec path to itself, so they remain green by construction):**
- Precedence: host fields > profile `defaults` > repo `defaults`; `*_options` tables merge **per-key** across all three layers.
- `os_type` selector is stamped onto the built host (`merged['os_type'] = selector`).
- Repo-level `defaults` only contribute `*_options` tables, and only those the target spec accepts (so a repo-wide `ssh_options` default is *not* injected onto an embedded host).

**Intended behavior *change* (call it out in the commit message):** a *misplaced* key that `_all_slots` used to silently drop (e.g. `ssh_options` declared directly on an embedded host, or any typo'd top-level field) now raises a pydantic `ValidationError` with a field suggestion. That is the `extra='forbid'` payoff and is desired.

- [ ] **Step 1: Read the existing factory tests and inventory the assertions to migrate**

Run: `pytest tests/unit/storage/test_factory.py -q` (capture the current green baseline), then read `tests/unit/storage/test_factory.py`. Note every `pytest.raises(TypeError)` (lines ~83, ~94, ~105, ~305) and every `pytest.raises(ValueError, match=...)` (e.g. ~350 `match='command_frame'`) — pydantic raises `ValidationError` (a subclass of `ValueError`, **not** `TypeError`), and message text differs. These assertions get updated in Step 6.

- [ ] **Step 2: Write/adjust the failing precedence + validation tests**

Add these focused tests to `tests/unit/storage/test_factory.py` (they pin the merge contract independent of message text):

```python
def test_create_merges_options_per_key_across_layers():
    from otto.storage.factory import create_host_from_dict
    # repo default (lowest) < profile default < host (highest), per-key.
    repo_defaults = {"ssh_options": {"port": 22, "connect_timeout": 1.0}}
    host = create_host_from_dict(
        {
            "ip": "10.0.0.1", "element": "carrot", "creds": {"u": "p"},
            "ssh_options": {"port": 2222},  # host overrides only 'port'
        },
        defaults=repo_defaults,
    )
    assert host.ssh_options.port == 2222              # host wins
    assert host.ssh_options.connect_timeout == 1.0    # repo default survives


def test_create_stamps_os_type_selector():
    from otto.storage.factory import create_host_from_dict
    host = create_host_from_dict(
        {"ip": "10.0.0.1", "element": "c", "creds": {"u": "p"}}
    )
    assert host.os_type == "unix"  # absent os_type -> default selector stamped


def test_validate_rejects_typo_with_pydantic_error():
    from pydantic import ValidationError
    from otto.storage.factory import validate_host_dict
    with pytest.raises(ValidationError):
        validate_host_dict(
            {"ip": "10.0.0.1", "element": "c", "creds": {"u": "p"}, "lab": ["x"]}
        )  # 'lab' is a typo for 'labs'


def test_validate_rejects_misplaced_ssh_options_on_embedded():
    from pydantic import ValidationError
    from otto.storage.factory import validate_host_dict
    with pytest.raises(ValidationError):
        validate_host_dict(
            {
                "ip": "192.0.2.1", "element": "dut", "os_type": "embedded",
                "command_frame": "zephyr", "ssh_options": {"port": 22},
            }
        )
```

- [ ] **Step 3: Run to confirm the new ones fail**

Run: `pytest tests/unit/storage/test_factory.py -q -k "per_key or selector or typo or misplaced"`
Expected: FAIL — e.g. the misplaced-`ssh_options`-on-embedded case currently *passes* (old factory silently drops it), and the per-key merge may differ.

- [ ] **Step 4: Rewrite `src/otto/storage/factory.py`**

Replace the whole file with the collapsed version:

```python
from typing import Any

from ..host.os_profile import (
    build_host_class,
    build_host_spec,
    build_os_profile,
    get_os_profile,
    registered_profile_names,
)
from ..host.remote_host import RemoteHost

# Names of the per-protocol option tables accepted on host dicts and as
# repo-level ``[host_defaults.<key>]`` tables. Kept here (and imported by
# ``configmodule.repo``) as the canonical option-key set.
OPTIONS_KEYS: frozenset[str] = frozenset({
    'ssh_options',
    'telnet_options',
    'sftp_options',
    'scp_options',
    'ftp_options',
    'nc_options',
})


def _merge_host_dict(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None,
    profile: 'Any',
    spec_cls: type,
) -> dict[str, Any]:
    """Precedence-merge profile defaults, repo defaults, and host fields into a
    single dict (the M1 merge), ready for ``spec_cls.model_validate``.

    Scalars: host field > profile default. ``*_options`` tables: per-key
    host > profile > repo-default. Only option keys the target spec actually
    declares are merged, so a repo-wide ``ssh_options`` default is never
    injected onto a host family that has no such field.
    """
    merged: dict[str, Any] = {**profile.defaults, **host_data}

    defaults = defaults or {}
    opt_keys = OPTIONS_KEYS & set(spec_cls.model_fields)
    for key in opt_keys:
        d = defaults.get(key)
        p = profile.defaults.get(key)
        h = host_data.get(key)
        table: dict[str, Any] = {
            **(d if isinstance(d, dict) else {}),
            **(p if isinstance(p, dict) else {}),
            **(h if isinstance(h, dict) else {}),
        }
        if table:
            merged[key] = table
        else:
            merged.pop(key, None)
    return merged


def create_host_from_dict(
    host_data: dict[str, Any],
    defaults: dict[str, dict[str, Any]] | None = None,
) -> RemoteHost:
    """Create the appropriate :class:`RemoteHost` subclass from a host dict.

    ``os_type`` names a registered :class:`~otto.host.os_profile.OsProfile`,
    which selects the base host class and carries a bundle of default field
    values. The profile's base resolves to a ``(host_class, host_spec)`` pair;
    the merged dict (host > profile > repo defaults, per-key for ``*_options``)
    is validated once by the spec (``extra='forbid'``, typed, with field-name
    suggestions on typos) and the spec builds the runtime host.

    Field precedence, highest to lowest: the host's own value; the profile's
    ``defaults``; repo-level ``*_options`` defaults (options only); the runtime
    class's stock default.

    Raises
    ------
    ValueError
        If ``os_type`` names no registered profile.
    pydantic.ValidationError
        If a field is missing, mistyped, misplaced, or unknown (a subclass of
        ``ValueError``).
    """
    selector = host_data.get('os_type', 'unix')
    profile = build_os_profile(selector)
    cls = build_host_class(profile.base)
    spec_cls = build_host_spec(profile.base)
    merged = _merge_host_dict(host_data, defaults, profile, spec_cls)
    merged['os_type'] = selector
    spec = spec_cls.model_validate(merged)
    return spec.to_host(cls)


def validate_host_dict(host_data: dict[str, Any]) -> None:
    """Validate a host dict without constructing the host.

    ``os_type`` must name a registered profile; the profile's base spec
    validates the merged dict (``extra='forbid'``, required fields, typed
    coercion, family-specific field validators for ``command_frame`` /
    ``filesystem`` / ``transfer`` / ``docker_capable``).

    Raises
    ------
    ValueError
        If ``os_type`` names no registered profile.
    pydantic.ValidationError
        On any structural problem (subclass of ``ValueError``).
    """
    selector = host_data.get('os_type', 'unix')
    profile = get_os_profile(selector)
    if profile is None:
        known = ', '.join(registered_profile_names())
        raise ValueError(
            f"Field 'os_type' {selector!r} is not a registered profile. "
            f"Registered profiles: {known}"
        )
    spec_cls = build_host_spec(profile.base)
    merged = _merge_host_dict(host_data, None, profile, spec_cls)
    merged['os_type'] = selector
    spec_cls.model_validate(merged)
```

This deletes `_build_toolchain`, `_build_ssh_options`, `_build_telnet_options`, `_build_sftp_options`, `_build_scp_options`, `_build_ftp_options`, `_build_nc_options`, `_build_snmp_options`, `_OPTIONS_BUILDERS`, `_create_unix_host`, `_create_embedded_host`, and all the now-unused imports (`Path`, `cast`, the option dataclasses, `Toolchain`, `build_command_frame`, `build_filesystem`, `_FILESYSTEM_CLASSES`, `_all_slots`, `OsProfile`, `EmbeddedHost`, `UnixHost`).

- [ ] **Step 5: Run the new factory tests + the 2a parity tests**

Run: `pytest tests/unit/storage/test_factory.py -q -k "per_key or selector or typo or misplaced" tests/unit/models/test_host_specs.py -q`
Expected: PASS for the new merge tests and all `*_matches_factory` parity tests.

- [ ] **Step 6: Migrate the remaining `test_factory.py` assertions to pydantic**

Run the full file: `pytest tests/unit/storage/test_factory.py -q`. For each failure:
- `pytest.raises(TypeError)` on a wrong-typed field → change to `pytest.raises(ValidationError)` (import `from pydantic import ValidationError`). pydantic raises `ValidationError`, not `TypeError`.
- `pytest.raises(ValueError, match='<old message>')` → keep `ValueError` (ValidationError subclasses it) but relax/replace `match=` to a substring pydantic actually emits (e.g. the field name, `'Field required'`, `'Extra inputs are not permitted'`, or the validator message text like `'command_frame'` / `'docker_capable'`). Preserve the *scenario* (missing field, wrong type, typo, embedded `docker_capable` rejected, unknown `filesystem`, unknown `os_type`) — only the exception class/message changes.

Re-run until green: `pytest tests/unit/storage/test_factory.py -q`.

- [ ] **Step 7: Confirm the downstream importer still resolves**

Run: `pytest tests/unit/configmodule/test_repo.py -q`
Expected: PASS — `configmodule/repo.py` imports `OPTIONS_KEYS` from the factory; the rewrite keeps that name with the same 6-key membership.

- [ ] **Step 8: Stage**

```bash
git add src/otto/storage/factory.py tests/unit/storage/test_factory.py
```

---

## Task 6: wire SNMP `address` through `address_for`

**Why:** completes the `interfaces` seam — a host's `snmp.address` may now name a secondary interface, resolved via the host's `address_for`, while a literal IP (or the default `host.ip`) passes through unchanged.

**Files:**
- Modify: `src/otto/monitor/factory.py` (~line 46)
- Test: `tests/unit/monitor/test_monitor_factory.py`

- [ ] **Step 1: Write the failing tests**

Read `tests/unit/monitor/test_monitor_factory.py` for how it builds a host + SNMP block and inspects the constructed `SnmpClient`. Following that pattern, add:

```python
def test_snmp_address_resolves_named_interface(...):
    # host.interfaces={"mgmt": "10.9.9.9"}, snmp.address="mgmt"
    # -> SnmpClient.address == "10.9.9.9"
    ...


def test_snmp_address_literal_passes_through(...):
    # snmp.address="203.0.113.5" (not an interface name) -> "203.0.113.5"
    ...


def test_snmp_address_defaults_to_host_ip(...):
    # snmp.address=None -> host.ip
    ...
```

(Fill the host/SNMP construction to match the file's existing fixtures — e.g. build a `UnixHost` with `snmp=SnmpOptions(oids=(...), address=...)` and `interfaces={...}`, run the monitor factory, and assert on the client's `address`.)

- [ ] **Step 2: Run to confirm the interface-name case fails**

Run: `pytest tests/unit/monitor/test_monitor_factory.py -q -k snmp_address`
Expected: FAIL — `test_snmp_address_resolves_named_interface` yields `"mgmt"` (the raw name) because today's code is `snmp.address or host.ip`.

- [ ] **Step 3: Resolve via `address_for`**

In `src/otto/monitor/factory.py` (~line 46), change:

```python
                address=snmp.address or host.ip,
```

to:

```python
                address=host.address_for(snmp.address or host.ip),
```

(`address_for` returns a literal unchanged, so `host.ip` and an explicit IP are untouched; an interface name resolves to its address.)

- [ ] **Step 4: Run to confirm green**

Run: `pytest tests/unit/monitor/test_monitor_factory.py -q`
Expected: PASS.

- [ ] **Step 5: Stage**

```bash
git add src/otto/monitor/factory.py tests/unit/monitor/test_monitor_factory.py
```

---

## Task 7: registry-wide drift guard + dead-code sweep + final checks

**Files:**
- Modify: `tests/unit/models/test_host_specs.py` (registry-pairs drift guard)
- Verify: ruff / ty across touched modules

- [ ] **Step 1: Add a drift guard over every *registered* `(cls, spec)` pair**

The 2a `HOST_SPEC_RUNTIME_PAIRS` guard covers the hardcoded built-ins. Add a guard that reads the live registry, so a future third-party registration (and the built-ins, via the same path) is covered. Append to `tests/unit/models/test_host_specs.py`:

```python
def test_registered_pairs_drift_guard():
    """Every registered (host_class, spec) pair has matching field sets — the
    same bidirectional check as HOST_SPEC_RUNTIME_PAIRS, but sourced from the
    live registry so it covers built-ins registered through register_host_class.
    """
    from otto.host.os_profile import _HOST_CLASSES, _HOST_SPECS
    for name, spec_cls in _HOST_SPECS.items():
        runtime_cls = _HOST_CLASSES[name]
        spec_fields = set(spec_cls.model_fields) - {"labs"}
        init_fields = {
            f.name for f in dataclasses.fields(runtime_cls)
            if f.init and not f.name.startswith("_")
        }
        assert spec_fields == init_fields, (
            f"{name}: {spec_cls.__name__} <-> {runtime_cls.__name__} mismatch — "
            f"spec-only={sorted(spec_fields - init_fields)}, "
            f"runtime-only={sorted(init_fields - spec_fields)}"
        )
```

(`dataclasses` is already imported at the top of this test file.)

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/models/test_host_specs.py::test_registered_pairs_drift_guard -q`
Expected: PASS (unix/embedded/zephyr all balance).

- [ ] **Step 3: Lint + type-check the touched modules**

Run:
```bash
ruff check src/otto/host/remote_host.py src/otto/host/unix_host.py \
           src/otto/host/embedded_host.py src/otto/host/os_profile.py \
           src/otto/models/host.py src/otto/storage/factory.py \
           src/otto/monitor/factory.py
ty check src/otto
```
Expected: ruff clean (fix any unused-import / I001 it flags from the factory rewrite); `ty` 0 diagnostics. Fix in place and re-run.

- [ ] **Step 4: Run the full unit surface this plan touches**

Run:
```bash
pytest tests/unit/host tests/unit/models tests/unit/storage \
       tests/unit/monitor tests/unit/configmodule -q
```
Expected: PASS. (This is the targeted, hermetic subset — **not** `make test`/`make coverage`, which are Chris's gate.)

- [ ] **Step 5: Stage**

```bash
git add tests/unit/models/test_host_specs.py
```

---

## Hand-off after Task 7

- **Do not commit.** Report the staged file list and the exact `pytest` lines that passed. Chris commits (paste-able message suggested: `feat(models): integrate host specs — registry carries spec, factory collapse, command_frame promotion, interfaces/address_for (Phase A 2b)`).
- Flag the one intended behavior change for the commit body: misplaced/typo'd host keys that `_all_slots` silently dropped now raise a pydantic `ValidationError` with a field suggestion (the `extra='forbid'` payoff).
- Note for Chris's pre-merge gate (not for the agent to run): `make test` (live VM tiers), `make coverage` (≥90%), `make nox`. No `docs/` RST/autodoc references changed, so `make docs` is not required by this plan — but mention it as a courtesy check if Chris's docs pull in these docstrings.
- Next: **Plan 3** (settings: `SettingsModel` + docker + os_profiles + reservation envelope + `pydantic-settings` `OTTO_*`).

---

## Self-Review

**Spec coverage** (against `2026-06-14-pydantic-phase-a-design.md` §2):
- `register_host_class(name, cls, spec)` carrying the spec, MRO default, otto dogfoods built-ins → Task 3. ✓
- Generic factory collapse (`merge → model_validate → spec.to_host`), delete `_create_*`/family-branching → Task 5. ✓
- M1 dict-space precedence merge, per-key `*_options` → Task 5 `_merge_host_dict`. ✓
- `command_frame` promoted to common + the one `UnixHost` runtime field touch → Task 2. ✓
- `interfaces` / `address_for`; `ip` stays literal; SNMP resolves via `address_for` → Tasks 1 + 6. ✓
- Drift guard over every registered `(cls, spec)` pair → Task 7. ✓
- Family-specific validation intrinsic to the spec (`docker_capable` rejected, embedded `transfer` Literal, `filesystem`/`command_frame` registry membership) → already on the specs + Task 4 validators. ✓

**Placeholder scan:** the only deliberately-open items are (a) confirming the registered no-op filesystem key (`"none"` vs actual) in Task 4 Step 1, and (b) matching the monitor-factory test fixtures in Task 6 Step 1 — both are "read this file and use its real names," not unspecified logic. Every code change shows complete code.

**Type/name consistency:** `build_host_spec` / `_HOST_SPECS` / `_nearest_registered_spec` (Task 3) are the exact names Task 5's factory imports. `_merge_host_dict` signature matches its two call sites. `OPTIONS_KEYS` keeps its name + 6-key membership for `configmodule/repo.py`. `command_frame`/`interfaces` added to both spec and runtime within the same task so the bidirectional drift guard never goes red between commits.
