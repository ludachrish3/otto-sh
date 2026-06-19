# WS#4 — Registry public API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open otto's `term` and `transfer` selectors to the same string-registry idiom used for host classes / command frames / filesystems, freezing a public `register_*_backend` + `create(ctx)` extension contract, without refactoring the monolithic backends.

**Architecture:** Two registries — a **term** registry (`connections.py`, UnixHost-only) and **one unified transfer** registry (`transfer.py`, both host families, keyed by a `host_families` applicability tag). A uniform `create(cls, ctx)` classmethod is the single construction seam; built-ins' `create` wraps today's exact constructor against a frozen context DTO. The host specs validate the selector strings against the registries via pydantic `field_validator`s. The `Literal`s are retired only after every consumer is migrated, so the tree stays green between tasks. Finally, the remaining class registries (`command_frame` / `embedded_filesystem` / `binary_loader`) are retrofitted to register their built-ins through their public `register_*` path.

**Tech Stack:** Python 3.10–3.14, pydantic v2, Typer/Click, asyncssh/aioftp, pytest + pytest-asyncio, `ty` type checker, Sphinx/MyST docs.

---

## ⚠️ Execution constraints (read before starting)

- **STAGE-ONLY — do NOT commit.** otto-sh's `prepare-commit-msg` hook needs `/dev/tty`; agent/subagent commits mis-tag the AI-assist trailer as "None". Each task ends by running its tests and `git add`-ing the changed files. **Chris commits by hand.** Each task provides a paste-able commit message. The controller reviews the *staged diff* (`git diff --cached`) between tasks instead of per-task SHAs.
- **Own branch.** This work must NOT land on `main`. Before Task 1, create `git switch -c ws4-registry-public-api` from `main` (HEAD `2d07d68` = v0.4.3).
- **Live-bed safety.** Tasks 5 and 10 exercise host construction; the full gate (`make test`) drives the live Unix VMs and the Zephyr bed. **Do not kill live-bed runs at tight timeouts** — a SIGTERM wedges the single-client embedded console; recover with `make qemu-restart`. Let runs finish.
- **Registry isolation.** Every test that registers a fake backend MUST snapshot/restore the global registry dict (fixture provided in Task 1), so global state never leaks between tests.
- **tmp_path only.** Never write probe files anywhere under the repo; use `tmp_path`.

---

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/otto/host/connections.py` | term registry (`_TERM_BACKENDS`, `register_term_backend`, `build_term_backend`), `TermContext` DTO, `ConnectionManager.create` | 1, 7 |
| `src/otto/host/transfer.py` | unified transfer registry, `TransferContext` DTO, `BaseFileTransfer.host_families` + `.create`, `FileTransfer.create` | 2, 7 |
| `src/otto/host/embedded_transfer.py` | `EmbeddedFileTransfer.host_families` + `.create`; registers `console`/`tftp` into the shared registry | 3, 7 |
| `src/otto/models/host.py` | `term`/`transfer` become `str` + registry/applicability `field_validator`s + `_transfer_host_family` ClassVar | 4 |
| `src/otto/models/jsonschema.py` | inject registry-derived `enum` into the generated `term`/`transfer` schema (editor autocomplete) | 4b |
| `src/otto/host/unix_host.py` | connection + transfer construction via `build_*_backend(...).create(ctx)`; `set_*_type` registry checks | 5, 6, 7 |
| `src/otto/host/embedded_host.py` | transfer construction via `build_transfer_backend(...).create(ctx)` | 5 |
| `src/otto/cli/host.py` | `--term`/`--transfer` dynamic registry-driven tab completion + `BadParameter` validation | 6 |
| `src/otto/host/host.py` | delete the duplicate `TermType`/`FileTransferType` `Literal`s | 7 |
| `src/otto/host/__init__.py` | re-export `register_term_backend` / `register_transfer_backend` / `build_transfer_backend`; drop `EmbeddedTransferType` re-export | 6, 7 |
| `src/otto/host/command_frame.py`, `embedded_filesystem.py`, `binary_loader.py` | retrofit built-ins through `register_*` at load | 8 |
| `todo/registry_builtin_registration_symmetry.md` | trim to the remaining monitor-parser case | 8 |
| `docs/guide/extending-backends.md` + `docs/guide/index.rst` | custom term/transfer backend guide | 9 |
| `src/otto/configmodule/completion_cache.py` | snapshot registered term/transfer backend names (schema v5) for fast-path completion | 10 |
| `src/otto/configmodule/__init__.py` | wire `collect_backend_names()` into the slow-path cache write | 10 |
| `todo/host-declared-transfer-term-lists.md` | tier-3 future work (per-host declared lists drive completion) | 10 |

### DTO field sets (frozen this plan; the design left exact fields to the plan)

`TermContext` (in `connections.py`) — mirrors `ConnectionManager.__init__`:
`ip: str`, `creds: dict[str, str]`, `user: str | None`, `term: str`, `name: str`, `hop=None`, `ssh_options=None`, `telnet_options=None`, `sftp_options=None`, `ftp_options=None`.

`TransferContext` (in `transfer.py`) — union of both families' construction inputs:
`transfer: str` (the selector), `host_name: str`, `max_filename_len: int = 255`, `exec_cmd: Callable | None = None`, `connections=None` (unix), `nc_options=None` (unix), `scp_options=None` (unix), `get_local_ip=None` (unix), `filesystem=None` (embedded).

### Scope note carried from design self-review (surface to Chris)

The design §6 says "resolve + **delete** `todo/registry_builtin_registration_symmetry.md`." That todo also names **monitor shell parsers** (`monitor/parsers.py` `DEFAULT_PARSERS`), but those have a *different shape* — host-scoped, instance-valued, with `register_host_parsers(host_id, {...})` as the only public entry, not a `register_X(type_name, cls)` per-parser path. Retrofitting them would require inventing a *new* public function (`register_default_parser`) — widening the frozen surface as a side effect of WS#4, which YAGNI/scope-discipline argue against. **This plan retrofits the three class registries that share the `register_X(type_name, cls)` shape (command_frame, embedded_filesystem, binary_loader; host_class + snmp_metric are already symmetric) and TRIMS the todo to the monitor-parser remainder rather than deleting it.** Task 8 implements the trim.

---

### Task 0: Branch off main

**Files:** none (git only)

- [ ] **Step 1: Create the workstream branch**

Run:
```bash
git -C /home/vagrant/otto-sh switch -c ws4-registry-public-api
git -C /home/vagrant/otto-sh status
```
Expected: on branch `ws4-registry-public-api`, clean tree, HEAD == `2d07d68`.

---

### Task 1: Term backend registry + `ConnectionManager.create` seam

**Files:**
- Modify: `src/otto/host/connections.py`
- Test: `tests/unit/host/test_term_registry.py` (create)

The `TermType` `Literal` stays alive this task (CLI + `set_term_type` still use it; retired in Task 7). This task is purely additive.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/host/test_term_registry.py`:
```python
"""Term backend registry + ConnectionManager.create construction seam (WS#4)."""

import pytest

from otto.host import connections as conn_mod
from otto.host.connections import (
    ConnectionManager,
    TermContext,
    build_term_backend,
    register_term_backend,
)


@pytest.fixture(autouse=True)
def _isolate_term_registry():
    """Snapshot/restore the global term registry around each test so a custom
    registration never leaks into the next test."""
    saved = dict(conn_mod._TERM_BACKENDS)
    try:
        yield
    finally:
        conn_mod._TERM_BACKENDS.clear()
        conn_mod._TERM_BACKENDS.update(saved)


class TestBuiltins:
    def test_ssh_and_telnet_registered_to_connection_manager(self):
        assert build_term_backend("ssh") is ConnectionManager
        assert build_term_backend("telnet") is ConnectionManager


class TestRegistry:
    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown term backend"):
            build_term_backend("nope")
        # known names are listed so a typo is diagnosable
        try:
            build_term_backend("nope")
        except ValueError as e:
            assert "ssh" in str(e) and "telnet" in str(e)

    def test_register_and_build_custom(self):
        class CustomTerm(ConnectionManager):
            pass

        register_term_backend("myterm", CustomTerm)
        assert build_term_backend("myterm") is CustomTerm


class TestCreate:
    def test_create_constructs_connection_manager(self):
        ctx = TermContext(
            ip="10.0.0.5", creds={"root": "x"}, user="root",
            term="ssh", name="h1",
        )
        cm = ConnectionManager.create(ctx)
        assert isinstance(cm, ConnectionManager)
        assert cm.ip == "10.0.0.5"
        assert cm.term == "ssh"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/host/test_term_registry.py -q`
Expected: FAIL — `ImportError` (`TermContext` / `build_term_backend` / `register_term_backend` don't exist yet).

- [ ] **Step 3: Implement the registry + DTO + create**

In `src/otto/host/connections.py`, add `dataclass` to the imports (`from dataclasses import dataclass`). Below the `TermType` line (keep it), add the DTO:
```python
@dataclass(frozen=True)
class TermContext:
    """Construction inputs a UnixHost provides to build its connection backend
    via :meth:`ConnectionManager.create`. The frozen public seam for custom
    term backends; carries only what the built-in already receives at its call
    site (no new coupling)."""

    ip: str
    creds: dict[str, str]
    user: str | None
    term: str
    name: str
    hop: "HopTransport | None" = None
    ssh_options: SshOptions | None = None
    telnet_options: TelnetOptions | None = None
    sftp_options: SftpOptions | None = None
    ftp_options: FtpOptions | None = None
```

Add a `create` classmethod on `ConnectionManager` (place it just after `__init__`):
```python
    @classmethod
    def create(cls, ctx: "TermContext") -> "ConnectionManager":
        """Build a connection backend from a :class:`TermContext`.

        The uniform construction seam (WS#4): a host calls
        ``build_term_backend(name).create(ctx)`` for built-in and custom
        backends alike. The built-in's ``create`` runs today's exact
        construction — internals untouched, only the call site moves here.
        """
        return cls(
            ip=ctx.ip,
            creds=ctx.creds,
            user=ctx.user,
            term=ctx.term,
            name=ctx.name,
            hop=ctx.hop,
            ssh_options=ctx.ssh_options,
            telnet_options=ctx.telnet_options,
            sftp_options=ctx.sftp_options,
            ftp_options=ctx.ftp_options,
        )
```

At the end of the module, add the registry (mirrors `command_frame`, but `build_*` returns the **class**, not an instance — the host calls `.create` on it):
```python
# Registry of term-protocol name -> ConnectionManager(-compatible) class.
# UnixHost-only; embedded hosts reach their console over a fixed telnet bridge
# that WS#4 does not model as a term backend. ``build_*`` returns the class so
# the host can call ``.create(ctx)`` on it.
_TERM_BACKENDS: dict[str, type[ConnectionManager]] = {}


def register_term_backend(name: str, cls: type[ConnectionManager]) -> None:
    """Make a custom connection backend available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.
    Once registered, a host's ``term`` field can select it by name.
    """
    _TERM_BACKENDS[name] = cls


def build_term_backend(name: str) -> type[ConnectionManager]:
    """Return the connection-backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _TERM_BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_TERM_BACKENDS))
        raise ValueError(
            f"Unknown term backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_term_backend()."
        ) from None


def _register_builtin_term_backends() -> None:
    """Register otto's built-in term backends through the public path, so
    first-party and third-party registrations travel the same code (mirrors
    ``os_profile._register_builtin_host_classes``)."""
    register_term_backend("ssh", ConnectionManager)
    register_term_backend("telnet", ConnectionManager)


_register_builtin_term_backends()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/host/test_term_registry.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/host/connections.py && uv run ruff check --fix src/otto/host/connections.py tests/unit/host/test_term_registry.py`
Then: `git add src/otto/host/connections.py tests/unit/host/test_term_registry.py`
Do NOT commit. Suggested message: `feat(host): term backend registry + ConnectionManager.create seam (WS#4)`

---

### Task 2: Unified transfer registry + `BaseFileTransfer` seam + `FileTransfer` entry

**Files:**
- Modify: `src/otto/host/transfer.py`
- Test: `tests/unit/host/test_transfer_registry.py` (create)

`FileTransferType` `Literal` stays alive this task. Additive.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/host/test_transfer_registry.py`:
```python
"""Unified transfer backend registry + create seam + applicability (WS#4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import transfer as xfer_mod
from otto.host.options import NcOptions, ScpOptions
from otto.host.transfer import (
    BaseFileTransfer,
    FileTransfer,
    TransferContext,
    build_transfer_backend,
    register_transfer_backend,
)


@pytest.fixture(autouse=True)
def _isolate_transfer_registry():
    saved = dict(xfer_mod._TRANSFER_BACKENDS)
    try:
        yield
    finally:
        xfer_mod._TRANSFER_BACKENDS.clear()
        xfer_mod._TRANSFER_BACKENDS.update(saved)


class TestBuiltins:
    @pytest.mark.parametrize("name", ["scp", "sftp", "ftp", "nc"])
    def test_unix_protocols_registered_to_filetransfer(self, name):
        cls = build_transfer_backend(name)
        assert cls is FileTransfer
        assert cls.host_families == frozenset({"unix"})


class TestRegistry:
    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown transfer backend"):
            build_transfer_backend("nope")

    def test_register_rejects_empty_host_families(self):
        class NoFamilies(BaseFileTransfer):
            host_families = frozenset()

            async def _run_put(self, *a):  # pragma: no cover - not invoked
                ...

            async def _run_get(self, *a):  # pragma: no cover - not invoked
                ...

        with pytest.raises(ValueError, match="host_families is empty"):
            register_transfer_backend("bad", NoFamilies)

    def test_register_and_build_custom(self):
        class XmodemTransfer(FileTransfer):
            host_families = frozenset({"unix"})

        register_transfer_backend("xmodem", XmodemTransfer)
        assert build_transfer_backend("xmodem") is XmodemTransfer


class TestCreate:
    def test_create_constructs_filetransfer(self):
        ctx = TransferContext(
            transfer="scp",
            host_name="h1",
            connections=MagicMock(),
            nc_options=NcOptions(),
            scp_options=ScpOptions(),
            get_local_ip=lambda: "1.2.3.4",
            exec_cmd=AsyncMock(),
            max_filename_len=255,
        )
        ft = FileTransfer.create(ctx)
        assert isinstance(ft, FileTransfer)
        assert ft.transfer == "scp"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/host/test_transfer_registry.py -q`
Expected: FAIL — `ImportError` (`TransferContext` / `build_transfer_backend` / `register_transfer_backend` don't exist).

- [ ] **Step 3: Implement the DTO, base seam, registry, and FileTransfer entry**

In `src/otto/host/transfer.py`:

Add `from dataclasses import dataclass` to the imports. Under the TYPE_CHECKING block, add `EmbeddedFileSystem` for annotation only:
```python
if TYPE_CHECKING:
    from .connections import ConnectionManager
    from .embedded_filesystem import EmbeddedFileSystem
    from .options import NcOptions, ScpOptions
```

Below the `FileTransferType` line (keep it), add the DTO:
```python
@dataclass(frozen=True)
class TransferContext:
    """Construction inputs a host provides to build its transfer backend via
    :meth:`BaseFileTransfer.create`. The frozen public seam for custom transfer
    backends. Carries the union of what any family's built-ins receive at their
    call sites; a unix backend reads the unix fields, an embedded backend the
    embedded ones. Selector validation (host-family applicability) runs before
    construction, so a backend never sees a ctx missing the fields it needs."""

    transfer: str
    host_name: str
    max_filename_len: int = 255
    exec_cmd: "Callable[..., Coroutine[Any, Any, CommandStatus]] | None" = None
    # unix-family fields
    connections: "ConnectionManager | None" = None
    nc_options: "NcOptions | None" = None
    scp_options: "ScpOptions | None" = None
    get_local_ip: "Callable[[], str] | None" = None
    # embedded-family fields
    filesystem: "EmbeddedFileSystem | None" = None
```

On `BaseFileTransfer`, add the applicability tag and the `create` contract. Add the class attribute just under the docstring:
```python
    host_families: frozenset[str] = frozenset()
    """Host families this backend serves: a subset of ``{'unix', 'embedded'}``.
    Subclasses declare it; the spec field_validator rejects a backend on a host
    of the wrong family. A backend with an empty set can never validate and is
    rejected at registration."""

    @classmethod
    def create(cls, ctx: "TransferContext") -> "BaseFileTransfer":
        """Build a transfer backend from a :class:`TransferContext`.

        The uniform construction seam (WS#4). Concrete backends override this to
        run their exact construction against the ctx fields they need. Not an
        ``abstractmethod`` deliberately: only registered built-ins are ever
        constructed through ``create``, and test doubles that subclass
        ``BaseFileTransfer`` only to exercise the progress contract must not be
        forced to implement it."""
        raise NotImplementedError(
            f"{cls.__name__} does not implement create(); a registered transfer "
            f"backend must override create(cls, ctx)."
        )
```

On `FileTransfer`, declare the family and the `create` (place `host_families` near the top of the class body, `create` just after `__init__`):
```python
    host_families = frozenset({"unix"})

    @classmethod
    def create(cls, ctx: "TransferContext") -> "FileTransfer":
        assert ctx.connections is not None
        assert ctx.exec_cmd is not None
        assert ctx.get_local_ip is not None
        assert ctx.nc_options is not None
        assert ctx.scp_options is not None
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            transfer=ctx.transfer,
            nc_options=ctx.nc_options,
            scp_options=ctx.scp_options,
            get_local_ip=ctx.get_local_ip,
            exec_cmd=ctx.exec_cmd,
            max_filename_len=ctx.max_filename_len,
        )
```

At the end of the module, add the registry. `build_*` returns the **class** (the host calls `.create` on it):
```python
# Unified registry of transfer-protocol name -> backend class, spanning BOTH
# host families. ``EmbeddedFileTransfer`` registers ``console``/``tftp`` into
# this same dict (see embedded_transfer.py), so one namespace holds every
# transfer protocol and a future cross-family protocol (tftp) is a single
# entry. ``build_*`` returns the class so the host can call ``.create(ctx)``.
_TRANSFER_BACKENDS: dict[str, type[BaseFileTransfer]] = {}


def register_transfer_backend(name: str, cls: type[BaseFileTransfer]) -> None:
    """Make a custom transfer backend available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The backend
    must declare a non-empty :attr:`BaseFileTransfer.host_families`; otherwise
    it could never validate against any host and is rejected here.
    """
    if not cls.host_families:
        raise ValueError(
            f"register_transfer_backend({name!r}): cls.host_families is empty; "
            f"a transfer backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    _TRANSFER_BACKENDS[name] = cls


def build_transfer_backend(name: str) -> type[BaseFileTransfer]:
    """Return the transfer-backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _TRANSFER_BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_TRANSFER_BACKENDS))
        raise ValueError(
            f"Unknown transfer backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_transfer_backend()."
        ) from None


def _register_builtin_transfer_backends() -> None:
    """Register otto's built-in unix transfer backends through the public path.
    Embedded's ``console``/``tftp`` register themselves in embedded_transfer.py."""
    for name in ("scp", "sftp", "ftp", "nc"):
        register_transfer_backend(name, FileTransfer)


_register_builtin_transfer_backends()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/host/test_transfer_registry.py -q`
Expected: PASS.

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/host/transfer.py && uv run ruff check --fix src/otto/host/transfer.py tests/unit/host/test_transfer_registry.py`
Then: `git add src/otto/host/transfer.py tests/unit/host/test_transfer_registry.py`
Suggested message: `feat(host): unified transfer registry + BaseFileTransfer.create seam (WS#4)`

---

### Task 3: `EmbeddedFileTransfer` joins the shared transfer registry

**Files:**
- Modify: `src/otto/host/embedded_transfer.py`
- Test: `tests/unit/host/test_transfer_registry.py` (extend)

`EmbeddedTransferType` `Literal` stays alive this task. Additive.

- [ ] **Step 1: Write the failing test (extend the registry test)**

Append to `tests/unit/host/test_transfer_registry.py`:
```python
class TestEmbeddedTransferRegistration:
    def test_console_registered_embedded_only(self):
        from otto.host.embedded_transfer import EmbeddedFileTransfer

        cls = build_transfer_backend("console")
        assert cls is EmbeddedFileTransfer
        assert cls.host_families == frozenset({"embedded"})

    def test_tftp_registered_embedded_only(self):
        cls = build_transfer_backend("tftp")
        assert cls.host_families == frozenset({"embedded"})

    def test_embedded_create_constructs(self):
        from unittest.mock import AsyncMock

        from otto.host.embedded_transfer import EmbeddedFileTransfer

        ctx = TransferContext(
            transfer="console",
            host_name="dut",
            exec_cmd=AsyncMock(),
            filesystem=None,
            max_filename_len=255,
        )
        ft = EmbeddedFileTransfer.create(ctx)
        assert isinstance(ft, EmbeddedFileTransfer)
        assert ft.transfer == "console"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/host/test_transfer_registry.py::TestEmbeddedTransferRegistration -q`
Expected: FAIL — `console`/`tftp` not registered; `EmbeddedFileTransfer.create` is the base `NotImplementedError`.

- [ ] **Step 3: Implement family tag, create, and shared registration**

In `src/otto/host/embedded_transfer.py`, extend the import from `.transfer`:
```python
from .transfer import (
    BaseFileTransfer,
    TransferContext,
    TransferProgressFactory,
    TransferProgressHandler,
    register_transfer_backend,
)
```

On `EmbeddedFileTransfer`, add the family tag (near the top of the class body) and the `create` classmethod (after `__init__`):
```python
    host_families = frozenset({"embedded"})

    @classmethod
    def create(cls, ctx: "TransferContext") -> "EmbeddedFileTransfer":
        assert ctx.exec_cmd is not None
        return cls(
            transfer=ctx.transfer,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            filesystem=ctx.filesystem,
            max_filename_len=ctx.max_filename_len,
        )
```

At the end of the module, register the embedded protocols into the shared registry:
```python
def _register_builtin_embedded_transfers() -> None:
    """Register the embedded transfer protocols into the shared transfer
    registry (transfer.py ``_TRANSFER_BACKENDS``) — one namespace for all
    families. ``tftp`` is reserved (raises NotImplementedError on use) but
    registered so the namespace welcomes it as a single cross-family entry
    once implemented."""
    register_transfer_backend("console", EmbeddedFileTransfer)
    register_transfer_backend("tftp", EmbeddedFileTransfer)


_register_builtin_embedded_transfers()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/host/test_transfer_registry.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/host/embedded_transfer.py && uv run ruff check --fix src/otto/host/embedded_transfer.py tests/unit/host/test_transfer_registry.py`
Then: `git add src/otto/host/embedded_transfer.py tests/unit/host/test_transfer_registry.py`
Suggested message: `feat(host): embedded transfers join the shared transfer registry (WS#4)`

---

### Task 4: Spec selector validation — `str` + applicability `field_validator`s

**Files:**
- Modify: `src/otto/models/host.py`
- Test: `tests/unit/models/test_host_specs.py` (extend)

> **Import-order invariant:** the validators read `_TERM_BACKENDS` / `_TRANSFER_BACKENDS`, which are seeded at `connections.py` / `transfer.py` / `embedded_transfer.py` load. `models/host.py` already imports those (directly and via `embedded_host`/`unix_host`), so the registries are populated before any spec validates. Keep those imports.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_host_specs.py` (add imports it needs at the top of the new block):
```python
class TestSelectorValidation:
    def test_builtin_term_and_transfer_accepted(self):
        from otto.models.host import EmbeddedHostSpec, UnixHostSpec

        u = UnixHostSpec(ip="10.0.0.1", element="e", creds={"root": "x"},
                         term="telnet", transfer="sftp")
        assert u.term == "telnet" and u.transfer == "sftp"
        em = EmbeddedHostSpec(ip="10.0.0.2", element="e", transfer="console")
        assert em.transfer == "console"

    def test_unknown_term_raises(self):
        from pydantic import ValidationError

        from otto.models.host import UnixHostSpec

        with pytest.raises(ValidationError, match="not a registered term backend"):
            UnixHostSpec(ip="10.0.0.1", element="e", creds={"root": "x"}, term="nope")

    def test_unknown_unix_transfer_raises(self):
        from pydantic import ValidationError

        from otto.models.host import UnixHostSpec

        with pytest.raises(ValidationError, match="not a registered transfer backend"):
            UnixHostSpec(ip="10.0.0.1", element="e", creds={"root": "x"},
                         transfer="frobnicate")

    def test_unix_rejects_embedded_only_transfer(self):
        from pydantic import ValidationError

        from otto.models.host import UnixHostSpec

        with pytest.raises(ValidationError, match="not valid on a unix host"):
            UnixHostSpec(ip="10.0.0.1", element="e", creds={"root": "x"},
                         transfer="console")

    def test_embedded_rejects_unix_only_transfer(self):
        from pydantic import ValidationError

        from otto.models.host import EmbeddedHostSpec

        with pytest.raises(ValidationError, match="not valid on an embedded host"):
            EmbeddedHostSpec(ip="10.0.0.2", element="e", transfer="scp")

    def test_cross_family_backend_validates_on_both(self):
        from otto.host import transfer as xfer_mod
        from otto.host.transfer import FileTransfer
        from otto.models.host import EmbeddedHostSpec, UnixHostSpec

        class DualTransfer(FileTransfer):
            host_families = frozenset({"unix", "embedded"})

        saved = dict(xfer_mod._TRANSFER_BACKENDS)
        xfer_mod._TRANSFER_BACKENDS["dual"] = DualTransfer
        try:
            assert UnixHostSpec(ip="10.0.0.1", element="e", creds={"root": "x"},
                                transfer="dual").transfer == "dual"
            assert EmbeddedHostSpec(ip="10.0.0.2", element="e",
                                    transfer="dual").transfer == "dual"
        finally:
            xfer_mod._TRANSFER_BACKENDS.clear()
            xfer_mod._TRANSFER_BACKENDS.update(saved)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/models/test_host_specs.py::TestSelectorValidation -q`
Expected: FAIL — the closed-`Literal` fields reject e.g. `term="nope"` with a *generic* literal error (not the registry message), and `transfer="console"` is rejected on `UnixHostSpec` by the `Literal` rather than the family-aware message; the cross-family `"dual"` test fails (Literal rejects it).

- [ ] **Step 3: Switch the fields to `str` + registry validators**

In `src/otto/models/host.py`:

Replace the type-import lines:
```python
from ..host.binary_loader import build_binary_loader
from ..host.command_frame import _FRAME_CLASSES, build_command_frame
from ..host.connections import _TERM_BACKENDS
from ..host.embedded_filesystem import _FILESYSTEM_CLASSES, build_filesystem
from ..host.embedded_host import EmbeddedHost
from ..host.remote_host import RemoteHost
from ..host.toolchain import Toolchain
from ..host.transfer import _TRANSFER_BACKENDS
from ..host.unix_host import UnixHost
```
(Delete `from ..host.embedded_transfer import EmbeddedTransferType` and `from ..host.host import FileTransferType, TermType`.)

Add `ClassVar` to the `typing` import (the file currently imports `from typing import Any` — make it `from typing import Any, ClassVar`).

Add a module-level helper (single source of the transfer applicability check, reused by both family validators and — in Task 4b — the schema enum injector):
```python
def _validate_transfer_for_family(v: str, family: str, host_label: str) -> str:
    """Validate a transfer selector against the registry and host-family applicability."""
    if v not in _TRANSFER_BACKENDS:
        known = ", ".join(sorted(_TRANSFER_BACKENDS))
        raise ValueError(
            f"transfer {v!r} is not a registered transfer backend. Known: {known}"
        )
    if family not in _TRANSFER_BACKENDS[v].host_families:
        fam = ", ".join(sorted(_TRANSFER_BACKENDS[v].host_families))
        raise ValueError(
            f"transfer {v!r} is not valid on {host_label} (it serves: {fam})."
        )
    return v
```

In `UnixHostSpec`, change the field types, declare the transfer family as a `ClassVar` (NOT a pydantic field — `ClassVar` is excluded from fields), and add validators:
```python
    term: str = "ssh"
    docker_capable: bool = False
    transfer: str = "scp"

    _transfer_host_family: ClassVar[str] = "unix"
```
```python
    @field_validator("term")
    @classmethod
    def _validate_term_name(cls, v: str) -> str:
        if v not in _TERM_BACKENDS:
            known = ", ".join(sorted(_TERM_BACKENDS))
            raise ValueError(
                f"term {v!r} is not a registered term backend. Known: {known}"
            )
        return v

    @field_validator("transfer")
    @classmethod
    def _validate_unix_transfer_name(cls, v: str) -> str:
        return _validate_transfer_for_family(v, cls._transfer_host_family, "a unix host")
```

In `EmbeddedHostSpec`, change the field type, declare the family `ClassVar`, and add a validator:
```python
    transfer: str = "console"

    _transfer_host_family: ClassVar[str] = "embedded"
```
```python
    @field_validator("transfer")
    @classmethod
    def _validate_embedded_transfer_name(cls, v: str) -> str:
        return _validate_transfer_for_family(v, cls._transfer_host_family, "an embedded host")
```

- [ ] **Step 4: Run the new tests + the full host-spec file**

Run: `uv run pytest tests/unit/models/test_host_specs.py -q`
Expected: PASS (new `TestSelectorValidation` + all pre-existing host-spec tests unchanged).

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/models/host.py && uv run ruff check --fix src/otto/models/host.py tests/unit/models/test_host_specs.py`
Then: `git add src/otto/models/host.py tests/unit/models/test_host_specs.py`
Suggested message: `feat(models): validate term/transfer selectors against the registries (WS#4)`

---

### Task 4b: Inject registry-derived enums into the generated schema (editor autocomplete)

**Files:**
- Modify: `src/otto/models/jsonschema.py`
- Test: `tests/unit/models/test_jsonschema.py` (extend)

> **Why:** Task 4's `Literal`→`str` change drops the `term`/`transfer` `enum` from the generated `hosts.json` schema, degrading the editor autocomplete that Plan 6 (§6) built. Re-inject the enum from the **registry** — which is strictly better than the old static `Literal` enum because `otto schema export` runs *after* init modules load, so the enum includes custom per-repo backends too. Applicability-filtered via the `_transfer_host_family` ClassVar Task 4 added. This is the editor-schema parallel to the CLI completion work (Task 6/10).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_jsonschema.py`:
```python
class TestSelectorEnums:
    def test_unix_host_schema_has_registry_enums(self):
        from otto.models.jsonschema import build_schemas

        props = build_schemas()["unix-host"]["properties"]
        assert props["term"]["enum"] == ["ssh", "telnet"]
        assert props["transfer"]["enum"] == ["ftp", "nc", "scp", "sftp"]

    def test_embedded_host_schema_has_registry_enums(self):
        from otto.models.jsonschema import build_schemas

        props = build_schemas()["embedded-host"]["properties"]
        assert props["transfer"]["enum"] == ["console", "tftp"]
        assert "term" not in props  # embedded has no term field

    def test_hosts_array_defs_carry_enums(self):
        from otto.models.jsonschema import build_schemas

        defs = build_schemas()["hosts"]["$defs"]
        unix_def = next(
            d for d in defs.values()
            if isinstance(d, dict) and "term" in d.get("properties", {})
        )
        assert unix_def["properties"]["transfer"]["enum"] == ["ftp", "nc", "scp", "sftp"]

    def test_custom_unix_transfer_appears_in_enum(self):
        from otto.host import transfer as xfer_mod
        from otto.host.transfer import FileTransfer
        from otto.models.jsonschema import build_schemas

        class XmodemTransfer(FileTransfer):
            host_families = frozenset({"unix"})

        saved = dict(xfer_mod._TRANSFER_BACKENDS)
        xfer_mod._TRANSFER_BACKENDS["xmodem"] = XmodemTransfer
        try:
            props = build_schemas()["unix-host"]["properties"]
            assert "xmodem" in props["transfer"]["enum"]
        finally:
            xfer_mod._TRANSFER_BACKENDS.clear()
            xfer_mod._TRANSFER_BACKENDS.update(saved)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/models/test_jsonschema.py::TestSelectorEnums -q`
Expected: FAIL — `KeyError: 'enum'` (the generated `term`/`transfer` properties are plain `{"type": "string"}` after Task 4).

- [ ] **Step 3: Inject the enums**

In `src/otto/models/jsonschema.py`, add registry imports near the existing `from ..host.os_profile import registered_host_specs`:
```python
from ..host.connections import _TERM_BACKENDS
from ..host.transfer import _TRANSFER_BACKENDS
```

Add the injector helper:
```python
def _inject_selector_enums(schema: dict[str, Any], spec_cls: type[HostSpec]) -> None:
    """Add registry-derived ``enum`` constraints to the term/transfer selector
    properties of a host spec's schema, in place.

    The schema is generated after init modules load, so the enum includes
    custom per-repo backends as well as the built-ins — strictly better than the
    old static ``Literal``. Transfer is filtered to the spec's host family via
    ``_transfer_host_family``. No-op for a spec that declares neither field.
    """
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    if "term" in props:
        props["term"] = {**props["term"], "enum": sorted(_TERM_BACKENDS)}
    if "transfer" in props:
        family = getattr(spec_cls, "_transfer_host_family", None)
        names = sorted(
            n for n, c in _TRANSFER_BACKENDS.items()
            if family is None or family in c.host_families
        )
        props["transfer"] = {**props["transfer"], "enum": names}
```

In `build_schemas`, inject into each per-spec doc:
```python
    for spec in distinct:
        stem = _stem(spec)
        doc = spec.model_json_schema()
        _inject_selector_enums(doc, spec)
        docs[stem] = _decorate(doc, stem, f'otto {stem}')
```

In `_host_array_schema`, inject into the shared `$defs` (each distinct spec's definition), right after `models_json_schema(...)`:
```python
    defs_map, top = models_json_schema(
        [(s, 'validation') for s in distinct],
        ref_template='#/$defs/{model}',
    )
    for s in distinct:
        key = defs_map[(s, 'validation')]['$ref'].rsplit('/', 1)[-1]
        if key in top['$defs']:
            _inject_selector_enums(top['$defs'][key], s)
    return {
        ...  # unchanged body
    }
```

- [ ] **Step 4: Run the new tests + the schema regression**

Run: `uv run pytest tests/unit/models/test_jsonschema.py -q`
Expected: PASS — the new `TestSelectorEnums` plus all pre-existing schema tests, including the correctness test that validates the real `tests/lab_data/*/hosts.json` against the generated `hosts` schema (the built-in selector values they use are all in the injected enums, so they still validate).

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/models/jsonschema.py && uv run ruff check --fix src/otto/models/jsonschema.py tests/unit/models/test_jsonschema.py`
Then: `git add src/otto/models/jsonschema.py tests/unit/models/test_jsonschema.py`
Suggested message: `feat(schema): registry-derived term/transfer enums in the generated hosts schema (WS#4)`

---

### Task 5: Host construction through `create(ctx)`

**Files:**
- Modify: `src/otto/host/unix_host.py` (`__post_init__`, `rebuild_connections`)
- Modify: `src/otto/host/embedded_host.py` (`_file_transfer` construction)
- Test: `tests/unit/host/test_host_backend_construction.py` (create)

> Embedded hosts keep constructing their `ConnectionManager` **directly** with `term='telnet'` — that telnet bridge is not a term backend (design §3.1). Only the *transfer* moves behind `create`. The `_connection_factory` override is honored by selecting the class *before* `create`: a UnixHost double (`FakeConnections(*args, **kwargs)`) inherits `ConnectionManager.create`, which calls `cls(**kwargs)` — the fake swallows the kwargs, so the override survives unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/host/test_host_backend_construction.py`:
```python
"""UnixHost / EmbeddedHost build their backends through the registry + create (WS#4)."""

from unittest.mock import MagicMock

import pytest

from otto.host import connections as conn_mod
from otto.host import transfer as xfer_mod
from otto.host.connections import ConnectionManager
from otto.host.transfer import FileTransfer
from otto.host.unix_host import UnixHost


@pytest.fixture(autouse=True)
def _isolate_registries():
    saved_t = dict(conn_mod._TERM_BACKENDS)
    saved_x = dict(xfer_mod._TRANSFER_BACKENDS)
    try:
        yield
    finally:
        conn_mod._TERM_BACKENDS.clear()
        conn_mod._TERM_BACKENDS.update(saved_t)
        xfer_mod._TRANSFER_BACKENDS.clear()
        xfer_mod._TRANSFER_BACKENDS.update(saved_x)


def test_unix_host_builds_registered_transfer_backend():
    """A custom transfer backend registered at runtime is the one the host builds."""
    built = {}

    class RecordingTransfer(FileTransfer):
        host_families = frozenset({"unix"})

        @classmethod
        def create(cls, ctx):
            built["name"] = ctx.transfer
            return super().create(ctx)

    xfer_mod._TRANSFER_BACKENDS["recording"] = RecordingTransfer

    h = UnixHost(ip="10.0.0.9", creds={"root": "x"}, element="e",
                 transfer="recording")
    assert isinstance(h._file_transfer, RecordingTransfer)
    assert built["name"] == "recording"


def test_connection_factory_override_still_wins():
    """A _connection_factory test double is still used in place of the registry."""
    class FakeConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):
            self._name = kwargs.get("name", "fake")
            self._term = kwargs.get("term", "ssh")
            self._hop = None

    h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e",
                 term="ssh", _connection_factory=FakeConnections)
    assert isinstance(h._connections, FakeConnections)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/host/test_host_backend_construction.py -q`
Expected: FAIL on `test_unix_host_builds_registered_transfer_backend` — `__post_init__` constructs `FileTransfer` directly, ignoring the `"recording"` registry entry. (The factory test may already pass via the direct-construction path; both must pass after Step 3.)

- [ ] **Step 3: Rewire construction to the registry + create**

In `src/otto/host/unix_host.py`, extend the two existing import blocks. Change
```python
from .connections import (
    ConnectionManager,
    TermType,
)
```
to
```python
from .connections import (
    ConnectionManager,
    TermContext,
    TermType,
    build_term_backend,
)
```
and change
```python
from .transfer import (
    FileTransfer,
    FileTransferType,
)
```
to
```python
from .transfer import (
    FileTransfer,
    FileTransferType,
    TransferContext,
    build_transfer_backend,
)
```
(`TermType` / `FileTransferType` stay until Task 7.)

In **both** `__post_init__` and `rebuild_connections`, replace the `factory = self._connection_factory or ConnectionManager` + `factory(...)` block with:
```python
        term_ctx = TermContext(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term=self.term,
            name=self.name,
            hop=hop_transport,
            ssh_options=self.ssh_options,
            telnet_options=self.telnet_options,
            sftp_options=self.sftp_options,
            ftp_options=self.ftp_options,
        )
        conn_cls = self._connection_factory or build_term_backend(self.term)
        self._connections = conn_cls.create(term_ctx)
```
and replace the `self._file_transfer = FileTransfer(...)` block (in both methods) with:
```python
        self._file_transfer = build_transfer_backend(self.transfer).create(
            TransferContext(
                transfer=self.transfer,
                host_name=self.name,
                connections=self._connections,
                nc_options=self.nc_options,
                scp_options=self.scp_options,
                get_local_ip=lambda: self._get_local_ip(),
                exec_cmd=lambda *a, **kw: self.oneshot(*a, **kw),
                max_filename_len=self.max_filename_len,
            )
        )
```

In `src/otto/host/embedded_host.py`, add to the existing `from .transfer import ...` (or add a new import) `TransferContext, build_transfer_backend`, and replace the `self._file_transfer = EmbeddedFileTransfer(...)` block with:
```python
        self._file_transfer = build_transfer_backend(self.transfer).create(
            TransferContext(
                transfer=self.transfer,
                host_name=self.name,
                exec_cmd=lambda *a, **kw: self._run_one(*a, **kw),
                filesystem=self.filesystem,
                max_filename_len=self.max_filename_len,
            )
        )
```
(Leave the embedded `ConnectionManager(... term='telnet' ...)` construction unchanged.)

- [ ] **Step 4: Run the new test + the host construction suites**

Run: `uv run pytest tests/unit/host/test_host_backend_construction.py tests/unit/host/test_docker_host.py tests/unit/host/test_hop.py tests/unit/host/test_embedded_transfer.py -q`
Expected: PASS (the `_connection_factory` doubles in `test_docker_host`/`test_hop` keep working through `create`).

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/host/unix_host.py src/otto/host/embedded_host.py && uv run ruff check --fix src/otto/host/unix_host.py src/otto/host/embedded_host.py tests/unit/host/test_host_backend_construction.py`
Then: `git add src/otto/host/unix_host.py src/otto/host/embedded_host.py tests/unit/host/test_host_backend_construction.py`
Suggested message: `refactor(host): build connection/transfer backends via registry + create(ctx) (WS#4)`

---

### Task 6: Migrate the remaining `Literal` consumers off the `Literal`s

**Files:**
- Modify: `src/otto/cli/host.py` (registry-driven `--term`/`--transfer` **dynamic tab completion** + `typer.BadParameter` validation)
- Modify: `src/otto/host/unix_host.py` (`set_term_type` / `set_transfer_type` registry checks)
- Modify: `src/otto/host/__init__.py` (add `register_*_backend` / `build_transfer_backend` re-exports)
- Test: `tests/unit/host/test_term_registry.py`, `tests/unit/cli/test_host_cli.py` (or nearest existing CLI test)

> This task removes every *reference* to the `Literal` names except their definitions and the field/param annotations, so Task 7 can delete the definitions with the tree staying green.
>
> **Completion design (replaces the static `click.Choice`).** The old `click.Choice(get_args(...))` snapshots its choices at module import — *before* a repo's `init` modules run their `register_*` calls — so it can only ever complete built-ins. We replace it with a **dynamic `autocompletion=` callback** (the idiom `cli/host.py` already uses for `_host_id_completer`): the completer reads the completion-cache key if present, else the **live registry** (built-ins are always registered at import; custom backends are present once init modules load on the slow path). This guarantees the built-in defaults complete, surfaces custom per-repo backends, and — because the callback receives `ctx` — keeps the door open for future per-host narrowing (Task 10 records that). Validation moves to a `typer.BadParameter` wrapper around the existing `set_*_type` registry check, so an invalid value still gives a clean CLI error (not a traceback). The cache **fast path** that lets custom backends complete without running user code is **Task 10**; this task's completer already reads that cache key (absent until Task 10 → graceful live fallback).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_term_registry.py`:
```python
class TestSetTypeOverrides:
    def test_set_term_type_accepts_registered(self):
        from otto.host.unix_host import UnixHost

        h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", term="ssh")
        h.set_term_type("telnet")
        assert h.term == "telnet"

    def test_set_term_type_rejects_unregistered(self):
        from otto.host.unix_host import UnixHost

        h = UnixHost(ip="10.0.0.1", creds={"root": "x"}, element="e", term="ssh")
        with pytest.raises((ValueError, TypeError)):
            h.set_term_type("bogus")
        assert h.term == "ssh"  # unchanged
```

Add a re-export test — append to `tests/unit/host/test_transfer_registry.py`:
```python
def test_public_reexports_available():
    import otto.host as host_pkg

    assert hasattr(host_pkg, "register_term_backend")
    assert hasattr(host_pkg, "register_transfer_backend")
    assert hasattr(host_pkg, "build_transfer_backend")
```

Add completer tests — create `tests/unit/cli/test_host_cli.py` (or append to the nearest existing `otto host` CLI test):
```python
"""Dynamic registry-driven tab completion for `otto host --term/--transfer` (WS#4)."""

import click

from otto.cli.host import _term_completer, _transfer_completer


def _ctx():
    return click.Context(click.Command("host"))


def test_term_completer_includes_builtins():
    names = _term_completer(_ctx(), "")
    assert "ssh" in names and "telnet" in names


def test_term_completer_filters_by_prefix():
    assert _term_completer(_ctx(), "te") == ["telnet"]


def test_transfer_completer_offers_unix_protocols_only():
    names = _transfer_completer(_ctx(), "")
    assert {"scp", "sftp", "ftp", "nc"} <= set(names)
    assert "console" not in names  # embedded-only, not offered for the unix override


def test_transfer_completer_surfaces_custom_unix_backend():
    from otto.host import transfer as xfer_mod
    from otto.host.transfer import FileTransfer

    class XmodemTransfer(FileTransfer):
        host_families = frozenset({"unix"})

    saved = dict(xfer_mod._TRANSFER_BACKENDS)
    xfer_mod._TRANSFER_BACKENDS["xmodem"] = XmodemTransfer
    try:
        assert "xmodem" in _transfer_completer(_ctx(), "")
    finally:
        xfer_mod._TRANSFER_BACKENDS.clear()
        xfer_mod._TRANSFER_BACKENDS.update(saved)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/host/test_term_registry.py::TestSetTypeOverrides tests/unit/host/test_transfer_registry.py::test_public_reexports_available tests/unit/cli/test_host_cli.py -q`
Expected: FAIL — `set_term_type` uses `is_literal(...)` (raises generic `TypeError` but for the wrong reason / message), the re-exports don't exist yet, and `_term_completer` / `_transfer_completer` are undefined.

- [ ] **Step 3: Migrate the consumers**

In `src/otto/host/unix_host.py`, rewrite the two setters to validate against the registries (import `_TERM_BACKENDS` from `.connections` and `_TRANSFER_BACKENDS` from `.transfer` — already importing those modules):
```python
    def set_term_type(self, term: str) -> None:
        from .connections import _TERM_BACKENDS

        if term not in _TERM_BACKENDS:
            known = ", ".join(sorted(_TERM_BACKENDS))
            raise ValueError(f"Unknown term backend {term!r}. Known: {known}")
        self.term = term
        self._connections.term = term

    def set_transfer_type(self, transfer: str) -> None:
        from .transfer import _TRANSFER_BACKENDS

        cls = _TRANSFER_BACKENDS.get(transfer)
        if cls is None or "unix" not in cls.host_families:
            known = ", ".join(
                n for n, c in sorted(_TRANSFER_BACKENDS.items())
                if "unix" in c.host_families
            )
            raise ValueError(
                f"{transfer!r} is not a valid unix transfer backend. Known: {known}"
            )
        self.transfer = transfer
        self._file_transfer.transfer = transfer
```
(If the `is_literal` import in `unix_host.py` becomes unused, remove it from the import block.)

In `src/otto/cli/host.py`:

Remove the now-dead imports: `from ..host.connections import TermType`, `from ..host.transfer import FileTransferType`, and `get_args` from the `typing` import. (Also drop `import click` if `click.Choice` was its only use — ruff will flag it.)

Add the two completer callbacks near `_host_id_completer` (same cache-then-live strategy):
```python
def _term_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Completion source for ``--term``: registered term backends.

    Prefers the completion-cache snapshot (populated by the slow path so custom
    per-repo backends complete without re-running user code — see WS#4 Task 10);
    falls back to the live registry, where otto's built-ins are always present."""
    from ..configmodule import get_completion_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get('term_backends'), list):
        names = cached['term_backends']
    else:
        from ..host.connections import _TERM_BACKENDS
        names = list(_TERM_BACKENDS)
    return sorted(n for n in names if n.startswith(incomplete))


def _transfer_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Completion source for ``--transfer``: unix-applicable transfer backends.

    Same cache-then-live strategy as :func:`_term_completer`. The unified
    transfer registry spans both host families; ``otto host`` operates on a unix
    host, so only backends whose ``host_families`` include ``'unix'`` are offered.
    Cached entries are ``{"name": str, "host_families": [...]}`` (see Task 10)."""
    from ..configmodule import get_completion_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get('transfer_backends'), list):
        names = [
            e['name'] for e in cached['transfer_backends']
            if isinstance(e, dict) and 'unix' in e.get('host_families', [])
        ]
    else:
        from ..host.transfer import _TRANSFER_BACKENDS
        names = [n for n, c in _TRANSFER_BACKENDS.items() if 'unix' in c.host_families]
    return sorted(n for n in names if n.startswith(incomplete))
```

Change the `--term` / `--transfer` options to use the completers (drop `click_type`):
```python
    term: Annotated[Optional[str], typer.Option(
        '--term',
        autocompletion=_term_completer,
        help="Override the terminal protocol for this session.",
    )] = None,
    transfer: Annotated[Optional[str], typer.Option(
        '--transfer',
        autocompletion=_transfer_completer,
        help="Override the file transfer protocol for this session.",
    )] = None,
```

Wrap the override calls so an invalid value gives a clean CLI error instead of a traceback. Change
```python
    if term:
        host.set_term_type(term)

    if transfer:
        host.set_transfer_type(transfer)
```
to
```python
    if term:
        try:
            host.set_term_type(term)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--term") from None

    if transfer:
        try:
            host.set_transfer_type(transfer)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--transfer") from None
```

In `src/otto/host/__init__.py`, add re-exports (next to the existing `register_host_class` re-export):
```python
from .connections import register_term_backend as register_term_backend
from .connections import build_term_backend as build_term_backend
from .transfer import register_transfer_backend as register_transfer_backend
from .transfer import build_transfer_backend as build_transfer_backend
```
(Leave the `EmbeddedTransferType` re-export in place for now; Task 7 removes it with the `Literal`.)

- [ ] **Step 4: Run the targeted tests + the CLI host suite**

Run: `uv run pytest tests/unit/host/test_term_registry.py tests/unit/host/test_transfer_registry.py tests/unit/cli/test_host_cli.py tests/unit/cli/ -q -k "host or term or transfer"`
Expected: PASS — completers return the built-ins (and a custom unix backend); `set_*_type` rejects unregistered names; re-exports present.

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/cli/host.py src/otto/host/unix_host.py src/otto/host/__init__.py && uv run ruff check --fix src/otto/cli/host.py src/otto/host/unix_host.py src/otto/host/__init__.py tests/unit/host/test_term_registry.py tests/unit/host/test_transfer_registry.py tests/unit/cli/test_host_cli.py`
Then: `git add src/otto/cli/host.py src/otto/host/unix_host.py src/otto/host/__init__.py tests/unit/host/test_term_registry.py tests/unit/host/test_transfer_registry.py tests/unit/cli/test_host_cli.py`
Suggested message: `feat(cli): registry-driven tab completion for host --term/--transfer (WS#4)`

---

### Task 7: Retire the `Literal` definitions; annotations become `str`

**Files:**
- Modify: `src/otto/host/host.py` (delete duplicate `TermType` / `FileTransferType`)
- Modify: `src/otto/host/connections.py` (delete `TermType`; annotations → `str`)
- Modify: `src/otto/host/transfer.py` (delete `FileTransferType`; annotation → `str`)
- Modify: `src/otto/host/embedded_transfer.py` (delete `EmbeddedTransferType`; annotation → `str`)
- Modify: `src/otto/host/unix_host.py` (field annotations → `str`; drop now-dead `Literal` imports)
- Modify: `src/otto/host/__init__.py` (drop `EmbeddedTransferType` re-export)
- Test: existing suites (no new behavior)

> After Task 6 the only remaining references to the three `Literal` names are their definitions, the dataclass field/param annotations, and the `__init__.py` `EmbeddedTransferType` re-export. This task removes them all. Verify with grep before and after.

- [ ] **Step 1: Confirm the remaining references**

Run:
```bash
grep -rn "TermType\|FileTransferType\|EmbeddedTransferType" src/
```
Expected: only the definition lines, the field/param annotations, and `__init__.py:36`. (If anything else appears, it was missed in Task 6 — migrate it before continuing.)

- [ ] **Step 2: Delete the definitions and switch annotations**

- `src/otto/host/host.py`: delete lines `TermType = Literal['ssh', 'telnet']` and `FileTransferType = Literal['scp', 'sftp', 'ftp', 'nc']`. If `Literal` is now unused in that file, drop it from the `typing` import.
- `src/otto/host/connections.py`: delete `TermType = Literal['ssh', 'telnet']`. Change `term: TermType` (param, line ~122) and the `term` property/setter return+param annotations (~183/187) to `str`. Drop `Literal` from the `typing` import if now unused.
- `src/otto/host/transfer.py`: delete `FileTransferType = Literal['scp', 'sftp', 'ftp', 'nc']`. Change `FileTransfer.__init__`'s `transfer: FileTransferType` to `transfer: str`. Drop `Literal` from imports if unused (note: `cast`/`Any` may still be needed — only remove `Literal`).
- `src/otto/host/embedded_transfer.py`: delete `EmbeddedTransferType = Literal['console', 'tftp']` and its docstring. Change `EmbeddedFileTransfer.__init__`'s `transfer: EmbeddedTransferType` to `transfer: str`. Drop `Literal` from imports if unused.
- `src/otto/host/unix_host.py`: change `term: TermType = 'ssh'` → `term: str = 'ssh'` and `transfer: FileTransferType = 'scp'` → `transfer: str = 'scp'`. Remove the `TermType` / `FileTransferType` names from the import block.
- `src/otto/host/__init__.py`: delete `from .embedded_transfer import EmbeddedTransferType as EmbeddedTransferType`.

- [ ] **Step 3: Confirm the names are gone**

Run:
```bash
grep -rn "TermType\|FileTransferType\|EmbeddedTransferType" src/ tests/
```
Expected: no matches in `src/`. If any test referenced these names, update it to use `str` / the registry.

- [ ] **Step 4: Run the host + models + cli unit suites**

Run: `uv run pytest tests/unit/host tests/unit/models tests/unit/cli -q`
Expected: PASS.

- [ ] **Step 5: Type-check and stage**

Run: `uv run ty check src/otto/host/ src/otto/models/host.py && uv run ruff check --fix src/otto/host/host.py src/otto/host/connections.py src/otto/host/transfer.py src/otto/host/embedded_transfer.py src/otto/host/unix_host.py src/otto/host/__init__.py`
Then: `git add src/otto/host/host.py src/otto/host/connections.py src/otto/host/transfer.py src/otto/host/embedded_transfer.py src/otto/host/unix_host.py src/otto/host/__init__.py`
Suggested message: `refactor(host): retire term/transfer Literals for registry-backed str selectors (WS#4)`

---

### Task 8: Retrofit-all symmetry — built-ins via the public `register_*` path

**Files:**
- Modify: `src/otto/host/command_frame.py`
- Modify: `src/otto/host/embedded_filesystem.py`
- Modify: `src/otto/host/binary_loader.py`
- Modify: `todo/registry_builtin_registration_symmetry.md` (trim)
- Test: `tests/unit/host/test_command_frame.py`, `test_embedded_filesystem.py`, `test_binary_loader.py` (extend)

> `host_class` (`os_profile._register_builtin_host_classes`) and `snmp_metric` are already symmetric, and the two new registries (term, transfer) already bootstrap through their public path. This task converts the three remaining class registries that share the `register_X(type_name, cls)` shape.

- [ ] **Step 1: Write the failing tests**

Append a built-in-via-public-path assertion to each registry's test file. For `tests/unit/host/test_command_frame.py`:
```python
def test_builtins_registered_via_public_path():
    from otto.host import command_frame as cf

    # The seed dict starts empty and is populated by _register_builtin_frames()
    # through register_command_frame — the same path third parties use.
    assert set(cf._FRAME_CLASSES) >= {"bash", "zephyr", "zephyr-serial"}
    assert cf.build_command_frame("bash").type_name == "bash"
```
For `tests/unit/host/test_embedded_filesystem.py`:
```python
def test_builtins_registered_via_public_path():
    from otto.host import embedded_filesystem as efs

    assert set(efs._FILESYSTEM_CLASSES) >= {"none", "fat-ram", "littlefs"}
```
For `tests/unit/host/test_binary_loader.py`:
```python
def test_builtins_registered_via_public_path():
    from otto.host import binary_loader as bl

    assert len(bl._LOADER_CLASSES) >= 1  # at least the built-in loader(s)
```
(Adjust the expected names to the actual built-ins in each module if they differ — read the current dict literal first.)

- [ ] **Step 2: Run them to verify they pass-but-for-the-wrong-reason / refactor target**

Run: `uv run pytest tests/unit/host/test_command_frame.py::test_builtins_registered_via_public_path tests/unit/host/test_embedded_filesystem.py::test_builtins_registered_via_public_path tests/unit/host/test_binary_loader.py::test_builtins_registered_via_public_path -q`
Expected: PASS already (the names are present via the dict literal). These tests **lock the post-condition** so the Step 3 refactor (empty dict + bootstrap) can't regress it. This is a refactor task: the behavior is invariant; the *insertion path* changes.

- [ ] **Step 3: Convert each seed to a `_register_builtin_*()` bootstrap**

For each of `command_frame.py`, `embedded_filesystem.py`, `binary_loader.py`: change the populated dict literal to an empty dict, and add a bootstrap that registers each built-in through the public `register_*` function, called at module load. Pattern (command_frame shown; mirror for the other two):
```python
_FRAME_CLASSES: dict[str, type[CommandFrame]] = {}
```
...and after `register_command_frame` / `build_command_frame` are defined:
```python
def _register_builtin_frames() -> None:
    """Register otto's built-in frames through the public path, so first-party
    and third-party registrations travel the same code (mirrors
    ``os_profile._register_builtin_host_classes``)."""
    register_command_frame(BashFrame.type_name, BashFrame)
    register_command_frame(ZephyrFrame.type_name, ZephyrFrame)
    register_command_frame(ZephyrSerialFrame.type_name, ZephyrSerialFrame)


_register_builtin_frames()
```
For `embedded_filesystem.py` (built-ins confirmed: `NoFileSystem`, `FatRamFileSystem`, `LittleFsFileSystem`):
```python
_FILESYSTEM_CLASSES: dict[str, type[EmbeddedFileSystem]] = {}


def _register_builtin_filesystems() -> None:
    register_filesystem(NoFileSystem.type_name, NoFileSystem)
    register_filesystem(FatRamFileSystem.type_name, FatRamFileSystem)
    register_filesystem(LittleFsFileSystem.type_name, LittleFsFileSystem)


_register_builtin_filesystems()
```

For `binary_loader.py` (built-in confirmed: `LlextHexLoader`):
```python
_LOADER_CLASSES: dict[str, type[BinaryLoader]] = {}


def _register_builtin_loaders() -> None:
    register_binary_loader(LlextHexLoader.type_name, LlextHexLoader)


_register_builtin_loaders()
```

> Order constraint: the bootstrap call must come **after** the built-in classes *and* the `register_*` function are defined (place it at the very bottom of the module). `register_*` keeps last-writer-wins semantics; the built-ins seed first.

- [ ] **Step 4: Run the three registry suites in full**

Run: `uv run pytest tests/unit/host/test_command_frame.py tests/unit/host/test_embedded_filesystem.py tests/unit/host/test_binary_loader.py -q`
Expected: PASS (all pre-existing tests + the new bootstrap assertions).

- [ ] **Step 5: Trim the symmetry todo (do NOT delete)**

Edit `todo/registry_builtin_registration_symmetry.md`: mark Command frames, Embedded filesystems, and Binary loaders **done** (registered via their public path as of WS#4), and leave **only the monitor shell parsers** row as the remaining case, with a one-line note that it has a different shape (host-scoped, instance-valued `register_host_parsers`), so converting it would widen the public surface and is intentionally deferred. Update the `## Status` section to "Partially resolved (WS#4): the three `register_X(type_name, cls)` class registries are symmetric; only the host-scoped monitor-parser case remains."

- [ ] **Step 6: Type-check and stage**

Run: `uv run ty check src/otto/host/command_frame.py src/otto/host/embedded_filesystem.py src/otto/host/binary_loader.py && uv run ruff check --fix src/otto/host/command_frame.py src/otto/host/embedded_filesystem.py src/otto/host/binary_loader.py`
Then: `git add src/otto/host/command_frame.py src/otto/host/embedded_filesystem.py src/otto/host/binary_loader.py todo/registry_builtin_registration_symmetry.md tests/unit/host/test_command_frame.py tests/unit/host/test_embedded_filesystem.py tests/unit/host/test_binary_loader.py`
Suggested message: `refactor(host): register class-registry built-ins through the public path (WS#4)`

---

### Task 9: Docs — custom term/transfer backend guide

**Files:**
- Create: `docs/guide/extending-backends.md`
- Modify: `docs/guide/index.rst` (toctree)
- (Optional) Modify: `docs/guide/os-profiles.md` / `docs/guide/library-usage.md` cross-links

- [ ] **Step 1: Write the guide page**

Create `docs/guide/extending-backends.md` covering: the `register_term_backend` / `register_transfer_backend` entry points (called from an `init` module listed in `.otto/settings.toml`, like command frames); the `host_families` applicability tag and how the spec validates a selector against it (with the family-aware error examples); the `create(cls, ctx)` construction contract and the `TermContext` / `TransferContext` DTOs; and that the config-facing selector is always a registered string while bare callables (`_connection_factory`) are code-only test conveniences. Note `tftp` is reserved (registered, applicable to both families, raises `NotImplementedError` until implemented). Use MyST `{class}` / `{func}` / `{doc}` cross-references consistent with `extending-embedded.md`. Include a minimal runnable-looking example:
```python
# .otto/init.py
from otto.host import register_transfer_backend
from otto.host.transfer import BaseFileTransfer, TransferContext

class XmodemTransfer(BaseFileTransfer):
    host_families = frozenset({"unix", "embedded"})

    @classmethod
    def create(cls, ctx: TransferContext) -> "XmodemTransfer":
        return cls(name=ctx.host_name, max_filename_len=ctx.max_filename_len)

    async def _run_put(self, src_files, dest_dir, progress_factory): ...
    async def _run_get(self, src_files, dest_dir, progress_factory): ...

register_transfer_backend("xmodem", XmodemTransfer)
```
Then a host selects it with `"transfer": "xmodem"` in `hosts.json`.

- [ ] **Step 2: Add the page to the toctree**

In `docs/guide/index.rst`, add `extending-backends` to the toctree immediately after `extending-embedded`.

- [ ] **Step 3: Build the docs**

Run: `make docs`
Expected: clean build, no warnings about the new page, no broken cross-references, doctests still pass.

- [ ] **Step 4: Stage**

Run: `git add docs/guide/extending-backends.md docs/guide/index.rst`
Suggested message: `docs(guide): custom term/transfer backend extension guide (WS#4)`

---

### Task 10: Completion cache fast-path for backend names

**Files:**
- Modify: `src/otto/configmodule/completion_cache.py` (schema bump, `collect_backend_names`, `write_cache`/`read_cache` extension)
- Modify: `src/otto/configmodule/__init__.py` (slow-path wiring + `get_completion_names` docstring)
- Create: `todo/host-declared-transfer-term-lists.md` (tier-3 future work)
- Test: `tests/unit/configmodule/test_completion_cache_unit.py` (extend)

> **Why:** the Task 6 completers read a `term_backends` / `transfer_backends` cache key when present, so custom per-repo backends complete on the **fast path** (without re-running user `init` modules). This task populates that key on the slow path. The cache fingerprint already covers every `init`-module file (`compute_fingerprint`), so a newly-registered backend invalidates the cache automatically — no fingerprint change needed. Bumping `SCHEMA_VERSION` invalidates older entries (safe: the slow path rewrites them).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/configmodule/test_completion_cache_unit.py` (it already imports `MagicMock` / `Path`):
```python
def test_collect_backend_names_includes_builtins():
    from otto.configmodule import completion_cache as cc

    snap = cc.collect_backend_names()
    assert "ssh" in snap["term_backends"] and "telnet" in snap["term_backends"]
    by_name = {e["name"]: e["host_families"] for e in snap["transfer_backends"]}
    assert by_name["scp"] == ["unix"]
    assert by_name["console"] == ["embedded"]


def test_write_read_cache_round_trips_backend_names(tmp_path: Path, monkeypatch) -> None:
    from otto.configmodule import completion_cache as cc

    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    (fake_repo.sut_dir / ".otto").mkdir()
    (fake_repo.sut_dir / ".otto" / "settings.toml").write_text("")
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.labs = []

    cc.write_cache(
        [fake_repo], instructions=[], suites=[], hosts=[],
        term_backends=["ssh", "telnet"],
        transfer_backends=[{"name": "scp", "host_families": ["unix"]}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["term_backends"] == ["ssh", "telnet"]
    assert out["transfer_backends"] == [{"name": "scp", "host_families": ["unix"]}]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache_unit.py -q -k "backend"`
Expected: FAIL — `collect_backend_names` doesn't exist; `write_cache` rejects the `term_backends` / `transfer_backends` kwargs.

- [ ] **Step 3: Extend the cache**

In `src/otto/configmodule/completion_cache.py`:

Bump the schema:
```python
SCHEMA_VERSION = 5
```

Add the writer-side collector (next to `collect_current_commands`):
```python
def collect_backend_names() -> dict[str, Any]:
    """Snapshot the registered term + transfer backend names for completion.

    Call after ``apply_repo_settings`` / ``import_init_modules`` so custom
    per-repo backends are present. Built-ins are always present (registered at
    module import). Each transfer backend carries its ``host_families`` so the
    completer can filter by family (e.g. unix-only for ``otto host --transfer``)."""
    from ..host.connections import _TERM_BACKENDS
    from ..host.transfer import _TRANSFER_BACKENDS

    return {
        "term_backends": sorted(_TERM_BACKENDS),
        "transfer_backends": [
            {"name": name, "host_families": sorted(cls.host_families)}
            for name, cls in sorted(_TRANSFER_BACKENDS.items())
        ],
    }
```

Extend `write_cache` — add two keyword params and store them in the entry:
```python
def write_cache(
    repos: list['Repo'],
    instructions: list[dict[str, Any]],
    suites: list[dict[str, Any]],
    hosts: list[str],
    docker_hosts: list[str] | None = None,
    term_backends: list[str] | None = None,
    transfer_backends: list[dict[str, Any]] | None = None,
) -> None:
```
and in the `existing[fingerprint] = {...}` dict add:
```python
        'term_backends': term_backends or [],
        'transfer_backends': transfer_backends or [],
```

Extend `read_cache` — read, type-check, and return the two new keys (default `[]` for tolerance, though the schema bump already drops old entries):
```python
    term_backends = entry.get('term_backends', [])
    transfer_backends = entry.get('transfer_backends', [])
```
add `or not isinstance(term_backends, list) or not isinstance(transfer_backends, list)` to the existing validation `if`, and add to the returned dict:
```python
        'term_backends': term_backends,
        'transfer_backends': transfer_backends,
```

In `src/otto/configmodule/__init__.py`: add `collect_backend_names` to the `completion_cache` import block; in the slow path (right after `_docker_host_ids = collect_docker_capable_host_ids(_repos)`), add `_backends = collect_backend_names()` and pass it through:
```python
        write_cache(
            _repos, _instructions, _suites, _host_ids, _docker_host_ids,
            term_backends=_backends['term_backends'],
            transfer_backends=_backends['transfer_backends'],
        )
```
Update the `get_completion_names` docstring to list the two new keys (`term_backends`: `list[str]`; `transfer_backends`: `list[{"name", "host_families"}]`).

- [ ] **Step 4: Create the tier-3 future-work note**

Create `todo/host-declared-transfer-term-lists.md` recording the far-future feature: each `hosts.json` entry declares its supported `term` / `transfer` lists, and the `--term` / `--transfer` completer narrows to *that host's* declared set by reading the already-typed `host_id` from `ctx.params` (the WS#4 completer already receives `ctx`, so this is a pure addition — no redesign). Note it needs a `hosts.json` schema addition (optional `supported_terms` / `supported_transfers` on `UnixHostSpec`) plus per-host completion data cached by host id (like host IDs are cached today), and that it is gated on a concrete need.

- [ ] **Step 5: Run the cache suite**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache.py tests/unit/configmodule/test_completion_cache_unit.py -q`
Expected: PASS (new backend tests + all pre-existing cache tests — the schema bump is internally consistent).

- [ ] **Step 6: Type-check and stage**

Run: `uv run ty check src/otto/configmodule/completion_cache.py src/otto/configmodule/__init__.py && uv run ruff check --fix src/otto/configmodule/completion_cache.py src/otto/configmodule/__init__.py tests/unit/configmodule/test_completion_cache_unit.py`
Then: `git add src/otto/configmodule/completion_cache.py src/otto/configmodule/__init__.py todo/host-declared-transfer-term-lists.md tests/unit/configmodule/test_completion_cache_unit.py`
Suggested message: `feat(completion): cache registered term/transfer backend names for fast-path completion (WS#4)`

---

### Task 11: Full gate + final review

**Files:** none (verification)

> This is the behavior-preservation proof. The connection/transfer integration suites (real ssh/telnet/scp/sftp/ftp/nc against the Unix lab VMs; console against the Zephyr bed) are authoritative — they must stay green unchanged. **Do not kill live-bed runs** (single-client console wedges on SIGTERM; recover with `make qemu-restart`).

- [ ] **Step 1: Coverage gate**

Run: `make coverage`
Expected: PASS, line coverage ≥ 90%. If a new branch is uncovered (e.g. `register_transfer_backend`'s empty-`host_families` guard, a `build_*` unknown path), add a focused unit test for it and re-run.

- [ ] **Step 2: Multi-Python gate**

Run: `make nox`
Expected: PASS on all of Python 3.10–3.14.

- [ ] **Step 3: Type + docs gate**

Run: `uv run ty check src/ && make docs`
Expected: `ty` clean; docs build clean (53+ doctests pass).

- [ ] **Step 4: Final grep sweep**

Run: `grep -rn "TermType\|FileTransferType\|EmbeddedTransferType" src/ tests/ docs/`
Expected: no matches (the `Literal` names are fully retired). Documentation refers to backends by registry name, not the old type aliases.

- [ ] **Step 5: Stage any gate-driven additions**

If Steps 1–3 required new tests, `git add` them. Suggested message: `test(host): cover registry guards surfaced by the WS#4 gate`

- [ ] **Step 6: Hand off for review + commit**

Report the full staged diff summary to the controller for the final holistic review. **The controller does NOT commit** — Chris reviews the staged branch and commits by hand (prepare-commit-msg hook + AI-assist trailer). After Chris confirms, use **superpowers:finishing-a-development-branch** to merge `ws4-registry-public-api` → `main`.

---

## Self-review (completed during planning)

- **Spec coverage:** §3.1 term registry → Task 1. §3.2 unified transfer registry + `host_families` → Tasks 2–3. §4 `create(ctx)` seam + bare-callable demotion → Tasks 2/5 (`_connection_factory` honored via class-before-`create`). §5 selector validation → Task 4. §6 retrofit-all → Task 8 (with the honest monitor-parser carve-out noted up top). §7 frozen surface (re-exports) → Tasks 6–7. §8 file map → covered file-by-file. §9 error handling → `ValueError` in `build_*` + validators, empty-`host_families` reject, `tftp`→`NotImplementedError` (Task 3 keeps the existing raise), CLI `BadParameter` on invalid override (Task 6). §10 testing → per-task tests + Task 11 gate + isolation fixtures. §11 risks (context creep, factory compat) → DTO field sets pinned + `FakeConnections(*args, **kwargs)` compat verified.
- **Completion (added per user request):** `--term`/`--transfer` keep tab completion and it becomes registry-driven, not a static `Literal` snapshot. Tier 1 (built-in defaults) is guaranteed by the live-registry fallback; tier 2 (custom per-repo backends) works on the slow path immediately (Task 6) and on the fast path via the cache snapshot (Task 10); tier 3 (per-host declared lists) is kept *possible* by the `ctx`-receiving completer and recorded as `todo/host-declared-transfer-term-lists.md`. A static `click.Choice` would have foreclosed tiers 2 and 3.
- **Placeholder scan:** none — every code step shows the code; doc step describes exact content + example.
- **Type consistency:** `build_term_backend`/`build_transfer_backend` return the **class** (host calls `.create`); `build_command_frame` returns an instance (unchanged) — deliberately different, called out in Tasks 1/2. `TransferContext.transfer` is the selector string; `host_name` is the host name (the two `name`s are disambiguated). `create(cls, ctx)` signature identical across `ConnectionManager` / `BaseFileTransfer` / `FileTransfer` / `EmbeddedFileTransfer`.
- **Green-between-tasks:** `Literal`s live until every consumer is migrated (Task 6), then deleted (Task 7) — no red window. Retrofit (Task 8) is behavior-invariant. Verified the only `_connection_factory` doubles (`test_docker_host`, `test_hop`) accept `**kwargs`, so `create(ctx)` is drop-in.

---

## Outcome — as built (2026-06-17)

Executed subagent-driven (fresh implementer + spec-review + quality-review per task, then a final holistic review) and committed by Chris as **`b093f6d`** (*"feat(host): registry public API for term/transfer backends (WS#4)"*), 33 files. Final gate: **1825 unit tests pass (90% coverage)**, `ty` clean, `make docs` clean (53 doctests); the holistic review returned **READY TO MERGE** after tracing a custom backend end-to-end through all six surfaces (register → validate → construct → complete → cache → schema-enum). The live `make coverage` (lab VMs + Zephyr bed) and `make nox` (5 Pythons) were run by Chris.

### As-built deviations from the task steps above

The step text above predates a few execution-time decisions; the **committed code is the source of truth**. The deviations:

- **`cast(<Literal>, ctx.<selector>)` bridges in the `create()` methods (Tasks 1–3).** While the `Literal`s were kept alive (additive phase), `create()` passed the widened `str` ctx field into a still-`Literal`-typed `__init__`, so `ty` required a `cast` (`cast(TermType, ctx.term)`, etc.). These were **removed in Task 7** when the `Literal`s were retired and the params widened to `str`. (The Task-5 `cast(FileTransfer/EmbeddedFileTransfer, …)` *construction* casts are unrelated — `BaseFileTransfer`→narrow-type — and remain.)
- **Four `# type: ignore` on the `unix_host` setter assignments (Task 6),** for the same `str`→`Literal` reason; **removed in Task 7**.
- **Task 8 also retrofitted `binary_loader`** (not in the symmetry todo's original table) and **trimmed** `todo/registry_builtin_registration_symmetry.md` rather than deleting it — the monitor shell parsers have a different shape (host-scoped, instance-valued `register_host_parsers`), so converting them would widen the frozen public surface and is intentionally deferred.
- **Post-review fix (folded into the same commit): `set_transfer_type` now rebuilds the transfer backend via the registry + `create()`** — extracting `_build_connections()` / `_build_file_transfer()` helpers, which also DRY `__post_init__` / `rebuild_connections`. The previous in-place `self._file_transfer.transfer = transfer` only worked for switching among protocols served by the one built-in `FileTransfer` instance; for a *custom* backend it left the wrong class in place. **`set_term_type` was deliberately left as the in-place ssh/telnet swap** — rebuilding it clobbers the live session (it broke two CLI tests), and full custom-*term* support is the post-freeze connection refactor (design §11).

### Process note

A subagent's `git stash` + `git stash pop` (used to inspect a pre-change baseline) silently **un-staged** two earlier tasks' files — content intact on disk, dropped from the index. Caught with a per-task `git diff --stat` drift check and re-`git add`. Capture review baselines with `git stash create` (non-destructive) instead.
