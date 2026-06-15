# Pydantic Phase A — Plan 2a: Host Spec Models — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Repo commit convention:** this repo's `prepare-commit-msg` hook needs `/dev/tty`; agent self-commits mis-tag the AI-assist trailer. Each task's final step **stages** the change (`git add`) and records the commit message — **Chris runs the actual `git commit`.** Do not invoke `git commit` yourself.

**Goal:** Add pydantic boundary models for the host record — `HostSpec` + `UnixHostSpec`/`EmbeddedHostSpec` (+ `ToolchainSpec`) — that validate a `hosts.json` entry and build the **unchanged** runtime `UnixHost`/`EmbeddedHost` via `to_host()`, reproducing today's `create_host_from_dict` output exactly.

**Architecture:** Pure additive models in the existing leaf package `src/otto/models/`. The specs nest Plan 1's `*OptionsSpec`s; a `HostSpec.to_host(cls)` builder maps validated fields to runtime constructor kwargs (reusing `to_runtime()` and the embedded registry resolvers). No runtime classes change; no new schema (`interfaces`/`command_frame`-promotion land in Plan 2b). A drift guard keeps each spec's fields a subset of its runtime class's init fields; a parity test asserts `to_host()` matches `create_host_from_dict`.

**Tech Stack:** Python 3.10+, pydantic v2, pytest.

---

## File Structure

- Create: `src/otto/models/host.py` — `ToolchainSpec`, `HostSpec`, `UnixHostSpec`, `EmbeddedHostSpec`
- Modify: `src/otto/models/__init__.py` — re-export the new specs
- Create: `tests/unit/models/test_host_specs.py` — validation, build, drift guard, factory parity
- **Unchanged:** `src/otto/host/{unix_host,embedded_host,toolchain}.py`, `src/otto/storage/factory.py` (the parity target; collapsed later in 2b)

**Field partition (derived from the runtime dataclasses — the drift guard enforces it):**

- **`HostSpec` (base, common to both families):** `ip`, `element`, `creds`, `name`, `os_type`, `os_name`, `os_version`, `user`, `element_id`, `board`, `slot`, `hop`, `is_virtual`, `default_dest_dir`, `max_filename_len`, `resources`, `log`, `log_stdout`, `telnet_options`, `snmp`, `toolchain`, `labs` (membership — validated but **not** passed to the host constructor). `log_stdout` is common — **both** `UnixHost` and `EmbeddedHost` declare it.
- **`UnixHostSpec` adds:** `creds` (overridden to **required**), `hw_version`, `sw_version`, `term` (`TermType`), `docker_capable`, `transfer` (`FileTransferType`), `ssh_options`, `sftp_options`, `scp_options`, `ftp_options`, `nc_options`.
- **`EmbeddedHostSpec` adds:** `transfer` (`EmbeddedTransferType`), `filesystem`/`command_frame`/`loader` (registry-name strings resolved at build time).

`telnet_options` is common (both families have it). `transfer` lives on each family because its `Literal` type differs. `command_frame` stays embedded-only here — it is promoted to the base (with the `UnixHost` runtime field) in **Plan 2b**.

---

### Task 1: `ToolchainSpec`

**Files:**
- Create: `src/otto/models/host.py`
- Test: `tests/unit/models/test_host_specs.py`

Runtime target `otto.host.toolchain.Toolchain` (`@dataclass(slots=True)`): `sysroot: Path = Path('/')`, `lcov: Path = Path('usr/bin/lcov')`, `gcov: Path = Path('usr/bin/gcov')`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/models/test_host_specs.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.host.toolchain import Toolchain
from otto.models.host import ToolchainSpec


def test_toolchain_spec_defaults_match_runtime():
    rt = ToolchainSpec().to_runtime()
    assert isinstance(rt, Toolchain)
    assert rt.sysroot == Path("/")
    assert rt.lcov == Path("usr/bin/lcov")
    assert rt.gcov == Path("usr/bin/gcov")


def test_toolchain_spec_coerces_str_paths():
    rt = ToolchainSpec(sysroot="/opt/arm", gcov="bin/arm-gcov").to_runtime()
    assert rt.sysroot == Path("/opt/arm")
    assert rt.gcov == Path("bin/arm-gcov")
    assert rt.lcov == Path("usr/bin/lcov")  # untouched default


def test_toolchain_spec_forbids_unknown():
    with pytest.raises(ValidationError):
        ToolchainSpec(sysrot="/x")  # typo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k toolchain -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.models.host'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/otto/models/host.py`:

```python
"""Pydantic boundary specs for the host record (a ``hosts.json`` entry).

``HostSpec`` and its family subclasses validate a host dict and build the
unchanged runtime ``UnixHost`` / ``EmbeddedHost`` via ``to_host()``. The specs
nest the per-protocol ``*OptionsSpec``s from ``otto.models.options`` and reuse
their ``to_runtime()`` builders; embedded registry-name fields
(``filesystem`` / ``command_frame`` / ``loader``) resolve through the existing
host registries at build time.
"""

from __future__ import annotations

from pathlib import Path

from ..host.toolchain import Toolchain
from .base import OttoModel


class ToolchainSpec(OttoModel):
    sysroot: Path = Path("/")
    lcov: Path = Path("usr/bin/lcov")
    gcov: Path = Path("usr/bin/gcov")

    def to_runtime(self) -> Toolchain:
        return Toolchain(sysroot=self.sysroot, lcov=self.lcov, gcov=self.gcov)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k toolchain -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_specs.py
# commit message: feat(models): add ToolchainSpec
```

---

### Task 2: `HostSpec` base — common fields + `_common_host_kwargs()`

**Files:**
- Modify: `src/otto/models/host.py`
- Test: `tests/unit/models/test_host_specs.py`

`HostSpec` is the abstract base: it declares the common fields and a helper that produces the constructor kwargs shared by both families. It is not built directly (families call `to_host`).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/models/test_host_specs.py`:

```python
from otto.models.host import HostSpec


def test_hostspec_requires_ip_and_element():
    with pytest.raises(ValidationError) as exc:
        HostSpec(ip="10.0.0.1")  # missing element
    assert "element" in str(exc.value)


def test_hostspec_forbids_unknown_field():
    with pytest.raises(ValidationError) as exc:
        HostSpec(ip="10.0.0.1", element="lab", lab=["x"])  # typo: lab vs labs
    assert "lab" in str(exc.value)


def test_hostspec_accepts_labs_and_coerces_resources_to_set():
    spec = HostSpec(ip="10.0.0.1", element="lab", labs=["a"], resources=["r1", "r1"])
    assert spec.labs == ["a"]
    assert spec.resources == {"r1"}


def test_common_host_kwargs_omits_unset_and_excludes_labs():
    spec = HostSpec(ip="10.0.0.1", element="lab", labs=["a"])
    kw = spec._common_host_kwargs()
    assert "labs" not in kw                  # membership, never a host field
    assert kw["ip"] == "10.0.0.1"
    assert kw["element"] == "lab"
    # unset common fields are omitted so the host class's own default applies
    for absent in ("os_name", "resources", "telnet_options", "snmp", "toolchain"):
        assert absent not in kw


def test_common_host_kwargs_builds_nested_when_set():
    spec = HostSpec(
        ip="10.0.0.1", element="lab",
        resources=["r1"], telnet_options={"port": 99}, toolchain={"sysroot": "/opt"},
    )
    kw = spec._common_host_kwargs()
    from otto.host.options import TelnetOptions
    from otto.host.toolchain import Toolchain
    assert kw["resources"] == {"r1"}
    assert isinstance(kw["telnet_options"], TelnetOptions) and kw["telnet_options"].port == 99
    assert isinstance(kw["toolchain"], Toolchain) and kw["toolchain"].sysroot == Path("/opt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k "hostspec or common_host_kwargs" -v`
Expected: FAIL — `ImportError: cannot import name 'HostSpec'`.

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `src/otto/models/host.py` (with the existing ones):

```python
from typing import Any

from .options import SnmpOptionsSpec, TelnetOptionsSpec
```

Append the module-level constant and the base class:

```python
# Common fields passed straight through to the host constructor (no conversion).
# Conversions for default_dest_dir/resources/telnet_options/snmp/toolchain are
# applied separately in _common_host_kwargs.
_COMMON_PLAIN_FIELDS = (
    "ip", "element", "creds", "name", "os_type", "os_name", "os_version",
    "user", "element_id", "board", "slot", "hop", "is_virtual",
    "max_filename_len", "log", "log_stdout",
)


class HostSpec(OttoModel):
    # --- required identity (both families) ---
    ip: str
    element: str

    # --- common optional fields ---
    creds: dict[str, str] = {}
    name: str | None = None
    os_type: str = "unix"
    os_name: str | None = None
    os_version: str | None = None
    user: str | None = None
    element_id: int | None = None
    board: str | None = None
    slot: int | None = None
    hop: str | None = None
    is_virtual: bool = False
    default_dest_dir: Path = Path()
    max_filename_len: int = 255
    resources: set[str] = set()
    log: bool = True
    log_stdout: bool = True  # common: both UnixHost and EmbeddedHost declare it
    telnet_options: TelnetOptionsSpec = TelnetOptionsSpec()
    snmp: SnmpOptionsSpec | None = None
    toolchain: ToolchainSpec = ToolchainSpec()

    # Lab membership — validated (so a `lab`/`labs` typo errors) but NOT a host
    # constructor argument; the repository uses it to filter hosts into a Lab.
    labs: list[str] = []

    def _common_host_kwargs(self) -> dict[str, Any]:
        """Constructor kwargs for the common fields the spec *explicitly set*.

        Mirrors the factory: a field absent from the source dict is omitted so
        the host class's own default applies — including subclass overrides
        (``UnixHost.os_name='Linux'``, ``ZephyrHost.os_name='Zephyr'``). Passing
        every field unconditionally would clobber those defaults with the spec's
        neutral ones. ``labs`` is never a constructor argument.
        """
        s = self.model_fields_set
        kw: dict[str, Any] = {n: getattr(self, n) for n in _COMMON_PLAIN_FIELDS if n in s}
        if "default_dest_dir" in s:
            kw["default_dest_dir"] = Path(self.default_dest_dir)
        if "resources" in s:
            kw["resources"] = set(self.resources)
        if "telnet_options" in s:
            kw["telnet_options"] = self.telnet_options.to_runtime()
        if "snmp" in s:
            kw["snmp"] = self.snmp.to_runtime() if self.snmp is not None else None
        if "toolchain" in s:
            kw["toolchain"] = self.toolchain.to_runtime()
        return kw
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k "hostspec or common_host_kwargs" -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_specs.py
# commit message: feat(models): add HostSpec base (common host fields + kwargs helper)
```

---

### Task 3: `UnixHostSpec` + `to_host()`

**Files:**
- Modify: `src/otto/models/host.py`
- Test: `tests/unit/models/test_host_specs.py`

`UnixHostSpec` adds the unix-only fields, overrides `creds` to required, nests the five SSH/transfer option specs, and builds a `UnixHost`.

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.unix_host import UnixHost
from otto.models.host import UnixHostSpec


def test_unix_spec_requires_creds():
    with pytest.raises(ValidationError) as exc:
        UnixHostSpec(ip="10.0.0.1", element="lab")  # creds required for unix
    assert "creds" in str(exc.value)


def test_unix_spec_builds_unix_host_with_defaults():
    spec = UnixHostSpec(ip="10.0.0.1", element="lab", creds={"u": "p"})
    host = spec.to_host()
    assert isinstance(host, UnixHost)
    assert host.ip == "10.0.0.1"
    assert host.term == "ssh"
    assert host.transfer == "scp"
    assert host.os_type == "unix"
    assert host.ssh_options.port == 22


def test_unix_spec_builds_nested_options_and_snmp():
    spec = UnixHostSpec(
        ip="10.0.0.1", element="lab", creds={"u": "p"},
        ssh_options={"port": 2222, "extra": {"x": 1}},
        snmp={"oids": ["1.3.6.1.2.1.1.3.0"], "port": 16101},
        resources=["r1"], labs=["veggies"],
    )
    host = spec.to_host()
    assert host.ssh_options.port == 2222
    assert host.ssh_options.extra == {"x": 1}
    assert host.snmp is not None and host.snmp.oids == ("1.3.6.1.2.1.1.3.0",)
    assert host.resources == {"r1"}


def test_unix_spec_rejects_embedded_only_field():
    with pytest.raises(ValidationError):
        UnixHostSpec(ip="1.1.1.1", element="lab", creds={"u": "p"}, filesystem="littlefs")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k unix_spec -v`
Expected: FAIL — `ImportError: cannot import name 'UnixHostSpec'`.

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `src/otto/models/host.py`:

```python
from ..host.host import FileTransferType, TermType
from ..host.unix_host import UnixHost
from .options import (
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SshOptionsSpec,
)
```

Append:

```python
class UnixHostSpec(HostSpec):
    creds: dict[str, str]  # override: required for a Unix host (SSH/telnet login)
    hw_version: str | None = None
    sw_version: str | None = None
    term: TermType = "ssh"
    docker_capable: bool = False
    transfer: FileTransferType = "scp"
    ssh_options: SshOptionsSpec = SshOptionsSpec()
    sftp_options: SftpOptionsSpec = SftpOptionsSpec()
    scp_options: ScpOptionsSpec = ScpOptionsSpec()
    ftp_options: FtpOptionsSpec = FtpOptionsSpec()
    nc_options: NcOptionsSpec = NcOptionsSpec()

    def to_host(self, cls: type[UnixHost] = UnixHost) -> UnixHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        for n in ("hw_version", "sw_version", "term",
                  "docker_capable", "transfer"):
            if n in s:
                kw[n] = getattr(self, n)
        for n in ("ssh_options", "sftp_options", "scp_options",
                  "ftp_options", "nc_options"):
            if n in s:
                kw[n] = getattr(self, n).to_runtime()
        return cls(**kw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k unix_spec -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_specs.py
# commit message: feat(models): add UnixHostSpec + to_host()
```

---

### Task 4: `EmbeddedHostSpec` + `to_host()` (registry-string resolution)

**Files:**
- Modify: `src/otto/models/host.py`
- Test: `tests/unit/models/test_host_specs.py`

`EmbeddedHostSpec` adds the embedded-only fields. `filesystem`/`command_frame`/`loader` are registry-name strings, resolved to instances at build time only when present (an absent field leaves the runtime class's own default — mirroring today's factory).

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.command_frame import ZephyrFrame
from otto.host.embedded_filesystem import NoFileSystem
from otto.host.embedded_host import EmbeddedHost
from otto.models.host import EmbeddedHostSpec


def test_embedded_spec_builds_with_command_frame():
    spec = EmbeddedHostSpec(ip="192.0.2.1", element="dut", command_frame="zephyr")
    host = spec.to_host()
    assert isinstance(host, EmbeddedHost)
    assert host.os_type == "embedded"
    assert isinstance(host.command_frame, ZephyrFrame)


def test_embedded_spec_absent_filesystem_keeps_runtime_default():
    spec = EmbeddedHostSpec(ip="192.0.2.1", element="dut", command_frame="zephyr")
    host = spec.to_host()
    assert isinstance(host.filesystem, NoFileSystem)  # EmbeddedHost default


def test_embedded_spec_rejects_unknown_filesystem():
    spec = EmbeddedHostSpec(ip="192.0.2.1", element="dut", filesystem="bogusfs")
    with pytest.raises(ValueError):
        spec.to_host()  # build_filesystem raises on an unregistered name


def test_embedded_spec_rejects_unix_only_field():
    with pytest.raises(ValidationError):
        EmbeddedHostSpec(ip="192.0.2.1", element="dut", docker_capable=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k embedded_spec -v`
Expected: FAIL — `ImportError: cannot import name 'EmbeddedHostSpec'`.

- [ ] **Step 3: Write minimal implementation**

Add imports at the top of `src/otto/models/host.py`:

```python
from ..host.binary_loader import build_binary_loader
from ..host.command_frame import build_command_frame
from ..host.embedded_filesystem import build_filesystem
from ..host.embedded_host import EmbeddedHost
from ..host.embedded_transfer import EmbeddedTransferType
```

Append:

```python
class EmbeddedHostSpec(HostSpec):
    os_type: str = "embedded"
    transfer: EmbeddedTransferType = "console"
    filesystem: str | None = None
    command_frame: str | None = None
    loader: str | None = None

    def to_host(self, cls: type[EmbeddedHost] = EmbeddedHost) -> EmbeddedHost:
        kw = self._common_host_kwargs()
        s = self.model_fields_set
        if "transfer" in s:
            kw["transfer"] = self.transfer
        if "filesystem" in s and self.filesystem is not None:
            kw["filesystem"] = build_filesystem(self.filesystem)
        if "command_frame" in s and self.command_frame is not None:
            kw["command_frame"] = build_command_frame(self.command_frame)
        if "loader" in s and self.loader is not None:
            kw["loader"] = build_binary_loader(self.loader)
        return cls(**kw)
```

> **Note (flagged for review):** today's `create_host_from_dict` resolves
> `filesystem` and `command_frame` strings but does **not** resolve `loader`
> from lab data (it has no loader builder call). `EmbeddedHostSpec` resolves
> `loader` via `build_binary_loader`, consistent with the other two
> registry-name fields. This closes a small gap rather than mirroring the gap;
> no shipped lab data is expected to set `loader` (it is normally provided by a
> host subclass/profile in code). If parity-only is preferred, drop the `loader`
> field + resolution.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k embedded_spec -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_specs.py
# commit message: feat(models): add EmbeddedHostSpec + to_host()
```

---

### Task 5: Host drift guard + factory parity

**Files:**
- Modify: `src/otto/models/host.py` (add a `HOST_SPEC_RUNTIME_PAIRS` registry)
- Test: `tests/unit/models/test_host_specs.py`

The drift guard asserts every spec field (except `labs`) is an init parameter of its runtime class. The parity test asserts `to_host()` reproduces `create_host_from_dict()` for a representative host dict — the contract 2b relies on when it swaps the factory onto these specs.

- [ ] **Step 1: Write the failing test**

Append:

```python
import dataclasses

from otto.storage.factory import create_host_from_dict
from otto.models.host import HOST_SPEC_RUNTIME_PAIRS


@pytest.mark.parametrize("spec_cls,runtime_cls", HOST_SPEC_RUNTIME_PAIRS)
def test_host_spec_fields_match_runtime_init(spec_cls, runtime_cls):
    """Bidirectional: every spec field maps to a constructor param AND every
    public init field of the runtime class is exposed by the spec. Catches a
    spec that *forgot* a runtime field (one-directional ``⊆`` would miss it).
    ``labs`` is the only allowed spec-only field (lab membership, not a host arg).
    """
    spec_fields = set(spec_cls.model_fields) - {"labs"}
    init_fields = {
        f.name for f in dataclasses.fields(runtime_cls)
        if f.init and not f.name.startswith("_")
    }
    assert spec_fields == init_fields, (
        f"{spec_cls.__name__} <-> {runtime_cls.__name__} field mismatch — "
        f"spec-only={sorted(spec_fields - init_fields)}, "
        f"runtime-only (spec forgot)={sorted(init_fields - spec_fields)}"
    )


def test_unix_to_host_matches_factory():
    d = {
        "ip": "10.10.200.11", "element": "carrot", "os_type": "unix",
        "board": "seed", "term": "ssh", "transfer": "scp", "is_virtual": True,
        "creds": {"vagrant": "vagrant"}, "resources": ["carrot"], "labs": ["veggies"],
        "ssh_options": {"port": 2200},
    }
    spec_host = UnixHostSpec.model_validate(d).to_host()
    factory_host = create_host_from_dict(d)
    for attr in ("ip", "element", "os_type", "os_name", "os_version", "board",
                 "term", "transfer", "is_virtual", "creds", "resources", "name",
                 "hop", "user"):
        assert getattr(spec_host, attr) == getattr(factory_host, attr), attr
    assert spec_host.ssh_options.port == factory_host.ssh_options.port == 2200


def test_embedded_to_host_matches_factory():
    d = {
        "ip": "192.0.2.1", "element": "dut", "os_type": "embedded",
        "command_frame": "zephyr", "telnet_options": {"port": 9023},
    }
    spec_host = EmbeddedHostSpec.model_validate(d).to_host()
    factory_host = create_host_from_dict(d)
    assert type(spec_host) is type(factory_host)
    assert spec_host.telnet_options.port == factory_host.telnet_options.port == 9023
    assert type(spec_host.command_frame) is type(factory_host.command_frame)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k "match_runtime or matches_factory" -v`
Expected: FAIL — `ImportError: cannot import name 'HOST_SPEC_RUNTIME_PAIRS'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/otto/models/host.py`:

```python
HOST_SPEC_RUNTIME_PAIRS: list[tuple[type[HostSpec], type]] = [
    (UnixHostSpec, UnixHost),
    (EmbeddedHostSpec, EmbeddedHost),
]
"""Each host spec paired with the runtime class it builds. Drives the drift
guard so a spec field that has no constructor counterpart is caught."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k "match_runtime or matches_factory" -v`
Expected: PASS (4 passed: 2 parametrized drift + 2 parity).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_specs.py
# commit message: feat(models): add host drift guard + factory parity tests
```

---

### Task 6: Re-exports + verification gate

**Files:**
- Modify: `src/otto/models/__init__.py`
- Test/verify: full model suite + ty + ruff

- [ ] **Step 1: Add re-exports**

In `src/otto/models/__init__.py`, add to the imports and `__all__`:

```python
from .host import (
    EmbeddedHostSpec,
    HostSpec,
    ToolchainSpec,
    UnixHostSpec,
)
```

Add these four names to the `__all__` list (after the option-spec names).

- [ ] **Step 2: Verify the package imports**

Run: `uv run python -c "from otto.models import HostSpec, UnixHostSpec, EmbeddedHostSpec, ToolchainSpec; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Run the full model test suite**

Run: `uv run pytest tests/unit/models -v`
Expected: all pass (Plan 1's `test_option_specs.py` + this plan's `test_host_specs.py`).

- [ ] **Step 4: Type-check + lint**

Run: `uv run ty check src/otto/models && uv run ruff check src/otto/models tests/unit/models`
Expected: `All checks passed!` for both.

- [ ] **Step 5: Confirm no runtime regression (no VM)**

Run: `uv run pytest tests/unit --collect-only -q`
Expected: collection succeeds (no import breakage); count ≥ the Plan 1 baseline.

- [ ] **Step 6: Stage (Chris commits)**

```bash
git add src/otto/models/__init__.py
# commit message: feat(models): re-export host specs from otto.models
```

---

## Self-Review

**Spec coverage (this plan = the host-models half of the spec's "Host models" section, *minus* the runtime-touching parts deferred to 2b):**
- `HostSpec` + `UnixHostSpec`/`EmbeddedHostSpec`, `extra='forbid'`, exhaustive fields → Tasks 2–4. ✓
- Nests Plan 1 option specs; `to_host()` reuses `to_runtime()` → Tasks 3–4. ✓
- `ToolchainSpec` (toolchain is a common nested field) → Task 1. ✓
- Embedded registry-name resolution (`filesystem`/`command_frame`/`loader`) → Task 4. ✓
- `creds` required-for-unix / optional-for-embedded → Task 3 (override). ✓
- Drift guard + factory parity (the contract 2b depends on) → Task 5. ✓
- **Deferred to 2b (not here):** `command_frame` promotion to base + `UnixHost` runtime field; `interfaces`/`address_for`; the `register_host_class(spec)` registry change; the generic factory collapse + call-site wiring; `EmbeddedHostSpec`'s `docker_capable` rejection validator (today handled by the factory/`validate_host_dict`, which still run in 2a — `docker_capable` simply isn't a field on `EmbeddedHostSpec`, so `extra='forbid'` already rejects it, covered by `test_embedded_spec_rejects_unix_only_field`).

**Placeholder scan:** none. The Task 4 `loader` note is a flagged decision with a concrete fallback, not a placeholder.

**Type consistency:** `to_host()` / `to_runtime()` used consistently; `_common_host_kwargs()` defined in Task 2 and consumed in Tasks 3–4; `HOST_SPEC_RUNTIME_PAIRS` defined in Task 5; field types match the runtime dataclasses (`TermType`/`FileTransferType`/`EmbeddedTransferType` imported from their definition modules); `creds` override is valid pydantic subclass narrowing.

---

## Notes for Plan 2b (not implemented here)

- Promote `command_frame` to `HostSpec` (base) and add the `UnixHost.command_frame` runtime field (default `BashFrame`, wired to `SessionManager`); move it off `EmbeddedHostSpec`.
- Add `interfaces: dict[str, str]` to `HostSpec` + the runtime field + `address_for()`; `SnmpOptionsSpec.address` resolves through it.
- `register_host_class(name, cls, spec=None)` carrying the spec; otto registers built-ins with their specs; spec defaults to nearest base's via MRO.
- Collapse `create_host_from_dict`/`validate_host_dict` onto `spec.to_host()` + the M1 precedence merge; wire `json_repository.py` + `completion_cache.py`; delete the per-family factory functions.
- Re-validate the parity tests from Task 5 still hold after the factory swap.
