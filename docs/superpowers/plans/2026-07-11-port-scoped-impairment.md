# Port-Scoped Link Impairment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `otto link impair <link> --port N [--proto tcp|udp]` degrades ONE service's traffic on a link (either direction of that port) while everything else flows clean — up to 8 independent selectors per link, per-selector expire timers, exact read-back from kernel state only.

**Architecture:** The NetEm impairer grows an optional "scoped" surface: a `prio` root qdisc (bands 4..11 hold per-selector `netem` leaves, bands 1–3 keep kernel-default semantics so unmatched traffic is pfifo_fast-equivalent) plus `u32` filters steering the selector's port into its band. Orchestration (`otto.link.manage`) reads back a discriminated `ScopedState` (clean / whole / scoped / foreign), enforces whole-link↔scoped exclusivity, assigns bands, verifies per-selector by meaning, and rolls back the full pre-call shape. Sentinel v2 (`otto-impair:v2:<link>:<netdev>:<port>:<proto-or-empty>`) tags per-selector expire timers; v1 stays parseable and whole-link timers keep launching v1.

**Tech Stack:** Python 3.10+ dataclasses, tc (prio/netem/u32), pytest + pytest-asyncio fake hosts, live 3-VM veggies bed for e2e.

**Spec:** `docs/superpowers/specs/2026-07-11-port-scoped-impairment-design.md` (approved 2026-07-11). Read it before starting any task.

## Global Constraints

- Whole-link impairment (no `--port`) stays **byte-identical**: same apply command `tc qdisc replace dev X root netem …`, same root-netem read-back parse, same goldens, same v1 timer sentinel. Existing tests in `tests/unit/link/` that pin those bytes must keep passing unmodified unless a task explicitly says to update them.
- Links only. **Nothing under `src/otto/tunnel/` is touched by any task.**
- Kernel qdisc/filter state is the ONLY state — no otto-side selector registry anywhere.
- Selector cap: **8** per placement netdev (bands 4..11). Exceeding it is a loud `ValueError`.
- Exclusive per link in v1: whole-link and port-scoped never coexist on a netdev; mixing is a loud error naming the remedy (exact strings pinned in Task 6/8).
- Third-party impairers unaffected: scoped surface is optional, `supports_selectors: ClassVar[bool] = False` default, capability error names the impairer.
- No half-impairments: mid-way failure restores every touched placement to its full pre-call shape, including a complete scoped mapping.
- No `from __future__ import annotations` (repo ban — breaks Sphinx nitpicky builds).
- Lint gate is `ruff check` **AND** `ruff format --check` — run both, agents habitually miss format.
- `ty` runs only at `make typecheck-python`, not inside pytest — the plan schedules explicit typecheck rounds.
- Never skip a test on host-down: live-bed tests raise a host-named `RuntimeError`.
- No heavy parallel test load on the dev VM: single `-n0`/default runs of scoped test files; `make coverage` only in the final task.
- Commit policy: this worktree branch (`worktree-tunnel-lifecycle-fixes`) allows self-commit — conventional prefix + `Assisted-by: Claude (Fable 5)` trailer, verify with `git log -1`.

## Deliberate refinements vs. the spec sketch (adjudicated at plan time — implement as written here)

1. **`scoped_root_command(netdev)` takes no `bands` argument; the root is always `bands 11`** (3 + the cap of 8) and is issued ONCE per clean→scoped transition, never re-issued while scoped. Re-`replace`-ing a live prio root to grow bands risks the kernel re-initializing bands and destroying sibling selectors' netem children; a fixed band count also makes read-back validation a constant. Spec's `bands <3+N>` sketch is satisfied at N=8.
2. **tc classids/handles are HEX.** Bands 10 and 11 make this load-bearing: band `b`'s class is `1:{b:x}` (`1:a`, `1:b`), its netem handle `{b:x}0:` (numeric value b*16). Filter `pref` is plain decimal (`band*10 + slot`, 40..113). Build and parse must both use these exact conventions.
3. **Our prio root with zero selectors and zero band netems parses as `clean`**, so a timer race that leaves an empty tree never wedges exclusivity. (Our root with bands/filters that don't fully validate is `foreign`.)
4. **The v2 timer script appends a conditional root cleanup** (`if [ -z "$(tc filter show …)" ]; then tc qdisc del … root; fi`) so the LAST selector's expiry restores pristine, per spec's "clearing the last selector deletes the root".
5. **Whole-link expire timers keep sentinel v1** (byte-identical constraint); v2 is used only for selector timers.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/otto/link/params.py` (modify) | + `Selector` frozen dataclass (port, proto, `describe()`) |
| `src/otto/link/impairer.py` (modify) | + `ScopedState` discriminated result, `FIRST_SELECTOR_BAND`/`MAX_SELECTORS`, optional scoped surface on `LinkImpairer` |
| `src/otto/link/sentinel.py` (modify) | + v2 codec, `ImpairTimer` result type, dual-version ps parsing |
| `src/otto/link/netem.py` (modify) | + scoped builders (root/band/filter/clear/read) and `parse_scoped` |
| `src/otto/link/manage.py` (modify) | selector-keyed orchestration: unified `ScopedState` reads, exclusivity, band assignment, full-shape rollback, `DirectionState`, v2 timers |
| `src/otto/link/__init__.py` (modify) | export `Selector`, `ScopedState`, `DirectionState` |
| `src/otto/cli/link.py` (modify) | `--port`/`--proto` on impair+repair, per-selector list rows, foreign rendering |
| `docs/guide/link.md` (modify) | "Port-scoped impairments" section |
| `tests/unit/link/test_params.py` (modify) | Selector tests |
| `tests/unit/link/test_impairer_registry.py` (modify) | contract/default tests |
| `tests/unit/link/test_impair_sentinel.py` (modify) | v2 codec + dual-version scan tests |
| `tests/unit/link/test_netem.py` (modify) | scoped builder goldens + parse_scoped fixtures |
| `tests/unit/link/test_manage_impair.py` (modify) | scoped apply/exclusivity/cap/rollback/timer tests, FakeHost filter queue |
| `tests/unit/link/test_manage_repair.py` (modify) | scoped repair + DirectionState read tests |
| `tests/unit/link/test_lazy_exports.py` (modify) | + `DirectionState` in lazy list |
| `tests/unit/link/test_cli.py` (modify) | CLI flag/rendering tests |
| `tests/e2e/test_link_impair_e2e.py` (modify) | scoped e2e: differential proof, 2 selectors, per-selector expiry, pristine repair |

Interfaces defined once, used everywhere (type-consistency contract):

```python
# params.py
Selector(port: int, proto: str | None = None)          # frozen; .describe() -> "5201/tcp" | "5201"

# impairer.py
FIRST_SELECTOR_BAND = 4
MAX_SELECTORS = 8
ScopedState(kind: Literal["clean","whole","scoped","foreign"],
            whole: ImpairmentParams | None,
            selectors: dict[Selector, tuple[int, ImpairmentParams]])   # value = (band, params)
LinkImpairer.supports_selectors: ClassVar[bool]
LinkImpairer.scoped_root_command(netdev: str) -> str
LinkImpairer.scoped_band_command(netdev: str, band: int, params: ImpairmentParams) -> str
LinkImpairer.scoped_filter_commands(netdev: str, band: int, selector: Selector) -> list[str]
LinkImpairer.scoped_clear_selector_commands(netdev: str, band: int, selector: Selector) -> list[str]
LinkImpairer.scoped_read_commands(netdev: str) -> list[str]            # [qdisc show, filter show]
LinkImpairer.parse_scoped(qdisc_output: str, filter_output: str) -> ScopedState

# sentinel.py
ImpairTimer(pid: int, link_id: str, netdev: str, selector: Selector | None)   # None = v1 whole-link
encode_impair_sentinel(link_id, netdev) -> str                                 # v1, unchanged
encode_impair_sentinel_v2(link_id, netdev, selector) -> str
parse_impair_sentinel(token) -> tuple[str, str, Selector | None] | None        # CHANGED shape (3-tuple)
parse_impair_ps(output) -> list[ImpairTimer]                                   # CHANGED shape

# manage.py
DirectionState(whole: ImpairmentParams | None, scoped: dict[Selector, ImpairmentParams], foreign: bool)
LinkState.by_direction: dict[FlowDirection, DirectionState | None]              # None = unreadable
AppliedPlacement.selector: Selector | None                                      # new field, default None
impair_link(lab, ident, params, *, from_host=None, expire=None, selector: Selector | None = None)
repair_link(lab, ident, *, selector: Selector | None = None)
```

---

### Task 1: `Selector` model

**Files:**
- Modify: `src/otto/link/params.py` (append after `ImpairmentParams`-related code, before `_canonical_rate_bps`)
- Test: `tests/unit/link/test_params.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Selector` frozen dataclass as pinned in the File Structure contract; module constants `_PROTOS = ("tcp", "udp")`, `_MAX_PORT = 65535`.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/link/test_params.py`:

```python
class TestSelector:
    def test_fields_and_describe(self) -> None:
        from otto.link.params import Selector

        assert Selector(5201).describe() == "5201"
        assert Selector(5201, "tcp").describe() == "5201/tcp"
        assert Selector(53, "udp").describe() == "53/udp"

    def test_distinct_keys_proto_none_vs_tcp(self) -> None:
        from otto.link.params import Selector

        assert Selector(5201) != Selector(5201, "tcp")
        assert len({Selector(5201), Selector(5201, "tcp"), Selector(5201)}) == 2

    def test_port_range_validated(self) -> None:
        from otto.link.params import Selector

        with pytest.raises(ValueError, match="port 0 out of range"):
            Selector(0)
        with pytest.raises(ValueError, match="port 65536 out of range"):
            Selector(65536)
        Selector(1)
        Selector(65535)

    def test_proto_vocabulary_validated(self) -> None:
        from otto.link.params import Selector

        with pytest.raises(ValueError, match="must be tcp or udp"):
            Selector(80, "icmp")
```

(`test_params.py` already imports `pytest`; if not at top, add `import pytest`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_params.py::TestSelector -v`
Expected: FAIL / ERROR with `ImportError: cannot import name 'Selector'`.

- [ ] **Step 3: Implement** — in `src/otto/link/params.py`, after the `ImpairmentParams` class definition, add:

```python
_PROTOS = ("tcp", "udp")
_MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class Selector:
    """One port-scoped impairment selector: a service port, EITHER side.

    Matches traffic whose SOURCE OR DESTINATION port is :attr:`port` —
    otto never needs to know which endpoint is the server. :attr:`proto`
    narrows to one L4 protocol; ``None`` = both tcp and udp.
    ``Selector(5201)`` and ``Selector(5201, "tcp")`` are DISTINCT keys;
    the former's filters simply match a superset of the latter's traffic
    (spec 2026-07-11 §1).
    """

    port: int
    proto: str | None = None
    """``"tcp"``, ``"udp"``, or ``None`` = both."""

    def __post_init__(self) -> None:
        if not 1 <= self.port <= _MAX_PORT:
            raise ValueError(f"selector port {self.port} out of range 1-{_MAX_PORT}")
        if self.proto is not None and self.proto not in _PROTOS:
            raise ValueError(f"selector proto {self.proto!r} must be tcp or udp")

    def describe(self) -> str:
        """The one string form (``5201`` / ``5201/tcp``) used uniformly by the
        CLI, ``list`` rows, error text, and the v2 sentinel payload."""
        return f"{self.port}/{self.proto}" if self.proto else str(self.port)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link/test_params.py -v`
Expected: ALL PASS (new + existing).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/otto/link/params.py tests/unit/link/test_params.py
uv run ruff format --check src/otto/link/params.py tests/unit/link/test_params.py
git add src/otto/link/params.py tests/unit/link/test_params.py
git commit -m "feat(link): Selector model for port-scoped impairment

Assisted-by: Claude (Fable 5)"
```

---

### Task 2: `ScopedState` + optional scoped surface on the `LinkImpairer` contract

**Files:**
- Modify: `src/otto/link/impairer.py`
- Test: `tests/unit/link/test_impairer_registry.py`

**Interfaces:**
- Consumes: `Selector`, `ImpairmentParams` from `otto.link.params`.
- Produces: `ScopedState` (with classmethod constructors `clean()`, `whole_link(params)`, `from_selectors(mapping)`, `foreign()`), `FIRST_SELECTOR_BAND = 4`, `MAX_SELECTORS = 8`, `LinkImpairer.supports_selectors` + the six scoped methods (all raising `NotImplementedError` by default).

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/link/test_impairer_registry.py`:

```python
class TestScopedContract:
    def test_base_impairer_does_not_support_selectors(self) -> None:
        from otto.link.impairer import LinkImpairer

        assert LinkImpairer.supports_selectors is False

    def test_scoped_methods_default_to_not_implemented(self) -> None:
        import pytest

        from otto.link.impairer import LinkImpairer
        from otto.link.params import ImpairmentParams, Selector

        imp = LinkImpairer()
        sel = Selector(5201, "tcp")
        with pytest.raises(NotImplementedError):
            imp.scoped_root_command("eth1")
        with pytest.raises(NotImplementedError):
            imp.scoped_band_command("eth1", 4, ImpairmentParams(delay_ms=1.0))
        with pytest.raises(NotImplementedError):
            imp.scoped_filter_commands("eth1", 4, sel)
        with pytest.raises(NotImplementedError):
            imp.scoped_clear_selector_commands("eth1", 4, sel)
        with pytest.raises(NotImplementedError):
            imp.scoped_read_commands("eth1")
        with pytest.raises(NotImplementedError):
            imp.parse_scoped("", "")

    def test_scoped_state_constructors(self) -> None:
        from otto.link.impairer import FIRST_SELECTOR_BAND, MAX_SELECTORS, ScopedState
        from otto.link.params import ImpairmentParams, Selector

        assert FIRST_SELECTOR_BAND == 4
        assert MAX_SELECTORS == 8
        assert ScopedState.clean().kind == "clean"
        params = ImpairmentParams(delay_ms=50.0)
        whole = ScopedState.whole_link(params)
        assert whole.kind == "whole"
        assert whole.whole == params
        mapping = {Selector(5201, "tcp"): (4, params)}
        scoped = ScopedState.from_selectors(mapping)
        assert scoped.kind == "scoped"
        assert scoped.selectors == mapping
        assert ScopedState.foreign().kind == "foreign"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_impairer_registry.py::TestScopedContract -v`
Expected: FAIL (`ImportError`/`AttributeError`).

- [ ] **Step 3: Implement** — in `src/otto/link/impairer.py`:

Replace the import block's params line and add typing imports:

```python
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import ClassVar, Literal

from ..registry import Registry, caller_module
from .params import ImpairmentParams, Selector
```

After the imports, before `class LinkImpairer`, add:

```python
FIRST_SELECTOR_BAND = 4
"""prio bands 1-3 keep kernel-default priomap semantics; selectors start at 1:4."""

MAX_SELECTORS = 8
"""Per-netdev selector cap (bands 4..11 inside the fixed 11-band prio root)."""


@dataclass(frozen=True, slots=True)
class ScopedState:
    """Discriminated read-back of one placement netdev's impairment shape.

    Exactly one of four kinds (spec §1): ``clean`` (no otto state — including
    kernel-default root qdiscs), ``whole`` (today's root netem,
    :attr:`whole` set), ``scoped`` (:attr:`selectors` maps each
    :class:`~otto.link.params.Selector` to its ``(band, params)``), or
    ``foreign`` (a root qdisc otto did not generate: reported by ``list``,
    loudly refused on mutate).
    """

    kind: Literal["clean", "whole", "scoped", "foreign"]
    whole: ImpairmentParams | None = None
    selectors: dict[Selector, tuple[int, ImpairmentParams]] = dc_field(default_factory=dict)

    @classmethod
    def clean(cls) -> "ScopedState":
        """No otto impairment state on the netdev."""
        return cls("clean")

    @classmethod
    def whole_link(cls, params: ImpairmentParams) -> "ScopedState":
        """Today's whole-link root netem."""
        return cls("whole", whole=params)

    @classmethod
    def from_selectors(
        cls, selectors: dict[Selector, tuple[int, ImpairmentParams]]
    ) -> "ScopedState":
        """A port-scoped tree: selector -> (band, params)."""
        return cls("scoped", selectors=dict(selectors))

    @classmethod
    def foreign(cls) -> "ScopedState":
        """A root qdisc otto did not generate — never mutated, only reported."""
        return cls("foreign")
```

Inside `class LinkImpairer`, after the `host_families` ClassVar, add:

```python
    supports_selectors: ClassVar[bool] = False
    """Whether this impairer implements the optional port-scoped surface
    (the ``scoped_*`` builders + :meth:`parse_scoped`). Defaults off so
    third-party impairers are unaffected; a ``--port`` request routed to a
    non-supporting impairer is a loud capability error in orchestration."""
```

and after `parse_read`, the six optional methods (stateless, no I/O — same philosophy as the existing four):

```python
    def scoped_root_command(self, netdev: str) -> str:
        """Command creating the scoped classful root on *netdev* (idempotent)."""
        raise NotImplementedError

    def scoped_band_command(self, netdev: str, band: int, params: ImpairmentParams) -> str:
        """Command applying *params* as band *band*'s per-selector leaf (idempotent)."""
        raise NotImplementedError

    def scoped_filter_commands(self, netdev: str, band: int, selector: Selector) -> list[str]:
        """Commands steering *selector*'s traffic into band *band*."""
        raise NotImplementedError

    def scoped_clear_selector_commands(
        self, netdev: str, band: int, selector: Selector
    ) -> list[str]:
        """Commands removing *selector*'s filters and its band leaf (root kept)."""
        raise NotImplementedError

    def scoped_read_commands(self, netdev: str) -> list[str]:
        """The two read commands whose outputs :meth:`parse_scoped` understands."""
        raise NotImplementedError

    def parse_scoped(self, qdisc_output: str, filter_output: str) -> ScopedState:
        """Parse :meth:`scoped_read_commands` outputs into a :class:`ScopedState`."""
        raise NotImplementedError
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link/test_impairer_registry.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/otto/link/impairer.py tests/unit/link/test_impairer_registry.py
uv run ruff format --check src/otto/link/impairer.py tests/unit/link/test_impairer_registry.py
git add src/otto/link/impairer.py tests/unit/link/test_impairer_registry.py
git commit -m "feat(link): ScopedState + optional scoped surface on LinkImpairer

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: Sentinel v2 (per-selector expire timers)

**Files:**
- Modify: `src/otto/link/sentinel.py`
- Test: `tests/unit/link/test_impair_sentinel.py`

**Interfaces:**
- Consumes: `Selector` (Task 1); `enc`/`dec`/`encode_token`/`split_token`/`parse_ps_output` from `otto.host.daemon` (existing).
- Produces: `encode_impair_sentinel_v2(link_id, netdev, selector) -> str`; **CHANGED** `parse_impair_sentinel(token) -> tuple[str, str, Selector | None] | None` (3-tuple; v1 tokens yield selector `None`); `ImpairTimer` dataclass; **CHANGED** `parse_impair_ps(output) -> list[ImpairTimer]`.
- BREAKS (fix in this task): `otto.link.manage._cancel_timers` and `tests/e2e/test_link_impair_e2e.py` consume `parse_impair_ps`'s old tuple shape. Update `manage.py`'s two consumption sites minimally here (field access instead of tuple unpack); the e2e file's uses (`t[1] == "edge"`, `timers!r` messages) become `t.link_id == "edge"` — update them now too.

- [ ] **Step 1: Write the failing tests** — in `tests/unit/link/test_impair_sentinel.py`, REPLACE `TestCodec.test_reject_foreign_and_malformed`, `TestPsScan.test_parse_ps_extracts_timer_pids`, and the v1 roundtrip assertions with the dual-version versions below, and add the v2 golden:

```python
"""otto-impair sentinel codec + ps-scan parsing (mirrors tunnel sentinel style)."""

from otto.link.params import Selector
from otto.link.sentinel import (
    IMPAIR_PS_COMMAND,
    ImpairTimer,
    encode_impair_sentinel,
    encode_impair_sentinel_v2,
    parse_impair_ps,
    parse_impair_sentinel,
)


class TestCodec:
    def test_v1_roundtrip(self) -> None:
        token = encode_impair_sentinel("lnk-abc123", "eth1.100")
        assert token == "otto-impair:v1:lnk-abc123:eth1.100"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100", None)

    def test_v2_roundtrip_with_proto(self) -> None:
        token = encode_impair_sentinel_v2("lnk-abc123", "eth1.100", Selector(5201, "tcp"))
        assert token == "otto-impair:v2:lnk-abc123:eth1.100:5201:tcp"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100", Selector(5201, "tcp"))

    def test_v2_roundtrip_proto_none_empty_segment(self) -> None:
        token = encode_impair_sentinel_v2("lnk-abc123", "eth1.100", Selector(53))
        assert token == "otto-impair:v2:lnk-abc123:eth1.100:53:"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100", Selector(53))

    def test_percent_encoding_of_separator(self) -> None:
        token = encode_impair_sentinel("name:with:colons", "eth1")
        assert parse_impair_sentinel(token) == ("name:with:colons", "eth1", None)
        token2 = encode_impair_sentinel_v2("name:with:colons", "eth1", Selector(80))
        assert parse_impair_sentinel(token2) == ("name:with:colons", "eth1", Selector(80))

    def test_reject_foreign_and_malformed(self) -> None:
        assert parse_impair_sentinel("otto-tunnel:v1:x:y") is None
        assert parse_impair_sentinel("otto-impair:v3:a:b:1:tcp") is None
        assert parse_impair_sentinel("otto-impair:v1:onlyone") is None
        assert parse_impair_sentinel("otto-impair:v2:a:b:notaport:tcp") is None
        assert parse_impair_sentinel("otto-impair:v2:a:b:80:icmp") is None
        assert parse_impair_sentinel("otto-impair:v2:a:b:0:tcp") is None


class TestPsScan:
    def test_ps_command_uses_separate_eo_flags(self) -> None:
        # procps-ng 3.3.10 mis-parses the comma-joined form (#2b lesson)
        assert "-eo pid= -eo etime= -eo args=" in IMPAIR_PS_COMMAND
        assert "grep -a ' otto-impair:'" in IMPAIR_PS_COMMAND

    def test_parse_ps_extracts_both_versions(self) -> None:
        v1 = encode_impair_sentinel("lnk-abc123", "eth1.100")
        v2 = encode_impair_sentinel_v2("lnk-abc123", "eth1.100", Selector(5201, "tcp"))
        text = "\n".join(
            [
                f"  4242 05:00 {v1} -c sleep 30 && tc qdisc del dev eth1.100 root",
                f"  4243 06:00 {v2} -c sleep 30 && tc filter del ...",
                "  4244 05:00 otto-impair:v1:mangled",
                "  10 01:00 socat TCP4-LISTEN:5000 STDIO",
                "garbage",
            ]
        )
        assert parse_impair_ps(text) == [
            ImpairTimer(4242, "lnk-abc123", "eth1.100", None),
            ImpairTimer(4243, "lnk-abc123", "eth1.100", Selector(5201, "tcp")),
        ]


class TestWireGolden:
    def test_ps_command_golden(self):
        # `\grep` bypasses the interactive-shell color alias that blinds the
        # scan on telnet-term hosts — see TestPsScanCommand in
        # tests/unit/host/test_daemon.py for the full story.
        assert IMPAIR_PS_COMMAND == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | \\grep -a ' otto-impair:' || true"
        )

    def test_encode_produces_the_exact_v1_bytes(self):
        assert encode_impair_sentinel("lnk-1", "eth0.100") == "otto-impair:v1:lnk-1:eth0.100"
        assert encode_impair_sentinel("a:b", "e/th") == "otto-impair:v1:a%3Ab:e%2Fth"

    def test_encode_produces_the_exact_v2_bytes(self):
        assert (
            encode_impair_sentinel_v2("a:b", "e/th", Selector(5201, "udp"))
            == "otto-impair:v2:a%3Ab:e%2Fth:5201:udp"
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_impair_sentinel.py -v`
Expected: FAIL (`ImportError: cannot import name 'ImpairTimer'`).

- [ ] **Step 3: Implement** — replace the body of `src/otto/link/sentinel.py` below the module docstring (update the docstring's wire-format line to mention both versions):

```python
"""otto-impair argv sentinel + expire-timer discovery (spec §7; v2 spec 2026-07-11 §1).

Wire formats, percent-encoded segments, framing via :mod:`otto.host.daemon`:

- v1 (whole-link timers): ``otto-impair:v1:<link-id>:<netdev>``
- v2 (per-selector timers): ``otto-impair:v2:<link-id>:<netdev>:<port>:<proto-or-empty>``

v1 stays parseable forever so repair cancels timers launched by older otto.
The timer process's argv IS the state — discoverable via ``ps``, unambiguously
otto's, owner-agnostic.
"""

from dataclasses import dataclass

from ..host.daemon import dec, enc, encode_token, parse_ps_output, ps_scan_command, split_token
from .params import Selector

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
IMPAIR_SENTINEL_VERSION_V2 = "v2"
_PAYLOAD_SEGMENTS_V1 = 2
_PAYLOAD_SEGMENTS_V2 = 4

IMPAIR_PS_COMMAND: str = ps_scan_command(IMPAIR_SENTINEL_PREFIX)
"""The per-host expire-timer scan. Built by
:func:`otto.host.daemon.ps_scan_command` — see it for the procps
portability story; bytes pinned by ``TestWireGolden``."""


@dataclass(frozen=True, slots=True)
class ImpairTimer:
    """One live expire-timer seen in a ps scan."""

    pid: int
    link_id: str
    netdev: str
    selector: Selector | None
    """``None`` = a v1 whole-link timer; set = a v2 per-selector timer."""


def encode_impair_sentinel(link_id: str, netdev: str) -> str:
    """v1 sentinel token tagging one placement's WHOLE-LINK expire timer.

    Whole-link timers deliberately stay on v1 — the whole-link path is
    byte-identical to pre-selector otto (spec 2026-07-11 hard constraint).
    """
    return encode_token(
        IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, (enc(link_id), enc(netdev))
    )


def encode_impair_sentinel_v2(link_id: str, netdev: str, selector: Selector) -> str:
    """v2 sentinel token tagging one selector's expire timer on one placement."""
    return encode_token(
        IMPAIR_SENTINEL_PREFIX,
        IMPAIR_SENTINEL_VERSION_V2,
        (enc(link_id), enc(netdev), enc(selector.port), enc(selector.proto or "")),
    )


def parse_impair_sentinel(token: str) -> tuple[str, str, Selector | None] | None:
    """Decode a v1 OR v2 token to ``(link_id, netdev, selector)``; ``None`` if not ours.

    v1 tokens decode with ``selector=None``. Unknown versions and malformed
    v2 payloads (non-numeric/out-of-range port, unknown proto) parse to
    ``None``, never an error — the framing stability contract.
    """
    v1 = split_token(token, IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, _PAYLOAD_SEGMENTS_V1)
    if v1 is not None:
        return dec(v1[0]), dec(v1[1]), None
    v2 = split_token(
        token, IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION_V2, _PAYLOAD_SEGMENTS_V2
    )
    if v2 is None:
        return None
    port_text, proto_text = dec(v2[2]), dec(v2[3])
    if not port_text.isdigit():
        return None
    try:
        selector = Selector(int(port_text), proto_text or None)
    except ValueError:
        return None
    return dec(v2[0]), dec(v2[1]), selector


def parse_impair_ps(output: str) -> list[ImpairTimer]:
    """Reconstruct live timers from :data:`IMPAIR_PS_COMMAND` output (v1 AND v2)."""
    out: list[ImpairTimer] = []
    for proc in parse_ps_output(output, IMPAIR_SENTINEL_PREFIX):
        parsed = parse_impair_sentinel(proc.token)
        if parsed is None:
            continue
        out.append(ImpairTimer(proc.pid, parsed[0], parsed[1], parsed[2]))
    return out
```

- [ ] **Step 4: Fix the two consumers of the old shapes**

In `src/otto/link/manage.py`, `_cancel_timers` currently does:

```python
    pids = [
        pid for pid, lid, dev in parse_impair_ps(result.value) if lid == link_id and dev == netdev
    ]
```

Change to (full scoping rules come in Task 8; this keeps today's cancel-everything-for-netdev semantics):

```python
    pids = [
        t.pid
        for t in parse_impair_ps(result.value)
        if t.link_id == link_id and t.netdev == netdev
    ]
```

In `tests/e2e/test_link_impair_e2e.py` (two sites): `timers = [t for t in parse_impair_ps(ps_result.value or "") if t[1] == "edge"]` → `if t.link_id == "edge"`. The `_assert_bed_hygiene` site only checks truthiness — no change needed.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/unit/link -v`
Expected: ALL PASS (manage tests still green — cancel semantics unchanged).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/otto/link tests/unit/link tests/e2e/test_link_impair_e2e.py
uv run ruff format --check src/otto/link tests/unit/link tests/e2e/test_link_impair_e2e.py
git add -A src/otto/link tests/unit/link tests/e2e/test_link_impair_e2e.py
git commit -m "feat(link): impair sentinel v2 with per-selector payload, v1 kept parseable

Assisted-by: Claude (Fable 5)"
```

---

### Task 4: NetEm scoped command builders (golden bytes)

**Files:**
- Modify: `src/otto/link/netem.py`
- Test: `tests/unit/link/test_netem.py`

**Interfaces:**
- Consumes: `ScopedState`, `FIRST_SELECTOR_BAND`, `MAX_SELECTORS` (Task 2); `Selector` (Task 1); existing `netem_args`.
- Produces: `NetEmImpairer.supports_selectors = True` + the five builder methods (parse_scoped is Task 5). Module constants `_SCOPED_BANDS = 11`, `_KERNEL_DEFAULT_PRIOMAP = "1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"`, `_SLOTS`, `_PROTO_NUM`, helper `_selector_slots(selector) -> list[int]`.

The u32 lines are the new stability-critical strings. Slot order per band is FIXED (spec §2): pref `band*10 + slot` with slot 0=dport/tcp, 1=sport/tcp, 2=dport/udp, 3=sport/udp; a single-proto selector uses only its two slots. Classids/handles are HEX (`1:{band:x}`, `{band:x}0:`); pref is decimal.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/link/test_netem.py` (add `from otto.link.params import Selector` to the imports):

```python
class TestScopedCommands:
    imp = NetEmImpairer()

    def test_supports_selectors(self) -> None:
        assert NetEmImpairer.supports_selectors is True

    def test_root_command_golden(self) -> None:
        assert self.imp.scoped_root_command("eth1.100") == (
            "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
            "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
        )

    def test_band_command_golden_hex_handles(self) -> None:
        params = ImpairmentParams(delay_ms=200.0)
        assert self.imp.scoped_band_command("eth1.100", 4, params) == (
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms"
        )
        # bands >= 10: classid minor and handle are HEX
        assert self.imp.scoped_band_command("eth1.100", 11, params) == (
            "tc qdisc replace dev eth1.100 parent 1:b handle b0: netem delay 200ms"
        )

    def test_filter_commands_proto_none_emits_four(self) -> None:
        cmds = self.imp.scoped_filter_commands("eth1.100", 4, Selector(5201))
        assert cmds == [
            "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
            "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
            "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 42 protocol ip u32 "
            "match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 43 protocol ip u32 "
            "match ip protocol 17 0xff match ip sport 5201 0xffff flowid 1:4",
        ]

    def test_filter_commands_single_proto_uses_its_two_slots(self) -> None:
        tcp = self.imp.scoped_filter_commands("eth1.100", 5, Selector(5201, "tcp"))
        assert [c.split(" pref ")[1].split(" ")[0] for c in tcp] == ["50", "51"]
        assert all("protocol 6 0xff" in c for c in tcp)
        udp = self.imp.scoped_filter_commands("eth1.100", 5, Selector(53, "udp"))
        assert [c.split(" pref ")[1].split(" ")[0] for c in udp] == ["52", "53"]
        assert all("protocol 17 0xff" in c for c in udp)
        assert all("flowid 1:5" in c for c in udp)

    def test_clear_selector_commands_golden(self) -> None:
        cmds = self.imp.scoped_clear_selector_commands("eth1.100", 4, Selector(5201, "tcp"))
        assert cmds == [
            "tc filter del dev eth1.100 parent 1: pref 40 protocol ip u32",
            "tc filter del dev eth1.100 parent 1: pref 41 protocol ip u32",
            "tc qdisc del dev eth1.100 parent 1:4 handle 40:",
        ]

    def test_read_commands_golden(self) -> None:
        assert self.imp.scoped_read_commands("eth1.100") == [
            "tc qdisc show dev eth1.100",
            "tc filter show dev eth1.100 parent 1: 2>/dev/null || true",
        ]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_netem.py::TestScopedCommands -v`
Expected: FAIL (`AttributeError: ... has no attribute 'scoped_root_command'` / supports_selectors False).

- [ ] **Step 3: Implement** — in `src/otto/link/netem.py`:

Update imports:

```python
from .impairer import LinkImpairer, ScopedState, register_impairer
from .params import ImpairmentParams, Selector
```

(`ScopedState` is unused until Task 5's parser but the constants below reference the layout; if ruff flags the unused import, add it in Task 5 instead.)

Add module constants after the existing ones:

```python
_SCOPED_BANDS = 11
"""Fixed prio band count: 3 kernel-default bands + the 8-selector cap. The
root is created ONCE per clean->scoped transition and never re-tuned while
scoped — re-`replace`-ing a live prio root risks the kernel re-initializing
bands and destroying sibling selectors' netem leaves."""

_KERNEL_DEFAULT_PRIOMAP = "1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
"""The kernel's default prio priomap: every TOS value maps to bands 1-3, so
unmatched traffic behaves exactly as with no qdisc (pfifo_fast equivalence)."""

_SLOTS: tuple[tuple[str, str], ...] = (
    ("dport", "tcp"),
    ("sport", "tcp"),
    ("dport", "udp"),
    ("sport", "udp"),
)
"""Fixed per-selector pref-slot order (spec §2): pref = band*10 + slot index."""

_PROTO_NUM = {"tcp": 6, "udp": 17}


def _selector_slots(selector: Selector) -> list[int]:
    """The pref-slot indices *selector* occupies (2 for one proto, 4 for both)."""
    return [i for i, (_side, proto) in enumerate(_SLOTS) if selector.proto in (None, proto)]
```

Inside `NetEmImpairer`, add `supports_selectors: ClassVar[bool] = True` next to `host_families`, and the builders (all `@override`):

```python
    @override
    def scoped_root_command(self, netdev: str) -> str:
        """Idempotent 11-band prio root; bands 1-3 keep kernel-default semantics."""
        return (
            f"tc qdisc replace dev {netdev} root handle 1: "
            f"prio bands {_SCOPED_BANDS} priomap {_KERNEL_DEFAULT_PRIOMAP}"
        )

    @override
    def scoped_band_command(self, netdev: str, band: int, params: ImpairmentParams) -> str:
        """Idempotent netem leaf for *band*. classid/handle minors are HEX."""
        return (
            f"tc qdisc replace dev {netdev} parent 1:{band:x} "
            f"handle {band:x}0: netem {netem_args(params)}"
        )

    @override
    def scoped_filter_commands(self, netdev: str, band: int, selector: Selector) -> list[str]:
        """u32 filters steering *selector* into band *band*, fixed pref-slot order."""
        cmds: list[str] = []
        for slot in _selector_slots(selector):
            side, proto = _SLOTS[slot]
            cmds.append(
                f"tc filter add dev {netdev} parent 1: pref {band * 10 + slot} "
                f"protocol ip u32 match ip protocol {_PROTO_NUM[proto]} 0xff "
                f"match ip {side} {selector.port} 0xffff flowid 1:{band:x}"
            )
        return cmds

    @override
    def scoped_clear_selector_commands(
        self, netdev: str, band: int, selector: Selector
    ) -> list[str]:
        """Delete *selector*'s filters (by pref) then its band's netem leaf."""
        cmds = [
            f"tc filter del dev {netdev} parent 1: pref {band * 10 + slot} protocol ip u32"
            for slot in _selector_slots(selector)
        ]
        cmds.append(f"tc qdisc del dev {netdev} parent 1:{band:x} handle {band:x}0:")
        return cmds

    @override
    def scoped_read_commands(self, netdev: str) -> list[str]:
        """qdisc + filter reads for :meth:`parse_scoped`. The filter read is
        guarded (``2>/dev/null || true``): on a netdev with no ``1:`` parent —
        every clean or whole-link netdev — ``tc filter show parent 1:`` fails,
        and the read path must treat that as 'no filters', not a host error."""
        return [
            f"tc qdisc show dev {netdev}",
            f"tc filter show dev {netdev} parent 1: 2>/dev/null || true",
        ]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link/test_netem.py -v`
Expected: ALL PASS (including the untouched whole-link goldens).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/otto/link/netem.py tests/unit/link/test_netem.py
uv run ruff format --check src/otto/link/netem.py tests/unit/link/test_netem.py
git add src/otto/link/netem.py tests/unit/link/test_netem.py
git commit -m "feat(link): NetEm scoped tree builders (prio bands + u32, golden bytes)

Assisted-by: Claude (Fable 5)"
```

---

### Task 5: NetEm `parse_scoped` (read-back → ScopedState)

**Files:**
- Modify: `src/otto/link/netem.py`
- Test: `tests/unit/link/test_netem.py`

**Interfaces:**
- Consumes: Task 4's constants/builders, existing `_parse_netem_tokens`, `parse_qdisc_show`.
- Produces: `NetEmImpairer.parse_scoped(qdisc_output, filter_output) -> ScopedState` and module-level `parse_scoped_outputs(qdisc_output, filter_output) -> ScopedState` (the impl; method delegates, mirroring `parse_qdisc_show`).

Parsing rules (only otto's own conventions parse; anything else → `foreign`):

- Root discrimination from `qdisc_output`:
  - root `netem` line → `whole` (delegate to the existing token parser — byte-identical behavior).
  - root `prio` line: must be `handle 1:`, `bands 11`, priomap exactly `_KERNEL_DEFAULT_PRIOMAP` → candidate scoped; anything off → `foreign`.
  - no root line, `noqueue`, or any root whose handle token is `0:` (kernel-attached default: pfifo_fast/fq_codel/mq show handle `0:`) → `clean`.
  - any other root qdisc (nonzero handle, not netem, not our prio) → `foreign`.
- Under a candidate scoped root: every `qdisc netem` child line must have `parent 1:<m>` with hex minor `band` in 4..11 AND handle == `{band:x}0:`; any non-netem child, or netem outside those bands, or handle mismatch → `foreign`.
- From `filter_output`: headers `filter parent 1: protocol ip pref <P> u32 ...`; only headers carrying `flowid 1:<m>` open a match block (the `ht divisor` header lines carry no matches); subsequent `match <VAL>/<MASK> at <OFF>` lines belong to the open block. Per block: `band = int(minor, 16)`, `slot = P - band*10` must be 0..3; the block must contain EXACTLY two matches: `at 8` with mask `00ff0000` whose `(value >> 16) & 0xff` equals `_PROTO_NUM[slot's proto]`, and `at 20` with either mask `0000ffff` (dport; `port = value & 0xffff`; slot side must be dport) or mask `ffff0000` (sport; `port = value >> 16`; slot side must be sport). Anything else → `foreign`.
- Reassembly per band: same port across all its slots; slot set must be exactly `{0,1,2,3}` (proto None), `{0,1}` (tcp), or `{2,3}` (udp); every filtered band must have a netem leaf and every netem leaf must be filtered; violations → `foreign`. Selector = `Selector(port, proto)`.
- Our root with ZERO netem leaves and ZERO filters → `clean` (deliberate refinement 3: a timer race that leaves an empty tree must not wedge exclusivity).
- Whole-link netem present AND our-prio artifacts can't coexist in one qdisc dump root — root wins; don't over-engineer.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/link/test_netem.py`. These fixtures are hand-modeled from iproute2 6.1 conventions; Task 12 re-validates them against live-bed captures and updates the BYTES (not the semantics) if reality differs.

```python
QDISC_SCOPED = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_SCOPED = (
    "filter parent 1: protocol ip pref 40 u32 chain 0\n"
    "filter parent 1: protocol ip pref 40 u32 chain 0 fh 800: ht divisor 1\n"
    "filter parent 1: protocol ip pref 40 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0 "
    "flowid 1:4 not_in_hw\n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter parent 1: protocol ip pref 41 u32 chain 0\n"
    "filter parent 1: protocol ip pref 41 u32 chain 0 fh 801: ht divisor 1\n"
    "filter parent 1: protocol ip pref 41 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0 "
    "flowid 1:4 not_in_hw\n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
    "filter parent 1: protocol ip pref 52 u32 chain 0\n"
    "filter parent 1: protocol ip pref 52 u32 chain 0 fh 802: ht divisor 1\n"
    "filter parent 1: protocol ip pref 52 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0 "
    "flowid 1:5 not_in_hw\n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter parent 1: protocol ip pref 53 u32 chain 0\n"
    "filter parent 1: protocol ip pref 53 u32 chain 0 fh 803: ht divisor 1\n"
    "filter parent 1: protocol ip pref 53 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0 "
    "flowid 1:5 not_in_hw\n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)


class TestParseScoped:
    imp = NetEmImpairer()

    def test_clean_variants(self) -> None:
        for qdisc in (
            "",
            "qdisc noqueue 0: root refcnt 2\n",
            "qdisc pfifo_fast 0: root refcnt 2 bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n",
            "qdisc fq_codel 0: root refcnt 2 limit 10240p flows 1024\n",
            "qdisc mq 0: root\n",
        ):
            assert self.imp.parse_scoped(qdisc, "").kind == "clean", qdisc

    def test_whole_link_delegates_to_v1_parser(self) -> None:
        out = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms  5ms loss 2%\n"
        state = self.imp.parse_scoped(out, "")
        assert state.kind == "whole"
        assert state.whole == ImpairmentParams(delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0)

    def test_scoped_two_selectors_roundtrip(self) -> None:
        state = self.imp.parse_scoped(QDISC_SCOPED, FILTER_SCOPED)
        assert state.kind == "scoped"
        assert state.selectors == {
            Selector(5201, "tcp"): (4, ImpairmentParams(delay_ms=200.0)),
            Selector(53, "udp"): (5, ImpairmentParams(loss_pct=5.0)),
        }

    def test_scoped_proto_none_selector_four_slots(self) -> None:
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
        )
        blocks = []
        for pref, proto_hex, port_match in (
            (40, "0006", "match 00001451/0000ffff at 20"),
            (41, "0006", "match 14510000/ffff0000 at 20"),
            (42, "0011", "match 00001451/0000ffff at 20"),
            (43, "0011", "match 14510000/ffff0000 at 20"),
        ):
            blocks.append(
                f"filter parent 1: protocol ip pref {pref} u32 fh 800::800 flowid 1:4\n"
                f"  match {proto_hex}0000/00ff0000 at 8\n"
                f"  {port_match}\n"
            )
        state = self.imp.parse_scoped(qdisc, "".join(blocks))
        assert state.kind == "scoped"
        assert state.selectors == {Selector(5201): (4, ImpairmentParams(delay_ms=200.0))}

    def test_empty_tree_is_clean_not_scoped(self) -> None:
        qdisc = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        assert self.imp.parse_scoped(qdisc, "").kind == "clean"

    def test_hex_band_ten_and_eleven(self) -> None:
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem a0: parent 1:a limit 1000 delay 1ms\n"
        )
        filt = (
            "filter parent 1: protocol ip pref 100 u32 fh 800::800 flowid 1:a\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00000050/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 101 u32 fh 801::800 flowid 1:a\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00500000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc, filt)
        assert state.kind == "scoped"
        assert state.selectors == {Selector(80, "tcp"): (10, ImpairmentParams(delay_ms=1.0))}

    def test_foreign_variants(self) -> None:
        ours_root = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        ok_filter = (
            "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00001451/0000ffff at 20\n"
        )
        cases = [
            # human htb root (nonzero handle, not ours)
            ("qdisc htb 8001: root refcnt 2 r2q 10\n", ""),
            # prio root with wrong bands
            ("qdisc prio 1: root refcnt 2 bands 4 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n", ""),
            # prio root with non-default priomap
            ("qdisc prio 1: root refcnt 2 bands 11 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n", ""),
            # non-netem child under our root
            (ours_root + "qdisc tbf 40: parent 1:4 rate 1Mbit\n", ok_filter),
            # netem child in a reserved band (1:1)
            (ours_root + "qdisc netem 10: parent 1:1 limit 1000 delay 5ms\n", ""),
            # handle/band mismatch (band 4 must be handle 40:)
            (ours_root + "qdisc netem 90: parent 1:4 limit 1000 delay 5ms\n", ok_filter),
            # band netem with NO filters (half-cleared tree)
            (ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n", ""),
            # filters with no netem leaf
            (ours_root, ok_filter),
            # slot/proto mismatch: pref 40 is the dport/tcp slot but matches udp
            (
                ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n",
                "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
                "  match 00110000/00ff0000 at 8\n"
                "  match 00001451/0000ffff at 20\n",
            ),
            # incomplete slot set: tcp selector with only its dport filter
            (
                ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n",
                ok_filter.replace("pref 41", "pref 99"),
            ),
        ]
        for qdisc, filt in cases:
            assert self.imp.parse_scoped(qdisc, filt).kind == "foreign", (qdisc, filt)

    def test_builder_parse_roundtrip(self) -> None:
        """What the builders emit, rendered as canned tc output, parses back equal."""
        sel = Selector(5201, "tcp")
        params = ImpairmentParams(delay_ms=200.0)
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            f"qdisc netem 40: parent 1:4 limit 1000 {netem_args(params)}\n"
        )
        filt = (
            "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00001451/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 41 u32 fh 801::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 14510000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc, filt)
        assert state.kind == "scoped"
        assert state.selectors == {sel: (4, params)}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_netem.py::TestParseScoped -v`
Expected: FAIL (`NotImplementedError` from the base class).

- [ ] **Step 3: Implement** — add to `src/otto/link/netem.py` (module level, above the class; the method delegates):

```python
_ROOT_MIN_TOKENS = 3
_PRIO_MINOR_MIN = FIRST_SELECTOR_BAND
_PRIO_MINOR_MAX = FIRST_SELECTOR_BAND + MAX_SELECTORS - 1  # 11


def _root_tokens(output: str) -> list[str] | None:
    """Tokens of the root-qdisc line in ``tc qdisc show`` output; ``None`` = no root line."""
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) >= _ROOT_MIN_TOKENS and tokens[0] == "qdisc" and "root" in tokens:
            return tokens
    return None


def _is_our_prio_root(tokens: list[str]) -> bool:
    """Exactly our generated root: ``prio 1:`` with 11 bands and the kernel-default priomap."""
    if tokens[1] != "prio" or tokens[2] != "1:":
        return False
    try:
        bands_i = tokens.index("bands")
        priomap_i = tokens.index("priomap")
    except ValueError:
        return False
    if tokens[bands_i + 1] != str(_SCOPED_BANDS):
        return False
    priomap = " ".join(tokens[priomap_i + 1 : priomap_i + 17])
    return priomap == _KERNEL_DEFAULT_PRIOMAP


def _parse_band_leaves(output: str) -> dict[int, ImpairmentParams] | None:
    """netem leaves under our root: ``{band: params}``; ``None`` = foreign artifact."""
    leaves: dict[int, ImpairmentParams] = {}
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < _ROOT_MIN_TOKENS or tokens[0] != "qdisc" or "root" in tokens:
            continue
        try:
            parent = tokens[tokens.index("parent") + 1]
        except ValueError:
            return None
        major, _, minor = parent.partition(":")
        if major != "1" or not minor:
            return None
        try:
            band = int(minor, 16)
        except ValueError:
            return None
        handle = tokens[2]
        if (
            tokens[1] != "netem"
            or not _PRIO_MINOR_MIN <= band <= _PRIO_MINOR_MAX
            or handle != f"{band:x}0:"
        ):
            return None
        leaves[band] = _parse_netem_tokens(tokens)
    return leaves


_MATCH_RE = re.compile(r"^match (?P<val>[0-9a-f]{8})/(?P<mask>[0-9a-f]{8}) at (?P<off>\d+)$")
_NUM_TO_PROTO = {6: "tcp", 17: "udp"}
_SLOT_COUNT = len(_SLOTS)


def _parse_filter_blocks(filter_output: str) -> list[tuple[int, int, list[tuple[str, str, int]]]] | None:
    """``(pref, flowid_band, [(val, mask, off), ...])`` per u32 block; ``None`` = foreign.

    Only ``filter parent 1: ... u32`` headers that carry a ``flowid`` open a
    block (the bare and ``ht divisor`` headers carry no matches). Any
    non-empty line that fits neither shape is foreign.
    """
    blocks: list[tuple[int, int, list[tuple[str, str, int]]]] = []
    current: list[tuple[str, str, int]] | None = None
    for raw in filter_output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("match "):
            m = _MATCH_RE.match(line)
            if m is None or current is None:
                return None
            current.append((m.group("val"), m.group("mask"), int(m.group("off"))))
            continue
        tokens = line.split()
        if tokens[0] != "filter" or "u32" not in tokens or "pref" not in tokens:
            return None
        current = None
        if "flowid" not in tokens:
            continue
        try:
            pref = int(tokens[tokens.index("pref") + 1])
            flowid = tokens[tokens.index("flowid") + 1]
        except ValueError:
            return None
        major, _, minor = flowid.partition(":")
        if major != "1" or not minor:
            return None
        try:
            band = int(minor, 16)
        except ValueError:
            return None
        current = []
        blocks.append((pref, band, current))
    return blocks


def _selector_from_slots(
    band: int, slots: dict[int, int]
) -> Selector | None:
    """Rebuild the band's Selector from ``{slot: port}``; ``None`` = not our shape."""
    if len(set(slots.values())) != 1:
        return None
    port = next(iter(slots.values()))
    present = frozenset(slots)
    proto_by_slots = {
        frozenset({0, 1, 2, 3}): None,
        frozenset({0, 1}): "tcp",
        frozenset({2, 3}): "udp",
    }
    if present not in proto_by_slots:
        return None
    try:
        return Selector(port, proto_by_slots[present])
    except ValueError:
        return None


def _decode_block(
    pref: int, band: int, matches: list[tuple[str, str, int]]
) -> tuple[int, int] | None:
    """Validate one u32 block against our conventions; return ``(slot, port)``."""
    slot = pref - band * 10
    if not 0 <= slot < _SLOT_COUNT or len(matches) != 2:
        return None
    side, proto = _SLOTS[slot]
    proto_match = next((m for m in matches if m[2] == 8), None)
    port_match = next((m for m in matches if m[2] == 20), None)
    if proto_match is None or port_match is None:
        return None
    val, mask, _ = proto_match
    if mask != "00ff0000" or (int(val, 16) >> 16) & 0xFF != _PROTO_NUM[proto]:
        return None
    val, mask, _ = port_match
    if side == "dport" and mask == "0000ffff":
        return slot, int(val, 16) & 0xFFFF
    if side == "sport" and mask == "ffff0000":
        return slot, int(val, 16) >> 16
    return None


def parse_scoped_outputs(qdisc_output: str, filter_output: str) -> ScopedState:
    """Parse the two :meth:`NetEmImpairer.scoped_read_commands` outputs.

    Only trees otto generated parse as ``scoped``; kernel-default roots
    (handle ``0:`` / ``noqueue``) are ``clean``; a root netem is ``whole``
    (the byte-identical v1 read-back); everything else is ``foreign``.
    An otherwise-ours root with zero leaves and zero filters is ``clean`` —
    a timer race that empties the tree must not wedge exclusivity.
    """
    root = _root_tokens(qdisc_output)
    if root is None or root[1] == "noqueue" or root[2] == "0:":
        return ScopedState.clean()
    if root[1] == "netem":
        params = parse_qdisc_show(qdisc_output)
        return ScopedState.whole_link(params) if params is not None else ScopedState.foreign()
    if not _is_our_prio_root(root):
        return ScopedState.foreign()
    leaves = _parse_band_leaves(qdisc_output)
    blocks = _parse_filter_blocks(filter_output)
    if leaves is None or blocks is None:
        return ScopedState.foreign()
    if not leaves and not blocks:
        return ScopedState.clean()
    slots_by_band: dict[int, dict[int, int]] = {}
    for pref, band, matches in blocks:
        decoded = _decode_block(pref, band, matches)
        if decoded is None or band in slots_by_band and decoded[0] in slots_by_band[band]:
            return ScopedState.foreign()
        slots_by_band.setdefault(band, {})[decoded[0]] = decoded[1]
    if set(slots_by_band) != set(leaves):
        return ScopedState.foreign()
    selectors: dict[Selector, tuple[int, ImpairmentParams]] = {}
    for band, slots in slots_by_band.items():
        selector = _selector_from_slots(band, slots)
        if selector is None or selector in selectors:
            return ScopedState.foreign()
        selectors[selector] = (band, leaves[band])
    return ScopedState.from_selectors(selectors)
```

Update the netem imports for `FIRST_SELECTOR_BAND`, `MAX_SELECTORS`, `ScopedState`, and add the method to `NetEmImpairer`:

```python
    @override
    def parse_scoped(self, qdisc_output: str, filter_output: str) -> ScopedState:
        """Parse :meth:`scoped_read_commands` outputs via :func:`parse_scoped_outputs`."""
        return parse_scoped_outputs(qdisc_output, filter_output)
```

Note `_parse_netem_tokens` on a `parent`ed line: it keyword-scans, so `parent`/`limit` tokens are skipped harmlessly — reuse as-is.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link/test_netem.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Typecheck round (first src cluster complete)**

Run: `make typecheck-python`
Expected: clean. Fix any `ty` findings (no `# ty: ignore` unless an existing pattern already uses one for the same construct).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/otto/link/netem.py tests/unit/link/test_netem.py
uv run ruff format --check src/otto/link/netem.py tests/unit/link/test_netem.py
git add src/otto/link/netem.py tests/unit/link/test_netem.py
git commit -m "feat(link): NetEm scoped read-back parser (u32 hex -> ScopedState)

Assisted-by: Claude (Fable 5)"
```

---

### Task 6: Manage — unified `ScopedState` reads, foreign refusal, bare-impair exclusivity, full-shape rollback

**Files:**
- Modify: `src/otto/link/manage.py`
- Test: `tests/unit/link/test_manage_impair.py`

**Interfaces:**
- Consumes: `ScopedState` (Task 2), `parse_scoped` (Task 5).
- Produces (manage-internal, used by Tasks 8/9): `_read_state(host, impairer, netdev) -> ScopedState`; `_restore_state(host, impairer, netdev, state) -> None`; `_RollbackEntry = tuple[Placement, Any, LinkImpairer, ScopedState]`. Public behavior added: bare `impair_link` raises `ValueError("link {id} has port-scoped impairments — repair them first or impair with --port")` on scoped state and `RuntimeError("{host}/{netdev} has a foreign qdisc otto did not create — refusing to modify it")` on foreign; `repair_link` raises the same foreign `RuntimeError`.
- Whole-link path stays byte-identical: same mutation commands, same verify semantics. The only read-path change: scoped-capable impairers now run BOTH `scoped_read_commands` (the qdisc-show bytes are unchanged; the filter read is additive).

- [ ] **Step 1: Extend the FakeHost** — in `tests/unit/link/test_manage_impair.py`, add a `filter_texts` queue mirroring `qdisc_texts`, dispatched on `tc filter show`:

In the `FakeHost` dataclass add the field:

```python
    filter_texts: list[str] = field(default_factory=lambda: [""])
```

and in `_result`, before the `tc qdisc show` branch:

```python
        if cmd.startswith("tc filter show"):
            text = self.filter_texts.pop(0) if len(self.filter_texts) > 1 else self.filter_texts[0]
            return CommandResult(status=Status.Success, value=text, command=cmd)
```

- [ ] **Step 2: Write the failing tests** — append to `tests/unit/link/test_manage_impair.py`:

```python
QDISC_SCOPED_ONE = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
)
FILTER_SCOPED_ONE = (
    "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter parent 1: protocol ip pref 41 u32 fh 801::800 flowid 1:4\n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
)
"""One selector, 5201/tcp delay 200ms, band 4 — the canned scoped read."""


class TestExclusivityAndForeign:
    @pytest.mark.asyncio
    async def test_bare_impair_against_scoped_state_is_loud(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        with pytest.raises(ValueError, match="has port-scoped impairments — repair them first"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert not carrot.sudo_commands  # refused BEFORE any mutation

    @pytest.mark.asyncio
    async def test_bare_impair_against_foreign_root_refuses(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        with pytest.raises(RuntimeError, match="foreign qdisc otto did not create"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_exclusivity_error_mid_link_rolls_back_first_placement(self) -> None:
        # carrot clean (applies fine), tomato scoped -> error; carrot restored to clean.
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["", DELAY_50_TEXT]
        tomato.qdisc_texts = [QDISC_SCOPED_ONE]
        tomato.filter_texts = [FILTER_SCOPED_ONE]
        with pytest.raises(ValueError, match="port-scoped impairments"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert carrot.sudo_commands[-1] == "tc qdisc del dev eth1.100 root"
        assert not tomato.sudo_commands
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_manage_impair.py::TestExclusivityAndForeign -v`
Expected: FAIL — today's code parses the prio/htb roots as `None` (clean) and happily mutates.

- [ ] **Step 4: Implement** — in `src/otto/link/manage.py`:

Update imports:

```python
from .impairer import LinkImpairer, ScopedState, build_impairer
from .params import ImpairmentParams, Selector, equivalent
```

REPLACE `_read_placement` with the unified read (keep the name `_read_placement` out — Tasks 8/9 use `_read_state`; delete `_read_placement` and update its call sites):

```python
async def _read_state(host: Any, impairer: LinkImpairer, netdev: str) -> ScopedState:
    """Read *netdev*'s full impairment shape on *host* as a :class:`ScopedState`.

    Scoped-capable impairers read qdisc + filters and discriminate all four
    kinds; legacy impairers keep their single-command read and can only ever
    report ``clean`` or ``whole`` (their read contract predates selectors).
    """
    if impairer.supports_selectors:
        qdisc_cmd, filter_cmd = impairer.scoped_read_commands(netdev)
        qdisc_out = (await _exec(host, qdisc_cmd)).value
        filter_out = (await _exec(host, filter_cmd)).value
        return impairer.parse_scoped(qdisc_out, filter_out)
    params = impairer.parse_read((await _exec(host, impairer.read_command(netdev))).value)
    return ScopedState.whole_link(params) if params is not None else ScopedState.clean()


def _ensure_not_foreign(host: Any, netdev: str, state: ScopedState) -> None:
    """A root qdisc otto did not generate is never mutated (spec §1)."""
    if state.kind == "foreign":
        raise RuntimeError(
            f"{host.id}/{netdev} has a foreign qdisc otto did not create — "
            "refusing to modify it (clear it manually with tc if it is expendable)"
        )
```

REPLACE `_RollbackEntry`, `_rollback`, and add `_restore_state`:

```python
_RollbackEntry = tuple[Placement, Any, LinkImpairer, ScopedState]


async def _restore_state(host: Any, impairer: LinkImpairer, netdev: str, state: ScopedState) -> None:
    """Rebuild *netdev* to exactly *state* (clean / whole params / full scoped mapping)."""
    if state.kind == "whole" and state.whole is not None:
        await _root_run(host, impairer.apply_command(netdev, state.whole))
        return
    await _root_run(host, impairer.clear_command(netdev))
    if state.kind != "scoped":
        return
    await _root_run(host, impairer.scoped_root_command(netdev))
    for selector, (band, params) in state.selectors.items():
        await _root_run(host, impairer.scoped_band_command(netdev, band, params))
        for cmd in impairer.scoped_filter_commands(netdev, band, selector):
            await _root_run(host, cmd)


async def _rollback(link_id: str, entries: list[_RollbackEntry]) -> None:
    """Best-effort restoration of already-applied placements after a mid-way failure.

    Restores in reverse application order to each placement's full pre-call
    shape — clean, whole-link params, or a complete scoped mapping. Any timer
    this run may have launched on the placement is cancelled first, matching
    the ordinary cancel-before-mutate invariant. One placement's restore
    failing must not stop the others from being attempted.
    """
    for placement, host, impairer, prior in reversed(entries):
        with contextlib.suppress(Exception):
            await _cancel_timers(host, link_id, placement.netdev)
            await _restore_state(host, impairer, placement.netdev, prior)
```

(Note: restoring a `clean` prior runs `clear_command` on a possibly-clean netdev; `tc qdisc del` then fails command-level, which `_root_run` deliberately ignores — today's exact posture.)

In `impair_link`, replace the per-placement body between `_cancel_timers` and `merged.validate()`:

```python
            await _cancel_timers(host, link.id, placement.netdev)
            state = await _read_state(host, impairer, placement.netdev)
            _ensure_not_foreign(host, placement.netdev, state)
            if state.kind == "scoped":
                raise ValueError(
                    f"link {link.id} has port-scoped impairments — "
                    "repair them first or impair with --port"
                )
            # Register the rollback entry BEFORE mutating: a verify or timer
            # failure on THIS placement must roll its own just-applied mutation
            # back too, not only the earlier placements' (final-review 2026-07-10).
            rollback_entries.append((placement, host, impairer, state))
            base = state.whole if state.whole is not None else ImpairmentParams()
            merged = params.merged_over(base)
            merged.validate()
```

and the post-apply verify re-read becomes:

```python
            observed_state = await _read_state(host, impairer, placement.netdev)
            observed = observed_state.whole
            expected = None if merged.is_empty() else merged
```

(the rest of the verify block is unchanged).

In `repair_link`, replace the read/clear body per placement:

```python
        timers_cancelled += await _cancel_timers(host, link.id, placement.netdev)
        state = await _read_state(host, impairer, placement.netdev)
        _ensure_not_foreign(host, placement.netdev, state)
        if state.kind != "clean":
            await _root_run(host, impairer.clear_command(placement.netdev))
            still = await _read_state(host, impairer, placement.netdev)
            if still.kind != "clean":
                raise RuntimeError(
                    f"repair failed on {host.id}/{placement.netdev}: impairment still present"
                )
            cleared.append(placement)
```

(A scoped netdev's bare repair is one root del — `clear_command` — per spec §2; the selector-targeted form is Task 9.)

In `_link_state`, replace the `_read_placement` call for now with a whole-only adapter (full `DirectionState` shape is Task 7):

```python
                state = await _read_state(host, impairer, placement.netdev)
                by_direction[placement.direction] = state.whole
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/unit/link -v`
Expected: ALL PASS — the new class AND every pre-existing test (rollback tests now restore via `_restore_state`, producing the same commands).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/otto/link/manage.py tests/unit/link/test_manage_impair.py
uv run ruff format --check src/otto/link/manage.py tests/unit/link/test_manage_impair.py
git add src/otto/link/manage.py tests/unit/link/test_manage_impair.py
git commit -m "feat(link): unified ScopedState reads, foreign refusal, scoped-vs-bare exclusivity

Assisted-by: Claude (Fable 5)"
```

---

### Task 7: `DirectionState` — the new read API shape (+ exports)

**Files:**
- Modify: `src/otto/link/manage.py`, `src/otto/link/__init__.py`
- Test: `tests/unit/link/test_manage_repair.py`, `tests/unit/link/test_lazy_exports.py`

**Interfaces:**
- Consumes: `_read_state` (Task 6).
- Produces: `DirectionState` dataclass; `LinkState.by_direction: dict[FlowDirection, DirectionState | None]` (`None` = that direction's host was unreachable — previously conflated with clean); package exports `Selector`, `ScopedState` (eager), `DirectionState` (lazy via manage). **Sanctioned breaking change** to the read API shape — `otto.cli.link` is fixed in Task 10; between Tasks 7 and 10 the CLI `list` unit tests would fail, so Task 10 must land before any whole-suite run (the plan's per-task runs are scoped to `tests/unit/link` minus `test_cli.py` until then; note it in the Task 7/8/9 run steps).

- [ ] **Step 1: Write the failing tests** — in `tests/unit/link/test_manage_repair.py`, REPLACE `TestReadStates` with:

```python
class TestReadStates:
    @pytest.mark.asyncio
    async def test_states_report_per_direction_whole_params(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert state.impairable
        assert not state.unreachable
        a = state.by_direction[FlowDirection.A_TO_B]
        b = state.by_direction[FlowDirection.B_TO_A]
        assert a is not None and a.whole == ImpairmentParams(delay_ms=50.0)
        assert a.scoped == {} and not a.foreign
        assert b is not None and b.whole is None and b.scoped == {} and not b.foreign

    @pytest.mark.asyncio
    async def test_states_report_scoped_selectors(self) -> None:
        from otto.link.manage import DirectionState
        from otto.link.params import Selector

        from .test_manage_impair import FILTER_SCOPED_ONE, QDISC_SCOPED_ONE

        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert state.by_direction[FlowDirection.A_TO_B] == DirectionState(
            whole=None, scoped={Selector(5201, "tcp"): ImpairmentParams(delay_ms=200.0)}
        )

    @pytest.mark.asyncio
    async def test_states_report_foreign_flag(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        a = state.by_direction[FlowDirection.A_TO_B]
        assert a is not None and a.foreign and a.whole is None and a.scoped == {}

    @pytest.mark.asyncio
    async def test_unimpairable_link_marked_not_error(self) -> None:
        bare = Link(
            a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare"
        )
        lab, *_ = _bed(link=bare)
        (state,) = await read_link_states(lab)
        assert not state.impairable

    @pytest.mark.asyncio
    async def test_unreachable_host_direction_is_none(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("down")

        carrot.exec = _boom  # type: ignore[method-assign]
        (state,) = await read_link_states(lab)
        assert state.unreachable
```

In `tests/unit/link/test_lazy_exports.py`, add `"DirectionState",` to the tuple in `test_manage_names_all_resolve` (alphabetical position: after `AppliedPlacement`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_manage_repair.py tests/unit/link/test_lazy_exports.py -v`
Expected: FAIL (`ImportError: DirectionState` / attribute assertions).

- [ ] **Step 3: Implement** — in `src/otto/link/manage.py`:

Add above `LinkState`:

```python
@dataclass(frozen=True, slots=True)
class DirectionState:
    """One direction's full impairment shape (the ``list``/GUI read feed).

    At most one of :attr:`whole` / :attr:`scoped` / :attr:`foreign` is
    populated (whole-link and port-scoped are exclusive per netdev in v1;
    a foreign tree is opaque). All three empty = clean.
    """

    whole: ImpairmentParams | None = None
    scoped: dict[Selector, ImpairmentParams] = dc_field(default_factory=dict)
    foreign: bool = False
```

Change `LinkState.by_direction`'s annotation and docstring:

```python
    by_direction: dict[FlowDirection, "DirectionState | None"] = dc_field(default_factory=dict)
    """Per-direction shape; ``None`` = that direction's host couldn't be read."""
```

In `_link_state`, replace the per-placement read with:

```python
            try:
                state = await _read_state(host, impairer, placement.netdev)
                by_direction[placement.direction] = DirectionState(
                    whole=state.whole,
                    scoped={sel: params for sel, (_band, params) in state.selectors.items()},
                    foreign=state.kind == "foreign",
                )
            except RuntimeError:
                unreachable = True
                by_direction[placement.direction] = None
```

and update the local annotation `by_direction: dict[FlowDirection, DirectionState | None] = {}`.

In `src/otto/link/__init__.py`: add `Selector` to the `.params` import line and `ScopedState` to the `.impairer` line; add `"Selector"`, `"ScopedState"`, `"DirectionState"` to `__all__` (sorted); add `"DirectionState"` to `_MANAGE_NAMES`; add `DirectionState` to the `TYPE_CHECKING` import from `.manage`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link --deselect tests/unit/link/test_cli.py -v`
Expected: ALL PASS. (`test_cli.py` still passes at this point — the CLI reads `by_direction.get(direction)` and only calls `.describe()` on it when truthy; a `DirectionState` is truthy but has no `describe` — CHECK: if `TestListCommand` fails here, that is expected breakage; leave it red only if so, it is fixed in Task 10. Record which it was in your task report.)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/otto/link tests/unit/link
uv run ruff format --check src/otto/link tests/unit/link
git add src/otto/link tests/unit/link
git commit -m "feat(link)!: DirectionState read-API shape (whole/scoped/foreign per direction)

Assisted-by: Claude (Fable 5)"
```

---

### Task 8: Manage — scoped impair apply path (bands, merge, verify, cap, capability, v2 timers)

**Files:**
- Modify: `src/otto/link/manage.py`
- Test: `tests/unit/link/test_manage_impair.py`

**Interfaces:**
- Consumes: everything above; `encode_impair_sentinel_v2`, `ImpairTimer` (Task 3); `FIRST_SELECTOR_BAND`, `MAX_SELECTORS` (Task 2); `launch_command`, `kill_command` (existing).
- Produces: `impair_link(lab, ident, params, *, from_host=None, expire=None, selector: Selector | None = None)`; `AppliedPlacement.selector: Selector | None = None` (new trailing field); `_cancel_timers(host, link_id, netdev, *, selector=None, everything=False) -> int` — the timer-cancellation scoping used by Task 9 too.

Timer-cancellation scoping (spec §1 + byte-identical constraint):

- `everything=True` → every v1 AND v2 timer for `(link_id, netdev)` (bare repair).
- `selector=None, everything=False` → v1 timers only (whole-link impair; scoped state can't hold v1 timers, so this is exactly today's semantics).
- `selector=S` → v2 timers whose selector equals S, only.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/link/test_manage_impair.py` (add to its imports: `from otto.link.params import ImpairmentParams, Selector` — adjust the existing params import — and `from otto.link.sentinel import encode_impair_sentinel_v2`):

```python
QDISC_SCOPED_TWO = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_SCOPED_TWO = FILTER_SCOPED_ONE + (
    "filter parent 1: protocol ip pref 52 u32 fh 802::800 flowid 1:5\n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter parent 1: protocol ip pref 53 u32 fh 803::800 flowid 1:5\n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)
"""5201/tcp (band 4, delay 200ms) + 53/udp (band 5, loss 5%)."""


class TestScopedImpair:
    @pytest.mark.asyncio
    async def test_first_selector_builds_root_band_filters(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", QDISC_SCOPED_ONE]
        carrot.filter_texts = ["", FILTER_SCOPED_ONE]
        report = await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=200.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert report.applied[0].selector == Selector(5201, "tcp")
        assert carrot.sudo_commands == [
            "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
            "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1",
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms",
            "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
            "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
            "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4",
        ]

    @pytest.mark.asyncio
    async def test_second_selector_takes_next_band_no_root_reissue(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, QDISC_SCOPED_TWO]
        carrot.filter_texts = [FILTER_SCOPED_ONE, FILTER_SCOPED_TWO]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=5.0),
            from_host="carrot_seed",
            selector=Selector(53, "udp"),
        )
        assert not any("prio bands" in c for c in carrot.sudo_commands)
        assert "tc qdisc replace dev eth1.100 parent 1:5 handle 50: netem loss 5%" in (
            carrot.sudo_commands
        )

    @pytest.mark.asyncio
    async def test_reimpair_merges_keeps_band_no_new_filters(self) -> None:
        merged_qdisc = QDISC_SCOPED_ONE.replace("delay 200ms", "delay 200ms loss 2%")
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, merged_qdisc]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=2.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert carrot.sudo_commands == [
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms loss 2%"
        ]

    @pytest.mark.asyncio
    async def test_selector_merged_to_empty_clears_that_selector(self) -> None:
        # zeroing the only param of the only selector -> full clear back to pristine
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=0.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert carrot.sudo_commands == ["tc qdisc del dev eth1.100 root"]

    @pytest.mark.asyncio
    async def test_scoped_against_whole_link_is_loud(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        with pytest.raises(ValueError, match="has a whole-link impairment — repair it first"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="carrot_seed",
                selector=Selector(5201),
            )
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_ninth_selector_hits_the_cap(self) -> None:
        bands = "".join(
            f"qdisc netem {b:x}0: parent 1:{b:x} limit 1000 delay 1ms\n" for b in range(4, 12)
        )
        filters = "".join(
            f"filter parent 1: protocol ip pref {b * 10} u32 fh 800::800 flowid 1:{b:x}\n"
            f"  match 00060000/00ff0000 at 8\n"
            f"  match {5000 + b:08x}/0000ffff at 20\n"
            f"filter parent 1: protocol ip pref {b * 10 + 1} u32 fh 801::800 flowid 1:{b:x}\n"
            f"  match 00060000/00ff0000 at 8\n"
            f"  match {(5000 + b) << 16:08x}/ffff0000 at 20\n"
            for b in range(4, 12)
        )
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            + bands
        ]
        carrot.filter_texts = [filters]
        with pytest.raises(ValueError, match="8 port-scoped impairments"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="carrot_seed",
                selector=Selector(9999, "tcp"),
            )
        # The cap error fires inside the mutation attempt, AFTER the rollback
        # entry is registered (same posture as a validate() failure today), so
        # a best-effort restore of the untouched prior mapping may run — but
        # nothing for the rejected selector may ever have been applied.
        assert not any("9999" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_capability_error_names_the_impairer(self) -> None:
        lab, carrot, _, _ = _bed()
        register_impairer("plainrec", _make_plain_recorder(), overwrite=True)
        carrot.impairer = "plainrec"
        with pytest.raises(ValueError, match="'plainrec' does not support port-scoped"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="carrot_seed",
                selector=Selector(80),
            )

    @pytest.mark.asyncio
    async def test_scoped_verify_mismatch_restores_full_prior_mapping(self) -> None:
        # prior: one selector; apply second; verify re-read shows nothing -> rollback
        # must rebuild the COMPLETE prior scoped mapping (root + band + filters).
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(loss_pct=5.0),
                from_host="carrot_seed",
                selector=Selector(53, "udp"),
            )
        restore = carrot.sudo_commands[-4:]
        assert restore == [
            "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
            "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1",
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms",
            "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
            "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
            "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4",
        ]
        # and the root was cleared before the rebuild
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands


def _make_plain_recorder():
    """A minimal legacy impairer class (supports_selectors stays False)."""
    from typing import ClassVar

    class _Plain(LinkImpairer):
        host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

        def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
            return f"PLAIN-APPLY {netdev}"

        def read_command(self, netdev: str) -> str:
            return f"PLAIN-READ {netdev}"

        def clear_command(self, netdev: str) -> str:
            return f"PLAIN-CLEAR {netdev}"

        def parse_read(self, output: str) -> ImpairmentParams | None:
            return None

    return _Plain


class TestScopedTimers:
    @pytest.mark.asyncio
    async def test_expire_launches_v2_timer_with_conditional_root_cleanup(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", QDISC_SCOPED_ONE]
        carrot.filter_texts = ["", FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=200.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
            expire=30,
        )
        launch = next(c for c in carrot.sudo_commands if "otto-impair:" in c)
        # LINK.id may percent-encode in the sentinel; assert the frame + payload
        # tail rather than interpolating the raw id (mirrors the v1 test).
        assert "otto-impair:v2:" in launch
        assert ":eth1.100:5201:tcp" in launch
        assert "sleep 30 && " in launch
        assert "tc filter del dev eth1.100 parent 1: pref 40 protocol ip u32" in launch
        assert "tc qdisc del dev eth1.100 parent 1:4 handle 40:" in launch
        assert (
            'if [ -z "$(tc filter show dev eth1.100 parent 1: 2>/dev/null || true)" ]; '
            "then tc qdisc del dev eth1.100 root; fi" in launch
        )
        assert launch.startswith("bash -c 'if command -v systemd-run")

    @pytest.mark.asyncio
    async def test_scoped_impair_cancels_only_its_selectors_v2_timer(self) -> None:
        v2_mine = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        v2_other = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(53, "udp"))
        lab, carrot, _, _ = _bed()
        carrot.ps_text = (
            f"  4242 05:00 {v2_mine} -c sleep 600\n  4243 05:00 {v2_other} -c sleep 600\n"
        )
        merged_qdisc = QDISC_SCOPED_ONE.replace("delay 200ms", "delay 200ms loss 2%")
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, merged_qdisc]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=2.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert "kill 4242" in carrot.sudo_commands
        assert not any("4243" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_whole_link_impair_does_not_cancel_v2_timers(self) -> None:
        # a v2 timer for another link's netdev-sharing selector must survive a
        # bare impair (which only owns v1 whole-link timers)
        v2 = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        lab, carrot, _, _ = _bed()
        carrot.ps_text = f"  4242 05:00 {v2} -c sleep 600\n"
        carrot.qdisc_texts = ["", DELAY_50_TEXT]
        carrot.filter_texts = [""]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert not any(c.startswith("kill") for c in carrot.sudo_commands)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_manage_impair.py -v`
Expected: new classes FAIL (`TypeError: impair_link() got an unexpected keyword argument 'selector'`); pre-existing classes PASS.

- [ ] **Step 3: Implement** — in `src/otto/link/manage.py`:

Imports: add `FIRST_SELECTOR_BAND`, `MAX_SELECTORS` to the `.impairer` import; sentinel import becomes:

```python
from .sentinel import IMPAIR_PS_COMMAND, encode_impair_sentinel, encode_impair_sentinel_v2, parse_impair_ps
```

`AppliedPlacement` gains a trailing field:

```python
    selector: Selector | None = None
    """Set when this was a port-scoped application (``--port``)."""
```

REPLACE `_cancel_timers`:

```python
async def _cancel_timers(
    host: Any,
    link_id: str,
    netdev: str,
    *,
    selector: Selector | None = None,
    everything: bool = False,
) -> int:
    """Kill live expire-timers for (*link_id*, *netdev*) on *host*, scoped.

    ``everything=True`` reaps every v1 AND v2 timer (bare repair). Otherwise
    ``selector=None`` matches only v1 whole-link timers (today's exact
    semantics — scoped state can't hold v1 timers, exclusivity guarantees
    it) and ``selector=S`` matches only S's own v2 timer. Best-effort: a
    scan failure returns 0 rather than raising — cancellation is a hygiene
    step, not the operation itself.
    """
    try:
        result = await _exec(host, IMPAIR_PS_COMMAND)
    except RuntimeError:
        return 0
    pids = [
        t.pid
        for t in parse_impair_ps(result.value)
        if t.link_id == link_id
        and t.netdev == netdev
        and (everything or t.selector == selector)
    ]
    if not pids:
        return 0
    await _root_run(host, kill_command(pids))
    return len(pids)
```

Update `_rollback`'s cancel call to `await _cancel_timers(host, link_id, placement.netdev, everything=True)` (a failed call may have launched either kind).

Add the scoped helpers after `_launch_timer`:

```python
def _assign_band(link_id: str, host: Any, netdev: str, state: ScopedState) -> int:
    """Lowest free selector band; a full tree is a loud cap error (spec §1)."""
    used = {band for band, _params in state.selectors.values()}
    for band in range(FIRST_SELECTOR_BAND, FIRST_SELECTOR_BAND + MAX_SELECTORS):
        if band not in used:
            return band
    raise ValueError(
        f"link {link_id} already has {MAX_SELECTORS} port-scoped impairments on "
        f"{host.id}/{netdev} (limit {MAX_SELECTORS}) — repair one first"
    )


def _ensure_selector_capable(host: Any, impairer: LinkImpairer) -> None:
    """--port routed to a non-supporting impairer is a loud capability error."""
    if not impairer.supports_selectors:
        name = getattr(host, "impairer", None) or type(impairer).__name__
        raise ValueError(
            f"impairer {name!r} does not support port-scoped impairment (--port); "
            f"host {host.id!r} needs a selector-capable impairer"
        )


async def _launch_selector_timer(
    host: Any,
    link: Link,
    placement: Placement,
    impairer: LinkImpairer,
    selector: Selector,
    band: int,
    expire: int,
) -> None:
    """Detached v2 timer clearing one selector after *expire* seconds.

    The timer can't know whether it will be the LAST selector when it fires,
    so the script ends with a conditional root cleanup: if no filters remain
    under the scoped root, delete the root — restoring pristine, per spec §2
    'clearing the last selector deletes the root'.
    """
    sentinel = encode_impair_sentinel_v2(link.id, placement.netdev, selector)
    clear_seq = " && ".join(
        impairer.scoped_clear_selector_commands(placement.netdev, band, selector)
    )
    filter_show = impairer.scoped_read_commands(placement.netdev)[1]
    root_del = impairer.clear_command(placement.netdev)
    script = (
        f"sleep {int(expire)} && {clear_seq} && "
        f'if [ -z "$({filter_show})" ]; then {root_del}; fi'
    )
    await _root_run(host, launch_command(sentinel, ["bash", "-c", script]))


def _expected_scoped_mapping(
    state: ScopedState, selector: Selector, merged: ImpairmentParams
) -> dict[Selector, ImpairmentParams]:
    """The post-mutation selector->params mapping the verify re-read must show."""
    expected = {sel: params for sel, (_band, params) in state.selectors.items()}
    if merged.is_empty():
        expected.pop(selector, None)
    else:
        expected[selector] = merged
    return expected


def _verify_scoped(
    host: Any,
    placement: Placement,
    expected: dict[Selector, ImpairmentParams],
    observed: ScopedState,
) -> None:
    """Post-apply verify for a scoped mutation: same selectors, equivalent params."""
    observed_map = {sel: params for sel, (_band, params) in observed.selectors.items()}
    ok = (
        (observed.kind == "scoped" or (observed.kind == "clean" and not expected))
        and set(observed_map) == set(expected)
        and all(equivalent(observed_map[sel], expected[sel]) for sel in expected)
    )
    if not ok:
        exp_text = ", ".join(f"{s.describe()} [{p.describe()}]" for s, p in expected.items()) or (
            "clean"
        )
        obs_text = ", ".join(
            f"{s.describe()} [{p.describe()}]" for s, p in observed_map.items()
        ) or observed.kind
        raise RuntimeError(
            f"post-apply verify failed on {host.id}/{placement.netdev}: "
            f"expected [{exp_text}], observed [{obs_text}]"
        )


async def _apply_selector(
    host: Any,
    link: Link,
    placement: Placement,
    impairer: LinkImpairer,
    state: ScopedState,
    selector: Selector,
    merged: ImpairmentParams,
) -> int | None:
    """One selector's mutation on one placement (state already exclusivity-checked).

    Returns the band the selector landed in, or ``None`` when the call was a
    clear (merged-to-empty). The caller launches any expire timer AFTER its
    own verify succeeds — the fresh-timer-only-after-verify invariant is
    today's rule, unchanged.
    """
    netdev = placement.netdev
    prior = state.selectors.get(selector)
    if merged.is_empty():
        if prior is None:
            return None
        if len(state.selectors) == 1:
            await _root_run(host, impairer.clear_command(netdev))
        else:
            for cmd in impairer.scoped_clear_selector_commands(netdev, prior[0], selector):
                await _root_run(host, cmd)
        return None
    band = prior[0] if prior is not None else _assign_band(link.id, host, netdev, state)
    if state.kind == "clean":
        await _root_run(host, impairer.scoped_root_command(netdev))
    await _root_run(host, impairer.scoped_band_command(netdev, band, merged))
    if prior is None:
        for cmd in impairer.scoped_filter_commands(netdev, band, selector):
            await _root_run(host, cmd)
    return band
```

REWRITE `impair_link`'s per-placement loop (the whole `try:` body) to branch on selector:

```python
    link = find_link(lab, ident)
    directions = _directions(link, from_host)
    placements = await _resolve_placements(lab, link, directions)

    applied: list[AppliedPlacement] = []
    rollback_entries: list[_RollbackEntry] = []
    try:
        for placement in placements:
            host = _host(lab, placement.host_id)
            impairer = _impairer_for(host)
            if selector is not None:
                _ensure_selector_capable(host, impairer)
            await _cancel_timers(host, link.id, placement.netdev, selector=selector)
            state = await _read_state(host, impairer, placement.netdev)
            _ensure_not_foreign(host, placement.netdev, state)
            if selector is None and state.kind == "scoped":
                raise ValueError(
                    f"link {link.id} has port-scoped impairments — "
                    "repair them first or impair with --port"
                )
            if selector is not None and state.kind == "whole":
                raise ValueError(
                    f"link {link.id} has a whole-link impairment — repair it first"
                )
            # Register the rollback entry BEFORE mutating: a verify or timer
            # failure on THIS placement must roll its own just-applied mutation
            # back too, not only the earlier placements' (final-review 2026-07-10).
            rollback_entries.append((placement, host, impairer, state))
            if selector is not None:
                prior_entry = state.selectors.get(selector)
                base = prior_entry[1] if prior_entry is not None else ImpairmentParams()
                merged = params.merged_over(base)
                merged.validate()
                band = await _apply_selector(
                    host, link, placement, impairer, state, selector, merged
                )
                expected_map = _expected_scoped_mapping(state, selector, merged)
                observed_state = await _read_state(host, impairer, placement.netdev)
                _verify_scoped(host, placement, expected_map, observed_state)
                if expire is not None and band is not None:
                    await _launch_selector_timer(
                        host, link, placement, impairer, selector, band, expire
                    )
                applied.append(AppliedPlacement(placement, merged, selector))
                continue
            base = state.whole if state.whole is not None else ImpairmentParams()
            merged = params.merged_over(base)
            merged.validate()
            await _apply_or_clear(host, impairer, placement.netdev, merged)
            observed_state = await _read_state(host, impairer, placement.netdev)
            observed = observed_state.whole
            expected = None if merged.is_empty() else merged
            # tc canonicalizes on display, so `observed` may spell the same
            # impairment differently than `expected`; compare by MEANING.
            observed_params = observed if observed is not None else ImpairmentParams()
            expected_params = expected if expected is not None else ImpairmentParams()
            if not equivalent(observed_params, expected_params):
                _raise_verify_mismatch(host, placement, expected, observed)
            if expire is not None:
                await _launch_timer(host, link, placement, impairer, expire)
            applied.append(AppliedPlacement(placement, merged))
    except Exception:
        await _rollback(link.id, rollback_entries)
        raise
    return ImpairReport(link.id, applied)
```

Update `impair_link`'s signature and extend its docstring with the selector semantics (exclusivity, merge-per-selector, cap, v2 timers):

```python
async def impair_link(
    lab: "Lab",
    ident: str,
    params: ImpairmentParams,
    *,
    from_host: str | None = None,
    expire: int | None = None,
    selector: Selector | None = None,
) -> ImpairReport:
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link/test_manage_impair.py tests/unit/link/test_manage_repair.py -v`
Expected: ALL PASS, including every pre-existing whole-link byte golden.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/otto/link/manage.py tests/unit/link/test_manage_impair.py
uv run ruff format --check src/otto/link/manage.py tests/unit/link/test_manage_impair.py
git add src/otto/link/manage.py tests/unit/link/test_manage_impair.py
git commit -m "feat(link): port-scoped impair orchestration (bands, exclusivity, cap, v2 timers)

Assisted-by: Claude (Fable 5)"
```

---

### Task 9: Manage — scoped repair path

**Files:**
- Modify: `src/otto/link/manage.py`
- Test: `tests/unit/link/test_manage_repair.py`

**Interfaces:**
- Consumes: Task 8's `_cancel_timers` scoping, `_apply_selector`-style clears via `scoped_clear_selector_commands`.
- Produces: `repair_link(lab, ident, *, selector: Selector | None = None) -> RepairReport`. Bare repair: cancels EVERY timer (v1+v2) per placement, one root del clears whole OR scoped state (Task 6 already did this), verified clean. Selector repair: cancels only that selector's v2 timers; clears just that selector (root del when it is the last); selector-vs-whole is a loud `ValueError("link {id} has a whole-link impairment — repair it without --port")`; a selector that isn't present clears nothing (not an error — matches bare repair's nothing-to-clear posture).

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/link/test_manage_repair.py` (imports: `Selector`, `encode_impair_sentinel_v2`, and the canned texts from `test_manage_impair`):

```python
from otto.link.params import Selector
from otto.link.sentinel import encode_impair_sentinel_v2

from .test_manage_impair import (
    FILTER_SCOPED_ONE,
    FILTER_SCOPED_TWO,
    QDISC_SCOPED_ONE,
    QDISC_SCOPED_TWO,
)


class TestScopedRepair:
    @pytest.mark.asyncio
    async def test_bare_repair_clears_scoped_tree_and_all_timers(self) -> None:
        v1 = encode_impair_sentinel(LINK.id, "eth1.100")
        v2 = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        lab, carrot, tomato, _ = _bed()
        carrot.ps_text = f"  4242 05:00 {v1} -c sleep 600\n  4243 05:00 {v2} -c sleep 600\n"
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge")
        assert "kill 4242 4243" in carrot.sudo_commands
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert report.timers_cancelled == 2

    @pytest.mark.asyncio
    async def test_selector_repair_clears_one_of_two(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_TWO, QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_TWO, FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge", selector=Selector(53, "udp"))
        assert carrot.sudo_commands == [
            "tc filter del dev eth1.100 parent 1: pref 52 protocol ip u32",
            "tc filter del dev eth1.100 parent 1: pref 53 protocol ip u32",
            "tc qdisc del dev eth1.100 parent 1:5 handle 50:",
        ]
        assert [p.netdev for p in report.cleared] == ["eth1.100"]

    @pytest.mark.asyncio
    async def test_selector_repair_of_last_selector_deletes_root(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        tomato.qdisc_texts = [""]
        await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
        assert carrot.sudo_commands == ["tc qdisc del dev eth1.100 root"]

    @pytest.mark.asyncio
    async def test_selector_repair_cancels_only_matching_v2_timer(self) -> None:
        mine = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        other = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(53, "udp"))
        lab, carrot, tomato, _ = _bed()
        carrot.ps_text = f"  4242 05:00 {mine} -c x\n  4243 05:00 {other} -c x\n"
        # post-clear re-read: only 53/udp (band 5) remains
        qdisc_53_only = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
        )
        filter_53_only = (
            "filter parent 1: protocol ip pref 52 u32 fh 802::800 flowid 1:5\n"
            "  match 00110000/00ff0000 at 8\n"
            "  match 00000035/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 53 u32 fh 803::800 flowid 1:5\n"
            "  match 00110000/00ff0000 at 8\n"
            "  match 00350000/ffff0000 at 20\n"
        )
        carrot.qdisc_texts = [QDISC_SCOPED_TWO, qdisc_53_only]
        carrot.filter_texts = [FILTER_SCOPED_TWO, filter_53_only]
        tomato.qdisc_texts = [""]
        await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
        assert "kill 4242" in carrot.sudo_commands
        assert not any("4243" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_selector_repair_against_whole_link_is_loud(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        tomato.qdisc_texts = [""]
        with pytest.raises(ValueError, match="repair it without --port"):
            await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_selector_repair_absent_selector_clears_nothing(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge", selector=Selector(9999, "tcp"))
        assert report.cleared == []
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_selector_clear_that_does_not_take_raises(self) -> None:
        lab, carrot, tomato, _ = _bed()
        # single-element queues: state unchanged after the clear commands
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        with pytest.raises(RuntimeError, match=r"repair failed on carrot_seed/eth1\.100"):
            await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_manage_repair.py -v`
Expected: new class FAILS (`TypeError: repair_link() got an unexpected keyword argument 'selector'`).

- [ ] **Step 3: Implement** — REPLACE `repair_link` in `src/otto/link/manage.py`:

```python
async def repair_link(
    lab: "Lab", ident: str, *, selector: Selector | None = None
) -> RepairReport:
    """Clear link *ident*'s impairment state and cancel its timers.

    Bare (``selector=None``): clears EVERYTHING per placement that has any
    otto state — whole-link or the entire scoped tree, each a single root
    delete — and cancels every v1 and v2 timer. With *selector*: clears just
    that selector (deleting the root when it is the last one) and cancels
    only its own v2 timer; a selector that isn't present clears nothing.

    Every clear is verified by a post-clear re-read: a clear that silently
    didn't take is a loud, host-named failure, never reported as ``cleared``.
    """
    link = find_link(lab, ident)
    directions = _directions(link, None)
    placements = await _resolve_placements(lab, link, directions)

    cleared: list[Placement] = []
    timers_cancelled = 0
    for placement in placements:
        host = _host(lab, placement.host_id)
        impairer = _impairer_for(host)
        if selector is not None:
            _ensure_selector_capable(host, impairer)
        timers_cancelled += await _cancel_timers(
            host, link.id, placement.netdev, selector=selector, everything=selector is None
        )
        state = await _read_state(host, impairer, placement.netdev)
        _ensure_not_foreign(host, placement.netdev, state)
        if selector is None:
            if state.kind == "clean":
                continue
            await _root_run(host, impairer.clear_command(placement.netdev))
            still = await _read_state(host, impairer, placement.netdev)
            if still.kind != "clean":
                raise RuntimeError(
                    f"repair failed on {host.id}/{placement.netdev}: impairment still present"
                )
            cleared.append(placement)
            continue
        if state.kind == "whole":
            raise ValueError(
                f"link {link.id} has a whole-link impairment — repair it without --port"
            )
        entry = state.selectors.get(selector)
        if entry is None:
            continue
        if len(state.selectors) == 1:
            await _root_run(host, impairer.clear_command(placement.netdev))
        else:
            for cmd in impairer.scoped_clear_selector_commands(
                placement.netdev, entry[0], selector
            ):
                await _root_run(host, cmd)
        still = await _read_state(host, impairer, placement.netdev)
        if selector in still.selectors or still.kind in ("whole", "foreign"):
            raise RuntimeError(
                f"repair failed on {host.id}/{placement.netdev}: impairment still present"
            )
        cleared.append(placement)
    return RepairReport(link.id, cleared, timers_cancelled)
```

(`repair_all` needs no change — it calls the bare form.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link -v` (test_cli.py may be red per Task 7's note — everything else must pass)

- [ ] **Step 5: Typecheck round (manage complete)**

Run: `make typecheck-python`
Expected: clean.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check src/otto/link/manage.py tests/unit/link/test_manage_repair.py
uv run ruff format --check src/otto/link/manage.py tests/unit/link/test_manage_repair.py
git add src/otto/link/manage.py tests/unit/link/test_manage_repair.py
git commit -m "feat(link): per-selector repair with scoped timer cancellation

Assisted-by: Claude (Fable 5)"
```

---

### Task 10: CLI — `--port`/`--proto` on impair + repair, per-selector `list` rows

**Files:**
- Modify: `src/otto/cli/link.py`
- Test: `tests/unit/link/test_cli.py`

**Interfaces:**
- Consumes: `Selector`, `DirectionState`, new `impair_link`/`repair_link` keywords, `AppliedPlacement.selector`.
- Produces: user-facing CLI. Usage rules: `--proto` without `--port` = exit 2; `--proto` outside tcp/udp = exit 2; bad `--port` range = exit 2 (typer `min=1, max=65535`). `repair --all` with `--port` = exit 2 (selector repair is per-link).

Rendering (pin these exact shapes):

- impair report row, scoped: `impaired lnk-x a->b on carrot_seed/eth1.100: 5201/tcp delay 200ms` (selector prefix before the params, space-separated).
- list link row summary text per direction: whole → `params.describe()` (unchanged); clean → `-`; unreachable → `?`; foreign → `foreign qdisc — not otto's`; scoped → `port-scoped (N)`.
- list selector sub-rows, one per selector under the link row, a→b before b→a, selectors sorted by `(port, proto or "")`:
  `  a->b  5201/tcp  delay 200ms`  (two leading spaces, two-space column gaps).

- [ ] **Step 1: Write the failing tests** — in `tests/unit/link/test_cli.py`, update the `LinkState` construction sites for the `DirectionState` shape and add:

Replace `TestListCommand.test_rows_and_partial_scan_warning`'s states with:

```python
        from otto.link import DirectionState

        state = LinkState(
            link=LINK,
            impairable=True,
            unreachable=False,
            by_direction={
                FlowDirection.A_TO_B: DirectionState(whole=ImpairmentParams(delay_ms=50.0)),
                FlowDirection.B_TO_A: DirectionState(),
            },
        )
        down = LinkState(
            link=INPATH,
            impairable=True,
            unreachable=True,
            by_direction={FlowDirection.A_TO_B: None, FlowDirection.B_TO_A: None},
        )
```

(assertions unchanged: `"delay 50ms"` and `"partial scan"`). Then append:

```python
from otto.link import DirectionState, Selector


class TestScopedCli:
    def test_impair_with_port_passes_selector(self) -> None:
        report = ImpairReport(link_id="lnk-abc", applied=[])
        mock = AsyncMock(return_value=report)
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", mock),
        ):
            result = runner.invoke(
                link_app,
                ["impair", "edge", "--delay", "200", "--port", "5201", "--proto", "tcp"],
            )
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["selector"] == Selector(5201, "tcp")

    def test_impair_report_row_includes_selector(self) -> None:
        report = ImpairReport(
            link_id="lnk-abc",
            applied=[
                AppliedPlacement(
                    Placement("carrot_seed", "eth1.100", FlowDirection.A_TO_B),
                    ImpairmentParams(delay_ms=200.0),
                    Selector(5201, "tcp"),
                ),
            ],
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(
                link_app, ["impair", "edge", "--delay", "200", "--port", "5201"]
            )
        assert "carrot_seed/eth1.100: 5201/tcp delay 200ms" in result.output

    def test_proto_without_port_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--delay", "1", "--proto", "tcp"])
        assert result.exit_code == 2
        assert "--proto needs --port" in result.output

    def test_bad_proto_is_usage_error(self) -> None:
        result = runner.invoke(
            link_app, ["impair", "edge", "--delay", "1", "--port", "80", "--proto", "icmp"]
        )
        assert result.exit_code == 2

    def test_repair_with_port_passes_selector(self) -> None:
        from otto.link import RepairReport

        mock = AsyncMock(return_value=RepairReport("lnk-abc"))
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_link", mock),
        ):
            result = runner.invoke(link_app, ["repair", "edge", "--port", "53", "--proto", "udp"])
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["selector"] == Selector(53, "udp")

    def test_repair_all_with_port_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["repair", "--all", "--port", "53"])
        assert result.exit_code == 2

    def test_list_renders_selector_rows_and_foreign(self) -> None:
        scoped = LinkState(
            link=LINK,
            impairable=True,
            unreachable=False,
            by_direction={
                FlowDirection.A_TO_B: DirectionState(
                    scoped={
                        Selector(5201, "tcp"): ImpairmentParams(delay_ms=200.0),
                        Selector(53, "udp"): ImpairmentParams(loss_pct=5.0),
                    }
                ),
                FlowDirection.B_TO_A: DirectionState(foreign=True),
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[scoped])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: port-scoped (2)" in result.output
        assert "b->a: foreign qdisc — not otto's" in result.output
        assert "  a->b  53/udp  loss 5%" in result.output
        assert "  a->b  5201/tcp  delay 200ms" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_cli.py -v`
Expected: FAIL (unknown `--port` option, `DirectionState` rendering).

- [ ] **Step 3: Implement** — in `src/otto/cli/link.py`:

Add `DirectionState`, `Selector` to the `..link` import. In `impair`, add options after `reorder`:

```python
    port: int | None = typer.Option(
        None,
        "--port",
        min=1,
        max=65535,
        help="Scope to one service port (matches source OR dest; see the guide).",
    ),
    proto: str | None = typer.Option(
        None, "--proto", help="With --port: narrow to tcp or udp (default: both)."
    ),
```

After the existing no-params usage check, build the selector (usage errors OUT of the library try/except, same typer.Exit rationale as the existing comment):

```python
    if proto is not None and port is None:
        rprint("[red]--proto needs --port.[/red]")
        raise typer.Exit(2)
    selector: Selector | None = None
    if port is not None:
        try:
            selector = Selector(port, proto)
        except ValueError as e:
            rprint(f"[red]{e}[/red]")
            raise typer.Exit(2) from e
```

and pass `selector=selector` to `impair_link`. Update `_print_impair_report`:

```python
def _print_impair_report(report: ImpairReport) -> None:
    for applied in report.applied:
        placement = applied.placement
        desc = applied.params.describe() or "cleared"
        if applied.selector is not None:
            desc = f"{applied.selector.describe()} {desc}"
        rprint(
            f"[green]impaired[/green] {report.link_id} {placement.direction.value} "
            f"on {placement.host_id}/{placement.netdev}: {desc}"
        )
```

In `repair`, add the same two options, the same `--proto needs --port` and `Selector` construction (exit 2), plus:

```python
    if all_ and port is not None:
        rprint("[red]--port repairs one selector on one link; it cannot combine with --all.[/red]")
        raise typer.Exit(2)
```

and pass `selector=selector` to `repair_link`.

Replace `_dir_text` and the `list_links` rendering:

```python
def _dir_text(state: LinkState, direction: FlowDirection) -> str:
    dstate = state.by_direction.get(direction)
    if dstate is None:
        return "?" if state.unreachable else "-"
    if dstate.foreign:
        return "foreign qdisc — not otto's"
    if dstate.scoped:
        return f"port-scoped ({len(dstate.scoped)})"
    if dstate.whole is not None:
        return dstate.whole.describe()
    return "-"


def _selector_rows(state: LinkState) -> list[str]:
    """One indented row per selector, a->b first, sorted by (port, proto)."""
    rows: list[str] = []
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        dstate = state.by_direction.get(direction)
        if dstate is None or not dstate.scoped:
            continue
        rows.extend(
            f"  {direction.value}  {sel.describe()}  {params.describe()}"
            for sel, params in sorted(
                dstate.scoped.items(), key=lambda kv: (kv[0].port, kv[0].proto or "")
            )
        )
    return rows
```

and inside the `for state in states:` loop, after the existing link-row `rprint`, add:

```python
        for row in _selector_rows(state):
            rprint(row)
```

CHECK `FlowDirection.A_TO_B.value` — confirm in `src/otto/link/placement.py` that the enum values are the `a->b`/`b->a` strings the existing impair-report row already prints; reuse whatever the existing code uses.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/link -v`
Expected: ALL PASS — including anything left red from Task 7.

- [ ] **Step 5: Typecheck + lint + commit**

```bash
make typecheck-python
uv run ruff check src/otto/cli/link.py tests/unit/link/test_cli.py
uv run ruff format --check src/otto/cli/link.py tests/unit/link/test_cli.py
git add src/otto/cli/link.py tests/unit/link/test_cli.py
git commit -m "feat(cli): otto link impair/repair --port/--proto + per-selector list rows

Assisted-by: Claude (Fable 5)"
```

---

### Task 11: Docs — "Port-scoped impairments" guide section

**Files:**
- Modify: `docs/guide/link.md` (new `## Port-scoped impairments` section between `## In-path impairment` and `## Repairing: otto link repair`; small updates to the repair/list/Python API sections)

**Interfaces:**
- Consumes: final CLI/API surface from Tasks 8–10.

- [ ] **Step 1: Write the section** — cover, with runnable command examples in the guide's existing style:

- `otto link impair edge --port 5201 --delay 200` — selector semantics: matches source OR destination port (otto never asks which side is the server); `--proto tcp|udp` narrows, omitted = both; without `--port` nothing changes vs. today.
- Exclusivity rule (v1): whole-link and port-scoped never mix on a link; the two exact error messages and their remedies.
- Multiple selectors: independent params, per-selector merge/zero-clear (same rules as whole-link, applied per selector), the cap of 8, `--expire` per selector.
- `otto link repair edge --port 5201 [--proto tcp]` clears one selector; bare repair clears everything.
- `otto link list` selector rows + `foreign qdisc — not otto's` rows (and that otto refuses to mutate or repair foreign trees — clear those manually with tc).
- Mechanism note (short): prio bands + u32 filters inside the NetEm impairer; unmatched traffic behaves exactly as with no qdisc; kernel state remains the only state.
- The documented u32 caveat: `match ip dport/sport` assumes a 20-byte IP header (no IP options) and non-fragmented packets — acceptable for lab traffic.
- Custom impairers: the scoped surface is optional (`supports_selectors`), `--port` against a non-supporting impairer is a capability error.
- Python API paragraph: `impair_link(..., selector=Selector(5201, "tcp"))`, `repair_link(..., selector=...)`, `DirectionState` in `read_link_states` results.

- [ ] **Step 2: Update stale statements** — in the same file: the repair section ("bare clears everything" now has a `--port` variant), the list section's row format, and the Python API section's result-type list (add `Selector`, `DirectionState`, `ScopedState`).

- [ ] **Step 3: Build gate**

Run: `make docs`
Expected: clean (nitpicky Sphinx — every ``:class:``/``:func:`` reference in the new docstrings must resolve; `docs/api/link.rst` automodule picks the new names up automatically, but if `Selector`/`ScopedState`/`DirectionState` end up double-documented, add them to the existing `:exclude-members:` list in `docs/api/link.rst` following the pattern already there).

- [ ] **Step 4: Commit**

```bash
git add docs/guide/link.md docs/api/link.rst
git commit -m "docs(link): port-scoped impairment guide section

Assisted-by: Claude (Fable 5)"
```

---

### Task 12: Live-bed fixture capture — validate the parser against real tc bytes

**Goal:** the spec requires parser fixtures **captured from the live bed** (modern iproute2) and the old-format posture checked (centos:7-era iproute2). This task proves the hand-modeled fixtures in Tasks 5/8 against reality and fixes any byte-level drift (hex vs decimal minors, priomap spacing, `not_in_hw` presence, `tc filter del` semantics, handle-`0:` default-qdisc assumption, `tc filter show parent 1:` failure mode on a clean netdev).

**Files:**
- Modify (as needed): `tests/unit/link/test_netem.py` fixture strings, `src/otto/link/netem.py` parser tolerances
- Scratch: capture script output under the session scratchpad, NOT committed.

**Live-bed rules:** carrot_seed=10.10.200.11 (test1). Use a throwaway VLAN device so the mgmt path is never touched. NEVER leave state behind. Do not power VMs. If a VM is down, STOP and report the host name — do not skip.

- [ ] **Step 1: Capture modern-format outputs on carrot** — run via ssh (`vagrant@10.10.200.11`, key auth as the e2e uses; simplest is a small python script using `otto.host.unix_host` + `tests._fixtures.labdata.make_host`, or plain `ssh`). Commands, in order, ALL via sudo:

```bash
ip link add link eth1 name eth1.240 type vlan id 240
ip link set eth1.240 up
tc qdisc replace dev eth1.240 root handle 1: prio bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1
tc qdisc replace dev eth1.240 parent 1:4 handle 40: netem delay 200ms
tc filter add dev eth1.240 parent 1: pref 40 protocol ip u32 match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4
tc filter add dev eth1.240 parent 1: pref 41 protocol ip u32 match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4
tc qdisc replace dev eth1.240 parent 1:5 handle 50: netem loss 5%
tc filter add dev eth1.240 parent 1: pref 52 protocol ip u32 match ip protocol 17 0xff match ip dport 53 0xffff flowid 1:5
tc filter add dev eth1.240 parent 1: pref 53 protocol ip u32 match ip protocol 17 0xff match ip sport 53 0xffff flowid 1:5
# captures:
tc qdisc show dev eth1.240                                  # -> fixture A
tc filter show dev eth1.240 parent 1:                       # -> fixture B
# selector clear semantics check:
tc filter del dev eth1.240 parent 1: pref 52 protocol ip u32
tc filter del dev eth1.240 parent 1: pref 53 protocol ip u32
tc qdisc del dev eth1.240 parent 1:5 handle 50:
tc filter show dev eth1.240 parent 1:                       # -> fixture C (only 5201/tcp remains)
# pristine + default-qdisc-handle checks:
tc qdisc del dev eth1.240 root
tc qdisc show dev eth1.240                                  # -> fixture D (the clean shape; confirm handle "0:" or noqueue)
tc filter show dev eth1.240 parent 1: ; echo "exit=$?"      # -> confirm failure mode on clean netdev
# teardown (MANDATORY, even on error):
ip link del eth1.240
```

Every command's stdout/stderr goes to a scratchpad log. If ANY tc command errors unexpectedly, capture the error, still run the teardown, and STOP to analyze — the builders may need adjusting (that is this task's purpose).

- [ ] **Step 2: Old-userland capture (best-effort)** — pepper_seed (10.10.200.13) is docker-capable and the repo builds a centos:7 image (`tests/repo2/docker/oldos/Dockerfile`). Attempt:

```bash
docker run --rm --cap-add=NET_ADMIN <oldos-image> sh -c 'yum -y -q install iproute 2>/dev/null; ip link add name d0 type dummy; tc qdisc replace dev d0 root handle 1: prio bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1; tc qdisc replace dev d0 parent 1:4 handle 40: netem delay 200.0ms; tc filter add dev d0 parent 1: pref 40 protocol ip u32 match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4; tc qdisc show dev d0; echo ---; tc filter show dev d0 parent 1:'
```

If the image is unavailable/unbuildable or `iproute` cannot install (air-gapped vault repo issues), FALL BACK cleanly: keep the hand-modeled old-format tolerance (the parser is token-order tolerant and ignores `chain`/`not_in_hw`) and record in the task report + a code comment on `_parse_filter_blocks` that old-format bytes were modeled, not captured — the existing dual-format posture for netem params already has live-verified coverage.

- [ ] **Step 3: Reconcile** — diff fixtures A/B/C/D against `QDISC_SCOPED`/`FILTER_SCOPED`/etc. in the unit tests. Update the TEST FIXTURE STRINGS to the captured bytes verbatim (add a `# captured live on the veggies bed, iproute2 <version>, 2026-07-11` provenance comment). If the parser rejects real bytes, fix the PARSER (never weaken a foreign-detection rule without noting why). If `tc filter del ... pref N protocol ip u32` or `tc qdisc del ... parent 1:5 handle 50:` had wrong syntax live, fix the Task 4 builder goldens accordingly (builders + tests together).

- [ ] **Step 4: Re-run + commit**

```bash
uv run pytest tests/unit/link -v
git add tests/unit/link/test_netem.py src/otto/link/netem.py
git commit -m "test(link): scoped read-back fixtures captured from the live bed

Assisted-by: Claude (Fable 5)"
```

(Commit only if something changed; if fixtures matched reality byte-for-byte, record that in the task report and skip the commit.)

---

### Task 13: Live-bed e2e — scoped impairment end to end

**Files:**
- Modify: `tests/e2e/test_link_impair_e2e.py`

**Interfaces:**
- Consumes: the full library surface; the module's existing `impair_lab` fixture, `_avg_rtt_ms`, hygiene sweep (extend the sweep: also assert no leftover prio root on the VLAN devices is NOT needed — the sweep already checks VLAN devices are deleted entirely).
- Live-bed rules: single `xdist_group` (already set module-wide), fail loud on host-down, guaranteed teardown via `try/finally` + the existing `repair_all` in the fixture finalizer, NEVER kill a wedged run at a tight timeout.

Add these tests to the existing module (constants at module level near the existing ones):

```python
_SCOPED_PORT = 5201
_CLEAN_PORT = 5202
_SCOPED_DELAY_MS = 200.0
_TCP_DELTA_MIN_MS = 150.0
```

- [ ] **Step 1: Test 6 — scoped read-back equivalence + repair to pristine**

```python
@pytest.mark.asyncio(loop_scope="module")
async def test_scoped_impair_readback_and_pristine_repair(impair_lab: Lab) -> None:
    """A scoped impair round-trips through kernel read-back by MEANING, and a
    bare repair returns the netdev to pristine (no root qdisc artifacts)."""
    from otto.link import DirectionState, Selector

    carrot = impair_lab.hosts[_CARROT]
    sel = Selector(_SCOPED_PORT, "tcp")
    report = await impair_link(
        impair_lab,
        "edge",
        ImpairmentParams(delay_ms=_SCOPED_DELAY_MS),
        from_host=_CARROT,
        selector=sel,
    )
    try:
        assert report.applied[0].selector == sel
        states = await read_link_states(impair_lab)
        edge_state = next(s for s in states if s.link.id == "edge")
        a = edge_state.by_direction[FlowDirection.A_TO_B]
        assert isinstance(a, DirectionState) and set(a.scoped) == {sel}
        assert equivalent(a.scoped[sel], ImpairmentParams(delay_ms=_SCOPED_DELAY_MS))
        assert a.whole is None and not a.foreign
    finally:
        await repair_link(impair_lab, "edge")
    qdisc = await carrot.exec(
        f"tc qdisc show dev {_VLAN100_DEV}", timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
    )
    line = (qdisc.value or "").splitlines()[0] if (qdisc.value or "").strip() else ""
    assert "prio 1:" not in line and "netem" not in line, (
        f"expected pristine root after bare repair, got: {qdisc.value!r}"
    )
```

- [ ] **Step 2: Test 7 — the differential traffic proof + two concurrent selectors**

ICMP ping cannot measure a port-scoped delay — measure TCP exchange wall-time. Launch two echo listeners on pepper via socat (present on the bed — the tunnel e2e relies on it), then time a round-trip from carrot to the impaired and the clean port; the impaired port must be ≥ `_TCP_DELTA_MIN_MS` slower while the clean port stays fast. Then impair the SECOND port too (concurrent selectors, different params) and confirm both selectors read back.

```python
async def _tcp_rtt_ms(host: UnixHost, target_ip: str, port: int) -> float:
    """Wall-clock ms for one tiny TCP echo exchange, timed ON the host so ssh
    overhead cancels out of the differential comparison."""
    cmd = (
        "python3 - <<'EOF'\n"
        "import socket, time\n"
        f"t0 = time.monotonic()\n"
        f"s = socket.create_connection(('{target_ip}', {port}), timeout=10)\n"
        "s.sendall(b'x')\n"
        "s.recv(1)\n"
        "s.close()\n"
        "print((time.monotonic() - t0) * 1000)\n"
        "EOF"
    )
    result = await host.exec(cmd, timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET)
    assert result.is_ok, f"tcp echo to {target_ip}:{port} failed: {result.value!r}"
    return float((result.value or "").strip().splitlines()[-1])


@pytest.mark.asyncio(loop_scope="module")
async def test_scoped_differential_and_two_selectors(impair_lab: Lab) -> None:
    """The impaired port is measurably slower than an unimpaired port on the
    SAME link (the spec's differential traffic proof), and a second concurrent
    selector applies independently."""
    from otto.link import DirectionState, Selector

    carrot = impair_lab.hosts[_CARROT]
    pepper = impair_lab.hosts[_PEPPER]
    for port in (_SCOPED_PORT, _CLEAN_PORT):
        await _root_best_effort(pepper, f"pkill -f 'TCP4-LISTEN:{port}'")
        await pepper.exec(
            f"setsid socat TCP4-LISTEN:{port},fork,reuseaddr EXEC:cat "
            f"</dev/null >/dev/null 2>&1 &",
            timeout=_HOST_CMD_TIMEOUT,
            log=LogMode.QUIET,
        )
    try:
        base_scoped = await _tcp_rtt_ms(carrot, _PEPPER_VLAN100_IP, _SCOPED_PORT)
        base_clean = await _tcp_rtt_ms(carrot, _PEPPER_VLAN100_IP, _CLEAN_PORT)

        await impair_link(
            impair_lab,
            "edge",
            ImpairmentParams(delay_ms=_SCOPED_DELAY_MS),
            selector=Selector(_SCOPED_PORT, "tcp"),
        )
        try:
            impaired = await _tcp_rtt_ms(carrot, _PEPPER_VLAN100_IP, _SCOPED_PORT)
            clean = await _tcp_rtt_ms(carrot, _PEPPER_VLAN100_IP, _CLEAN_PORT)
            delta = impaired - base_scoped
            assert delta >= _TCP_DELTA_MIN_MS, (
                f"impaired port {_SCOPED_PORT} should be >= {_TCP_DELTA_MIN_MS}ms slower "
                f"(baseline {base_scoped:.1f}ms, impaired {impaired:.1f}ms)"
            )
            assert clean - base_clean < _TCP_DELTA_MIN_MS, (
                f"unimpaired port {_CLEAN_PORT} must stay fast "
                f"(baseline {base_clean:.1f}ms, now {clean:.1f}ms)"
            )

            await impair_link(
                impair_lab,
                "edge",
                ImpairmentParams(loss_pct=5.0),
                selector=Selector(_CLEAN_PORT, "tcp"),
            )
            states = await read_link_states(impair_lab)
            edge_state = next(s for s in states if s.link.id == "edge")
            a = edge_state.by_direction[FlowDirection.A_TO_B]
            assert isinstance(a, DirectionState)
            assert set(a.scoped) == {
                Selector(_SCOPED_PORT, "tcp"),
                Selector(_CLEAN_PORT, "tcp"),
            }
        finally:
            await repair_link(impair_lab, "edge")
    finally:
        for port in (_SCOPED_PORT, _CLEAN_PORT):
            await _root_best_effort(pepper, f"pkill -f 'TCP4-LISTEN:{port}'")
```

NOTE for the implementer: a `delay` netem on BOTH endpoints applies twice per direction pair; `impair_link` without `--from` places on both carrot and pepper, so the observed TCP delta may be ~2× the delay (SYN, data, and ACKs each delayed on the matching port). `_TCP_DELTA_MIN_MS = 150.0` is deliberately below one 200ms delay so the assertion is robust either way. If the bed shows the double-apply, do NOT tighten the bound — record the observed numbers in the task report.

- [ ] **Step 3: Test 8 — per-selector expiry clears only its selector**

```python
@pytest.mark.asyncio(loop_scope="module")
async def test_scoped_expire_clears_only_its_selector(impair_lab: Lab) -> None:
    """--expire on one selector self-heals just that selector; its sibling and
    the scoped root survive until an explicit repair."""
    from otto.link import DirectionState, Selector

    carrot = impair_lab.hosts[_CARROT]
    keep, expire_sel = Selector(_SCOPED_PORT, "tcp"), Selector(_CLEAN_PORT, "tcp")
    await impair_link(
        impair_lab, "edge", ImpairmentParams(delay_ms=50.0), from_host=_CARROT, selector=keep
    )
    try:
        await impair_link(
            impair_lab,
            "edge",
            ImpairmentParams(delay_ms=80.0),
            from_host=_CARROT,
            selector=expire_sel,
            expire=_EXPIRE_SECONDS,
        )
        deadline = time.monotonic() + _EXPIRE_POLL_MAX
        remaining: set = set()
        while time.monotonic() < deadline:
            await asyncio.sleep(_EXPIRE_POLL_INTERVAL)
            states = await read_link_states(impair_lab)
            edge_state = next(s for s in states if s.link.id == "edge")
            a = edge_state.by_direction[FlowDirection.A_TO_B]
            remaining = set(a.scoped) if isinstance(a, DirectionState) else set()
            if remaining == {keep}:
                break
        assert remaining == {keep}, (
            f"expected only {keep.describe()} to survive expiry, got "
            f"{sorted(s.describe() for s in remaining)!r}"
        )
        ps_result = await carrot.exec(
            IMPAIR_PS_COMMAND, timeout=_HOST_CMD_TIMEOUT, log=LogMode.QUIET
        )
        stale = [
            t
            for t in parse_impair_ps(ps_result.value or "")
            if t.link_id == "edge" and t.selector == expire_sel
        ]
        assert not stale, f"expired selector's timer still running: {stale!r}"
    finally:
        await repair_link(impair_lab, "edge")
```

- [ ] **Step 4: Run the module** (single pass, no parallelism — dev-VM rule):

Run: `uv run pytest tests/e2e/test_link_impair_e2e.py -v --timeout=600`
Expected: ALL PASS (old tests 1–5 AND new 6–8), hygiene sweep clean. If a scoped test fails against the bed, treat it as a real finding — diagnose mechanism-first (systematic-debugging), never loosen an assertion to pass, and check the Task 12 captures before suspecting the bed.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check tests/e2e/test_link_impair_e2e.py
uv run ruff format --check tests/e2e/test_link_impair_e2e.py
git add tests/e2e/test_link_impair_e2e.py
git commit -m "test(link): live-bed e2e for port-scoped impairment (differential proof)

Assisted-by: Claude (Fable 5)"
```

---

### Task 14: Whole-tree gates

- [ ] **Step 1:** `make lint` — clean (Python + TS; TS untouched, must stay green).
- [ ] **Step 2:** `make typecheck-python` — clean.
- [ ] **Step 3:** `make docs` — clean.
- [ ] **Step 4:** `uv run pytest tests/unit -q` — full unit tier, single pass (catches cross-module fallout scoped runs miss: import budget, schema drift, lazy-export subprocess test).
- [ ] **Step 5:** `make coverage` — the real merge gate (runs the full suite including live-bed tiers + coverage floor + web parity). ONE pass; if the dev VM is under memory pressure (check `free -h` — VSCode swap-thrash is a known false "hang"), coordinate with Chris rather than re-running repeatedly.
- [ ] **Step 6:** Fix anything red (root-cause first, no speculative patches), then commit fixes individually with conventional prefixes.
- [ ] **Step 7:** Final review of the diff vs. the spec's Hard Constraints list (byte-identical whole-link, tunnels untouched — `git diff main --stat -- src/otto/tunnel` must be EMPTY, kernel-only state, cap 8, optional surface). Record the checklist outcome in the task report.

---

## Self-review notes (done at plan time)

- Spec §1–§6 each map to tasks: selector/state model → 1/2/6/7; tc layout → 4/5/12; impairer contract → 2/4/5; orchestration → 6/8/9; CLI/presentation → 10; testing → all + 12/13; docs → 11.
- Exact error strings pinned: exclusivity both directions (6/8/9), cap (8), capability (8), foreign (6), whole-vs-port repair (9).
- Type consistency: `ScopedState.selectors: dict[Selector, tuple[int, ImpairmentParams]]` everywhere; `DirectionState.scoped: dict[Selector, ImpairmentParams]` (bands dropped at the public API); `AppliedPlacement.selector` trailing optional field.
- Known judgment calls documented in "Deliberate refinements" — do not silently revert them.

