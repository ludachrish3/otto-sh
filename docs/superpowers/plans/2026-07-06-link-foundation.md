# Link Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build sub-project #1 of the Link stack: the `Link` data model, the `hosts.json` → `lab.json` hard cutover, per-netdev interface objects, static (implicit + declared) link derivation, and the dynamic-link sentinel contract — all pure, hostless, unit-tested; no live network side effects.

**Spec:** `docs/superpowers/specs/2026-07-06-link-foundation-design.md` (read it first). Working notes: `todo/link.md`.

**Architecture:** Three layers, mirroring the host subsystem: pydantic *boundary specs* (`src/otto/models/link.py`, `InterfaceSpec` in `models/host.py`) validate `lab.json`; frozen runtime *dataclasses* (`src/otto/link/model.py`, `src/otto/host/interface.py`) are what consumers hold; pure *derivation/codec functions* (`src/otto/link/derive.py`, `sentinel.py`, `discovery.py`) produce links from hosts, lab data, or process listings. The `Lab` container gains `links` + `static_links()`.

**Tech Stack:** Python 3.10+, pydantic v2 (`OttoModel`, `extra="forbid"`), stdlib `dataclasses`/`enum`/`hashlib`/`urllib.parse`, pytest (hostless).

## Global Constraints

- **NEVER** add `from __future__ import annotations` — it breaks the Sphinx nitpicky (`-W`) docs gate. Use real 3.10+ annotations and module-top imports.
- New pydantic models subclass `OttoModel` (`src/otto/models/base.py`) — inherits `extra="forbid"`.
- Method overrides need `@override` from `typing_extensions` (ty enforces `all=error`).
- Lint is strict (`ruff select=ALL`): fix findings properly, never blanket-ignore; a narrow per-site `# noqa: RULE — reason` is the last resort. Run `uv run ruff format <changed files>` before committing (agents habitually forget; `nox` lint = `ruff check` **and** `format --check`).
- `ty` runs ONLY in `nox -s typecheck` — run it after every task that edits `src/` (`make coverage` does NOT typecheck).
- Per-task gate: `make coverage` (from the worktree root). Full end gate: coverage + `nox -s lint typecheck` + `make docs`.
- Commits in this worktree: self-commit allowed. Conventional prefix, `-m` message, manual trailer line `Assisted-by: Claude Opus 4.8`, then verify with `git log -1`.
- **Hard cutover** (spec §2/§13): no dual-format loader, no `hosts.json` fallback anywhere. Task 4 flips every reader, writer, fixture, and test **in one commit** so the tree is never half-cut.
- **Stability contracts** (long-term: sub-projects #2–#6 and cross-version discovery depend on them; changing either later invalidates live tunnels): the link-id algorithm (Task 3) and the sentinel wire format (Task 6). Both carry explicit docstring warnings.
- Keep `otto.link` off import-hot paths: `Lab` imports it lazily inside methods (the import-budget guard at `tests/unit/import_budget/` will catch violations).
- Do NOT re-export `Link` etc. from `otto/__init__.py` in this sub-project (YAGNI — #2 decides the public surface).
- Never write test files inside the repo; use `tmp_path`.

---

### Task 1: Interface model — `InterfaceSpec` (boundary) + `Interface` (runtime)

Evolve host `interfaces` from `dict[name → ip-string]` to `dict[netdev-name → object]`, keeping the bare-string shorthand as ergonomic coercion (spec §4).

**Files:**
- Create: `src/otto/host/interface.py`
- Modify: `src/otto/models/host.py` (field at `:175`, validator at `:221-231`, kwargs at `:292-293`)
- Modify: `src/otto/host/remote_host.py` (contract `:179-183`, `address_for` `:286-295`)
- Modify: `src/otto/host/unix_host.py:241-244`, `src/otto/host/embedded_host.py:239-242`
- Test: `tests/unit/models/test_host_specs.py`, `tests/unit/host/test_remote_host_addressing.py`

**Interfaces:**
- Consumes: `OttoModel` (`otto.models.base`), `ip_address` (stdlib).
- Produces: `Interface(ip: str)` frozen dataclass in `otto.host.interface`; `InterfaceSpec(ip: str)` with `.to_runtime() -> Interface`; host spec field `interfaces: dict[str, InterfaceSpec]` accepting `"eth0": "10.0.0.5"` shorthand; runtime `host.interfaces: dict[str, Interface]`; `RemoteHost.address_for(name_or_literal) -> str` unchanged signature, now reads `.ip`. Tasks 5's declared-link resolution relies on the shorthand-or-object duality when reading **raw host dicts**.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/models/test_host_specs.py` (follow the module's existing style — it builds minimal unix host dicts; add `from otto.host.interface import Interface` to its imports):

```python
class TestInterfaceSpec:
    """interfaces: dict[netdev -> InterfaceSpec], with bare-string shorthand."""

    def _host(self, interfaces: object) -> dict:
        return {
            "ip": "192.0.2.1",
            "element": "iface-host",
            "creds": [{"login": "u", "password": "p"}],
            "interfaces": interfaces,
        }

    def test_object_form_parses(self):
        spec = UnixHostSpec.model_validate(self._host({"eth1": {"ip": "10.0.0.5"}}))
        assert spec.interfaces["eth1"].ip == "10.0.0.5"

    def test_string_shorthand_coerces(self):
        spec = UnixHostSpec.model_validate(self._host({"eth1": "10.0.0.5"}))
        assert spec.interfaces["eth1"].ip == "10.0.0.5"

    def test_bad_ip_rejected(self):
        with pytest.raises(ValidationError, match="not a valid IP"):
            UnixHostSpec.model_validate(self._host({"eth1": {"ip": "not-an-ip"}}))

    def test_unknown_interface_key_rejected(self):
        with pytest.raises(ValidationError):
            UnixHostSpec.model_validate(self._host({"eth1": {"ip": "10.0.0.5", "mac": "x"}}))

    def test_runtime_host_gets_interface_objects(self):
        host = UnixHostSpec.model_validate(self._host({"eth1": "10.0.0.5"})).to_host()
        assert host.interfaces["eth1"] == Interface(ip="10.0.0.5")
```

Update `tests/unit/host/test_remote_host_addressing.py`: wherever a host is built with `interfaces={"name": "10.0.0.5"}` (raw strings), change to `interfaces={"name": Interface(ip="10.0.0.5")}` (import `from otto.host.interface import Interface`) and assert `address_for("name") == "10.0.0.5"` still (behavior unchanged: name → address string; literal passthrough unchanged).

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest tests/unit/models/test_host_specs.py -k InterfaceSpec -x -q`
Expected: FAIL — `NameError: Interface`/`ValidationError` (dict[str,str] rejects the object form).

- [ ] **Step 3: Implement**

Create `src/otto/host/interface.py`:

```python
"""Runtime record for one named network device on a host.

The ``host.interfaces`` map is keyed by the **netdev name** (``eth0``,
``eth1.100``, …) so link impairment/capture can address the device directly;
the value object is deliberately extensible (future: mac, cidr, role, …).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Interface:
    """One named network device on a host."""

    ip: str
    """Address assigned to this interface."""
```

In `src/otto/models/host.py`: add near `ToolchainSpec`:

```python
class InterfaceSpec(OttoModel):
    """One ``interfaces`` entry, keyed by the netdev name (``eth0``, …).

    A bare string value (``"eth0": "10.0.0.5"``) is accepted as shorthand for
    ``{"ip": "10.0.0.5"}`` (coerced in ``HostSpec._coerce_interface_shorthand``).
    """

    ip: str

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, v: str) -> str:
        try:
            ip_address(v)
        except ValueError:
            raise ValueError(f"interface address {v!r} is not a valid IP") from None
        return v

    def to_runtime(self) -> Interface:
        """Build the runtime ``Interface`` dataclass."""
        return Interface(ip=self.ip)
```

(import `Interface` from `..host.interface` at module top). Replace the `interfaces` field + old validator on `HostSpec`:

```python
    interfaces: dict[str, InterfaceSpec] = Field(default_factory=dict)
```

```python
    @field_validator("interfaces", mode="before")
    @classmethod
    def _coerce_interface_shorthand(cls, v: object) -> object:
        # "eth0": "10.0.0.5"  ->  "eth0": {"ip": "10.0.0.5"}
        if isinstance(v, dict):
            return {k: ({"ip": e} if isinstance(e, str) else e) for k, e in v.items()}
        return v
```

Delete `_validate_interface_addresses` (`:221-231`) — IP validation now lives in `InterfaceSpec` (pydantic's error path names the key: `interfaces.eth1.ip`). In `_common_host_kwargs` (`:292-293`):

```python
        if "interfaces" in s:
            kw["interfaces"] = {k: e.to_runtime() for k, e in self.interfaces.items()}
```

In `src/otto/host/remote_host.py`: change the contract annotation to `interfaces: dict[str, "Interface"]` (add `from .interface import Interface` to the `TYPE_CHECKING` block at `:40-47`), update its docstring example to `{"eth0": Interface(ip="10.0.0.5")}` and note the key is the netdev name. Rewrite `address_for` body:

```python
        entry = self.interfaces.get(name_or_literal)
        return entry.ip if entry is not None else name_or_literal
```

In `unix_host.py:241` and `embedded_host.py:239` change the field to `interfaces: dict[str, Interface] = field(default_factory=dict, repr=False)` with a real (non-TYPE_CHECKING) `from .interface import Interface` import — dataclass fields need the runtime type.

- [ ] **Step 4: Sweep remaining consumers**

Run: `grep -rn "address_for\|interfaces\[" src/ tests/ --include="*.py" | grep -v "\.claude"`
Fix any site still assuming string values (expected: only the two test files from Step 1; `SnmpOptions.address` resolution is doc-only today — update the `todo/multi_interface_hosts.md` pointer in `src/otto/host/options.py:522-526` docstring to say the map now holds `Interface` objects).

- [ ] **Step 5: Run gates**

Run: `uv run pytest tests/unit/models tests/unit/host -q` then `make coverage` then `uv run nox -s typecheck`
Expected: all green. The host-spec **drift guard** must stay green (field name `interfaces` unchanged on both sides — see `HOST_SPEC_RUNTIME_PAIRS`, `models/host.py:457`).

- [ ] **Step 6: Commit**

```bash
git add -A src/otto tests/unit && git commit -m "feat(host): interfaces become netdev-keyed Interface objects with string shorthand

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 2: `LinkSpec` / `LinkEndpointSpec` boundary models

Structural validation of one `links` entry (spec §5). Reference resolution (host ids, interface keys) is Task 5 — it needs the host set.

**Files:**
- Create: `src/otto/models/link.py`
- Test: create `tests/unit/models/test_link_specs.py`

**Interfaces:**
- Consumes: `OttoModel`.
- Produces: `LinkEndpointSpec(host: str, interface: str | None)`; `LinkSpec(endpoints: list[LinkEndpointSpec] (exactly 2), protocol: str = "tcp" (lowercased), name: str | None, impair: str | None, management: str | None)`. `_`-prefixed keys stripped (JSON comment idiom, same as hosts). Task 5 calls `LinkSpec.model_validate(entry)`; Task 8 exports its JSON schema.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/models/test_link_specs.py`:

```python
"""Boundary validation for lab.json ``links`` entries."""

import pytest
from pydantic import ValidationError

from otto.models.link import LinkSpec


def _entry(**overrides) -> dict:
    base = {
        "endpoints": [
            {"host": "carrot_seed", "interface": "eth1"},
            {"host": "tomato_seed", "interface": "eth1"},
        ],
        "protocol": "udp",
    }
    return {**base, **overrides}


class TestLinkSpec:
    def test_full_entry_parses(self):
        spec = LinkSpec.model_validate(_entry(name="data-plane-a"))
        assert spec.endpoints[0].host == "carrot_seed"
        assert spec.protocol == "udp"
        assert spec.name == "data-plane-a"

    def test_protocol_defaults_to_tcp(self):
        entry = _entry()
        del entry["protocol"]
        assert LinkSpec.model_validate(entry).protocol == "tcp"

    def test_protocol_lowercased(self):
        assert LinkSpec.model_validate(_entry(protocol="UDP")).protocol == "udp"

    def test_interface_optional(self):
        entry = _entry(endpoints=[{"host": "a"}, {"host": "b"}])
        spec = LinkSpec.model_validate(entry)
        assert spec.endpoints[0].interface is None

    @pytest.mark.parametrize("count", [1, 3])
    def test_exactly_two_endpoints(self, count):
        entry = _entry(endpoints=[{"host": f"h{i}"} for i in range(count)])
        with pytest.raises(ValidationError):
            LinkSpec.model_validate(entry)

    def test_self_link_rejected(self):
        entry = _entry(endpoints=[{"host": "a", "interface": "eth0"}] * 2)
        with pytest.raises(ValidationError, match="must differ"):
            LinkSpec.model_validate(entry)

    def test_same_host_different_interface_allowed(self):
        entry = _entry(
            endpoints=[
                {"host": "a", "interface": "eth0"},
                {"host": "a", "interface": "eth1"},
            ]
        )
        LinkSpec.model_validate(entry)  # loopback cabling: legal

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            LinkSpec.model_validate(_entry(bandwidth="10G"))

    def test_underscore_comment_keys_stripped(self):
        LinkSpec.model_validate(_entry(_comment="a note"))

    def test_reserved_fields_accepted(self):
        spec = LinkSpec.model_validate(_entry(impair="netem", management="mgmt-01"))
        assert (spec.impair, spec.management) == ("netem", "mgmt-01")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/models/test_link_specs.py -q`
Expected: FAIL — `ModuleNotFoundError: otto.models.link`.

- [ ] **Step 3: Implement**

Create `src/otto/models/link.py`:

```python
"""Pydantic boundary specs for a ``lab.json`` ``links`` entry.

Structural validation only: endpoint *references* (host ids, interface keys)
are resolved against the loaded host set at lab-load time
(:func:`otto.link.derive.resolve_declared_links`), where the hosts are known.
"""

from pydantic import Field, field_validator, model_validator

from .base import OttoModel


class LinkEndpointSpec(OttoModel):
    """One end of a declared link: a host id plus (optionally) a named interface.

    ``interface`` (a key in the host's ``interfaces`` map, i.e. a netdev name)
    is required only when the host defines more than one interface; with one or
    none, otto assumes the sole interface / the management ``ip``.
    """

    host: str
    interface: str | None = None


class LinkSpec(OttoModel):
    """Boundary spec for one ``links`` entry in ``lab.json``.

    ``protocol`` is informational for declared links (what the route carries);
    it becomes functional for dynamic links (sub-project #2). ``impair`` and
    ``management`` are reserved for sub-projects #3/#5: accepted and carried,
    not yet consumed.
    """

    endpoints: list[LinkEndpointSpec] = Field(min_length=2, max_length=2)
    protocol: str = "tcp"
    name: str | None = None
    impair: str | None = None
    management: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _strip_comment_keys(cls, data: object) -> object:
        """Drop ``_``-prefixed keys — the JSON comment idiom (see HostSpec)."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}
        return data

    @field_validator("protocol")
    @classmethod
    def _normalize_protocol(cls, v: str) -> str:
        return v.lower()

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> "LinkSpec":
        a, b = self.endpoints
        if a.host == b.host and a.interface == b.interface:
            raise ValueError("link endpoints must differ (same host and interface on both ends)")
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/models/test_link_specs.py -q` — Expected: PASS. Then `uv run nox -s typecheck`.

- [ ] **Step 5: Commit**

```bash
git add src/otto/models/link.py tests/unit/models/test_link_specs.py && git commit -m "feat(link): LinkSpec/LinkEndpointSpec boundary models for lab.json links entries

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 3: Runtime `Link` / `LinkEndpoint` / `Provenance` + deterministic id

The unified edge type all provenances share (spec §6).

**Files:**
- Create: `src/otto/link/__init__.py`, `src/otto/link/model.py`
- Test: create `tests/unit/link/__init__.py` (empty), `tests/unit/link/test_model.py`

**Interfaces:**
- Consumes: stdlib only (`dataclasses`, `enum`, `hashlib`).
- Produces (consumed by Tasks 5/6/7 and sub-projects #2–#6):
  - `Provenance` enum: `IMPLICIT`/`DECLARED`/`DYNAMIC` (values `"implicit"`/`"declared"`/`"dynamic"`).
  - `LinkEndpoint(host: str, interface: str | None = None, ip: str = "", port: int | None = None)` — frozen. `port` is set only on dynamic links (#2); static endpoints leave it `None`.
  - `Link(a: LinkEndpoint, b: LinkEndpoint, protocol: str = "tcp", provenance: Provenance = Provenance.DECLARED, id: str = "", name: str | None = None)` — frozen; empty `id` auto-computes in `__post_init__`.
  - `make_link_id(a, b, protocol) -> str` — **STABILITY CONTRACT**: `"lnk-"` + first 12 hex of sha256 over `f"{lo.host}|{lo.interface or ''}|{hi.host}|{hi.interface or ''}|{protocol}"` with endpoints sorted by `(host, interface or "")`. **Ports and ips excluded** — the id names the *route*, so a dynamic tunnel over a declared route reconciles to the same id, and endpoint order never matters.
- All re-exported from `otto.link`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/link/test_model.py`:

```python
"""Runtime Link model: identity, normalization, provenance."""

import dataclasses

import pytest

from otto.link import Link, LinkEndpoint, Provenance, make_link_id


def _ep(host: str, iface: str | None = "eth1") -> LinkEndpoint:
    return LinkEndpoint(host=host, interface=iface, ip="10.0.0.1")


class TestLinkId:
    def test_id_auto_computed(self):
        link = Link(a=_ep("carrot"), b=_ep("tomato"))
        assert link.id.startswith("lnk-") and len(link.id) == 16

    def test_id_endpoint_order_invariant(self):
        assert Link(a=_ep("carrot"), b=_ep("tomato")).id == Link(a=_ep("tomato"), b=_ep("carrot")).id

    def test_id_ignores_ip_and_port(self):
        moved = LinkEndpoint(host="carrot", interface="eth1", ip="10.9.9.9", port=5000)
        assert Link(a=moved, b=_ep("tomato")).id == Link(a=_ep("carrot"), b=_ep("tomato")).id

    def test_id_distinguishes_protocol(self):
        a, b = _ep("carrot"), _ep("tomato")
        assert Link(a=a, b=b, protocol="udp").id != Link(a=a, b=b, protocol="tcp").id

    def test_id_distinguishes_interface(self):
        assert Link(a=_ep("carrot", "eth1"), b=_ep("tomato")).id != Link(a=_ep("carrot", "eth2"), b=_ep("tomato")).id

    def test_explicit_id_preserved(self):
        assert Link(a=_ep("a"), b=_ep("b"), id="lnk-abcdef123456").id == "lnk-abcdef123456"

    def test_make_link_id_matches_dataclass(self):
        a, b = _ep("carrot"), _ep("tomato")
        assert make_link_id(a, b, "tcp") == Link(a=a, b=b).id


class TestLinkDefaults:
    def test_protocol_defaults_tcp(self):
        assert Link(a=_ep("a"), b=_ep("b")).protocol == "tcp"

    def test_provenance_defaults_declared(self):
        assert Link(a=_ep("a"), b=_ep("b")).provenance is Provenance.DECLARED

    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            Link(a=_ep("a"), b=_ep("b")).protocol = "udp"  # type: ignore[misc]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/link -q` — Expected: FAIL, `ModuleNotFoundError: otto.link`.

- [ ] **Step 3: Implement**

Create `src/otto/link/model.py`:

```python
"""Runtime ``Link`` model — the unified edge object across all provenances.

One type regardless of where the link came from, so the CLI, topology
derivation, and monitor GUI all speak the same object (foundation spec §6).
"""

import enum
import hashlib
from dataclasses import dataclass


class Provenance(enum.Enum):
    """Where a link came from."""

    IMPLICIT = "implicit"
    """Derived from a host's ``hop`` chain (the ssh/telnet management path)."""

    DECLARED = "declared"
    """Declared in ``lab.json``'s ``links`` section (a data-plane route)."""

    DYNAMIC = "dynamic"
    """Observed live: an otto-created tunnel discovered on the hosts."""


@dataclass(frozen=True, slots=True)
class LinkEndpoint:
    """One end of a link: a host, optionally pinned to a named interface."""

    host: str
    """Host id (see ``make_host_id``)."""

    interface: str | None = None
    """Netdev name (a key in the host's ``interfaces`` map); ``None`` = the
    management ``ip`` / the host's sole interface."""

    ip: str = ""
    """Resolved address of this end (empty when unresolvable, e.g. a sentinel
    parsed without lab context)."""

    port: int | None = None
    """Bound port on this end — dynamic links only (sub-project #2)."""


def _endpoint_key(e: LinkEndpoint) -> tuple[str, str]:
    return (e.host, e.interface or "")


def make_link_id(a: LinkEndpoint, b: LinkEndpoint, protocol: str) -> str:
    """Deterministic id for the *route* ``a <-> b`` over *protocol*.

    STABILITY CONTRACT — changing this algorithm invalidates every live
    tunnel's sentinel and every recorded id across otto versions:

    - endpoints are sorted by ``(host, interface or "")`` so a<->b == b<->a;
    - **ports and ips are excluded** — the id names the route, so a dynamic
      tunnel over a declared route reconciles to the same id;
    - format: ``"lnk-"`` + first 12 hex chars of sha256 over
      ``"{lo.host}|{lo.interface}|{hi.host}|{hi.interface}|{protocol}"``.
    """
    lo, hi = sorted((a, b), key=_endpoint_key)
    canon = f"{lo.host}|{lo.interface or ''}|{hi.host}|{hi.interface or ''}|{protocol}"
    return "lnk-" + hashlib.sha256(canon.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Link:
    """An edge between two endpoints, from any provenance."""

    a: LinkEndpoint
    b: LinkEndpoint
    protocol: str = "tcp"
    provenance: Provenance = Provenance.DECLARED
    id: str = ""
    """Deterministic route id (``make_link_id``); auto-computed when empty."""
    name: str | None = None
    """Optional friendly handle from the lab data."""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", make_link_id(self.a, self.b, self.protocol))
```

Create `src/otto/link/__init__.py`:

```python
"""The link subsystem: the unified ``Link`` edge model and its derivations.

Foundation (sub-project #1): model, static derivation, sentinel codec, and
the discovery *contract*. Live tunnel creation/discovery arrives with the
``otto link`` CLI (sub-project #2).
"""

from .model import Link, LinkEndpoint, Provenance, make_link_id

__all__ = ["Link", "LinkEndpoint", "Provenance", "make_link_id"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link -q` — Expected: PASS. Then `uv run nox -s typecheck`.

- [ ] **Step 5: Commit**

```bash
git add src/otto/link tests/unit/link && git commit -m "feat(link): runtime Link/LinkEndpoint/Provenance with deterministic route ids

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 4: Hard cutover — `hosts.json` → `lab.json` everywhere

Every reader, writer, fixture, template, and test flips **in this one task / one commit** (spec §3, §13 atomicity). The file becomes `{"hosts": [...], "links": [...]}`; top-level `_`-prefixed keys are allowed as comments; unknown sections fail loud but the section registry makes future additions (`elements`, `sources`) one-line changes.

**Files:**
- Modify: `src/otto/storage/json_repository.py` (whole file surface: `:1,20,24-31,41-60,92-161`)
- Modify: `src/otto/configmodule/completion_cache.py` (`:106,139,286-291,790-822,825-907,911,945`)
- Modify: `src/otto/cli/init.py` (`:26,40-104,236-237,291,341,354,456`)
- Modify (strings/docstrings only): `src/otto/host/os_profile.py:11,47,95,111`, `src/otto/models/settings.py:122`, `src/otto/cli/host.py:57`, `src/otto/cli/builtin_commands.py:65`, `src/otto/configmodule/__init__.py:71`, `src/otto/cli/docker.py:69`, `src/otto/examples/lab_repository.py:45`, `src/otto/cli/schema.py:8,22,31`, `src/otto/cli/cov.py:15`, `src/otto/docker/compose.py:112`, `src/otto/cli/main.py:138`, `src/otto/models/jsonschema.py` docstrings (schema content changes are Task 8)
- Rename: `tests/_fixtures/lab_data/tech1/hosts.json` → `lab.json`, `tests/_fixtures/lab_data/tech2/hosts.json` → `lab.json` (+ wrap in object)
- Test: every test file matching `grep -rln "hosts.json" tests/` (~39 files; `tests/unit/storage/test_json_repository.py`, `tests/unit/configmodule/test_load_lab.py`, `test_completion_*.py`, `tests/unit/cli/test_init_scaffold.py`, `test_init_validate.py`, `tests/test_lab_data_hops.py` are the substantive ones)

**Interfaces:**
- Consumes: nothing new.
- Produces: `LAB_FILENAME = "lab.json"` (in both `json_repository` and `completion_cache`); `JsonFileLabRepository._load_lab_file(path) -> dict[str, list[dict]]` returning `{"hosts": [...], "links": [...]}`; `_read_lab_hosts(lab_file: Path) -> list[dict]` helper in `completion_cache`; init scaffolds `lab.json`. Task 5 extends `load_lab` to consume the `links` section this task starts aggregating.

- [ ] **Step 1: Write the failing loader tests first**

In `tests/unit/storage/test_json_repository.py`, update the file-shape tests (they write `hosts.json` arrays to `tmp_path`) to the new contract, and add:

```python
def _write_lab(tmp_path, hosts=(), links=(), name="lab.json"):
    payload = {"hosts": list(hosts), "links": list(links)}
    (tmp_path / name).write_text(json.dumps(payload))


class TestLabFileShape:
    def test_array_top_level_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps([{"ip": "192.0.2.1"}]))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="JSON object"):
            repo.load_lab("veggies")

    def test_unknown_section_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({"hosts": [], "routes": []}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="unknown section"):
            repo.load_lab("veggies")

    def test_top_level_comment_keys_allowed(self, tmp_path):
        _write_lab(tmp_path, hosts=[HOST_ENTRY])  # HOST_ENTRY: module's existing example dict
        payload = json.loads((tmp_path / "lab.json").read_text())
        payload["_comment"] = "a note"
        (tmp_path / "lab.json").write_text(json.dumps(payload))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        assert repo.load_lab("veggies").hosts  # loads fine

    def test_missing_sections_default_empty(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabNotFoundError):  # no hosts -> lab not found
            repo.load_lab("veggies")

    def test_hosts_json_is_not_read(self, tmp_path):
        """Hard cutover: a legacy hosts.json is invisible."""
        (tmp_path / "hosts.json").write_text(json.dumps([HOST_ENTRY]))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabNotFoundError, match="lab.json"):
            repo.load_lab("veggies")
```

(Adapt `HOST_ENTRY` and lab name to the module's existing fixtures.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/storage/test_json_repository.py -q` — Expected: FAIL (loader still reads arrays from `hosts.json`).

- [ ] **Step 3: Cut over `json_repository.py`**

- Module docstring + class docstring: `hosts.json` → `lab.json`, describe the object form.
- `HOSTS_FILENAME = "hosts.json"` → `LAB_FILENAME = "lab.json"`.
- Rename `_find_hosts_files` → `_find_lab_files` (same logic, new constant/messages).
- Replace `_load_json_hosts` with:

```python
_LAB_SECTIONS = frozenset({"hosts", "links"})


def _load_lab_file(self, lab_file: Path) -> dict[str, list[dict[str, Any]]]:
    """Load one ``lab.json``: an object with ``hosts`` / ``links`` array sections.

    Top-level ``_``-prefixed keys are comment space (same idiom as host
    entries). Unknown sections fail loud; adding a future section (e.g.
    ``elements``) means extending ``_LAB_SECTIONS`` and handling it here.
    """
    try:
        with lab_file.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise LabRepositoryError(f"Lab file '{lab_file}' contains malformed JSON: {e}") from e

    if not isinstance(data, dict):
        raise LabRepositoryError(
            f"Lab file '{lab_file}' must contain a JSON object with "
            f"'hosts'/'links' sections, got {type(data).__name__}"
        )
    unknown = {k for k in data if not k.startswith("_")} - _LAB_SECTIONS
    if unknown:
        raise LabRepositoryError(
            f"Lab file '{lab_file}' has unknown section(s) {sorted(unknown)}; "
            f"known sections: {sorted(_LAB_SECTIONS)}"
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for section in _LAB_SECTIONS:
        value = data.get(section, [])
        if not isinstance(value, list):
            raise LabRepositoryError(
                f"Lab file '{lab_file}': section '{section}' must be a JSON array, "
                f"got {type(value).__name__}"
            )
        out[section] = value
    return out
```

- `load_lab`: aggregate per section —

```python
        all_hosts_data: list[dict[str, Any]] = []
        all_links_data: list[dict[str, Any]] = []
        for lab_file in lab_files:
            sections = self._load_lab_file(lab_file)
            all_hosts_data.extend(sections["hosts"])
            all_links_data.extend(sections["links"])
```

(`all_links_data` is unused until Task 5 — add it now so this task owns the merge shape.) `list_labs` iterates `self._load_lab_file(f)["hosts"]`.

- [ ] **Step 4: Cut over `completion_cache.py`**

- `HOSTS_FILENAME = "hosts.json"` (`:139`) → `LAB_FILENAME = "lab.json"`; update the `:106` docstring.
- Add one raw-reading helper (the fast path must stay import-light — stdlib only):

```python
def _read_lab_hosts(lab_file: Path) -> list[dict[str, Any]]:
    """Best-effort read of a lab.json's ``hosts`` array ([] on any problem).

    Completion must never crash on bad user data, so malformed shapes are
    silently empty here (the real loader raises with full diagnostics).
    """
    try:
        data = json.loads(lab_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    hosts = data.get("hosts", [])
    return hosts if isinstance(hosts, list) else []
```

- In `collect_docker_capable_host_ids` (`:802-810`) and `collect_host_ids` (`:862-870`): replace the `hosts_file = lab_path / HOSTS_FILENAME` + `json.loads` + `isinstance(data, list)` blocks with `for host_data in _read_lab_hosts(lab_path / LAB_FILENAME):` (keep the per-entry guards). Same for the lab-name enumerator near `:911`.
- `_hash_file(h, lab_path / HOSTS_FILENAME)` (`:291`) → `LAB_FILENAME`.
- Bump `SCHEMA_VERSION = 10` (`:137`) → `11` (cache entries hash different files now).

- [ ] **Step 5: Cut over `otto init`**

In `src/otto/cli/init.py`:

- Rename `HOSTS_JSON_ENTRY` → `EXAMPLE_HOST_ENTRY` (content unchanged except the `_comment` first sentence: "Example host — replace these values. Full host schema: docs/guide/host-database.md or `otto schema export`.").
- Add below it:

```python
LAB_JSON_TEMPLATE: dict[str, Any] = {
    "_comment": (
        "otto lab database: 'hosts' lists every lab host; 'links' declares "
        "data-plane routes between them (see docs/guide/lab-config.md). "
        "Keys starting with _ are comments."
    ),
    "hosts": [EXAMPLE_HOST_ENTRY],
    "links": [],
}
```

- `_scaffold_lab` (`:236-237`): `hosts = lab_dir / "lab.json"` and `hosts.write_text(json.dumps(LAB_JSON_TEMPLATE, indent=4) + "\n")` (rename the local to `lab_file`).
- `_detect_lab` (`:291`): glob `"lab.json"`.
- `_validate_lab` (`:341,354`): read `lab_dir / "lab.json"`, iterate `data["hosts"]` through `validate_host_dict` (guard the object shape first; a non-dict top level is one clear error). Validate each `data["links"]` entry with `LinkSpec.model_validate` (import `from ..models.link import LinkSpec`) — structural only; cross-references are checked at load time.
- `SETTINGS_TEMPLATE` (`:26`): comment → `directories searched for lab.json`.
- `LAB_README_TEMPLATE` (`:56-104`): update to `lab.json`, describe the two sections, note interfaces are keyed by netdev name, and add a short `links` field list (endpoints/protocol/name — mirror the spec §5 wording). Update the `--lab` help at `:456`.

- [ ] **Step 6: Sweep the remaining src strings**

Run: `grep -rn "hosts\.json\|hosts_json\|HOSTS_JSON\|HOSTS_FILENAME" src/`
Fix every hit (they are docstrings/comments/messages listed in **Files** above; `compose.py:112`'s user-facing error becomes "Mark it in lab.json…"). Expected after: zero hits in `src/`.

- [ ] **Step 7: Convert fixtures + test sweep**

```bash
git mv tests/_fixtures/lab_data/tech1/hosts.json tests/_fixtures/lab_data/tech1/lab.json
git mv tests/_fixtures/lab_data/tech2/hosts.json tests/_fixtures/lab_data/tech2/lab.json
```

Edit both: wrap the existing array as `{"hosts": <existing array>}` (preserve formatting; add `"links": []` only to tech1 — Task 5 populates it). Then sweep tests:

Run: `grep -rln "hosts.json" tests/`
For each file apply the two mechanical patterns: (a) path `"hosts.json"` → `"lab.json"`; (b) payload `json.dumps([<entries>])` → `json.dumps({"hosts": [<entries>]})` (and dict-literal equivalents). Tests that *assert* on the old shape (loader error-message tests, init scaffold assertions, completion cache hashing) get their expectations updated to the Step 1/3/4/5 behavior instead. Expected after: `grep -rln "hosts.json" tests/` → only matches inside test strings that deliberately exercise the legacy-rejection path (Step 1's `test_hosts_json_is_not_read`).

- [ ] **Step 8: Run the full gate**

Run: `make coverage` then `uv run nox -s typecheck`
Expected: green. Pay attention to `tests/test_lab_data_hops.py`, `tests/unit/cli/test_init_*.py`, `tests/unit/configmodule/test_completion_*.py`, and e2e fixture repos (`tests/repo1/`, `tests/repo_e2e/`, `tests/repo_broken/`, `tests/repo3/` — if their lab dirs carry `hosts.json`, convert them identically).

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "feat(lab)!: hard cutover hosts.json -> lab.json object with hosts/links sections

BREAKING CHANGE: the lab database file is now lab.json with the shape
{\"hosts\": [...], \"links\": [...]}; bare hosts.json arrays are no longer read.

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 5: Declared-link resolution, implicit derivation, `Lab.links` + `static_links()`

The static link layer (spec §5 membership/resolution, §6 accessor, §7 implicit derivation).

**Files:**
- Create: `src/otto/link/derive.py`
- Modify: `src/otto/configmodule/lab.py` (`Lab` fields `:28-35`, `add_host` untouched, `__add__` `:60-74`), `src/otto/storage/json_repository.py` (`load_lab` `:36-90`)
- Modify: `tests/_fixtures/lab_data/tech1/lab.json` (add a links example)
- Test: create `tests/unit/link/test_derive.py`; extend `tests/unit/configmodule/test_lab.py`, `tests/unit/configmodule/test_load_lab.py`, `tests/unit/storage/test_json_repository.py`

**Interfaces:**
- Consumes: `Link`/`LinkEndpoint`/`Provenance`/`make_link_id` (Task 3), `LinkSpec` (Task 2), `make_host_id` (`otto.host.remote_host:52`), `BUILTIN_LOCAL_HOST_ID` (`otto.host.builtin_hosts`).
- Produces:
  - `HostAddressing` — `dataclass(frozen=True)` with `ip: str`, `interfaces: dict[str, str]` (iface name → ip): the *minimal* addressing view of a host, buildable from either a raw host dict or a runtime host.
  - `addressing_from_dict(host_data: dict) -> tuple[str, HostAddressing]` — returns `(host_id, addressing)`; applies the interface string-shorthand.
  - `resolve_declared_links(link_data: list[dict], hosts: Mapping[str, HostAddressing], *, source: str) -> list[Link]` — validates each entry via `LinkSpec`, resolves endpoints, raises `ValueError` with the entry index + reason on unknown host / unknown interface / ambiguous interface.
  - `implicit_links(hosts: Mapping[str, Host]) -> list[Link]` — hop edges + root edges to `local`.
  - `Lab.links: list[Link]` (declared only) and `Lab.static_links() -> list[Link]` (implicit ∪ declared, declared wins on id collision). `Lab.__add__` unions links de-duplicated by id.

- [ ] **Step 1: Write the failing derivation tests**

Create `tests/unit/link/test_derive.py`:

```python
"""Static link derivation: declared resolution + implicit hop edges."""

import pytest

from otto.link import Provenance
from otto.link.derive import HostAddressing, implicit_links, resolve_declared_links

CARROT = HostAddressing(ip="10.10.200.11", interfaces={"eth1": "192.168.1.11"})
TOMATO = HostAddressing(ip="10.10.200.12", interfaces={"eth1": "192.168.1.12", "eth2": "192.168.2.12"})
BARE = HostAddressing(ip="10.10.200.13", interfaces={})

HOSTS = {"carrot_seed": CARROT, "tomato_seed": TOMATO, "basil_seed": BARE}


def _entry(**overrides) -> dict:
    base = {
        "endpoints": [
            {"host": "carrot_seed", "interface": "eth1"},
            {"host": "tomato_seed", "interface": "eth1"},
        ],
        "protocol": "udp",
    }
    return {**base, **overrides}


class TestResolveDeclaredLinks:
    def test_resolves_named_interfaces(self):
        (link,) = resolve_declared_links([_entry()], HOSTS, source="lab.json")
        assert link.provenance is Provenance.DECLARED
        ips = {link.a.ip, link.b.ip}
        assert ips == {"192.168.1.11", "192.168.1.12"}

    def test_omitted_interface_single_iface_host_assumed(self):
        entry = _entry(endpoints=[{"host": "carrot_seed"}, {"host": "basil_seed"}])
        (link,) = resolve_declared_links([entry], HOSTS, source="lab.json")
        by_host = {e.host: e for e in (link.a, link.b)}
        assert by_host["carrot_seed"].interface == "eth1"          # sole iface assumed
        assert by_host["carrot_seed"].ip == "192.168.1.11"
        assert by_host["basil_seed"].interface is None             # no ifaces -> mgmt ip
        assert by_host["basil_seed"].ip == "10.10.200.13"

    def test_omitted_interface_multi_iface_host_errors(self):
        entry = _entry(endpoints=[{"host": "tomato_seed"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match="ambiguous interface.*eth1.*eth2"):
            resolve_declared_links([entry], HOSTS, source="lab.json")

    def test_unknown_host_errors(self):
        entry = _entry(endpoints=[{"host": "nope"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match="unknown host 'nope'"):
            resolve_declared_links([entry], HOSTS, source="lab.json")

    def test_unknown_interface_errors(self):
        entry = _entry(endpoints=[{"host": "carrot_seed", "interface": "eth9"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match="no interface 'eth9'"):
            resolve_declared_links([entry], HOSTS, source="lab.json")

    def test_error_names_source_and_index(self):
        entry = _entry(endpoints=[{"host": "nope"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match=r"lab\.json.*index 0"):
            resolve_declared_links([entry], HOSTS, source="lab.json")
```

For `implicit_links`, use lightweight stand-in hosts (the function must be duck-typed — it reads only `id`, `ip`, `hop`, `term`):

```python
class _FakeHost:
    def __init__(self, id: str, ip: str = "203.0.113.1", hop: str | None = None, term: str = "ssh"):
        self.id, self.ip, self.hop, self.term = id, ip, hop, term


class TestImplicitLinks:
    def test_hop_edge_per_hopped_host(self):
        hosts = {
            "local": _FakeHost("local", ip="127.0.0.1"),
            "gw": _FakeHost("gw"),
            "sprout1": _FakeHost("sprout1", hop="gw", term="telnet"),
        }
        links = implicit_links(hosts)
        by_pair = {frozenset((link.a.host, link.b.host)): link for link in links}
        assert frozenset(("gw", "sprout1")) in by_pair
        assert by_pair[frozenset(("gw", "sprout1"))].protocol == "telnet"
        assert all(link.provenance is Provenance.IMPLICIT for link in links)

    def test_hopless_host_attaches_to_local_root(self):
        hosts = {"local": _FakeHost("local", ip="127.0.0.1"), "gw": _FakeHost("gw")}
        links = implicit_links(hosts)
        assert {frozenset((link.a.host, link.b.host)) for link in links} == {frozenset(("local", "gw"))}

    def test_local_itself_emits_no_edge(self):
        assert implicit_links({"local": _FakeHost("local")}) == []

    def test_missing_hop_target_still_edges_with_empty_ip(self):
        hosts = {"sprout1": _FakeHost("sprout1", hop="ghost")}
        (edge,) = [link for link in implicit_links(hosts) if "ghost" in (link.a.host, link.b.host)]
        ghost = edge.a if edge.a.host == "ghost" else edge.b
        assert ghost.ip == ""
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/link/test_derive.py -q` — Expected: FAIL, no `otto.link.derive`.

- [ ] **Step 3: Implement `src/otto/link/derive.py`**

```python
"""Pure derivations of the static link layer (implicit hop edges + declared links).

No I/O and no live host access: callers hand in host dicts / host objects,
these functions hand back :class:`~otto.link.model.Link`s. That keeps every
rule here unit-testable without a lab.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from ..host.remote_host import make_host_id
from ..models.link import LinkSpec
from .model import Link, LinkEndpoint, Provenance


@dataclass(frozen=True)
class HostAddressing:
    """The minimal addressing view of a host a link endpoint needs."""

    ip: str
    interfaces: dict[str, str] = field(default_factory=dict)
    """Interface name -> address (values already flattened to strings)."""


def addressing_from_dict(host_data: dict[str, Any]) -> tuple[str, HostAddressing]:
    """``(host_id, HostAddressing)`` from a raw lab.json host dict.

    Applies the interface string-shorthand (a bare string value is the ip),
    mirroring ``InterfaceSpec``'s coercion — this reads *raw* dicts so
    cross-lab (dangling) endpoints resolve without constructing hosts.
    """
    host_id = make_host_id(
        host_data["element"],
        host_data.get("element_id"),
        host_data.get("board"),
        host_data.get("slot"),
    )
    raw = host_data.get("interfaces", {})
    interfaces = {
        name: (entry if isinstance(entry, str) else entry.get("ip", ""))
        for name, entry in raw.items()
        if not name.startswith("_")
    }
    return host_id, HostAddressing(ip=host_data.get("ip", ""), interfaces=interfaces)


def _resolve_endpoint(host_id: str, interface: str | None, hosts: Mapping[str, HostAddressing]) -> LinkEndpoint:
    addressing = hosts.get(host_id)
    if addressing is None:
        raise ValueError(f"unknown host {host_id!r} (no such host in any lab file)")
    if interface is not None:
        if interface not in addressing.interfaces:
            known = ", ".join(sorted(addressing.interfaces)) or "<none defined>"
            raise ValueError(f"host {host_id!r} has no interface {interface!r} (known: {known})")
        return LinkEndpoint(host=host_id, interface=interface, ip=addressing.interfaces[interface])
    if len(addressing.interfaces) > 1:
        known = ", ".join(sorted(addressing.interfaces))
        raise ValueError(
            f"host {host_id!r}: ambiguous interface — it defines more than one; specify one of: {known}"
        )
    if len(addressing.interfaces) == 1:
        ((name, ip),) = addressing.interfaces.items()
        return LinkEndpoint(host=host_id, interface=name, ip=ip)
    return LinkEndpoint(host=host_id, interface=None, ip=addressing.ip)


def resolve_declared_links(
    link_data: list[dict[str, Any]],
    hosts: Mapping[str, HostAddressing],
    *,
    source: str,
) -> list[Link]:
    """Validate + resolve raw ``links`` entries into DECLARED ``Link``s.

    *source* names the origin (a file path or "lab.json") for error messages.
    """
    links: list[Link] = []
    for idx, entry in enumerate(link_data):
        try:
            spec = LinkSpec.model_validate(entry)
            a = _resolve_endpoint(spec.endpoints[0].host, spec.endpoints[0].interface, hosts)
            b = _resolve_endpoint(spec.endpoints[1].host, spec.endpoints[1].interface, hosts)
        except ValueError as e:  # noqa: PERF203 — per-item resilience, matches the host loop
            raise ValueError(f"Invalid link in {source} at index {idx}: {e}") from e
        links.append(
            Link(a=a, b=b, protocol=spec.protocol, provenance=Provenance.DECLARED, name=spec.name)
        )
    return links


def implicit_links(hosts: Mapping[str, Any]) -> list[Link]:
    """IMPLICIT edges from ``hop`` chains, rooted at the built-in ``local`` host.

    Duck-typed on purpose (reads ``id``/``ip``/``hop``/``term``): callers pass
    ``lab.hosts``, tests pass stand-ins. A host with a ``hop`` edges to its hop
    host; a hop-less host edges to ``local`` (the "you are here" root — the
    monitor's reachability cascade needs the full chain back to local).
    Protocol = the child's management term (ssh/telnet).
    """
    links: list[Link] = []
    for host in hosts.values():
        host_id = getattr(host, "id", "")
        if host_id == BUILTIN_LOCAL_HOST_ID:
            continue
        hop_id = getattr(host, "hop", None) or BUILTIN_LOCAL_HOST_ID
        parent = hosts.get(hop_id)
        links.append(
            Link(
                a=LinkEndpoint(host=hop_id, ip=getattr(parent, "ip", "") if parent is not None else ""),
                b=LinkEndpoint(host=host_id, ip=getattr(host, "ip", "")),
                protocol=getattr(host, "term", "ssh") or "ssh",
                provenance=Provenance.IMPLICIT,
            )
        )
    return links
```

- [ ] **Step 4: Wire `Lab` and the repository**

In `src/otto/configmodule/lab.py` — `Lab` gains (after `hosts`):

```python
    links: "list[Link]" = field(default_factory=list)
    """Declared links loaded from lab data (implicit links are derived, not stored)."""
```

(`Link` under `TYPE_CHECKING` import from `..link.model` — keeps `otto.link` off the import-hot path.) Add the accessor:

```python
    def static_links(self) -> "list[Link]":
        """The static link layer: implicit hop edges ∪ declared links.

        Free (no I/O). Declared wins over implicit on route-id collision.
        Dynamic links are NOT here — see ``otto.link.discovery`` (async, costed).
        """
        from ..link.derive import implicit_links  # lazy: keep Lab import-light

        merged = {link.id: link for link in implicit_links(self.hosts)}
        for link in self.links:
            merged[link.id] = link
        return list(merged.values())
```

In `__add__` (after `self.hosts.update(...)`):

```python
        by_id = {link.id: link for link in self.links}
        by_id.update({link.id: link for link in other.links})
        self.links = list(by_id.values())
```

In `json_repository.load_lab`, after the host-construction loop (`:78-87`), add:

```python
        from ..link.derive import addressing_from_dict, resolve_declared_links

        # Guard: all_hosts_data spans ALL lab files, including entries never
        # validated (they belong to other labs) — skip shapes that can't
        # produce an id rather than crash link resolution on someone else's typo.
        addressing = dict(
            addressing_from_dict(h)
            for h in all_hosts_data
            if isinstance(h, dict) and isinstance(h.get("element"), str)
        )
        loaded_ids = set(lab.hosts)
        try:
            declared = resolve_declared_links(all_links_data, addressing, source=LAB_FILENAME)
        except ValueError as e:
            raise LabRepositoryError(str(e)) from e
        lab.links = [
            link for link in declared if link.a.host in loaded_ids or link.b.host in loaded_ids
        ]
```

Membership = derived (≥1 endpoint in the loaded lab — spec §5: union across labs, cross-lab links surface in both, dangling endpoints stay resolved from the raw dicts).

- [ ] **Step 5: Loader-level tests**

Extend `tests/unit/storage/test_json_repository.py` (using Task 4's `_write_lab`) with: a declared link between two in-lab hosts loads into `lab.links` with resolved ips; a cross-lab link (endpoint B's host dict tagged `"labs": ["other"]`) still loads when lab A is requested (dangling endpoint resolved); a link whose endpoints are both out-of-lab does NOT appear; an unknown-host link raises `LabRepositoryError` naming index + source. Extend `tests/unit/configmodule/test_lab.py` with: `static_links()` contains implicit + declared, declared-wins-on-collision (declare a link that duplicates a hop edge's route — same hosts, no interfaces, protocol matching the term — and assert the merged entry has `Provenance.DECLARED`), and `lab_a + lab_b` dedupes a shared link by id. Add a `"links"` example to `tests/_fixtures/lab_data/tech1/lab.json` connecting two existing veggies hosts over `"protocol": "udp"` (give both hosts an `interfaces` entry, e.g. `"eth1": "192.168.1.11"` / `.12`) and assert it round-trips in `tests/unit/configmodule/test_load_lab.py`.

- [ ] **Step 6: Run gates**

Run: `uv run pytest tests/unit/link tests/unit/storage tests/unit/configmodule -q`, then `make coverage`, then `uv run nox -s typecheck`. The import-budget guard (`tests/unit/import_budget/`) must stay green — `otto.link` imports are lazy.

- [ ] **Step 7: Commit**

```bash
git add -A src/otto tests && git commit -m "feat(link): declared-link resolution, implicit hop derivation, Lab.static_links()

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 6: Sentinel codec — `encode_sentinel` / `parse_sentinel` / `parse_discovery`

The dynamic-link wire contract as pure functions (spec §8). Live spawning/gathering is #2.

**Files:**
- Create: `src/otto/link/sentinel.py`
- Test: create `tests/unit/link/test_sentinel.py`

**Interfaces:**
- Consumes: `Link`/`LinkEndpoint`/`Provenance` (Task 3), stdlib `urllib.parse.quote/unquote`.
- Produces (**STABILITY CONTRACT** — #2's spawned processes carry this format for their lifetime):
  - Wire format: 10 colon-joined, percent-encoded segments — `otto-link:v1:<id>:<proto>:<a-host>:<a-iface>:<a-port>:<b-host>:<b-iface>:<b-port>`. Percent-encoding makes a literal `:` inside a segment safe (netdev alias names like `eth0:1`); `None` iface/port encode as empty segments. **No owner/username segment** (owner-agnostic by design). Version `v1`; parsers skip other versions (forward tolerance).
  - `encode_sentinel(link: Link) -> str` (ports read from the endpoints).
  - `parse_sentinel(token: str) -> Link | None` — `None` for non-otto/other-version/malformed tokens; endpoints carry host/iface/port, `ip=""` (no lab context at parse time), `provenance=DYNAMIC`, id taken verbatim from the wire.
  - `parse_discovery(ps_output: str) -> list[Link]` — feeds on `pgrep -af`-style lines (`<pid> <argv...>`), extracts the first `otto-link:` token per line, groups multi-process links by id (first non-None port per end wins).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/link/test_sentinel.py`:

```python
"""Sentinel wire-format codec: encode <-> parse round-trips and discovery parsing."""

import getpass

from otto.link import Link, LinkEndpoint, Provenance
from otto.link.sentinel import encode_sentinel, parse_discovery, parse_sentinel


def _dynamic_link(a_port=5000, b_port=5001, a_iface="eth1", proto="udp") -> Link:
    return Link(
        a=LinkEndpoint(host="carrot_seed", interface=a_iface, port=a_port),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", port=b_port),
        protocol=proto,
        provenance=Provenance.DYNAMIC,
    )


class TestRoundTrip:
    def test_encode_parse_round_trip(self):
        link = _dynamic_link()
        parsed = parse_sentinel(encode_sentinel(link))
        assert parsed is not None
        assert parsed.id == link.id
        assert parsed.protocol == "udp"
        assert parsed.provenance is Provenance.DYNAMIC
        assert {(e.host, e.interface, e.port) for e in (parsed.a, parsed.b)} == {
            ("carrot_seed", "eth1", 5000),
            ("tomato_seed", "eth1", 5001),
        }

    def test_colon_in_interface_name_survives(self):
        link = _dynamic_link(a_iface="eth0:1")
        parsed = parse_sentinel(encode_sentinel(link))
        assert parsed is not None and "eth0:1" in {parsed.a.interface, parsed.b.interface}

    def test_none_iface_and_port_round_trip(self):
        link = Link(
            a=LinkEndpoint(host="a"), b=LinkEndpoint(host="b"), provenance=Provenance.DYNAMIC
        )
        parsed = parse_sentinel(encode_sentinel(link))
        assert parsed is not None
        assert parsed.a.interface is None and parsed.a.port is None

    def test_no_username_in_wire_format(self):
        assert getpass.getuser() not in encode_sentinel(_dynamic_link())


class TestParseRejections:
    def test_non_otto_token_none(self):
        assert parse_sentinel("socat:UDP4-LISTEN:5000") is None

    def test_future_version_none(self):
        good = encode_sentinel(_dynamic_link())
        assert parse_sentinel(good.replace(":v1:", ":v2:", 1)) is None

    def test_malformed_none(self):
        assert parse_sentinel("otto-link:v1:only-three") is None
        assert parse_sentinel("") is None


class TestParseDiscovery:
    def test_groups_processes_by_id(self):
        link = _dynamic_link()
        token = encode_sentinel(link)
        ps = (
            f"1201 {token}\n"
            f"1202 {token}\n"                                  # second process, same link
            "1300 socat UDP4-LISTEN:9999,fork TCP4:10.0.0.1:9999\n"  # non-otto: excluded
            "1400 /usr/sbin/sshd -D\n"
        )
        links = parse_discovery(ps)
        assert len(links) == 1 and links[0].id == link.id

    def test_distinct_links_kept_separate(self):
        one, two = _dynamic_link(), _dynamic_link(proto="tcp")
        ps = f"1 {encode_sentinel(one)}\n2 {encode_sentinel(two)}\n"
        assert {l.id for l in parse_discovery(ps)} == {one.id, two.id}

    def test_port_backfill_across_processes(self):
        with_ports, port_less = _dynamic_link(), _dynamic_link(a_port=None, b_port=None)
        ps = f"1 {encode_sentinel(port_less)}\n2 {encode_sentinel(with_ports)}\n"
        (merged,) = parse_discovery(ps)
        assert {merged.a.port, merged.b.port} == {5000, 5001}

    def test_empty_and_garbage_input(self):
        assert parse_discovery("") == []
        assert parse_discovery("not a ps line at all\n\n") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/link/test_sentinel.py -q` — Expected: FAIL, no `otto.link.sentinel`.

- [ ] **Step 3: Implement `src/otto/link/sentinel.py`**

```python
"""Sentinel wire format: the argv marker every otto tunnel process carries.

The running processes on the hosts ARE the dynamic-link record — zero
persisted state. Each process is launched (sub-project #2) with an argv[0]
of the form::

    otto-link:v1:<id>:<proto>:<a-host>:<a-iface>:<a-port>:<b-host>:<b-iface>:<b-port>

so ``pgrep -af '^otto-link:'`` on a host returns exactly otto's tunnels, and
parsing the marker reconstructs the full ``Link`` with no ledger lookup.

STABILITY CONTRACT — live tunnels outlive otto processes, so a v1 marker
must parse forever; evolve the format only by adding a new version segment
and keeping v1 parsing intact:

- 10 colon-joined segments, each percent-encoded (a literal ``:`` inside a
  segment — e.g. the netdev alias ``eth0:1`` — survives);
- ``None`` interface/port encode as empty segments;
- deliberately **no username segment**: markers are owner-agnostic so any
  user can discover and reap any otto tunnel;
- unknown versions parse to ``None`` (skipped), never an error.
"""

from urllib.parse import quote, unquote

from .model import Link, LinkEndpoint, Provenance

SENTINEL_PREFIX = "otto-link"
SENTINEL_VERSION = "v1"
_SEGMENT_COUNT = 10


def _enc(value: str | int | None) -> str:
    return quote(str(value), safe="") if value is not None else ""


def encode_sentinel(link: Link) -> str:
    """The wire token for *link* (ports read from the endpoints)."""
    segments = (
        SENTINEL_PREFIX,
        SENTINEL_VERSION,
        _enc(link.id),
        _enc(link.protocol),
        _enc(link.a.host),
        _enc(link.a.interface),
        _enc(link.a.port),
        _enc(link.b.host),
        _enc(link.b.interface),
        _enc(link.b.port),
    )
    return ":".join(segments)


def parse_sentinel(token: str) -> Link | None:
    """Parse one wire token; ``None`` for non-otto / other-version / malformed input."""
    parts = token.split(":")
    if len(parts) != _SEGMENT_COUNT or parts[0] != SENTINEL_PREFIX or parts[1] != SENTINEL_VERSION:
        return None
    link_id, proto = unquote(parts[2]), unquote(parts[3])
    if not link_id or not proto:
        return None

    def endpoint(host_seg: str, iface_seg: str, port_seg: str) -> LinkEndpoint | None:
        host = unquote(host_seg)
        if not host:
            return None
        port: int | None = None
        if port_seg:
            try:
                port = int(unquote(port_seg))
            except ValueError:
                return None
        return LinkEndpoint(host=host, interface=unquote(iface_seg) or None, port=port)

    a = endpoint(parts[4], parts[5], parts[6])
    b = endpoint(parts[7], parts[8], parts[9])
    if a is None or b is None:
        return None
    return Link(a=a, b=b, protocol=proto, provenance=Provenance.DYNAMIC, id=link_id)


def parse_discovery(ps_output: str) -> list[Link]:
    """Reconstruct links from ``pgrep -af``-style output (one process per line).

    One link is usually several tagged processes (a socat per end, a forward
    on the hop) sharing the same id: group by id, first non-``None`` port per
    end wins. Non-otto lines are ignored — discovery must never misattribute
    a stranger's socat.
    """
    by_id: dict[str, Link] = {}
    for line in ps_output.splitlines():
        token = next((w for w in line.split() if w.startswith(f"{SENTINEL_PREFIX}:")), None)
        if token is None:
            continue
        parsed = parse_sentinel(token)
        if parsed is None:
            continue
        seen = by_id.get(parsed.id)
        if seen is None:
            by_id[parsed.id] = parsed
            continue
        # Merge: keep the first non-None port per end.
        merged_a = seen.a if seen.a.port is not None else parsed.a
        merged_b = seen.b if seen.b.port is not None else parsed.b
        by_id[parsed.id] = Link(
            a=merged_a, b=merged_b, protocol=seen.protocol,
            provenance=Provenance.DYNAMIC, id=seen.id, name=seen.name,
        )
    return list(by_id.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link -q` — Expected: PASS. Then `uv run nox -s typecheck`.

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/sentinel.py tests/unit/link/test_sentinel.py && git commit -m "feat(link): versioned owner-agnostic sentinel codec + discovery parser

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 7: Discovery contract + `all_links` reconciliation

The async accessor pair (spec §6): signatures fixed now, live gathering lands in #2. Reconciliation logic is real and tested via injected fakes.

**Files:**
- Create: `src/otto/link/discovery.py`
- Modify: `src/otto/link/__init__.py` (re-exports)
- Test: create `tests/unit/link/test_discovery.py`

**Interfaces:**
- Consumes: `Lab.static_links()` (Task 5), `Link` (Task 3).
- Produces:
  - `async discover_dynamic_links(lab: Lab) -> list[Link]` — raises `NotImplementedError` (with a message naming sub-project #2) until the CLI work wires the live `asyncio.gather`.
  - `async all_links(lab: Lab, *, discover: DiscoverFn = discover_dynamic_links) -> list[Link]` — static ∪ dynamic merged by id, **dynamic wins** (observed reality is the higher-fidelity provenance). `DiscoverFn = Callable[["Lab"], Awaitable[list[Link]]]` — #2 replaces the default; tests inject fakes.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/link/test_discovery.py`:

```python
"""all_links reconciliation: static ∪ dynamic merged by route id."""

import pytest

from otto.configmodule.lab import Lab
from otto.link import Link, LinkEndpoint, Provenance
from otto.link.discovery import all_links, discover_dynamic_links


def _declared(proto="udp") -> Link:
    return Link(
        a=LinkEndpoint(host="carrot_seed", interface="eth1", ip="192.168.1.11"),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", ip="192.168.1.12"),
        protocol=proto,
        provenance=Provenance.DECLARED,
    )


def _lab_with(links: list[Link]) -> Lab:
    lab = Lab(name="t")
    lab.links = links
    return lab


async def test_default_discovery_not_implemented():
    with pytest.raises(NotImplementedError, match="sub-project #2"):
        await discover_dynamic_links(_lab_with([]))


async def test_all_links_unions_static_and_dynamic():
    declared = _declared()
    dynamic = Link(
        a=LinkEndpoint(host="basil_seed", port=5000),
        b=LinkEndpoint(host="carrot_seed", port=5000),
        provenance=Provenance.DYNAMIC,
    )

    async def fake(lab: Lab) -> list[Link]:
        return [dynamic]

    ids = {link.id for link in await all_links(_lab_with([declared]), discover=fake)}
    assert ids == {declared.id, dynamic.id}


async def test_dynamic_wins_on_same_route():
    declared = _declared()
    live = Link(  # same route -> same id; observed tunnel with ports
        a=LinkEndpoint(host="carrot_seed", interface="eth1", port=5000),
        b=LinkEndpoint(host="tomato_seed", interface="eth1", port=5001),
        protocol="udp",
        provenance=Provenance.DYNAMIC,
    )
    assert declared.id == live.id  # precondition: route ids reconcile

    async def fake(lab: Lab) -> list[Link]:
        return [live]

    (merged,) = [
        link
        for link in await all_links(_lab_with([declared]), discover=fake)
        if link.id == declared.id
    ]
    assert merged.provenance is Provenance.DYNAMIC and merged.a.port == 5000
```

(Async tests run under the repo's existing pytest asyncio configuration — match the `async def test_` style used in `tests/unit/host/`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/link/test_discovery.py -q` — Expected: FAIL, no `otto.link.discovery`.

- [ ] **Step 3: Implement `src/otto/link/discovery.py`**

```python
"""Dynamic-link discovery contract + the all-provenance accessor.

Cost-split by design (foundation spec §6): ``Lab.static_links()`` is free and
synchronous; everything here is async because the dynamic layer costs one
round-trip per lab host. The live ``asyncio.gather`` of ``pgrep`` across hosts
(feeding :func:`otto.link.sentinel.parse_discovery`) arrives with the
``otto link`` CLI — this module fixes the signatures consumers rely on.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .model import Link

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

DiscoverFn = Callable[["Lab"], Awaitable[list[Link]]]


async def discover_dynamic_links(lab: "Lab") -> list[Link]:
    """Discover live otto tunnels across the lab's Unix hosts.

    Contract only in the foundation: the live implementation (gather a
    ``pgrep -af '^otto-link:'`` across hosts, parse via
    ``sentinel.parse_discovery``, resolve endpoint ips against *lab*) ships
    with the ``otto link`` CLI (sub-project #2).
    """
    raise NotImplementedError(
        "dynamic-link discovery arrives with the otto link CLI (sub-project #2)"
    )


async def all_links(lab: "Lab", *, discover: DiscoverFn = discover_dynamic_links) -> list[Link]:
    """Every link across provenances, merged by route id.

    Static (implicit ∪ declared) plus dynamic; on a shared id the **dynamic**
    entry wins — an observed tunnel is higher-fidelity than the declaration
    it realizes. *discover* is injectable for tests and for #2's live layer.
    """
    merged = {link.id: link for link in lab.static_links()}
    for link in await discover(lab):
        merged[link.id] = link
    return list(merged.values())
```

Update `src/otto/link/__init__.py`:

```python
from .discovery import all_links, discover_dynamic_links
from .model import Link, LinkEndpoint, Provenance, make_link_id

__all__ = [
    "Link",
    "LinkEndpoint",
    "Provenance",
    "all_links",
    "discover_dynamic_links",
    "make_link_id",
]
```

- [ ] **Step 4: Run tests + gates**

Run: `uv run pytest tests/unit/link -q`, `make coverage`, `uv run nox -s typecheck` — Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/otto/link tests/unit/link && git commit -m "feat(link): async discovery contract and all_links reconciliation

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 8: JSON Schema export — `lab` + `link` documents

`otto schema export` must describe the new file (spec §3): the `hosts` array schema wraps into a `lab` object schema; `LinkSpec` gets its own document.

**Files:**
- Modify: `src/otto/models/jsonschema.py` (`_host_array_schema` `:110-138`, `build_schemas` `:141-170`, module docstring)
- Modify: `src/otto/cli/schema.py` (docstrings `:8,22,31` — mention lab.json)
- Test: `tests/unit/models/test_jsonschema.py`, `tests/unit/models/test_jsonschema_validation.py`

**Interfaces:**
- Consumes: `LinkSpec` (Task 2).
- Produces: `build_schemas()` emits stems `lab` (object: `hosts` array + `links` array, `additionalProperties: false` with `^_` pattern-property escape for comments) and `link` (the `LinkSpec` document); the `hosts` stem is **removed** (hard cutover — `make schema` consumers regenerate).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/models/test_jsonschema.py` add (mirroring its existing stem assertions):

```python
def test_lab_schema_emitted():
    docs = build_schemas(builtins_only=True)
    assert "hosts" not in docs  # hard cutover: array-only schema retired
    lab = docs["lab"]
    assert lab["type"] == "object"
    assert set(lab["properties"]) == {"hosts", "links"}
    assert lab["properties"]["hosts"]["type"] == "array"
    assert lab["properties"]["links"]["type"] == "array"
    assert lab["additionalProperties"] is False
    assert "^_" in lab.get("patternProperties", {})  # top-level comment keys


def test_link_schema_emitted():
    docs = build_schemas(builtins_only=True)
    link = docs["link"]
    assert link["title"] == "otto link"
    assert "endpoints" in link["properties"]
```

In `test_jsonschema_validation.py` (it validates example payloads against the generated schemas with a JSON-schema validator): add a case validating `{"hosts": [<existing valid host example>], "links": [<valid link entry>], "_comment": "x"}` against the `lab` schema, and a failing case for `{"routes": []}`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/models/test_jsonschema.py tests/unit/models/test_jsonschema_validation.py -q` — Expected: FAIL (`lab` stem missing).

- [ ] **Step 3: Implement**

In `src/otto/models/jsonschema.py`: import `LinkSpec` from `.link`. Rename `_host_array_schema` → `_hosts_array_schema` (unchanged body — it still builds the array part). Add:

```python
def _lab_schema(hosts_array: dict[str, Any]) -> dict[str, Any]:
    """The ``lab.json`` object schema: ``hosts``/``links`` sections + ``_`` comments."""
    link_doc = LinkSpec.model_json_schema(ref_template="#/$defs/{model}")
    defs = {**hosts_array.pop("$defs", {}), **link_doc.pop("$defs", {})}
    return {
        "type": "object",
        "properties": {
            "hosts": hosts_array,
            "links": {"type": "array", "items": link_doc},
        },
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
        "$defs": defs,
    }
```

In `build_schemas`, replace the `docs["hosts"] = ...` line with:

```python
    docs["lab"] = _decorate(
        _lab_schema(_hosts_array_schema(distinct, names)), "lab", "otto lab.json"
    )
    docs["link"] = _decorate(
        LinkSpec.model_json_schema(), "link", "otto link"
    )
```

Update the module docstring's emitted-documents list (`hosts` → `lab` + `link`, described as the `lab.json` schema). Update `cli/schema.py` help strings to "lab.json / settings.toml / reservations" (and the same string in `cli/builtin_commands.py:65`, if Task 4 didn't already).

- [ ] **Step 4: Run tests + verify the export end-to-end**

Run: `uv run pytest tests/unit/models -q` then `uv run otto schema export --out /tmp/claude-schemas && ls /tmp/claude-schemas`
Expected: PASS; the directory lists `lab.schema.json` and `link.schema.json`, no `hosts.schema.json`. (Check the actual `schema export` CLI signature in `src/otto/cli/schema.py` and adapt the command.)

- [ ] **Step 5: Commit**

```bash
git add src/otto/models/jsonschema.py src/otto/cli/schema.py src/otto/cli/builtin_commands.py tests/unit/models && git commit -m "feat(schema): export lab.json object schema + link schema, retire hosts array schema

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 9: Documentation sweep + `otto.link` API docs

Living docs must describe `lab.json`; historical specs/plans under `docs/superpowers/` are records and stay untouched (except nothing — do not edit them).

**Files:**
- Modify (the `hosts.json`-referencing living docs): `docs/getting-started.md`, `docs/contributing.md`, `docs/guide/host-database.md`, `docs/guide/lab-config.md`, `docs/guide/repo-setup.md`, `docs/guide/editor-schemas.md`, `docs/guide/extending-backends.md`, `docs/guide/library-usage.md`, `docs/guide/monitor.md`, `docs/guide/os-profiles.md`, `docs/guide/coverage.md`, `docs/guide/embedded.md`, `docs/guide/docker.md`, `docs/guide/cli-reference.md`, `docs/guide/host/connections.md`, `docs/guide/host/configuration.md`, `docs/guide/host/commands/index.md`, `docs/guide/host/commands/netcat.md`, `docs/cookbook/connection-options.md`, `docs/architecture/overview.md`, `docs/architecture/subsystems/registries.md`, `docs/architecture/subsystems/data-boundary.md`, `docs/architecture/subsystems/docker-hosts.md`, `docs/architecture/lifecycles/index.md`, `docs/architecture/lifecycles/docker.md`, `docs/architecture/lifecycles/schema.md`, `docs/architecture/lifecycles/init.md`, `docs/api/storage.rst`
- Create: `docs/api/link.rst`
- Modify: `docs/api/index.rst` (toctree)
- Test: the Sphinx gate (`make docs` builds with `-W`)

**Interfaces:** none produced — documentation of Tasks 1–8's surface.

- [ ] **Step 1: Sweep the rename through living docs**

Run: `grep -rln "hosts.json" docs/ | grep -v superpowers`
In each hit: `hosts.json` → `lab.json`, and where the file *shape* is shown (notably `docs/guide/host-database.md` and `docs/guide/lab-config.md`), update examples to the `{"hosts": [...], "links": [...]}` object. In `docs/guide/lab-config.md` add a short **Links** section documenting the declared-link entry (copy the field semantics from spec §5: endpoints, interface-required-iff->1, protocol default `tcp`, derived cross-lab membership) and the `interfaces` netdev-keyed object + string shorthand. In `docs/guide/editor-schemas.md`, update stem names (`hosts` → `lab`, add `link`). Expected after: `grep -rln "hosts.json" docs/ | grep -v superpowers` → empty.

- [ ] **Step 2: Add the API page**

Create `docs/api/link.rst` (mirror `docs/api/storage.rst`'s automodule style):

```rst
Link
====

.. automodule:: otto.link
   :members:

.. automodule:: otto.link.model
   :members:

.. automodule:: otto.link.derive
   :members:

.. automodule:: otto.link.sentinel
   :members:

.. automodule:: otto.link.discovery
   :members:

.. automodule:: otto.models.link
   :members:
```

Add `link` to the toctree in `docs/api/index.rst` (alphabetical position, near `logger`). Check `docs/api/host/` for where host modules are listed and add `otto.host.interface` there the same way.

- [ ] **Step 3: Build the docs gate**

Run: `make docs`
Expected: builds clean under `-W` (nitpicky). Fix any unresolved cross-references (common cause: a docstring `:class:` target missing from the new rst pages).

- [ ] **Step 4: Commit**

```bash
git add docs && git commit -m "docs: lab.json cutover across living docs + otto.link API pages

Assisted-by: Claude Opus 4.8" && git log -1
```

---

### Task 10: Final full gate + spec cross-check

**Files:** none (verification only).

- [ ] **Step 1: Run the complete gate set**

```bash
make coverage && uv run nox -s lint typecheck && make docs
```

Expected: all green. If lint flags formatting, run `uv run ruff format src tests` and re-run.

- [ ] **Step 2: Grep-audit the cutover**

Run: `grep -rn "hosts.json" src/ docs/ --include="*.py" --include="*.md" --include="*.rst" | grep -v superpowers`
Expected: zero hits. Run: `grep -rln "hosts.json" tests/` — expected: only deliberate legacy-rejection tests.

- [ ] **Step 3: Cross-check against the spec**

Walk `docs/superpowers/specs/2026-07-06-link-foundation-design.md` §3–§11 and confirm each has landed: lab.json object + merge (§3/Task 4-5), InterfaceSpec (§4/Task 1), LinkSpec + membership (§5/Tasks 2,5), Link type + id + accessors (§6/Tasks 3,7), implicit derivation (§7/Task 5), sentinel contract (§8/Task 6), static topology (§9/Task 5), affected code (§10/Tasks 4,8,9), testing (§11/all). Report any gap before hand-off.

- [ ] **Step 4: Hand off**

Do not merge to main; report status (branch `worktree-link-foundation`, commit list via `git log --oneline origin/main..HEAD`) for Chris's review — sub-project #2 (`otto link` CLI + live discovery) is the next spec, not this branch.

---

## Long-term consequences ledger (why these shapes)

Decisions here that later sub-projects inherit — recorded so nobody "simplifies" them away:

1. **Sentinel is versioned (`v1`) and percent-encoded** — live tunnels outlive otto upgrades; parsers must read old markers forever, and netdev alias names (`eth0:1`) must not corrupt the frame. Owner-agnostic on purpose (anyone reaps orphans).
2. **`make_link_id` excludes ports/ips** — the id names the *route*, which is what makes declared↔dynamic reconciliation and `add`-idempotency work in #2. Port variants are endpoint attributes, not new identities.
3. **`lab.json` sections are a registry** (`_LAB_SECTIONS`) — the monitor's element model (`elements`) and management-source declarations (`sources`, #5) become one-line section additions, with unknown-section fail-loud preserved.
4. **`InterfaceSpec` is an object, not a string** — mac/cidr/role/speed land as optional fields, never a reshaping.
5. **`discover` is dependency-injected** — #2 swaps in the live gather without touching `all_links` consumers; the GUI's TTL cache wraps the same callable.
6. **No `otto/__init__.py` re-exports yet** — the public surface is decided when the CLI ships (#2); until then `otto.link` is importable but not advertised.
