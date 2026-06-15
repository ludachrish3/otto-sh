# Pydantic Phase A — Plan 1: Foundation + Option Two-Type Split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Repo commit convention:** this repo's `prepare-commit-msg` hook needs `/dev/tty`; agent self-commits mis-tag the AI-assist trailer. Each task's final step **stages** the change (`git add`) and records the commit message — **Chris runs the actual `git commit`.** Do not invoke `git commit` yourself.

**Goal:** Add pydantic v2 at the option-table boundary — a leaf `*OptionsSpec` model per protocol that validates lab-JSON option blocks and builds the existing, unchanged runtime `*Options` dataclasses.

**Architecture:** A new leaf package `src/otto/models/` holds pydantic boundary models. Each protocol gets a `*OptionsSpec` (pydantic, `extra='forbid'`) that validates external data and a `to_runtime()` method that constructs the existing `host/options.py` dataclass. The runtime dataclasses and their consumers (`connections.py`/`transfer.py`/`session.py`) are **not touched**. A drift-guard test keeps each spec's field set a subset of its runtime twin.

**Tech Stack:** Python 3.10+, pydantic v2, pydantic-settings, pytest.

---

## File Structure

- Create: `src/otto/models/__init__.py` — package marker + public re-exports
- Create: `src/otto/models/base.py` — `OttoModel` shared base (`extra='forbid'`)
- Create: `src/otto/models/options.py` — forward specs + the eight `*OptionsSpec` + `to_runtime()`
- Create: `tests/unit/models/__init__.py`
- Create: `tests/unit/models/test_option_specs.py` — per-spec validation/coercion + drift guard
- Modify: `pyproject.toml` — add `pydantic` + `pydantic-settings` dependencies
- **Unchanged:** `src/otto/host/options.py` (runtime dataclasses are the build target)

**Naming convention (locked for all Phase A plans):** every pydantic boundary model that mirrors a runtime type carries the `Spec` suffix (`SshOptionsSpec`, `LocalPortForwardSpec`, …). Standalone boundary models with no runtime twin do not (`OttoModel`, later `SettingsModel`, `MetricPoint`).

---

### Task 1: Add pydantic dependencies

**Files:**
- Modify: `pyproject.toml` (the `dependencies` array, starts line 38)

- [ ] **Step 1: Add the dependencies**

In `pyproject.toml`, inside the `dependencies = [` array, add two entries (keep the array alphabetic if it already is):

```toml
    "pydantic>=2.6,<3",
    "pydantic-settings>=2.2,<3",
```

- [ ] **Step 2: Sync and verify import**

Run: `uv sync && uv run python -c "import pydantic, pydantic_settings; print(pydantic.VERSION)"`
Expected: prints a `2.x` version, no error.

- [ ] **Step 3: Stage (Chris commits)**

```bash
git add pyproject.toml uv.lock
# commit message: build(deps): add pydantic + pydantic-settings for boundary models
```

---

### Task 2: `OttoModel` shared base

**Files:**
- Create: `src/otto/models/__init__.py`
- Create: `src/otto/models/base.py`
- Create: `tests/unit/models/__init__.py`
- Test: `tests/unit/models/test_option_specs.py` (created here, grown later)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/models/__init__.py` (empty), then create `tests/unit/models/test_option_specs.py`:

```python
import pytest
from pydantic import ValidationError

from otto.models.base import OttoModel


class _Sample(OttoModel):
    x: int = 1


def test_otto_model_forbids_unknown_fields():
    with pytest.raises(ValidationError) as exc:
        _Sample(x=1, nope=2)
    # extra='forbid' surfaces the offending key
    assert "nope" in str(exc.value)


def test_otto_model_accepts_known_fields():
    assert _Sample(x=5).x == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.models'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/otto/models/__init__.py`:

```python
"""Pydantic boundary models — the validation layer for external data
(lab JSON, settings.toml, OTTO_* env, monitor import/export).

These spec models depend on the runtime data modules they validate and build
(``otto.host.options``, ``otto.host.transfer``); those runtime modules do not
import from here, so the dependency runs one way (models -> runtime data) with
no cycle. Each model mirroring a runtime type carries the ``Spec`` suffix.
"""

from .base import OttoModel

__all__ = ["OttoModel"]
```

Create `src/otto/models/base.py`:

```python
from pydantic import BaseModel, ConfigDict


class OttoModel(BaseModel):
    """Base for every otto boundary model.

    ``extra='forbid'`` turns a typo'd or unknown config field into a
    validation error that names the offending key (instead of silently
    dropping it, as the old hand-rolled merge did).
    """

    model_config = ConfigDict(extra="forbid")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/__init__.py src/otto/models/base.py tests/unit/models/
# commit message: feat(models): add OttoModel boundary base (extra='forbid')
```

---

### Task 3: Forward specs (SSH structured forwards)

**Files:**
- Create: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

The runtime targets in `src/otto/host/options.py` are the frozen dataclasses
`LocalPortForward`, `RemotePortForward` (`listen_host`, `listen_port`,
`dest_host`, `dest_port`) and `SocksForward` (`listen_host`, `listen_port`).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/models/test_option_specs.py`:

```python
from otto.host.options import LocalPortForward, RemotePortForward, SocksForward
from otto.models.options import (
    LocalPortForwardSpec,
    RemotePortForwardSpec,
    SocksForwardSpec,
)


def test_local_forward_spec_builds_runtime():
    spec = LocalPortForwardSpec(
        listen_host="127.0.0.1", listen_port=8080,
        dest_host="10.0.0.1", dest_port=80,
    )
    rt = spec.to_runtime()
    assert isinstance(rt, LocalPortForward)
    assert rt == LocalPortForward("127.0.0.1", 8080, "10.0.0.1", 80)


def test_remote_forward_spec_builds_runtime():
    spec = RemotePortForwardSpec(
        listen_host="0.0.0.0", listen_port=2222,
        dest_host="127.0.0.1", dest_port=22,
    )
    assert spec.to_runtime() == RemotePortForward("0.0.0.0", 2222, "127.0.0.1", 22)


def test_socks_forward_spec_builds_runtime():
    spec = SocksForwardSpec(listen_host="127.0.0.1", listen_port=1080)
    assert spec.to_runtime() == SocksForward("127.0.0.1", 1080)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k forward -v`
Expected: FAIL — `ImportError: cannot import name 'LocalPortForwardSpec'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/otto/models/options.py`:

```python
"""Boundary specs for the per-protocol ``*_options`` tables.

Each ``*OptionsSpec`` validates the JSON-serializable curated fields of its
protocol and builds the matching runtime dataclass from
``otto.host.options`` via ``to_runtime()``. The runtime dataclasses (which
carry the library adapters, callables, and open ``extra`` dicts) are never
modified here.
"""

from __future__ import annotations

from typing import Any

from ..host import options as rt
from .base import OttoModel


class LocalPortForwardSpec(OttoModel):
    listen_host: str
    listen_port: int
    dest_host: str
    dest_port: int

    def to_runtime(self) -> rt.LocalPortForward:
        return rt.LocalPortForward(
            self.listen_host, self.listen_port, self.dest_host, self.dest_port
        )


class RemotePortForwardSpec(OttoModel):
    listen_host: str
    listen_port: int
    dest_host: str
    dest_port: int

    def to_runtime(self) -> rt.RemotePortForward:
        return rt.RemotePortForward(
            self.listen_host, self.listen_port, self.dest_host, self.dest_port
        )


class SocksForwardSpec(OttoModel):
    listen_host: str
    listen_port: int

    def to_runtime(self) -> rt.SocksForward:
        return rt.SocksForward(self.listen_host, self.listen_port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k forward -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add SSH forward specs
```

---

### Task 4: `SshOptionsSpec` (library-forwarding, with `extra` + forwards)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime target `SshOptions` curated fields (from `host/options.py`): `port`,
`known_hosts`, `connect_timeout`, `keepalive_interval`, `keepalive_count_max`,
`client_keys`, `client_host_keys`, `agent_forwarding`, `preferred_auth`,
`encryption_algs`, `server_host_key_algs`, `compression_algs`,
`local_forwards`, `remote_forwards`, `socks_forwards`, `extra`. The code-only
`post_connect` callable is **omitted** from the spec.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/models/test_option_specs.py`:

```python
from otto.host.options import SshOptions
from otto.models.options import SshOptionsSpec


def test_ssh_spec_defaults_match_runtime_defaults():
    rt_obj = SshOptionsSpec().to_runtime()
    assert isinstance(rt_obj, SshOptions)
    assert rt_obj.port == 22
    assert rt_obj.known_hosts is None
    assert rt_obj.agent_forwarding is False


def test_ssh_spec_builds_forwards_and_extra():
    spec = SshOptionsSpec(
        port=2222,
        connect_timeout=5.0,
        local_forwards=[{
            "listen_host": "127.0.0.1", "listen_port": 8080,
            "dest_host": "10.0.0.1", "dest_port": 80,
        }],
        extra={"rekey_bytes": 1000000},
    )
    rt_obj = spec.to_runtime()
    assert rt_obj.port == 2222
    assert rt_obj.connect_timeout == 5.0
    assert rt_obj.local_forwards[0].dest_port == 80
    assert rt_obj.extra == {"rekey_bytes": 1000000}


def test_ssh_spec_rejects_unknown_top_level_key():
    with pytest.raises(ValidationError) as exc:
        SshOptionsSpec(connet_timeout=5.0)  # typo
    assert "connet_timeout" in str(exc.value)


def test_ssh_spec_has_no_post_connect_field():
    assert "post_connect" not in SshOptionsSpec.model_fields
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k ssh -v`
Expected: FAIL — `ImportError: cannot import name 'SshOptionsSpec'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/otto/models/options.py`:

```python
class SshOptionsSpec(OttoModel):
    port: int = 22
    known_hosts: Any = None
    connect_timeout: float | None = None
    keepalive_interval: float | None = None
    keepalive_count_max: int | None = None
    client_keys: list[str] | None = None
    client_host_keys: list[str] | None = None
    agent_forwarding: bool = False
    preferred_auth: str | list[str] | None = None
    encryption_algs: list[str] | None = None
    server_host_key_algs: list[str] | None = None
    compression_algs: list[str] | None = None
    local_forwards: list[LocalPortForwardSpec] = []
    remote_forwards: list[RemotePortForwardSpec] = []
    socks_forwards: list[SocksForwardSpec] = []
    extra: dict[str, Any] = {}

    def to_runtime(self) -> rt.SshOptions:
        return rt.SshOptions(
            port=self.port,
            known_hosts=self.known_hosts,
            connect_timeout=self.connect_timeout,
            keepalive_interval=self.keepalive_interval,
            keepalive_count_max=self.keepalive_count_max,
            client_keys=self.client_keys,
            client_host_keys=self.client_host_keys,
            agent_forwarding=self.agent_forwarding,
            preferred_auth=self.preferred_auth,
            encryption_algs=self.encryption_algs,
            server_host_key_algs=self.server_host_key_algs,
            compression_algs=self.compression_algs,
            local_forwards=[f.to_runtime() for f in self.local_forwards],
            remote_forwards=[f.to_runtime() for f in self.remote_forwards],
            socks_forwards=[f.to_runtime() for f in self.socks_forwards],
            extra=dict(self.extra),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k ssh -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add SshOptionsSpec with extra passthrough + forwards
```

---

### Task 5: `TelnetOptionsSpec` (str→bytes `login_prompt` validator)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime `TelnetOptions` fields: `port`, `write_chunk_size`, `write_chunk_delay`,
`cols`, `rows`, `encoding` (`str | bool`), `connect_timeout`,
`echo_negotiation_timeout`, `login_prompt` (`bytes`), `login`,
`single_client_console`, `auto_window_resize`, `extra`.

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.options import TelnetOptions
from otto.models.options import TelnetOptionsSpec


def test_telnet_spec_defaults_match_runtime():
    rt_obj = TelnetOptionsSpec().to_runtime()
    assert isinstance(rt_obj, TelnetOptions)
    assert rt_obj.port == 23
    assert rt_obj.cols == 400
    assert rt_obj.login_prompt == b":"


def test_telnet_spec_encodes_login_prompt_from_str():
    rt_obj = TelnetOptionsSpec(login_prompt="Password:").to_runtime()
    assert rt_obj.login_prompt == b"Password:"


def test_telnet_spec_accepts_encoding_false():
    rt_obj = TelnetOptionsSpec(encoding=False).to_runtime()
    assert rt_obj.encoding is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k telnet -v`
Expected: FAIL — `ImportError: cannot import name 'TelnetOptionsSpec'`.

- [ ] **Step 3: Write minimal implementation**

Add the import at the top of `options.py` (with the other typing imports):

```python
from pydantic import field_validator
```

Append the spec:

```python
class TelnetOptionsSpec(OttoModel):
    port: int = 23
    write_chunk_size: int = 0
    write_chunk_delay: float = 0.0
    cols: int = 400
    rows: int = 24
    encoding: str | bool = False
    connect_timeout: float | None = None
    echo_negotiation_timeout: float = 3.0
    login_prompt: bytes = b":"
    login: bool = True
    single_client_console: bool = False
    auto_window_resize: bool = False
    extra: dict[str, Any] = {}

    @field_validator("login_prompt", mode="before")
    @classmethod
    def _encode_login_prompt(cls, v: object) -> object:
        """Lab JSON carries the delimiter as a string; encode to bytes."""
        return v.encode() if isinstance(v, str) else v

    def to_runtime(self) -> rt.TelnetOptions:
        return rt.TelnetOptions(
            port=self.port,
            write_chunk_size=self.write_chunk_size,
            write_chunk_delay=self.write_chunk_delay,
            cols=self.cols,
            rows=self.rows,
            encoding=self.encoding,
            connect_timeout=self.connect_timeout,
            echo_negotiation_timeout=self.echo_negotiation_timeout,
            login_prompt=self.login_prompt,
            login=self.login,
            single_client_console=self.single_client_console,
            auto_window_resize=self.auto_window_resize,
            extra=dict(self.extra),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k telnet -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add TelnetOptionsSpec (str->bytes login_prompt)
```

---

### Task 6: `SftpOptionsSpec` + `ScpOptionsSpec` (library-forwarding, `extra`)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime `SftpOptions`: `env` (`dict|None`), `send_env` (`list[str]|None`),
`extra`. Runtime `ScpOptions`: `preserve` (False), `recurse` (True),
`block_size` (16384), `extra`.

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.options import ScpOptions, SftpOptions
from otto.models.options import ScpOptionsSpec, SftpOptionsSpec


def test_sftp_spec_defaults_and_extra():
    rt_obj = SftpOptionsSpec(extra={"block_size": 32768}).to_runtime()
    assert isinstance(rt_obj, SftpOptions)
    assert rt_obj.env is None
    assert rt_obj.extra == {"block_size": 32768}


def test_scp_spec_defaults_match_runtime():
    rt_obj = ScpOptionsSpec().to_runtime()
    assert isinstance(rt_obj, ScpOptions)
    assert rt_obj.recurse is True
    assert rt_obj.block_size == 16384
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k "sftp or scp" -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Append:

```python
class SftpOptionsSpec(OttoModel):
    env: dict[str, str] | None = None
    send_env: list[str] | None = None
    extra: dict[str, Any] = {}

    def to_runtime(self) -> rt.SftpOptions:
        return rt.SftpOptions(
            env=self.env, send_env=self.send_env, extra=dict(self.extra)
        )


class ScpOptionsSpec(OttoModel):
    preserve: bool = False
    recurse: bool = True
    block_size: int = 16384
    extra: dict[str, Any] = {}

    def to_runtime(self) -> rt.ScpOptions:
        return rt.ScpOptions(
            preserve=self.preserve,
            recurse=self.recurse,
            block_size=self.block_size,
            extra=dict(self.extra),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k "sftp or scp" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add SftpOptionsSpec + ScpOptionsSpec
```

---

### Task 7: `FtpOptionsSpec` (library-forwarding, `passive_commands` list→tuple)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime `FtpOptions`: `port` (21), `encoding` ('utf-8'), `socket_timeout`,
`connection_timeout`, `path_timeout`, `read_speed_limit`, `write_speed_limit`,
`ssl` (`Any`), `passive_commands` (`tuple[str, ...]` = `('epsv', 'pasv')`),
`extra`.

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.options import FtpOptions
from otto.models.options import FtpOptionsSpec


def test_ftp_spec_coerces_passive_commands_to_tuple():
    rt_obj = FtpOptionsSpec(passive_commands=["pasv"]).to_runtime()
    assert isinstance(rt_obj, FtpOptions)
    assert rt_obj.passive_commands == ("pasv",)


def test_ftp_spec_defaults_match_runtime():
    rt_obj = FtpOptionsSpec().to_runtime()
    assert rt_obj.port == 21
    assert rt_obj.passive_commands == ("epsv", "pasv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k ftp -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Append (pydantic coerces a JSON list into `tuple[str, ...]` automatically):

```python
class FtpOptionsSpec(OttoModel):
    port: int = 21
    encoding: str = "utf-8"
    socket_timeout: float | None = None
    connection_timeout: float | None = None
    path_timeout: float | None = None
    read_speed_limit: int | None = None
    write_speed_limit: int | None = None
    ssl: Any = None
    passive_commands: tuple[str, ...] = ("epsv", "pasv")
    extra: dict[str, Any] = {}

    def to_runtime(self) -> rt.FtpOptions:
        return rt.FtpOptions(
            port=self.port,
            encoding=self.encoding,
            socket_timeout=self.socket_timeout,
            connection_timeout=self.connection_timeout,
            path_timeout=self.path_timeout,
            read_speed_limit=self.read_speed_limit,
            write_speed_limit=self.write_speed_limit,
            ssl=self.ssl,
            passive_commands=self.passive_commands,
            extra=dict(self.extra),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k ftp -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add FtpOptionsSpec
```

---

### Task 8: `NcOptionsSpec` (otto-owned, strict, no `extra`)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime `NcOptions`: `exec_name` ('nc'), `port` (9000),
`port_strategy` (`NcPortStrategy` = 'auto'), `port_cmd` (`str|None`),
`listener_check` (`NcListenerCheck` = 'auto'), `listener_cmd` (`str|None`),
`listener_timeout` (30.0). The two `Literal` aliases live in
`otto.host.transfer`. No `extra` field — netcat has no library option set.

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.options import NcOptions
from otto.models.options import NcOptionsSpec


def test_nc_spec_defaults_match_runtime():
    rt_obj = NcOptionsSpec().to_runtime()
    assert isinstance(rt_obj, NcOptions)
    assert rt_obj.exec_name == "nc"
    assert rt_obj.port == 9000
    assert rt_obj.port_strategy == "auto"


def test_nc_spec_rejects_unknown_key():
    with pytest.raises(ValidationError):
        NcOptionsSpec(extra={"x": 1})  # otto-owned: no passthrough
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k "nc_spec" -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add the import at the top of `options.py`:

```python
from ..host.transfer import NcListenerCheck, NcPortStrategy
```

Append:

```python
class NcOptionsSpec(OttoModel):
    exec_name: str = "nc"
    port: int = 9000
    port_strategy: NcPortStrategy = "auto"
    port_cmd: str | None = None
    listener_check: NcListenerCheck = "auto"
    listener_cmd: str | None = None
    listener_timeout: float = 30.0

    def to_runtime(self) -> rt.NcOptions:
        return rt.NcOptions(
            exec_name=self.exec_name,
            port=self.port,
            port_strategy=self.port_strategy,
            port_cmd=self.port_cmd,
            listener_check=self.listener_check,
            listener_cmd=self.listener_cmd,
            listener_timeout=self.listener_timeout,
        )
```

> If `from ..host.transfer import ...` raises a circular-import error at
> collection time, fall back to importing the two `Literal` aliases lazily is
> **not** an option for a class-body annotation — instead re-declare them
> locally: `NcPortStrategy = Literal["auto", "ss", "netstat", "python", "proc", "custom"]`
> and `NcListenerCheck = Literal["auto", "ss", "netstat", "proc", "custom"]`
> (copy the exact members from `otto/host/transfer.py`), and add a test asserting
> they equal the transfer aliases so they cannot drift.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k "nc_spec" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add NcOptionsSpec (strict, otto-owned)
```

---

### Task 9: `SnmpOptionsSpec` (otto-owned, `oids` list→tuple)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime `SnmpOptions`: `oids` (`tuple[str, ...]` = `()`), `community`
('public'), `port` (161), `version` (`'2c'`), `address` (`str | None`). No
`extra`. `version` is constrained to `'1'`/`'2c'`.

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.options import SnmpOptions
from otto.models.options import SnmpOptionsSpec


def test_snmp_spec_coerces_oids_to_tuple():
    rt_obj = SnmpOptionsSpec(oids=["1.3.6.1.2.1.1.3.0"]).to_runtime()
    assert isinstance(rt_obj, SnmpOptions)
    assert rt_obj.oids == ("1.3.6.1.2.1.1.3.0",)


def test_snmp_spec_defaults_and_address():
    rt_obj = SnmpOptionsSpec(address="10.0.0.9").to_runtime()
    assert rt_obj.community == "public"
    assert rt_obj.port == 161
    assert rt_obj.version == "2c"
    assert rt_obj.address == "10.0.0.9"


def test_snmp_spec_rejects_bad_version():
    with pytest.raises(ValidationError):
        SnmpOptionsSpec(version="3")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k snmp -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Add `from typing import Literal` to the imports if not already present, then append:

```python
class SnmpOptionsSpec(OttoModel):
    oids: tuple[str, ...] = ()
    community: str = "public"
    port: int = 161
    version: Literal["1", "2c"] = "2c"
    address: str | None = None

    def to_runtime(self) -> rt.SnmpOptions:
        return rt.SnmpOptions(
            oids=self.oids,
            community=self.community,
            port=self.port,
            version=self.version,
            address=self.address,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k snmp -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add SnmpOptionsSpec (oids list->tuple, version literal)
```

---

### Task 10: `TftpOptionsSpec` (otto-owned)

**Files:**
- Modify: `src/otto/models/options.py`
- Test: `tests/unit/models/test_option_specs.py`

Runtime `TftpOptions`: `port` (69), `server_ip` (`str | None`),
`block_size` (512), `timeout` (5.0).

- [ ] **Step 1: Write the failing test**

Append:

```python
from otto.host.options import TftpOptions
from otto.models.options import TftpOptionsSpec


def test_tftp_spec_defaults_match_runtime():
    rt_obj = TftpOptionsSpec(server_ip="10.0.0.2").to_runtime()
    assert isinstance(rt_obj, TftpOptions)
    assert rt_obj.port == 69
    assert rt_obj.block_size == 512
    assert rt_obj.server_ip == "10.0.0.2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k tftp -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Append:

```python
class TftpOptionsSpec(OttoModel):
    port: int = 69
    server_ip: str | None = None
    block_size: int = 512
    timeout: float = 5.0

    def to_runtime(self) -> rt.TftpOptions:
        return rt.TftpOptions(
            port=self.port,
            server_ip=self.server_ip,
            block_size=self.block_size,
            timeout=self.timeout,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k tftp -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): add TftpOptionsSpec
```

---

### Task 11: Public re-exports + the drift guard

**Files:**
- Modify: `src/otto/models/__init__.py`
- Modify: `src/otto/models/options.py` (add a registry of `(spec, runtime)` pairs)
- Test: `tests/unit/models/test_option_specs.py`

The drift guard asserts every spec's field set is a subset of its runtime
dataclass's field set (so the duplicated lists cannot diverge) and that every
spec builds a runtime instance from defaults.

- [ ] **Step 1: Write the failing test**

Append:

```python
import dataclasses

from otto.models.options import OPTION_SPEC_RUNTIME_PAIRS


@pytest.mark.parametrize("spec_cls,runtime_cls", OPTION_SPEC_RUNTIME_PAIRS)
def test_spec_fields_subset_of_runtime(spec_cls, runtime_cls):
    spec_fields = set(spec_cls.model_fields)
    runtime_fields = {f.name for f in dataclasses.fields(runtime_cls)}
    missing = spec_fields - runtime_fields
    assert not missing, (
        f"{spec_cls.__name__} has fields absent from "
        f"{runtime_cls.__name__}: {sorted(missing)}"
    )


# The three SSH forward specs are required-field value objects (no sensible
# defaults) — always nested inside SshOptionsSpec, and their to_runtime() is
# already covered by the explicit forward tests in Task 3. The no-arg
# "default builds runtime" check only applies to the fully-defaulted option
# specs, so exclude the forwards here (they stay in the subset guard above).
_FORWARD_SPECS = (LocalPortForwardSpec, RemotePortForwardSpec, SocksForwardSpec)
_DEFAULT_CONSTRUCTIBLE_PAIRS = [
    (s, r) for s, r in OPTION_SPEC_RUNTIME_PAIRS if s not in _FORWARD_SPECS
]


@pytest.mark.parametrize("spec_cls,runtime_cls", _DEFAULT_CONSTRUCTIBLE_PAIRS)
def test_default_spec_builds_runtime(spec_cls, runtime_cls):
    assert isinstance(spec_cls().to_runtime(), runtime_cls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/models/test_option_specs.py -k "subset or builds_runtime" -v`
Expected: FAIL — `ImportError: cannot import name 'OPTION_SPEC_RUNTIME_PAIRS'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/otto/models/options.py` (after all specs):

```python
OPTION_SPEC_RUNTIME_PAIRS: list[tuple[type[OttoModel], type]] = [
    (LocalPortForwardSpec, rt.LocalPortForward),
    (RemotePortForwardSpec, rt.RemotePortForward),
    (SocksForwardSpec, rt.SocksForward),
    (SshOptionsSpec, rt.SshOptions),
    (TelnetOptionsSpec, rt.TelnetOptions),
    (SftpOptionsSpec, rt.SftpOptions),
    (ScpOptionsSpec, rt.ScpOptions),
    (FtpOptionsSpec, rt.FtpOptions),
    (NcOptionsSpec, rt.NcOptions),
    (SnmpOptionsSpec, rt.SnmpOptions),
    (TftpOptionsSpec, rt.TftpOptions),
]
"""Each boundary option spec paired with the runtime dataclass it builds.
Drives the drift guard so the duplicated field lists cannot silently diverge."""
```

Replace the body of `src/otto/models/__init__.py` to re-export the specs:

```python
"""Pydantic boundary models — the validation layer for external data
(lab JSON, settings.toml, OTTO_* env, monitor import/export).

These spec models depend on the runtime data modules they validate and build
(``otto.host.options``, ``otto.host.transfer``); those runtime modules do not
import from here, so the dependency runs one way (models -> runtime data) with
no cycle. Each model mirroring a runtime type carries the ``Spec`` suffix.
"""

from .base import OttoModel
from .options import (
    FtpOptionsSpec,
    LocalPortForwardSpec,
    NcOptionsSpec,
    RemotePortForwardSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SnmpOptionsSpec,
    SocksForwardSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
    TftpOptionsSpec,
)

__all__ = [
    "OttoModel",
    "SshOptionsSpec",
    "TelnetOptionsSpec",
    "SftpOptionsSpec",
    "ScpOptionsSpec",
    "FtpOptionsSpec",
    "NcOptionsSpec",
    "SnmpOptionsSpec",
    "TftpOptionsSpec",
    "LocalPortForwardSpec",
    "RemotePortForwardSpec",
    "SocksForwardSpec",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/models/test_option_specs.py -v`
Expected: PASS (all tests in the file, including the parametrized drift guard).

- [ ] **Step 5: Stage (Chris commits)**

```bash
git add src/otto/models/__init__.py src/otto/models/options.py tests/unit/models/test_option_specs.py
# commit message: feat(models): re-export option specs + add spec<->runtime drift guard
```

---

### Task 12: Full-suite + typecheck + lint gate

**Files:** none (verification only).

- [ ] **Step 1: Type-check the new package**

Run: `uv run ty check src/otto/models`
Expected: 0 diagnostics.

- [ ] **Step 2: Lint the new package**

Run: `uv run ruff check src/otto/models tests/unit/models`
Expected: clean (no new findings).

- [ ] **Step 3: Run the model unit tests under coverage settings**

Run: `uv run pytest tests/unit/models -v`
Expected: all pass.

- [ ] **Step 4: Confirm no runtime regression**

Run: `uv run pytest -m "not stability" -q`
Expected: the existing suite still passes (Plan 1 adds an isolated leaf package and touches no runtime path, so nothing should change). Do **not** kill live-VM tiers mid-run.

- [ ] **Step 5: Stage any lint/type fixups (Chris commits)**

```bash
git add -A
# commit message: chore(models): satisfy ty + ruff for option specs
```

---

## Self-Review

**Spec coverage (this plan = "Option models — the two-type split" section of the spec):**
- Two-type split, `extra='forbid'`, explicit `extra` on the five library-forwarding specs → Tasks 4–7. ✓
- otto-owned specs strict, no `extra` (Nc/Snmp/Tftp + forwards) → Tasks 3, 8–10. ✓
- Conversions moved into specs (`login_prompt` str→bytes, `oids` list→tuple) → Tasks 5, 9. ✓
- Drift guard → Task 11. ✓
- Runtime `host/options.py` unchanged → no task modifies it. ✓
- HostSpec/factory integration, settings, monitor, JSON schema, spike → **deferred to Plans 2–5** (out of this plan's scope by design).

**Placeholder scan:** the only conditional is the Task 8 circular-import fallback, which gives the exact local `Literal` re-declaration to use — not a placeholder.

**Type consistency:** `to_runtime()` used uniformly; every spec maps to the runtime class named in `OPTION_SPEC_RUNTIME_PAIRS` (Task 11); `Spec` suffix consistent throughout; `OttoModel` base used by all.

---

## Notes for Plans 2–5 (not implemented here)

- **Plan 2** consumes these specs: `HostSpec`/`UnixHostSpec`/`EmbeddedHostSpec` nest the `*OptionsSpec`s; `register_host_class(name, cls, spec)`; the generic factory + M1 merge; `command_frame` promotion (incl. the small `UnixHost` field touch); `interfaces`/`address_for`.
- **Plan 3** reuses the option specs for `[host_defaults]` partial validation (`model_validate(...).model_dump(exclude_unset=True)`).
- **Plan 4** (monitor) is independent and may run in parallel.
- **Plan 5** wires JSON Schema export and runs the dual-purpose spike.
