# Link Impairment (#3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `otto link impair/repair/list` — netem impairment of static lab links at endpoint or in-path (middlebox) placements, behind a pluggable `LinkImpairer` registry with a host-level `impairer` pin.

**Architecture:** A *placement* `(host, netdev, direction)` is the unit of impairment; pure resolvers map link+mode→placements, a stateless `LinkImpairer` (NetEm first-party, registry-selected per placement host) builds tc commands, and async orchestration in `otto.link.manage` applies/reads/clears with kernel qdisc state as the only state. Spec: `docs/superpowers/specs/2026-07-10-link-impairment-design.md`.

**Tech Stack:** Python 3.10+, pydantic v2 (specs), typer CLI, asyncio host API (`oneshot`/`run(sudo=)`), tc/netem on unix hosts.

## Global Constraints

- **NEVER** `from __future__ import annotations` (breaks the Sphinx `-W` docs gate). Real 3.10+ annotations, module-top imports.
- otto always emits **explicit units in tc argv** (`50ms`, `2%`, `10mbit`) — never rely on tc's bare-number semantics (spec §3.1).
- Mandatory refusals (spec §9): management-interface refusal per resolved placement; local-host link refusal (either endpoint `== BUILTIN_LOCAL_HOST_ID`) before placement resolution, both modes.
- `typer.Exit` subclasses `RuntimeError` in the vendored click fork — usage-error exits (`Exit(2)`) must be raised OUTSIDE any `try` guarded by `except (ValueError, RuntimeError)`.
- Test fakes return real `CommandResult`/`Results` objects — **never `SimpleNamespace`**.
- Unit tests live under `tests/unit/` and carry NO bed markers (`tests/unit/test_tier_marker_invariants.py` fails on `integration`/`embedded`/`hops` references in unit files).
- Fail-loud on host-down (host-named error) — never `pytest.skip` and never a silent partial impair.
- Single-pass test runs only (dev-VM rule): `uv run pytest <path>` once; no loops, no extra `-n` oversubscription.
- Commit per task from the worktree: conventional prefix, trailer line `Assisted-by: Claude Fable 5`, named paths only (never `git add -u`/`-A`).
- Host-field drift guard: a new runtime host field must mirror in `models/host.py` with equal name sets (`tests/unit/models/test_host_specs.py:219`); after Task 6 run the FULL unit tier once.
- Docs build is gated with `-W` (warnings = errors) and is part of `make validate`.
- **Single API (Chris, 2026-07-10):** the `otto.link` library surface (`manage`/`params`/`placement`/`impairer`) is THE api for three consumers — the CLI, the monitor/GUI backend (#6 topology overlay will consume `read_link_states` the way it consumes `discover_tunnels`), and Python users importing otto. Consequences: `otto.cli.link` is a thin rendering wrapper (parse CLI strings → call library → print; zero business logic); library functions never print and return typed dataclasses; everything a consumer needs is re-exported from `otto.link` with library-grade docstrings.

## File Structure

```
src/otto/host/detached.py        NEW  launch_command extracted from tunnel/socat (Task 1)
src/otto/link/params.py          NEW  ImpairmentParams + unit parsing + merge (Task 2)
src/otto/link/impairer.py        NEW  LinkImpairer base + IMPAIRERS registry (Task 3)
src/otto/link/netem.py           NEW  NetEmImpairer: tc argv builders + qdisc-show parser (Task 4)
src/otto/link/model.py           MOD  Link grows `impair` field (Task 5)
src/otto/link/derive.py          MOD  impair host-reference validation + carry (Task 5)
src/otto/models/link.py          MOD  impair docstring → middlebox host id (Task 5)
src/otto/models/host.py          MOD  UnixHostSpec valid_impairers/impairer + validators (Task 6)
src/otto/host/capability.py      MOD  IMPAIRER_RESOLVER (Task 6)
src/otto/host/unix_host.py       MOD  runtime impairer/valid_impairers fields (Task 6)
src/otto/models/settings.py      MOD  _HOST_PREFERENCE_CAPABILITIES += "impairer" (Task 6)
src/otto/link/sentinel.py        NEW  otto-impair sentinel + ps scan/parse (Task 7)
src/otto/link/placement.py       NEW  Placement, resolvers, refusals, ip-addr parser (Task 7)
src/otto/link/manage.py          NEW  impair_link/repair_link/repair_all/read_link_states (Task 8)
src/otto/link/__init__.py        MOD  re-exports (Tasks 2-8, incremental)
src/otto/cli/link.py             NEW  link_app: impair/repair/list (Task 9)
src/otto/cli/builtin_commands.py MOD  register "link" (Task 9)
src/otto/configmodule/completion_cache.py MOD collect_link_ids (Task 9)
docs/guide/link.md               NEW  user guide (Task 10)
docs/{guide,api}/*.{rst,md}      MOD  toctrees + cross-refs (Task 10)
tests/unit/link/test_*.py        NEW  per-module unit tests (Tasks 2-9)
tests/e2e/test_link_impair_e2e.py NEW live-bed e2e, VLAN fixture (Task 11)
```

Working branch: `worktree-link-foundation` (this worktree). Base: current tip.

---

### Task 1: Extract `launch_command` to `otto.host.detached`

`otto.link` must not import from `otto.tunnel` (link is the underlay, tunnel the overlay). `launch_command` — the systemd-run→setsid detached-process launcher with argv sentinel tagging — is generic host machinery. Move it; `otto.tunnel.socat` re-exports so its callers and tests stay untouched.

**Files:**
- Create: `src/otto/host/detached.py`
- Modify: `src/otto/tunnel/socat.py` (remove body, import + re-export)
- Test: `tests/unit/host/test_detached.py` (create; `tests/unit/host/` already exists)

**Interfaces:**
- Produces: `otto.host.detached.launch_command(sentinel: str, argv: list[str]) -> str` — identical behavior to the current `otto.tunnel.socat.launch_command` (socat.py:79-113). Consumed by Task 8's expire timers and (unchanged) by tunnel manage.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/host/test_detached.py
"""launch_command moved here from otto.tunnel.socat (#3 Task 1) — generic
detached-process launcher, shared by tunnel socats and impair expire timers."""

from otto.host.detached import launch_command


class TestLaunchCommand:
    def test_survival_template_shape(self) -> None:
        cmd = launch_command("otto-impair:v1:lnk:eth1", ["bash", "-c", "sleep 5 && tc qdisc del dev eth1 root"])
        # real systemd-run invocation folded INTO the if condition (falls through
        # to setsid when systemd-run is present but unusable — no dbus session)
        assert cmd.startswith("if command -v systemd-run >/dev/null 2>&1 && systemd-run --user")
        assert "setsid bash -c" in cmd
        assert "otto-impair:v1:lnk:eth1" in cmd

    def test_tunnel_reexport_is_same_object(self) -> None:
        from otto.tunnel.socat import launch_command as tunnel_launch
        assert tunnel_launch is launch_command
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/unit/host/test_detached.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.host.detached'`

- [ ] **Step 3: Move the code**

Create `src/otto/host/detached.py`: move the `launch_command` function and its `import shlex` VERBATIM from `src/otto/tunnel/socat.py:79-113` (keep the full docstring, including the present-but-broken-systemd-run rationale). Module docstring:

```python
"""Detached, sentinel-tagged process launching on remote hosts.

``launch_command`` wraps an argv in ``bash -c 'exec -a "$1" "${@:2}"'`` so the
process's argv[0] IS the sentinel (discoverable via ``ps -eo args=``), launched
under ``systemd-run --user`` with a ``setsid`` fallback. Extracted from
``otto.tunnel.socat`` (#2b) so both tunnels and link-impairment timers use one
proven launcher without a tunnel<->link import edge.
"""
```

In `src/otto/tunnel/socat.py` replace the function with a re-export (keep the name importable exactly as before):

```python
from ..host.detached import launch_command

__all__ = [*globals().get("__all__", []), "launch_command"]  # only if socat.py defines __all__; else just the import
```

(If `socat.py` has no `__all__`, the bare import suffices — check the file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_detached.py tests/unit/tunnel/test_socat.py tests/unit/tunnel/test_manage_add.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS (existing tunnel launch tests prove behavior unchanged)

- [ ] **Step 5: Commit**

```bash
git add src/otto/host/detached.py src/otto/tunnel/socat.py tests/unit/host/test_detached.py
git commit -m "refactor(host): extract launch_command to otto.host.detached

Assisted-by: Claude Fable 5"
```

---

### Task 2: `ImpairmentParams` — typed params, unit parsing, merge

**Files:**
- Create: `src/otto/link/params.py`
- Modify: `src/otto/link/__init__.py` (re-export)
- Test: `tests/unit/link/test_params.py`

**Interfaces:**
- Produces (consumed by Tasks 3/4/8/9):
  - `ImpairmentParams(delay_ms, jitter_ms, loss_pct, corrupt_pct, duplicate_pct, reorder_pct: float | None = None, rate: str | None = None)` — frozen slots dataclass; `None` = not set.
  - `ImpairmentParams.is_empty() -> bool`; `.merged_over(base) -> ImpairmentParams` (per-param last-one-wins, zeros normalize to `None` = cleared); `.validate() -> None` (jitter/reorder require delay — call AFTER merge); `.describe() -> str` (human summary, e.g. `"delay 50ms 5ms loss 2%"`).
  - `parse_time_ms(text, *, option) -> float`; `parse_percent(text, *, option) -> float`; `parse_rate(text) -> str` — all raise `ValueError` with the offending option name.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/link/test_params.py
"""ImpairmentParams: unit parsing (spec §3.1), merge (spec §3.3), coupling rules."""

import pytest

from otto.link.params import ImpairmentParams, parse_percent, parse_rate, parse_time_ms


class TestParsing:
    def test_bare_time_is_milliseconds(self) -> None:
        assert parse_time_ms("50", option="--delay") == 50.0

    @pytest.mark.parametrize(
        ("text", "ms"), [("500us", 0.5), ("50ms", 50.0), ("1.5s", 1500.0), ("0", 0.0)]
    )
    def test_time_suffixes(self, text: str, ms: float) -> None:
        assert parse_time_ms(text, option="--delay") == ms

    def test_bad_time_names_option(self) -> None:
        with pytest.raises(ValueError, match=r"--jitter .* not a time value"):
            parse_time_ms("fast", option="--jitter")

    @pytest.mark.parametrize(("text", "pct"), [("2", 2.0), ("2%", 2.0), ("0", 0.0), ("0.5", 0.5)])
    def test_bare_percent_is_percent(self, text: str, pct: float) -> None:
        assert parse_percent(text, option="--loss") == pct

    def test_percent_over_100_rejected(self) -> None:
        with pytest.raises(ValueError, match="over 100"):
            parse_percent("150", option="--loss")

    def test_rate_requires_explicit_unit(self) -> None:
        with pytest.raises(ValueError, match="explicit unit"):
            parse_rate("10")

    def test_rate_unit_accepted_and_lowercased(self) -> None:
        assert parse_rate("10Mbit") == "10mbit"

    def test_rate_bare_zero_is_the_clear_sentinel(self) -> None:
        assert parse_rate("0") == "0"


class TestMerge:
    def test_last_one_wins_per_param(self) -> None:
        base = ImpairmentParams(delay_ms=20.0)
        new = ImpairmentParams(delay_ms=10.0, loss_pct=2.0)
        assert new.merged_over(base) == ImpairmentParams(delay_ms=10.0, loss_pct=2.0)

    def test_unset_params_persist_from_base(self) -> None:
        base = ImpairmentParams(delay_ms=20.0, rate="10mbit")
        assert ImpairmentParams(loss_pct=1.0).merged_over(base) == ImpairmentParams(
            delay_ms=20.0, loss_pct=1.0, rate="10mbit"
        )

    def test_explicit_zero_clears_just_that_param(self) -> None:
        base = ImpairmentParams(delay_ms=20.0, loss_pct=2.0)
        merged = ImpairmentParams(loss_pct=0.0).merged_over(base)
        assert merged == ImpairmentParams(delay_ms=20.0)

    def test_zero_rate_clears_rate(self) -> None:
        merged = ImpairmentParams(rate="0").merged_over(ImpairmentParams(rate="10mbit"))
        assert merged.rate is None

    def test_all_cleared_is_empty(self) -> None:
        assert ImpairmentParams(delay_ms=0.0).merged_over(ImpairmentParams(delay_ms=50.0)).is_empty()


class TestValidateAndDescribe:
    def test_jitter_without_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="--jitter requires a delay"):
            ImpairmentParams(jitter_ms=5.0).validate()

    def test_reorder_without_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="--reorder requires a delay"):
            ImpairmentParams(reorder_pct=5.0).validate()

    def test_jitter_with_merged_delay_ok(self) -> None:
        ImpairmentParams(jitter_ms=5.0).merged_over(ImpairmentParams(delay_ms=50.0)).validate()

    def test_describe(self) -> None:
        p = ImpairmentParams(delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0, rate="10mbit")
        assert p.describe() == "delay 50ms 5ms loss 2% rate 10mbit"

    def test_describe_empty(self) -> None:
        assert ImpairmentParams().describe() == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_params.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'ImpairmentParams'`

- [ ] **Step 3: Implement**

```python
# src/otto/link/params.py
"""Typed impairment parameters: unit parsing, merge, and coupling rules.

Spec §3.1/§3.3 (docs/superpowers/specs/2026-07-10-link-impairment-design.md):
bare time = milliseconds, bare percent = percent, rate REQUIRES an explicit tc
unit; re-impair merges per-param last-one-wins and an explicit zero clears just
that param.
"""

import re
from dataclasses import dataclass, fields

_TIME_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>us|ms|s)?$")
_PERCENT_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)%?$")
_RATE_RE = re.compile(r"^\d+(?:\.\d+)?(?:bit|kbit|mbit|gbit|tbit|bps|kbps|mbps|gbps)$")
_TIME_TO_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0, None: 1.0}


def parse_time_ms(text: str, *, option: str) -> float:
    """Parse a time value in milliseconds; a bare number means ms (spec §3.1)."""
    m = _TIME_RE.match(text.strip().lower())
    if m is None:
        raise ValueError(
            f"{option} {text!r} is not a time value (bare number = ms; us/ms/s suffixes)"
        )
    return float(m.group("num")) * _TIME_TO_MS[m.group("unit")]


def parse_percent(text: str, *, option: str) -> float:
    """Parse a percentage; a bare number means percent (spec §3.1)."""
    m = _PERCENT_RE.match(text.strip())
    if m is None:
        raise ValueError(f"{option} {text!r} is not a percentage (bare number = percent)")
    value = float(m.group("num"))
    if value > 100.0:
        raise ValueError(f"{option} {value:g} is over 100%")
    return value


def parse_rate(text: str) -> str:
    """Parse a rate; an explicit tc unit is REQUIRED. Bare ``"0"`` clears (§3.3)."""
    cleaned = text.strip().lower()
    if cleaned == "0":
        return "0"
    if _RATE_RE.match(cleaned) is None:
        raise ValueError(
            f"--rate {text!r} needs an explicit unit (kbit/mbit/gbit/...) — "
            "there is no natural default for bandwidth"
        )
    return cleaned


def _fmt(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True, slots=True)
class ImpairmentParams:
    """One impairment parameter set. ``None`` = not set/absent."""

    delay_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None
    corrupt_pct: float | None = None
    duplicate_pct: float | None = None
    reorder_pct: float | None = None
    rate: str | None = None
    """Canonical lowercase tc rate string (``"10mbit"``); ``"0"`` = clear."""

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    def merged_over(self, base: "ImpairmentParams") -> "ImpairmentParams":
        """Per-param last-one-wins over *base*; explicit zeros clear (spec §3.3)."""
        merged: dict[str, float | str | None] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                value = getattr(base, f.name)
            if value in (0.0, "0"):
                value = None
            merged[f.name] = value
        return ImpairmentParams(**merged)  # type: ignore[arg-type]

    def validate(self) -> None:
        """netem coupling rules — evaluate AFTER merge (a jitter may join an
        already-applied delay)."""
        if self.jitter_ms is not None and self.delay_ms is None:
            raise ValueError("--jitter requires a delay (given now or already applied)")
        if self.reorder_pct is not None and self.delay_ms is None:
            raise ValueError("--reorder requires a delay (given now or already applied)")

    def describe(self) -> str:
        """Human/argv-shaped summary with explicit units, e.g. ``delay 50ms loss 2%``."""
        parts: list[str] = []
        if self.delay_ms is not None:
            token = f"delay {_fmt(self.delay_ms)}ms"
            if self.jitter_ms is not None:
                token += f" {_fmt(self.jitter_ms)}ms"
            parts.append(token)
        if self.loss_pct is not None:
            parts.append(f"loss {_fmt(self.loss_pct)}%")
        if self.corrupt_pct is not None:
            parts.append(f"corrupt {_fmt(self.corrupt_pct)}%")
        if self.duplicate_pct is not None:
            parts.append(f"duplicate {_fmt(self.duplicate_pct)}%")
        if self.reorder_pct is not None:
            parts.append(f"reorder {_fmt(self.reorder_pct)}%")
        if self.rate is not None:
            parts.append(f"rate {self.rate}")
        return " ".join(parts)
```

Add to `src/otto/link/__init__.py` imports and `__all__`: `ImpairmentParams`, `parse_time_ms`, `parse_percent`, `parse_rate` (from `.params`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/test_params.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/params.py src/otto/link/__init__.py tests/unit/link/test_params.py
git commit -m "feat(link): ImpairmentParams — unit parsing, zero-clear merge, coupling rules

Assisted-by: Claude Fable 5"
```

---

### Task 3: `LinkImpairer` base + `IMPAIRERS` registry

Mirror `otto.host.transfer.registry` (src/otto/host/transfer/registry.py:19-52) exactly.

**Files:**
- Create: `src/otto/link/impairer.py`
- Modify: `src/otto/link/__init__.py`
- Test: `tests/unit/link/test_impairer_registry.py`

**Interfaces:**
- Consumes: `ImpairmentParams` (Task 2); `otto.registry.Registry`, `caller_module`.
- Produces (consumed by Tasks 4/6/8):
  - `class LinkImpairer` with `host_families: ClassVar[frozenset[str]]` and methods `apply_command(netdev: str, params: ImpairmentParams) -> str`, `read_command(netdev: str) -> str`, `clear_command(netdev: str) -> str`, `parse_read(output: str) -> ImpairmentParams | None` (all raise `NotImplementedError` on the base).
  - `IMPAIRERS: Registry[type[LinkImpairer]]`
  - `register_impairer(name: str, cls: type[LinkImpairer], *, overwrite: bool = False) -> None` (rejects empty `host_families`; `origin=caller_module()`)
  - `build_impairer(name: str) -> type[LinkImpairer]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/link/test_impairer_registry.py
"""IMPAIRERS registry mirrors the transfer-backend registry (spec §5).

No manual snapshot/cleanup needed: the autouse ``_isolate_registries`` fixture
(tests/unit/conftest.py) discovers every module-level ``Registry`` dynamically
and restores it after each test.
"""

from typing import ClassVar

import pytest

from otto.link.impairer import IMPAIRERS, LinkImpairer, build_impairer, register_impairer


class _FakeImpairer(LinkImpairer):
    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})


class TestRegistry:
    def test_register_and_build_roundtrip(self) -> None:
        register_impairer("fake", _FakeImpairer)
        assert build_impairer("fake") is _FakeImpairer
        assert "fake" in IMPAIRERS

    def test_empty_host_families_rejected(self) -> None:
        class _Homeless(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset()

        with pytest.raises(ValueError, match="host_families is empty"):
            register_impairer("homeless", _Homeless)

    def test_unknown_name_error_lists_hint(self) -> None:
        with pytest.raises(ValueError, match="register_impairer"):
            build_impairer("no-such-impairer")

    def test_origin_recorded_as_this_module(self) -> None:
        register_impairer("fake", _FakeImpairer)
        assert IMPAIRERS.origin("fake").endswith("test_impairer_registry")

    def test_base_methods_are_abstract(self) -> None:
        base = LinkImpairer()
        for call in (
            lambda: base.apply_command("eth0", None),  # type: ignore[arg-type]
            lambda: base.read_command("eth0"),
            lambda: base.clear_command("eth0"),
            lambda: base.parse_read(""),
        ):
            with pytest.raises(NotImplementedError):
                call()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_impairer_registry.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.link.impairer'`

- [ ] **Step 3: Implement**

```python
# src/otto/link/impairer.py
"""Pluggable link impairers: the ``LinkImpairer`` contract + ``IMPAIRERS`` registry.

Mirrors the transfer-backend registry (``otto.host.transfer.registry``): custom
impairers register from init modules under a name; a host's ``impairer`` pin /
``valid_impairers`` menu select one per placement host (spec §5). NetEm is the
only first-party registrant (``otto.link.netem``).
"""

from typing import ClassVar

from ..registry import Registry, caller_module
from .params import ImpairmentParams


class LinkImpairer:
    """Builds the shell commands that apply/read/clear one placement's impairment.

    Stateless: implementations build command strings and parse output; the
    orchestration layer (``otto.link.manage``) runs them on hosts.
    """

    host_families: ClassVar[frozenset[str]] = frozenset()
    """Host families this impairer serves (e.g. ``frozenset({"unix"})``)."""

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        """Shell command applying *params* to *netdev* (idempotent replace)."""
        raise NotImplementedError

    def read_command(self, netdev: str) -> str:
        """Shell command whose output :meth:`parse_read` understands."""
        raise NotImplementedError

    def clear_command(self, netdev: str) -> str:
        """Shell command removing this impairer's state from *netdev*."""
        raise NotImplementedError

    def parse_read(self, output: str) -> ImpairmentParams | None:
        """Parse :meth:`read_command` output; ``None`` = no impairment present."""
        raise NotImplementedError


IMPAIRERS: Registry[type[LinkImpairer]] = Registry(
    "impairer", register_hint="otto.link.register_impairer()"
)


def register_impairer(name: str, cls: type[LinkImpairer], *, overwrite: bool = False) -> None:
    """Make a custom impairer available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The impairer
    must declare a non-empty :attr:`LinkImpairer.host_families`; otherwise it
    could never validate against any host and is rejected here.
    """
    if not cls.host_families:
        raise ValueError(
            f"register_impairer({name!r}): cls.host_families is empty; an impairer "
            f"must declare at least one host family (e.g. frozenset({{'unix'}}))."
        )
    IMPAIRERS.register(name, cls, overwrite=overwrite, origin=caller_module())


def build_impairer(name: str) -> type[LinkImpairer]:
    """Return the impairer class registered under *name* (rich unknown-name error)."""
    return IMPAIRERS.get(name)
```

Add to `src/otto/link/__init__.py`: `IMPAIRERS`, `LinkImpairer`, `register_impairer`, `build_impairer` (from `.impairer`), extend `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/test_impairer_registry.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/impairer.py src/otto/link/__init__.py tests/unit/link/test_impairer_registry.py
git commit -m "feat(link): LinkImpairer contract + IMPAIRERS registry

Assisted-by: Claude Fable 5"
```

---

### Task 4: `NetEmImpairer` — tc argv builders + qdisc-show parser

**Files:**
- Create: `src/otto/link/netem.py`
- Modify: `src/otto/link/__init__.py` (import `.netem` so registration is an import side effect of the package, exactly like `otto.host.transfer` registers its builtins)
- Test: `tests/unit/link/test_netem.py`

**Interfaces:**
- Consumes: `LinkImpairer`, `register_impairer` (Task 3); `ImpairmentParams` (Task 2).
- Produces (consumed by Tasks 6/8/11): `NetEmImpairer` registered as `"netem"` with `host_families = frozenset({"unix"})`; module functions `netem_args(params) -> str` and `parse_qdisc_show(output) -> ImpairmentParams | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/link/test_netem.py
"""NetEm impairer: exact tc argv (explicit units ALWAYS) + qdisc-show parsing
against canned modern and centos:7-era iproute2 output."""

import pytest

from otto.link.impairer import IMPAIRERS
from otto.link.netem import NetEmImpairer, netem_args, parse_qdisc_show
from otto.link.params import ImpairmentParams

FULL = ImpairmentParams(
    delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0, corrupt_pct=0.1,
    duplicate_pct=1.0, reorder_pct=5.0, rate="10mbit",
)


class TestCommands:
    def test_registered_as_netem_for_unix(self) -> None:
        assert IMPAIRERS.get("netem") is NetEmImpairer
        assert NetEmImpairer.host_families == frozenset({"unix"})

    def test_apply_command_exact(self) -> None:
        cmd = NetEmImpairer().apply_command("eth1.100", ImpairmentParams(delay_ms=50.0, loss_pct=2.0))
        assert cmd == "tc qdisc replace dev eth1.100 root netem delay 50ms loss 2%"

    def test_apply_all_params_explicit_units(self) -> None:
        assert netem_args(FULL) == (
            "delay 50ms 5ms loss 2% corrupt 0.1% duplicate 1% reorder 5% rate 10mbit"
        )

    def test_read_and_clear_commands(self) -> None:
        imp = NetEmImpairer()
        assert imp.read_command("eth1") == "tc qdisc show dev eth1"
        assert imp.clear_command("eth1") == "tc qdisc del dev eth1 root"


class TestParser:
    def test_modern_ubuntu_2404(self) -> None:
        # verified live on the bed (iproute2 6.1.0)
        out = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms  5ms loss 2%\n"
        assert parse_qdisc_show(out) == ImpairmentParams(delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0)

    def test_old_iproute2_float_times(self) -> None:
        # centos:7-era formatting: float time values
        out = "qdisc netem 8002: root refcnt 2 limit 1000 delay 50.0ms loss 2% rate 10Mbit\n"
        assert parse_qdisc_show(out) == ImpairmentParams(delay_ms=50.0, loss_pct=2.0, rate="10mbit")

    def test_no_netem_returns_none(self) -> None:
        assert parse_qdisc_show("qdisc noqueue 0: root refcnt 2\n") is None
        assert parse_qdisc_show("") is None

    def test_non_root_netem_ignored(self) -> None:
        # a netem leaf someone attached under a classful parent is not ours
        assert parse_qdisc_show("qdisc netem 10: parent 1:1 limit 1000 delay 5ms\n") is None

    def test_delay_without_jitter(self) -> None:
        out = "qdisc netem 8001: root refcnt 2 limit 1000 delay 100ms\n"
        assert parse_qdisc_show(out) == ImpairmentParams(delay_ms=100.0)

    @pytest.mark.parametrize("keyword", ["corrupt", "duplicate", "reorder"])
    def test_percent_keywords(self, keyword: str) -> None:
        out = f"qdisc netem 8001: root refcnt 2 limit 1000 {keyword} 3%\n"
        parsed = parse_qdisc_show(out)
        assert parsed is not None
        assert getattr(parsed, f"{keyword}_pct") == 3.0

    def test_roundtrip_apply_then_parse(self) -> None:
        # what we render is what we re-read: rendering tokens parse back equal
        rendered = f"qdisc netem 8003: root refcnt 2 limit 1000 {netem_args(FULL)}\n"
        assert parse_qdisc_show(rendered) == FULL
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_netem.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.link.netem'`

- [ ] **Step 3: Implement**

```python
# src/otto/link/netem.py
"""NetEm — the first-party ``LinkImpairer`` (tc qdisc netem on unix hosts).

argv builders ALWAYS emit explicit units (spec §3.1) — tc's bare-number
semantics vary by parameter and iproute2 version. The parser reads
``tc qdisc show dev X`` back into :class:`ImpairmentParams` (kernel qdisc
config is the only state — spec §6) and tolerates both modern and old
iproute2 formatting (``50ms`` vs ``50.0ms``).
"""

import re
from typing import ClassVar

from .impairer import LinkImpairer, register_impairer
from .params import ImpairmentParams

_TIME_TOKEN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>us|usec|ms|msec|s|sec)$")
_PERCENT_TOKEN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)%$")
_TIME_TO_MS = {"us": 0.001, "usec": 0.001, "ms": 1.0, "msec": 1.0, "s": 1000.0, "sec": 1000.0}
_PERCENT_KEYWORDS = {
    "loss": "loss_pct",
    "corrupt": "corrupt_pct",
    "duplicate": "duplicate_pct",
    "reorder": "reorder_pct",
}


def netem_args(params: ImpairmentParams) -> str:
    """Render *params* as netem qdisc arguments with explicit units."""
    return params.describe()


def _parse_time(token: str) -> float | None:
    m = _TIME_TOKEN.match(token)
    return float(m.group("num")) * _TIME_TO_MS[m.group("unit")] if m else None


def _parse_percent_token(token: str) -> float | None:
    m = _PERCENT_TOKEN.match(token)
    return float(m.group("num")) if m else None


def parse_qdisc_show(output: str) -> ImpairmentParams | None:
    """Parse ``tc qdisc show dev X`` output; ``None`` = no root netem qdisc."""
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) >= 4 and tokens[0] == "qdisc" and tokens[1] == "netem" and "root" in tokens:
            return _parse_netem_tokens(tokens)
    return None


def _parse_netem_tokens(tokens: list[str]) -> ImpairmentParams:
    kw: dict[str, float | str | None] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "delay" and i + 1 < len(tokens):
            kw["delay_ms"] = _parse_time(tokens[i + 1])
            if i + 2 < len(tokens):
                jitter = _parse_time(tokens[i + 2])
                if jitter is not None:
                    kw["jitter_ms"] = jitter
                    i += 1
            i += 2
            continue
        if token in _PERCENT_KEYWORDS and i + 1 < len(tokens):
            kw[_PERCENT_KEYWORDS[token]] = _parse_percent_token(tokens[i + 1])
            i += 2
            continue
        if token == "rate" and i + 1 < len(tokens):
            kw["rate"] = tokens[i + 1].lower()
            i += 2
            continue
        i += 1
    return ImpairmentParams(**{k: v for k, v in kw.items() if v is not None})  # type: ignore[arg-type]


class NetEmImpairer(LinkImpairer):
    """tc/netem on a unix host's interface."""

    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        return f"tc qdisc replace dev {netdev} root netem {netem_args(params)}"

    def read_command(self, netdev: str) -> str:
        return f"tc qdisc show dev {netdev}"

    def clear_command(self, netdev: str) -> str:
        return f"tc qdisc del dev {netdev} root"

    def parse_read(self, output: str) -> ImpairmentParams | None:
        return parse_qdisc_show(output)


register_impairer("netem", NetEmImpairer)
```

Note: `_fmt` moves nothing — `describe()` (Task 2) already renders exactly the netem argument grammar, so `netem_args` delegates to it; the parser's roundtrip test locks that equivalence. In `src/otto/link/__init__.py` add `from .netem import NetEmImpairer` and include it in `__all__` — importing the package now registers `"netem"` (same builtin-registration-on-package-import pattern as `otto.host.transfer`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/test_netem.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/netem.py src/otto/link/__init__.py tests/unit/link/test_netem.py
git commit -m "feat(link): NetEm impairer — tc argv builders + qdisc-show parser

Assisted-by: Claude Fable 5"
```

---

### Task 5: `Link.impair` field + derive-time validation

The `LinkSpec.impair` field already exists (`src/otto/models/link.py:37`, reserved). Give it its #3 semantics — the in-path middlebox **host id** — validate the reference at lab load, and carry it onto the runtime `Link`.

**Files:**
- Modify: `src/otto/link/model.py` (Link grows `impair`), `src/otto/link/derive.py` (validate + carry), `src/otto/models/link.py` (docstring only: impair = middlebox host id, spec §10)
- Test: extend `tests/unit/link/test_model.py`, `tests/unit/link/test_derive.py`; update `tests/unit/models/test_link_specs.py` reserved-fields test to use a host-id-shaped value (`impair="wanem_seed"`)

**Interfaces:**
- Produces: `Link.impair: str | None = None` (LAST field — keeps existing positional construction working). `resolve_declared_links` raises `ValueError` for an unknown impair host and for an impair host that is one of the link's endpoints.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/link/test_derive.py` (reuse that file's existing `HostAddressing` map fixtures — read it first; the class below follows its local conventions):

```python
class TestImpairField:
    def test_impair_carried_onto_link(self) -> None:
        hosts = {
            "a_seed": HostAddressing(ip="10.0.0.1", interfaces={"eth1": "10.1.0.1"}),
            "b_seed": HostAddressing(ip="10.0.0.2", interfaces={"eth1": "10.1.0.2"}),
            "wanem_seed": HostAddressing(ip="10.0.0.3", interfaces={}),
        }
        entry = {
            "endpoints": [
                {"host": "a_seed", "interface": "eth1"},
                {"host": "b_seed", "interface": "eth1"},
            ],
            "impair": "wanem_seed",
        }
        (link,) = resolve_declared_links([entry], hosts, source="lab.json", loaded_ids=set(hosts))
        assert link.impair == "wanem_seed"

    def test_unknown_impair_host_rejected(self) -> None:
        # same hosts map WITHOUT wanem_seed
        with pytest.raises(ValueError, match="impair host 'wanem_seed' is not a known host"):
            resolve_declared_links([entry], hosts_without_wanem, source="lab.json", loaded_ids=...)

    def test_impair_host_must_not_be_an_endpoint(self) -> None:
        entry_self = {**entry, "impair": "a_seed"}
        with pytest.raises(ValueError, match="is an endpoint of the link"):
            resolve_declared_links([entry_self], hosts, source="lab.json", loaded_ids=set(hosts))
```

(Write these as complete tests against the file's actual local helpers; the assertions and match strings above are the requirements.) Also add to `tests/unit/link/test_model.py`:

```python
def test_impair_defaults_none_and_is_carried() -> None:
    a = LinkEndpoint(host="a_seed", interface="eth1")
    b = LinkEndpoint(host="b_seed", interface="eth1")
    assert Link(a=a, b=b).impair is None
    assert Link(a=a, b=b, impair="wanem_seed").impair == "wanem_seed"
```

And update `tests/unit/models/test_link_specs.py:67-69` (`test_reserved_fields_accepted`): change `impair="netem"` to `impair="wanem_seed"` — the field's value is a middlebox host id, not an impairer name (spec §10; the impairer knob is the host-level `impairer` pin, Task 6).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_derive.py tests/unit/link/test_model.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `TypeError: Link.__init__() got an unexpected keyword argument 'impair'`

- [ ] **Step 3: Implement**

In `src/otto/link/model.py`, append to `Link` (after `name`):

```python
    impair: str | None = None
    """In-path middlebox host id servicing this link's impairment (spec §10);
    ``None`` = endpoint-anchored impairment."""
```

In `src/otto/link/derive.py` `resolve_declared_links`, inside the existing per-entry `try` (after both endpoints resolve, before constructing `Link`):

```python
            if spec.impair is not None:
                if spec.impair not in hosts:
                    raise ValueError(f"impair host {spec.impair!r} is not a known host")
                if spec.impair in (a.host, b.host):
                    raise ValueError(
                        f"impair host {spec.impair!r} is an endpoint of the link — "
                        "an in-path middlebox must be a third host"
                    )
```

and extend the `Link(...)` construction with `impair=spec.impair`.

In `src/otto/models/link.py`, replace the `impair` field's reserved-for-#3 comment/docstring with: `impair: str | None = None` — "In-path middlebox **host id** servicing this link's impairment (sub-project #3, spec §10). Reference-validated at lab load (`otto.link.derive`). `management` remains reserved for #5."

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/ tests/unit/models/test_link_specs.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/model.py src/otto/link/derive.py src/otto/models/link.py tests/unit/link/test_derive.py tests/unit/link/test_model.py tests/unit/models/test_link_specs.py
git commit -m "feat(link): impair field = in-path middlebox host id, validated at lab load

Assisted-by: Claude Fable 5"
```

---

### Task 6: Host-level `impairer` pin (menu + resolver + preferences)

Mirror the `transfer` capability EXACTLY: menu field + pin field on `UnixHostSpec`, validators against `IMPAIRERS` + family, `CapabilityResolver` resolution in `to_host()`, runtime fields on `UnixHost`, preferences key. Embedded hosts get NO impairer fields (no impairer serves that family).

**Files:**
- Modify: `src/otto/host/capability.py` (add `IMPAIRER_RESOLVER = CapabilityResolver("impairer")` next to line 54), `src/otto/models/host.py`, `src/otto/host/unix_host.py`, `src/otto/models/settings.py` (line ~237)
- Test: extend `tests/unit/models/test_host_specs.py`

**Interfaces:**
- Consumes: `IMPAIRERS` via `from ..link import IMPAIRERS` in models/host.py — import the PACKAGE (its `__init__` imports `.netem`, so `"netem"` is registered before any validation runs; same reason models/host.py imports `..host.transfer`).
- Produces (consumed by Task 8): runtime `UnixHost.impairer: str` — the RESOLVED active impairer name (default `"netem"`); `UnixHost.valid_impairers: list[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/models/test_host_specs.py` (mirror the existing `TestMenuValidation` / `TestPreferenceResolution` style at :397-546; `_unix(**kw)`-style helpers may already exist — reuse them):

```python
class TestImpairerValidation:
    def test_default_menu_is_netem(self) -> None:
        spec = UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}])
        assert spec.valid_impairers == ["netem"]
        assert spec.impairer is None  # pin unset; resolved at to_host

    def test_unknown_impairer_in_menu_raises(self) -> None:
        with pytest.raises(ValueError, match="not a registered impairer"):
            UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}],
                         valid_impairers=["bogus"])

    def test_family_inapplicable_impairer_rejected(self) -> None:
        class _EmbeddedOnly(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset({"embedded"})

        register_impairer("embedded-only", _EmbeddedOnly)
        with pytest.raises(ValueError, match="not valid on a unix host"):
            UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}],
                         valid_impairers=["embedded-only"])

    def test_empty_menu_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a non-empty"):
            UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}],
                         valid_impairers=[])

    def test_pin_out_of_menu_rejected_at_to_host(self) -> None:
        spec = UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}],
                            impairer="fake")
        with pytest.raises(ValueError, match="impairer 'fake' is not in"):
            spec.to_host()

    def test_to_host_resolves_family_default(self) -> None:
        spec = UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}])
        assert spec.to_host().impairer == "netem"

    def test_preference_beats_default(self) -> None:
        class _Fake(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

        register_impairer("fake", _Fake)
        spec = UnixHostSpec(ip="1.1.1.1", element="x", creds=[{"login": "u", "password": "p"}],
                            valid_impairers=["netem", "fake"])
        host = spec.to_host(preferences={"impairer": ["fake"]})
        assert host.impairer == "fake"
```

Also add a settings test near the existing host_preferences tests (find them via `grep -rn host_preferences tests/unit/models/`): a `[host_preferences]` table entry `{"selector": {"impairer": ["netem"]}}` validates (no "unknown key" error).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/models/test_host_specs.py -p no:cacheprovider --no-cov -o addopts="" -k Impairer`
Expected: FAIL — `valid_impairers` unexpected keyword / attribute missing

- [ ] **Step 3: Implement**

`src/otto/host/capability.py` (after line 54): `IMPAIRER_RESOLVER = CapabilityResolver("impairer")`.

`src/otto/models/host.py` — module-level helpers mirroring `_validate_transfer_for_family`/`_validate_transfer_menu` (lines 102-140), importing `from ..link import IMPAIRERS` at the top (alongside the `TRANSFER_BACKENDS` import at line 28) and `IMPAIRER_RESOLVER` alongside the other resolvers (line 19):

```python
def _validate_impairer_for_family(v: str, family: str, host_label: str) -> str:
    """Validate an impairer selector against the registry and host-family applicability."""
    if v not in IMPAIRERS:
        known = ", ".join(sorted(IMPAIRERS.names()))
        raise ValueError(f"impairer {v!r} is not a registered impairer. Known: {known}")
    families = IMPAIRERS.get(v).host_families
    if family not in families:
        fam = ", ".join(sorted(families))
        raise ValueError(f"impairer {v!r} is not valid on {host_label} (it serves: {fam}).")
    return v


def _validate_impairer_menu(v: list[str], family: str, host_label: str) -> list[str]:
    if not v:
        raise ValueError("valid_impairers must be a non-empty list of impairers")
    return [_validate_impairer_for_family(entry, family, host_label) for entry in v]
```

On `UnixHostSpec` (next to `valid_transfers`/`transfer`, lines 378-380):

```python
    valid_impairers: list[str] = Field(default_factory=lambda: ["netem"])
    impairer: str | None = None  # optional active pin; resolved at to_host

    @field_validator("valid_impairers")
    @classmethod
    def _validate_unix_valid_impairers(cls, v: list[str]) -> list[str]:
        return _validate_impairer_menu(v, cls._host_family, "a unix host")
```

In `UnixHostSpec.to_host` (next to the transfer resolution, lines 421-423):

```python
        kw["valid_impairers"] = list(self.valid_impairers)
        kw["impairer"] = IMPAIRER_RESOLVER.resolve_active(
            self.valid_impairers, pin=self.impairer, preference=prefs.get("impairer")
        )
```

`src/otto/host/unix_host.py` — runtime fields next to `transfer`/`valid_transfers` (lines 182-189), mirroring their exact declaration style and docstring voice:

```python
    impairer: str = "netem"
    """Active impairer used for link-impairment placements on this host."""

    valid_impairers: list[str] = field(default_factory=lambda: ["netem"])
    """Closed menu of impairers this host supports (active is ``impairer``)."""
```

and mirror the `TRANSFER_RESOLVER.validate_choice(self.valid_transfers, self.transfer)` post-init validation at line 351 with `IMPAIRER_RESOLVER.validate_choice(self.valid_impairers, self.impairer)` right beside it (same method).

`src/otto/models/settings.py` line ~237: `_HOST_PREFERENCE_CAPABILITIES: frozenset[str] = frozenset({"term", "transfer", "impairer"})` — and extend the constant's comment listing.

- [ ] **Step 4: Run tests, then the FULL unit tier**

Run: `uv run pytest tests/unit/models/ -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS — including the two drift-guard tests (`test_host_spec_fields_match_runtime_init`, `test_registered_pairs_drift_guard`), which is the point of this task's field mirroring.

Then ONE full unit pass (host-field changes have broken main before — feedback rule):
Run: `uv run pytest tests/unit`
Expected: all pass, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add src/otto/host/capability.py src/otto/models/host.py src/otto/host/unix_host.py src/otto/models/settings.py tests/unit/models/test_host_specs.py
git commit -m "feat(host): impairer capability — menu + pin + preferences, resolved at to_host

Assisted-by: Claude Fable 5"
```

---

### Task 7: Impair sentinel + placement resolution & refusals

**Files:**
- Create: `src/otto/link/sentinel.py`, `src/otto/link/placement.py`
- Modify: `src/otto/link/__init__.py`
- Test: `tests/unit/link/test_impair_sentinel.py`, `tests/unit/link/test_placement.py`

**Interfaces:**
- Consumes: `Link`/`LinkEndpoint` (Task 5), `BUILTIN_LOCAL_HOST_ID` (`otto.host.builtin_hosts`).
- Produces (consumed by Task 8):
  - `sentinel.py`: `IMPAIR_SENTINEL_PREFIX = "otto-impair"`, `IMPAIR_PS_COMMAND: str`, `encode_impair_sentinel(link_id: str, netdev: str) -> str`, `parse_impair_sentinel(token: str) -> tuple[str, str] | None`, `parse_impair_ps(output: str) -> list[tuple[int, str, str]]` (pid, link_id, netdev).
  - `placement.py`: `FlowDirection` enum (`A_TO_B`/`B_TO_A`, values `"a->b"`/`"b->a"`), `Placement(host_id, netdev, direction)` frozen dataclass, `parse_ip_addr(output) -> dict[str, list[IPv4Interface]]`, `endpoint_placements(link, directions) -> list[Placement]`, `inpath_placements(link, impair_host_id, addr_table, directions) -> list[Placement]`, `ensure_not_local_link(link) -> None`, `ensure_not_mgmt(placement, addr_table, mgmt_ip) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/link/test_impair_sentinel.py
"""otto-impair sentinel codec + ps-scan parsing (mirrors tunnel sentinel style)."""

from otto.link.sentinel import (
    IMPAIR_PS_COMMAND,
    encode_impair_sentinel,
    parse_impair_ps,
    parse_impair_sentinel,
)


class TestCodec:
    def test_roundtrip(self) -> None:
        token = encode_impair_sentinel("lnk-abc123", "eth1.100")
        assert token == "otto-impair:v1:lnk-abc123:eth1.100"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100")

    def test_percent_encoding_of_separator(self) -> None:
        token = encode_impair_sentinel("name:with:colons", "eth1")
        assert parse_impair_sentinel(token) == ("name:with:colons", "eth1")

    def test_reject_foreign_and_malformed(self) -> None:
        assert parse_impair_sentinel("otto-tunnel:v1:x:y") is None
        assert parse_impair_sentinel("otto-impair:v2:x:y") is None
        assert parse_impair_sentinel("otto-impair:v1:onlyone") is None


class TestPsScan:
    def test_ps_command_uses_separate_eo_flags(self) -> None:
        # procps-ng 3.3.10 mis-parses the comma-joined form (#2b lesson)
        assert "-eo pid= -eo etime= -eo args=" in IMPAIR_PS_COMMAND
        assert "grep -a ' otto-impair:'" in IMPAIR_PS_COMMAND

    def test_parse_ps_extracts_timer_pids(self) -> None:
        token = encode_impair_sentinel("lnk-abc123", "eth1.100")
        text = "\n".join([
            f"  4242 05:00 {token} -c sleep 30 && tc qdisc del dev eth1.100 root",
            "  4243 05:00 otto-impair:v1:mangled",
            "  10 01:00 socat TCP4-LISTEN:5000 STDIO",
            "garbage",
        ])
        assert parse_impair_ps(text) == [(4242, "lnk-abc123", "eth1.100")]
```

```python
# tests/unit/link/test_placement.py
"""Placement resolution (endpoint + in-path) and the two mandatory refusals."""

import pytest

from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from otto.link.model import Link, LinkEndpoint
from otto.link.placement import (
    FlowDirection,
    Placement,
    endpoint_placements,
    ensure_not_local_link,
    ensure_not_mgmt,
    inpath_placements,
    parse_ip_addr,
)

BOTH = {FlowDirection.A_TO_B, FlowDirection.B_TO_A}

LINK = Link(
    a=LinkEndpoint(host="carrot_seed", interface="eth1.100", ip="10.10.201.11"),
    b=LinkEndpoint(host="tomato_seed", interface="eth1.200", ip="10.10.202.12"),
)

# real `ip -o addr show` shape (verified live on the bed)
PEPPER_ADDRS = parse_ip_addr(
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
    "3: eth1    inet 10.10.200.13/24 brd 10.10.200.255 scope global eth1\\       valid_lft forever\n"
    "4: eth1.100    inet 10.10.201.13/24 brd 10.10.201.255 scope global eth1.100\\  valid_lft forever\n"
    "5: eth1.200    inet 10.10.202.13/24 brd 10.10.202.255 scope global eth1.200\\  valid_lft forever\n"
)


class TestParseIpAddr:
    def test_netdevs_and_prefixes(self) -> None:
        assert set(PEPPER_ADDRS) == {"lo", "eth1", "eth1.100", "eth1.200"}
        (eth1,) = PEPPER_ADDRS["eth1"]
        assert str(eth1.ip) == "10.10.200.13" and eth1.network.prefixlen == 24


class TestEndpointPlacements:
    def test_both_directions(self) -> None:
        assert endpoint_placements(LINK, BOTH) == [
            Placement("carrot_seed", "eth1.100", FlowDirection.A_TO_B),
            Placement("tomato_seed", "eth1.200", FlowDirection.B_TO_A),
        ]

    def test_single_direction(self) -> None:
        (p,) = endpoint_placements(LINK, {FlowDirection.B_TO_A})
        assert p == Placement("tomato_seed", "eth1.200", FlowDirection.B_TO_A)

    def test_unnamed_interface_not_impairable(self) -> None:
        bare = Link(a=LinkEndpoint(host="a_seed"), b=LINK.b)
        with pytest.raises(ValueError, match="no named interface"):
            endpoint_placements(bare, BOTH)


class TestInpathPlacements:
    def test_facing_resolution_by_subnet(self) -> None:
        # A→B egresses toward B: pepper's eth1.200 faces tomato; B→A faces carrot
        assert inpath_placements(LINK, "pepper_seed", PEPPER_ADDRS, BOTH) == [
            Placement("pepper_seed", "eth1.200", FlowDirection.A_TO_B),
            Placement("pepper_seed", "eth1.100", FlowDirection.B_TO_A),
        ]

    def test_not_in_path_fails_loud(self) -> None:
        off_path = Link(
            a=LinkEndpoint(host="x_seed", interface="eth9", ip="192.168.99.1"), b=LINK.b
        )
        with pytest.raises(ValueError, match="no interface on 'x_seed'.*192.168.99.1"):
            inpath_placements(off_path, "pepper_seed", PEPPER_ADDRS, BOTH)

    def test_unresolved_endpoint_ip_rejected(self) -> None:
        no_ip = Link(a=LinkEndpoint(host="a_seed", interface="eth1"), b=LINK.b)
        with pytest.raises(ValueError, match="unresolved ip"):
            inpath_placements(no_ip, "pepper_seed", PEPPER_ADDRS, BOTH)


class TestRefusals:
    def test_local_endpoint_link_refused(self) -> None:
        local = Link(a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth0"), b=LINK.b)
        with pytest.raises(ValueError, match="local host as an endpoint"):
            ensure_not_local_link(local)

    def test_normal_link_passes(self) -> None:
        ensure_not_local_link(LINK)

    def test_mgmt_netdev_refused(self) -> None:
        p = Placement("pepper_seed", "eth1", FlowDirection.A_TO_B)
        with pytest.raises(ValueError, match="management interface"):
            ensure_not_mgmt(p, PEPPER_ADDRS, "10.10.200.13")

    def test_vlan_subinterface_passes_mgmt_check(self) -> None:
        # the e2e's whole premise: eth1.100 is NOT the mgmt netdev even though
        # it rides the same wire as eth1
        p = Placement("pepper_seed", "eth1.100", FlowDirection.A_TO_B)
        ensure_not_mgmt(p, PEPPER_ADDRS, "10.10.200.13")

    def test_unknown_mgmt_ip_does_not_refuse(self) -> None:
        # mgmt address not visible in the table (e.g. NAT-fronted): no positive
        # match on the placement netdev → allow
        p = Placement("pepper_seed", "eth1.100", FlowDirection.A_TO_B)
        ensure_not_mgmt(p, PEPPER_ADDRS, "203.0.113.7")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_impair_sentinel.py tests/unit/link/test_placement.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — modules don't exist

- [ ] **Step 3: Implement**

```python
# src/otto/link/sentinel.py
"""otto-impair argv sentinel + expire-timer discovery (spec §7).

Wire format: ``otto-impair:v1:<link-id>:<netdev>`` with percent-encoded
segments. Same philosophy as the tunnel sentinel: the timer process's argv IS
the state — discoverable via ``ps``, unambiguously otto's, owner-agnostic.
"""

from urllib.parse import quote, unquote

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
_SEGMENT_COUNT = 4

# Separate -eo flags (NOT comma-joined): procps-ng 3.3.10 mis-parses the
# combined form. `|| true` so a no-match grep isn't a command failure.
IMPAIR_PS_COMMAND: str = (
    "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-impair:' || true"
)


def encode_impair_sentinel(link_id: str, netdev: str) -> str:
    """Sentinel token tagging one placement's expire timer."""
    return ":".join(
        (
            IMPAIR_SENTINEL_PREFIX,
            IMPAIR_SENTINEL_VERSION,
            quote(link_id, safe=""),
            quote(netdev, safe=""),
        )
    )


def parse_impair_sentinel(token: str) -> tuple[str, str] | None:
    """Decode a sentinel token to ``(link_id, netdev)``; ``None`` if not ours."""
    parts = token.split(":")
    if (
        len(parts) != _SEGMENT_COUNT
        or parts[0] != IMPAIR_SENTINEL_PREFIX
        or parts[1] != IMPAIR_SENTINEL_VERSION
    ):
        return None
    return unquote(parts[2]), unquote(parts[3])


def parse_impair_ps(output: str) -> list[tuple[int, str, str]]:
    """Reconstruct ``(pid, link_id, netdev)`` from :data:`IMPAIR_PS_COMMAND` output."""
    out: list[tuple[int, str, str]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        token = next(
            (w for w in fields[2:] if w.startswith(f"{IMPAIR_SENTINEL_PREFIX}:")), None
        )
        if token is None:
            continue
        parsed = parse_impair_sentinel(token)
        if parsed is None:
            continue
        out.append((int(fields[0]), parsed[0], parsed[1]))
    return out
```

```python
# src/otto/link/placement.py
"""Impairment placements: where one direction's qdisc lands (spec §4/§9).

A placement is ``(host, netdev, direction)``. Two resolvers map a link to
placements — endpoint mode and in-path (middlebox) mode — plus the two
mandatory refusals: never a management interface, never a link with the local
host as an endpoint.
"""

import enum
from collections.abc import Collection
from dataclasses import dataclass
from ipaddress import IPv4Interface, ip_address, ip_interface

from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from .model import Link, LinkEndpoint


class FlowDirection(enum.Enum):
    """One direction of a link's traffic, in endpoint order."""

    A_TO_B = "a->b"
    B_TO_A = "b->a"


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one direction's impairment lands: a netdev on a host."""

    host_id: str
    netdev: str
    direction: FlowDirection


def parse_ip_addr(output: str) -> dict[str, list[IPv4Interface]]:
    """Parse ``ip -o addr show`` output into netdev -> addressed interfaces."""
    table: dict[str, list[IPv4Interface]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] == "inet":
            table.setdefault(fields[1], []).append(ip_interface(fields[3]))
    return table


def ensure_not_local_link(link: Link) -> None:
    """Refuse any link with the local host as an endpoint (spec §9) — the local
    host's connectivity to the bed IS otto's management path, in EVERY mode."""
    for end in (link.a, link.b):
        if end.host == BUILTIN_LOCAL_HOST_ID:
            raise ValueError(
                f"link {link.id!r} has the local host as an endpoint — otto's own "
                "path to the bed; refusing to impair it in any placement mode"
            )


def ensure_not_mgmt(
    placement: Placement, addr_table: dict[str, list[IPv4Interface]], mgmt_ip: str
) -> None:
    """Refuse a placement on the netdev carrying the host's management ip (§9).

    Only a POSITIVE match refuses: a mgmt ip invisible in the table (e.g. a
    NAT-fronted host) cannot be on the placement netdev.
    """
    for netdev, addrs in addr_table.items():
        if any(str(a.ip) == mgmt_ip for a in addrs) and netdev == placement.netdev:
            raise ValueError(
                f"refusing to impair {placement.netdev!r} on {placement.host_id!r} — "
                "it is the management interface otto reaches the host through "
                "(self-lockout)"
            )


def endpoint_placements(
    link: Link, directions: Collection[FlowDirection]
) -> list[Placement]:
    """Endpoint mode: each direction lands on its ORIGIN endpoint's interface."""
    out: list[Placement] = []
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        if direction not in directions:
            continue
        end = link.a if direction is FlowDirection.A_TO_B else link.b
        if end.interface is None:
            raise ValueError(
                f"link {link.id!r}: endpoint {end.host!r} has no named interface — "
                "not impairable (spec §4)"
            )
        out.append(Placement(end.host, end.interface, direction))
    return out


def inpath_placements(
    link: Link,
    impair_host_id: str,
    addr_table: dict[str, list[IPv4Interface]],
    directions: Collection[FlowDirection],
) -> list[Placement]:
    """In-path mode: each direction lands on the middlebox netdev FACING the
    direction's target endpoint, auto-resolved by subnet match (spec §4)."""
    out: list[Placement] = []
    for direction in (FlowDirection.A_TO_B, FlowDirection.B_TO_A):
        if direction not in directions:
            continue
        toward = link.b if direction is FlowDirection.A_TO_B else link.a
        netdev = _facing_netdev(addr_table, toward)
        if netdev is None:
            raise ValueError(
                f"{impair_host_id!r} has no interface on {toward.host!r}'s subnet "
                f"({toward.ip}) — it is not in this link's path"
            )
        out.append(Placement(impair_host_id, netdev, direction))
    return out


def _facing_netdev(
    addr_table: dict[str, list[IPv4Interface]], endpoint: LinkEndpoint
) -> str | None:
    if not endpoint.ip:
        raise ValueError(
            f"endpoint {endpoint.host!r} has an unresolved ip — cannot resolve the "
            "middlebox's facing interface"
        )
    target = ip_address(endpoint.ip)
    for netdev, addrs in addr_table.items():
        if any(target in a.network for a in addrs):
            return netdev
    return None
```

Add re-exports to `src/otto/link/__init__.py`: `FlowDirection`, `Placement` (from `.placement`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/ -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/sentinel.py src/otto/link/placement.py src/otto/link/__init__.py tests/unit/link/test_impair_sentinel.py tests/unit/link/test_placement.py
git commit -m "feat(link): impair sentinel + placement resolvers with mandatory refusals

Assisted-by: Claude Fable 5"
```

---

### Task 8: Orchestration — `impair_link` / `repair_link` / `repair_all` / `read_link_states`

**Files:**
- Create: `src/otto/link/manage.py`
- Modify: `src/otto/link/__init__.py`
- Test: `tests/unit/link/test_manage_impair.py`, `tests/unit/link/test_manage_repair.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7; host API `await host.oneshot(cmd, log=LogMode.QUIET)` (reads) and `await host.run(cmd, sudo=..., log=LogMode.QUIET) -> Results` (mutations; `need_sudo = host.current_user != "root"`); `launch_command` (Task 1); `lab.static_links()`, `lab.hosts`.
- Produces (consumed by Task 9):
  - `async impair_link(lab, ident: str, params: ImpairmentParams, *, from_host: str | None = None, expire: int | None = None) -> ImpairReport`
  - `async repair_link(lab, ident: str) -> RepairReport`
  - `async repair_all(lab) -> tuple[list[RepairReport], list[str]]` (reports, failures)
  - `async read_link_states(lab) -> list[LinkState]`
  - `ImpairReport(link_id, applied: list[AppliedPlacement])`; `AppliedPlacement(placement, params)`; `RepairReport(link_id, cleared: list[Placement], timers_cancelled: int)`; `LinkState(link, by_direction: dict[FlowDirection, ImpairmentParams | None], impairable: bool, unreachable: bool)`
  - `find_link(lab, ident) -> Link` (id-or-name lookup, rich unknown error)
  - **These functions ARE the public API** (Global Constraints: single API) — the CLI, the future GUI topology overlay (`read_link_states` = the impairment layer, consumed like `discover_tunnels`), and direct importers all call exactly these. No printing, no CLI concepts (exit codes, colors) in this module; docstrings written for library users.

**Behavioral contract (each numbered item gets a test below):**
1. Endpoint mode, both directions default; `--from` narrows to the originating direction; `--from` a non-endpoint → `ValueError`.
2. In-path mode when `link.impair` set: `ip -o addr show` fetched from the middlebox, placements land there.
3. Refusals run before any mutation (local-link, mgmt-per-placement).
4. Merge read-modify-replace: read current params, `params.merged_over(current)`, `validate()`, apply `tc qdisc replace`; post-apply verify (re-read == merged) else raise; merged-to-empty clears instead.
5. Every impair/repair cancels existing timers for the link's placements first; `expire` launches a fresh sentinel-tagged timer via `launch_command` (run with sudo — the systemd-run path degrades to setsid under sudo, by design of the fold).
6. No half-impairments: on any failure mid-way, already-applied placements are restored to their PRIOR state (re-apply prior params, or clear if there were none), then re-raise.
7. `repair_link`: cancel timers, clear each placement that has state; report cleared placements + timer count.
8. `repair_all`: iterate `lab.static_links()`; `ValueError` per link (not impairable: implicit/mgmt/local/unnamed) → silently skipped; `RuntimeError` (host down / command failed) → collected into `failures`.
9. `read_link_states` (for `list`): never raises per-link — refusal/structural errors → `impairable=False`; unreachable placement host → `unreachable=True`; else per-direction parsed params. Reads via `oneshot` (no sudo needed for `tc qdisc show`).
10. Impairer selection per placement host: `getattr(host, "impairer", None)`; falsy → `ValueError` "no impairer support"; `build_impairer(name)()` instance used for all commands on that placement. **The registry round-trip** (spec §12): a fake impairer selected via the pin drives the exact commands run.

All timeouts: `asyncio.wait_for(..., 30.0)` (module constant `_IMPAIR_HOST_TIMEOUT = 30.0`); timeout/OSError on a host → `RuntimeError` naming the host (fail-loud, never skip).

- [ ] **Step 1: Write the failing tests**

Scripted fake pattern (mirror `tests/unit/tunnel/test_manage_add.py:32-96` — real `CommandResult`/`Results`, never SimpleNamespace). Shared test fake at the top of `tests/unit/link/test_manage_impair.py`:

```python
# tests/unit/link/test_manage_impair.py
"""impair_link orchestration against scripted fake hosts (no bed).

The fake dispatches on command text the way the tunnel manage fakes do, and
returns REAL CommandResult/Results objects (global constraint)."""

import asyncio
from dataclasses import dataclass, field

import pytest

from otto.link.impairer import LinkImpairer, register_impairer
from otto.link.manage import ImpairReport, find_link, impair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams
from otto.link.placement import FlowDirection
from otto.link.sentinel import IMPAIR_PS_COMMAND
from otto.logger.mode import LogMode
from otto.result import CommandResult, Results, Status

CARROT_ADDR = (
    "3: eth1    inet 10.10.200.11/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.11/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
)
TOMATO_ADDR = (
    "3: eth1    inet 10.10.200.12/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.200    inet 10.10.202.12/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)
PEPPER_ADDR = (
    "3: eth1    inet 10.10.200.13/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.13/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
    "5: eth1.200    inet 10.10.202.13/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)


@dataclass
class FakeHost:
    """Self-consistent fake: `ip -o addr` -> addr_text; `tc qdisc show` -> qdisc_text
    (a queue: pop while >1 left, then repeat); IMPAIR_PS_COMMAND -> ps_text; every
    mutation is recorded verbatim in `commands` and succeeds unless `fail_on`
    matches."""

    id: str
    ip: str
    addr_text: str = ""
    qdisc_texts: list[str] = field(default_factory=lambda: [""])
    ps_text: str = ""
    impairer: str = "netem"
    current_user: str = "vagrant"
    fail_on: str | None = None
    commands: list[str] = field(default_factory=list)
    sudo_commands: list[str] = field(default_factory=list)

    def _result(self, cmd: str) -> CommandResult:
        if self.fail_on is not None and self.fail_on in cmd:
            return CommandResult(status=Status.Failed, value="", command=cmd, msg="scripted failure")
        if cmd == "ip -o addr show":
            return CommandResult(status=Status.Success, value=self.addr_text, command=cmd)
        if cmd == IMPAIR_PS_COMMAND:
            return CommandResult(status=Status.Success, value=self.ps_text, command=cmd)
        if cmd.startswith("tc qdisc show"):
            text = self.qdisc_texts.pop(0) if len(self.qdisc_texts) > 1 else self.qdisc_texts[0]
            return CommandResult(status=Status.Success, value=text, command=cmd)
        return CommandResult(status=Status.Success, value="", command=cmd)

    async def oneshot(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        return self._result(cmd)

    async def run(self, cmd: str, sudo: bool = False, **_: object) -> Results:
        self.commands.append(cmd)
        if sudo:
            self.sudo_commands.append(cmd)
        return Results.collect([self._result(cmd)])


@dataclass
class FakeLab:
    hosts: dict
    links: list

    def static_links(self) -> list:
        return list(self.links)


LINK = Link(
    a=LinkEndpoint(host="carrot_seed", interface="eth1.100", ip="10.10.201.11"),
    b=LinkEndpoint(host="tomato_seed", interface="eth1.200", ip="10.10.202.12"),
    name="edge",
)
INPATH = Link(a=LINK.a, b=LINK.b, name="dataplane", impair="pepper_seed")


def _bed(link: Link = LINK, **host_kw) -> tuple[FakeLab, FakeHost, FakeHost, FakeHost]:
    carrot = FakeHost(id="carrot_seed", ip="10.10.200.11", addr_text=CARROT_ADDR, **host_kw)
    tomato = FakeHost(id="tomato_seed", ip="10.10.200.12", addr_text=TOMATO_ADDR)
    pepper = FakeHost(id="pepper_seed", ip="10.10.200.13", addr_text=PEPPER_ADDR)
    lab = FakeLab(
        hosts={h.id: h for h in (carrot, tomato, pepper)}, links=[link]
    )
    return lab, carrot, tomato, pepper
```

Test classes (complete bodies required; asserted behavior per the numbered contract):

```python
class TestFindLink:
    def test_by_id_and_by_name(self) -> None:
        lab, *_ = _bed()
        assert find_link(lab, LINK.id) is lab.links[0]
        assert find_link(lab, "edge") is lab.links[0]

    def test_unknown_lists_known_ids(self) -> None:
        lab, *_ = _bed()
        with pytest.raises(ValueError, match=f"known: {LINK.id}"):
            find_link(lab, "nope")


class TestEndpointImpair:
    @pytest.mark.asyncio
    async def test_both_directions_apply_on_both_endpoints(self) -> None:
        lab, carrot, tomato, _ = _bed()
        report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert [a.placement.host_id for a in report.applied] == ["carrot_seed", "tomato_seed"]
        assert "tc qdisc replace dev eth1.100 root netem delay 50ms" in carrot.sudo_commands
        assert "tc qdisc replace dev eth1.200 root netem delay 50ms" in tomato.sudo_commands

    @pytest.mark.asyncio
    async def test_from_narrows_to_one_direction(self) -> None:
        lab, carrot, tomato, _ = _bed()
        report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="tomato_seed")
        assert [a.placement.direction for a in report.applied] == [FlowDirection.B_TO_A]
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_from_non_endpoint_rejected(self) -> None:
        lab, *_ = _bed()
        with pytest.raises(ValueError, match="--from 'pepper_seed' is not an endpoint"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0), from_host="pepper_seed")

    @pytest.mark.asyncio
    async def test_merge_reads_current_and_replaces(self) -> None:
        lab, carrot, _, _ = _bed()
        applied = "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"
        merged = "qdisc netem 8001: root refcnt 2 limit 1000 delay 10ms loss 2%\n"
        carrot.qdisc_texts = [applied, merged]  # pre-read, then post-apply verify
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=10.0, loss_pct=2.0), from_host="carrot_seed")
        assert "tc qdisc replace dev eth1.100 root netem delay 10ms loss 2%" in carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_merged_to_empty_clears_instead(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n", ""]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=0.0), from_host="carrot_seed")
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert not any("replace" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_post_apply_verify_mismatch_raises(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", ""]  # post-apply read shows nothing applied
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")


class TestInpath:
    @pytest.mark.asyncio
    async def test_placements_land_on_middlebox(self) -> None:
        lab, carrot, tomato, pepper = _bed(link=INPATH)
        report = await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=50.0))
        assert {a.placement.host_id for a in report.applied} == {"pepper_seed"}
        assert {a.placement.netdev for a in report.applied} == {"eth1.100", "eth1.200"}
        assert not carrot.sudo_commands and not tomato.sudo_commands


class TestRefusalsAndSafety:
    @pytest.mark.asyncio
    async def test_local_endpoint_refused_before_any_command(self) -> None:
        from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
        local_link = Link(
            a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth0", ip="10.0.0.1"),
            b=LINK.b, name="to-local",
        )
        lab, carrot, tomato, _ = _bed(link=local_link)
        with pytest.raises(ValueError, match="local host as an endpoint"):
            await impair_link(lab, "to-local", ImpairmentParams(delay_ms=1.0))
        assert not carrot.commands and not tomato.commands

    @pytest.mark.asyncio
    async def test_mgmt_interface_placement_refused(self) -> None:
        mgmt_link = Link(
            a=LinkEndpoint(host="carrot_seed", interface="eth1", ip="10.10.200.11"),
            b=LinkEndpoint(host="tomato_seed", interface="eth1", ip="10.10.200.12"),
            name="mgmt-edge",
        )
        lab, carrot, _, _ = _bed(link=mgmt_link)
        with pytest.raises(ValueError, match="management interface"):
            await impair_link(lab, "mgmt-edge", ImpairmentParams(delay_ms=1.0))
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_rollback_restores_prior_state_on_partial_failure(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",  # verify ok
        ]
        tomato.fail_on = "tc qdisc replace"  # second placement fails
        with pytest.raises(RuntimeError):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        # carrot restored to its PRIOR params, not cleared
        assert carrot.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"

    @pytest.mark.asyncio
    async def test_unreachable_host_fails_loud_with_host_name(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("boom")

        carrot.oneshot = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="carrot_seed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0))


class TestExpireTimers:
    @pytest.mark.asyncio
    async def test_expire_launches_sentinel_tagged_timer(self) -> None:
        lab, carrot, _, _ = _bed()
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0),
                          from_host="carrot_seed", expire=30)
        # skip the qdisc-mutation command; find the timer launch
        launch = next(c for c in carrot.sudo_commands if "otto-impair:" in c)
        assert "otto-impair:v1:" in launch and "eth1.100" in launch
        assert "sleep 30 && tc qdisc del dev eth1.100 root" in launch
        assert launch.startswith("if command -v systemd-run")

    @pytest.mark.asyncio
    async def test_impair_cancels_stale_timers_first(self) -> None:
        from otto.link.sentinel import encode_impair_sentinel
        lab, carrot, _, _ = _bed()
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        carrot.ps_text = f"  4242 05:00 {token} -c sleep 600 && tc qdisc del dev eth1.100 root\n"
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert "kill 4242" in carrot.sudo_commands


class TestRegistryRoundtrip:
    """Spec §12: a fake impairer selected via the host pin drives the EXACT
    commands run — registration -> selection -> build -> orchestration."""

    @pytest.mark.asyncio
    async def test_fake_impairer_commands_execute(self) -> None:
        from typing import ClassVar

        class _Recorder(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

            def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
                return f"FAKE-APPLY {netdev} {params.describe()}"

            def read_command(self, netdev: str) -> str:
                return f"FAKE-READ {netdev}"

            def clear_command(self, netdev: str) -> str:
                return f"FAKE-CLEAR {netdev}"

            def parse_read(self, output: str) -> ImpairmentParams | None:
                return ImpairmentParams(delay_ms=50.0) if "APPLIED" in output else None

        register_impairer("recorder", _Recorder)
        lab, carrot, _, _ = _bed()
        carrot.impairer = "recorder"  # the host-level pin, post-resolution

        def _fake_result(cmd: str) -> CommandResult:
            if cmd.startswith("FAKE-READ"):
                text = carrot.qdisc_texts.pop(0) if len(carrot.qdisc_texts) > 1 else carrot.qdisc_texts[0]
                return CommandResult(status=Status.Success, value=text, command=cmd)
            return FakeHost._result(carrot, cmd)

        carrot._result = _fake_result  # type: ignore[method-assign]
        carrot.qdisc_texts = ["", "APPLIED"]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert "FAKE-APPLY eth1.100 delay 50ms" in carrot.sudo_commands
        assert not any(c.startswith("tc ") for c in carrot.sudo_commands)  # netem never ran

    @pytest.mark.asyncio
    async def test_host_without_impairer_support_fails_loud(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.impairer = ""  # e.g. an embedded host: no impairer attribute/value
        with pytest.raises(ValueError, match="no impairer support"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0), from_host="carrot_seed")
```

`tests/unit/link/test_manage_repair.py` (import the fakes from `test_manage_impair` — same directory):

```python
class TestRepair:
    @pytest.mark.asyncio
    async def test_repair_clears_impaired_placements_and_timers(self) -> None:
        lab, carrot, tomato, _ = _bed()
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        carrot.ps_text = f"  4242 05:00 {token} -c sleep 600\n"
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        tomato.qdisc_texts = [""]  # b-side has nothing to clear
        report = await repair_link(lab, "edge")
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert "kill 4242" in carrot.sudo_commands
        assert not any("del" in c for c in tomato.sudo_commands)
        assert [p.netdev for p in report.cleared] == ["eth1.100"]
        assert report.timers_cancelled == 1

    @pytest.mark.asyncio
    async def test_repair_all_skips_unimpairable_collects_failures(self) -> None:
        unnamed = Link(a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare")
        lab, carrot, _, _ = _bed()
        lab.links.append(unnamed)
        carrot.fail_on = "tc qdisc show"  # the impairable link's read fails
        reports, failures = await repair_all(lab)
        assert reports == []  # the impairable link failed, the bare one skipped
        assert len(failures) == 1 and LINK.id in failures[0]


class TestReadStates:
    @pytest.mark.asyncio
    async def test_states_report_per_direction_params(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert state.impairable and not state.unreachable
        assert state.by_direction[FlowDirection.A_TO_B] == ImpairmentParams(delay_ms=50.0)
        assert state.by_direction[FlowDirection.B_TO_A] is None

    @pytest.mark.asyncio
    async def test_unimpairable_link_marked_not_error(self) -> None:
        bare = Link(a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare")
        lab, *_ = _bed(link=bare)
        (state,) = await read_link_states(lab)
        assert not state.impairable

    @pytest.mark.asyncio
    async def test_unreachable_host_marks_state_uncertain(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("down")

        carrot.oneshot = _boom  # type: ignore[method-assign]
        (state,) = await read_link_states(lab)
        assert state.unreachable
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_manage_impair.py tests/unit/link/test_manage_repair.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `otto.link.manage` doesn't exist

- [ ] **Step 3: Implement `src/otto/link/manage.py`**

Structure (write complete; the contract is fully pinned by Step 1's tests):

```python
"""Impair/repair/list orchestration — kernel qdisc state is the ONLY state.

Reads go through ``host.oneshot`` (no privilege needed); mutations through
``host.run(cmd, sudo=host.current_user != "root")``. Every host call is
wrapped in ``asyncio.wait_for(..., _IMPAIR_HOST_TIMEOUT)`` and a down host is
a loud, host-named ``RuntimeError`` — never a skip (spec §9, dev-VM rule).
"""

import asyncio
import contextlib
from dataclasses import dataclass, field as dc_field
from typing import TYPE_CHECKING, Any

from ..host.detached import launch_command
from ..logger.mode import LogMode
from .impairer import LinkImpairer, build_impairer
from .model import Link
from .params import ImpairmentParams
from .placement import (
    FlowDirection,
    Placement,
    endpoint_placements,
    ensure_not_local_link,
    ensure_not_mgmt,
    inpath_placements,
    parse_ip_addr,
)
from .sentinel import IMPAIR_PS_COMMAND, encode_impair_sentinel, parse_impair_ps

_IMPAIR_HOST_TIMEOUT = 30.0
_BOTH = frozenset({FlowDirection.A_TO_B, FlowDirection.B_TO_A})
```

Key functions (all complete in the real file):

```python
def find_link(lab: Any, ident: str) -> Link:
    links = lab.static_links()
    for link in links:
        if link.id == ident or (link.name is not None and link.name == ident):
            return link
    known = ", ".join(sorted(link.id for link in links)) or "<none>"
    raise ValueError(f"no link {ident!r} in the loaded lab (known: {known})")


async def _oneshot(host: Any, cmd: str) -> Any:
    try:
        result = await asyncio.wait_for(
            host.oneshot(cmd, log=LogMode.QUIET), _IMPAIR_HOST_TIMEOUT
        )
    except (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError) as e:
        raise RuntimeError(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    if not result.is_ok:
        raise RuntimeError(f"{cmd!r} failed on {host.id!r}: {result.msg or result.value}")
    return result


async def _root_run(host: Any, cmd: str) -> Any:
    need_sudo = host.current_user != "root"
    try:
        results = await asyncio.wait_for(
            host.run(cmd, sudo=need_sudo, log=LogMode.QUIET), _IMPAIR_HOST_TIMEOUT
        )
    except (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError) as e:
        raise RuntimeError(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e
    return results[0]
```

(FakeHost.current_user is `"vagrant"` so `sudo=True` — the tests' `sudo_commands` assertions depend on this.) Then `_host(lab, host_id)` (KeyError → ValueError "not in the loaded lab"), `_impairer_for(host)` (contract item 10), `_directions(link, from_host)` (item 1), `_resolve_placements(lab, link, directions)` (items 2/3: `ensure_not_local_link` first; in-path via `parse_ip_addr(await _oneshot(mbox, "ip -o addr show").value)`; then per placement fetch that host's addr table (cache per call) and `ensure_not_mgmt(placement, table, host.ip)`), `_read_placement(host, impairer, netdev)` (oneshot + `parse_read`), `impair_link` (items 4/5/6 — track `(placement, merged, prior)` per applied placement; rollback re-applies `prior` or clears; cancel timers before mutating; launch timer after verify when `expire`), `_launch_timer` (item 5: `launch_command(encode_impair_sentinel(link.id, placement.netdev), ["bash", "-c", f"sleep {int(expire)} && {impairer.clear_command(placement.netdev)}"])` via `_root_run`), `_cancel_timers(host, link_id, netdev) -> int` (scan `IMPAIR_PS_COMMAND` via plain `oneshot` best-effort — a scan failure returns 0 rather than raising — then `kill <pids>` via `_root_run`), `repair_link` (item 7), `repair_all` (item 8), `read_link_states` + `_link_state` (item 9 — every read wrapped so `ValueError` → `impairable=False`, `RuntimeError` → `unreachable=True`).

Report dataclasses exactly as the Interfaces block names them. Re-export from `src/otto/link/__init__.py`: `impair_link`, `repair_link`, `repair_all`, `read_link_states`, `find_link`, `ImpairReport`, `RepairReport`, `LinkState`, `AppliedPlacement`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/ -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS (all link unit files)

- [ ] **Step 5: Commit**

```bash
git add src/otto/link/manage.py src/otto/link/__init__.py tests/unit/link/test_manage_impair.py tests/unit/link/test_manage_repair.py
git commit -m "feat(link): impair/repair/list orchestration — merge, verify, rollback, expire timers

Assisted-by: Claude Fable 5"
```

---

### Task 9: `otto link` CLI + completion + registration

**Files:**
- Create: `src/otto/cli/link.py`
- Modify: `src/otto/cli/builtin_commands.py` (register after the tunnel block, lines 70-78), `src/otto/configmodule/completion_cache.py` (add `collect_link_ids`)
- Test: `tests/unit/link/test_cli.py`, extend `tests/unit/configmodule/test_completion_host_ids.py` sibling file with `tests/unit/configmodule/test_completion_link_ids.py`

**Interfaces:**
- Consumes: Task 8's API; CLI helpers from `otto.cli.tunnel`'s idiom (`get_lab`, `get_repos`, `rprint`, `async_typer_command`); `make_static_link_id`/`LinkEndpoint` for completion ids.
- Produces: `link_app` (typer app named "link"); `collect_link_ids(repos: list[Repo]) -> list[str]`.

**Command contract:**
- `otto link impair <link> [--delay S] [--jitter S] [--loss S] [--rate S] [--corrupt S] [--duplicate S] [--reorder S] [--from HOST] [--expire N]` — parse options via Task 2 parsers. Usage errors (bad value, no param option at all, `--expire < 1`) → red message + `Exit(2)`, raised OUTSIDE the orchestration `try`. Known orchestration failures (`ValueError`/`RuntimeError`) → red message + `Exit(1)`. Success prints one green line per applied placement: `impaired <id> a->b on <host>/<netdev>: delay 50ms loss 2%` (or `cleared` when merged-to-empty).
- `otto link repair [<link>] [--all]` — exactly one of link/`--all` (else `Exit(2)` outside the try). Per-link: prints cleared placements + timers cancelled; `--all` prints a summary and exits 1 if `failures` is non-empty (listing them).
- `otto link list` — one line per `LinkState`: `<id>  <a.host>@<iface> <-> <b.host>@<iface>  via <impair|- >  a->b: <describe()|-|?>  b->a: <...>`; not-impairable links show `n/a` for both directions; a partial scan (any unreachable) appends the yellow `partial scan` warning line (mirror tunnel.py:205-209).

- [ ] **Step 1: Write the failing tests**

`tests/unit/link/test_cli.py` — mirror `tests/unit/tunnel/test_cli.py` conventions (module-level `runner = CliRunner()`; `patch("otto.cli.link.get_lab", ...)` + `AsyncMock`; NO bed markers). Complete tests to write:

```python
class TestImpairCommand:
    def test_happy_path_prints_placements(self) -> None:
        report = ImpairReport(link_id="lnk-abc", applied=[
            AppliedPlacement(Placement("carrot_seed", "eth1.100", FlowDirection.A_TO_B),
                             ImpairmentParams(delay_ms=50.0)),
        ])
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["impair", "edge", "--delay", "50"])
        assert result.exit_code == 0, result.output
        assert "impaired lnk-abc" in result.output and "carrot_seed/eth1.100" in result.output

    def test_no_param_options_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge"])
        assert result.exit_code == 2
        assert "at least one parameter option" in result.output

    def test_bad_unit_is_usage_error_2_not_1(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--rate", "10"])
        assert result.exit_code == 2
        assert "explicit unit" in result.output

    def test_known_failure_exits_1(self) -> None:
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", AsyncMock(side_effect=ValueError("management interface"))),
        ):
            result = runner.invoke(link_app, ["impair", "edge", "--delay", "50"])
        assert result.exit_code == 1
        assert "management interface" in result.output


class TestRepairCommand:
    def test_neither_link_nor_all_exits_2(self) -> None:
        result = runner.invoke(link_app, ["repair"])
        assert result.exit_code == 2

    def test_both_link_and_all_exits_2(self) -> None:
        result = runner.invoke(link_app, ["repair", "edge", "--all"])
        assert result.exit_code == 2

    def test_repair_all_failures_exit_1(self) -> None:
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_all", AsyncMock(return_value=([], ["lnk-abc: host down"]))),
        ):
            result = runner.invoke(link_app, ["repair", "--all"])
        assert result.exit_code == 1
        assert "lnk-abc: host down" in result.output


class TestListCommand:
    def test_rows_and_partial_scan_warning(self) -> None:
        state = LinkState(link=LINK, impairable=True, unreachable=False,
                          by_direction={FlowDirection.A_TO_B: ImpairmentParams(delay_ms=50.0),
                                        FlowDirection.B_TO_A: None})
        down = LinkState(link=INPATH, impairable=True, unreachable=True,
                         by_direction={FlowDirection.A_TO_B: None, FlowDirection.B_TO_A: None})
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[state, down])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0
        assert "delay 50ms" in result.output
        assert "partial scan" in result.output


class TestCompleter:
    def test_link_completer_filters_prefix(self) -> None:
        with (
            patch("otto.cli.link.get_repos", return_value=[]),
            patch("otto.cli.link.collect_link_ids", return_value=["edge", "dataplane", "lnk-1"]),
        ):
            assert _link_completer(None, "e") == ["edge"]
```

`tests/unit/configmodule/test_completion_link_ids.py` — mirror `test_completion_host_ids.py`'s `_repo_with_hosts(tmp_path, ...)` fixture style, with a lab.json carrying a `links` array:

```python
def test_collect_link_ids_names_and_derived(tmp_path: Path) -> None:
    # lab.json with two links: one named ("edge"), one unnamed (id = "a_seed--b_seed")
    repo = _repo_with_lab(tmp_path, hosts=[_CARROT, _TOMATO], links=[
        {"endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}], "name": "edge"},
        {"endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}, ]},
    ])
    assert collect_link_ids([repo]) == ["carrot_seed--tomato_seed", "edge"]
```

(Adapt `_repo_with_lab` from the sibling file's `_repo_with_hosts` helper — same tmp_path lab.json write, plus the links array. Read that file first and follow it.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/link/test_cli.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: FAIL — `otto.cli.link` doesn't exist

- [ ] **Step 3: Implement**

`src/otto/configmodule/completion_cache.py` — add near `collect_host_ids` (line 835), following its repo/lab-file iteration pattern exactly (read that function first, reuse its internal lab-data access):

```python
def collect_link_ids(repos: list["Repo"]) -> list[str]:
    """Static link ids/names for ``otto link`` completion — name if set, else the
    ``lo--hi`` static id. Pure lab-data derivation (sync, no live scan)."""
```

Body: iterate the same lab data source `collect_host_ids` uses; for each raw `links` entry compute `spec_name = entry.get("name")` or sort the two endpoint host ids and join with `"--"` (exactly `make_static_link_id`'s no-name form, model.py:75-78); return `sorted(set(...))`.

`src/otto/cli/link.py` — mirror `src/otto/cli/tunnel.py` structure: typer app (`name="link"`, help `"Inspect and impair the lab's static links."`, `no_args_is_help=True`, same `context_settings`), callback, `_link_completer` (never raises; `collect_link_ids(get_repos())` filtered by prefix, sorted), then the three commands per the Command contract above. The impair parse step (usage errors OUT of the orchestration try):

```python
    given: dict[str, str | None] = {
        "--delay": delay, "--jitter": jitter, "--loss": loss, "--rate": rate,
        "--corrupt": corrupt, "--duplicate": duplicate, "--reorder": reorder,
    }
    try:
        params = _parse_params(given)
    except ValueError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    if all(v is None for v in given.values()):
        rprint("[red]impair needs at least one parameter option (--delay/--loss/--rate/...).[/red]")
        raise typer.Exit(2)
    lab = get_lab()
    try:
        report = await impair_link(lab, link, params, from_host=from_host, expire=expire)
    except (ValueError, RuntimeError) as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
```

with `_parse_params` mapping each given option through `parse_time_ms`/`parse_percent`/`parse_rate` into `ImpairmentParams` kwargs. `--expire` is `int | None = typer.Option(None, "--expire", min=1, ...)` (typer enforces the floor → its own usage error).

`src/otto/cli/builtin_commands.py` — after the tunnel block:

```python
    register_cli_command(
        "link",
        "otto.cli.link:link_app",
        help="Inspect and impair the lab's static links.",
        # Short-lived host-touching group like tunnel: no per-invocation
        # output directory of its own.
        output_dir=False,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/link/test_cli.py tests/unit/configmodule/test_completion_link_ids.py -p no:cacheprovider --no-cov -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/link.py src/otto/cli/builtin_commands.py src/otto/configmodule/completion_cache.py tests/unit/link/test_cli.py tests/unit/configmodule/test_completion_link_ids.py
git commit -m "feat(cli): otto link impair/repair/list with static-link completion

Assisted-by: Claude Fable 5"
```

---

### Task 10: Docs

**Files:**
- Create: `docs/guide/link.md`
- Modify: `docs/guide/index.rst` (toctree: add `link` before `tunnel`), `docs/api/link.rst` (automodule blocks for the new submodules), `docs/guide/lab-config.md` (links section: `impair` field), `docs/guide/cli-reference.md` (link group summary), `docs/guide/tunnel.md` (one cross-ref sentence: links vs tunnels)

**Interfaces:** none produced; consumes the shipped behavior (write docs that match Tasks 2-9 EXACTLY — no aspirational features).

- [ ] **Step 1: Write `docs/guide/link.md`** — MyST markdown, hand-authored prose like `docs/guide/tunnel.md` (headers per subcommand, fenced usage blocks, option tables, `{doc}`/`{ref}` cross-refs, `{note}` admonitions). Required content:
  - Links vs tunnels taxonomy (underlay/overlay, one paragraph; cross-ref `{doc}`tunnel``).
  - `## Impairing a link: otto link impair` — usage block, option table INCLUDING the unit rules verbatim (bare time = ms; bare percent = %; `--rate` requires explicit unit), both-directions default + RTT math note (`--delay 50` both ways = 100 ms RTT), merge semantics with a worked `--delay 20` → `--loss 2 --delay 10` example, zero-clears, `--from`, `--expire` (opt-in, default indefinite).
  - `## In-path impairment` — the lab.json `impair` field (bare host id), facing auto-resolution, endpoint purity rationale, netdev-granularity caveat (two links sharing a middlebox interface are impaired together).
  - `## Repairing: otto link repair` (+ `--all`), `## Listing: otto link list` (status column semantics incl. `?` unreachable), `## Safety` (both refusals, elevation requirement), `## Custom impairers` (`register_impairer` from an init module, host `impairer` pin / `valid_impairers` menu, `[host_preferences]` `impairer` key).
  - `## Python API` — the single-API story: a short working snippet (`from otto.link import impair_link, read_link_states, ImpairmentParams` … `await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))`), noting the CLI and the monitor/GUI consume exactly this surface; cross-ref `{doc}`API reference <../api/link>``.
- [ ] **Step 2: Wire toctrees + cross-refs** — `docs/guide/index.rst` toctree gains `link`; `docs/api/link.rst` gains `.. automodule::` blocks for `otto.link.params`, `otto.link.impairer`, `otto.link.netem`, `otto.link.placement`, `otto.link.manage`, `otto.link.sentinel` (mirror the existing per-submodule style in that file); `docs/guide/lab-config.md` links section documents `impair`; `docs/guide/cli-reference.md` gains the `otto link` group.
- [ ] **Step 3: Build the docs gate**

Run: `make docs`
Expected: exit 0 — zero warnings (`-W`). Fix any nitpicky cross-ref/docstring warnings surfaced by the new automodule blocks now.

- [ ] **Step 4: Commit**

```bash
git add docs/guide/link.md docs/guide/index.rst docs/api/link.rst docs/guide/lab-config.md docs/guide/cli-reference.md docs/guide/tunnel.md
git commit -m "docs: otto link guide — impair/repair/list, in-path model, custom impairers

Assisted-by: Claude Fable 5"
```

---

### Task 11: Live-bed e2e (VLAN fixture)

The peers' only shared network (10.10.200.0/24 on `eth1`) is the mgmt path, so the fixture creates VLAN sub-interfaces as the data plane — **verified live 2026-07-10**: VLAN tags pass the VirtualBox network; netem on `eth1.100` moved ping RTT 1.1ms → 51.6ms while the untagged mgmt path stayed at 0.9ms. Chris approved this fixture approach and its `sudo ip` commands.

**Topology (fixture-created, torn down after):**
- VLAN 100 (10.10.201.0/24): carrot `eth1.100` = .11, pepper `eth1.100` = .13
- VLAN 200 (10.10.202.0/24): tomato `eth1.200` = .12, pepper `eth1.200` = .13
- pepper: `sysctl net.ipv4.ip_forward=1` (record prior value, restore on teardown; docker peers usually already have 1)
- carrot: `ip route add 10.10.202.0/24 via 10.10.201.13`; tomato: `ip route add 10.10.201.0/24 via 10.10.202.13` (routes die with the VLAN links)
- Links (constructed as `Link` objects on the runtime `Lab`, no lab.json file):
  - `edge` — carrot@eth1.100 ↔ pepper@eth1.100 (endpoint-mode target, same subnet, direct)
  - `dataplane` — carrot@eth1.100 ↔ tomato@eth1.200, `impair="pepper_seed"` (in-path target; traffic genuinely routes through pepper)

**Files:**
- Create: `tests/e2e/test_link_impair_e2e.py`

**Structure (mirror `tests/e2e/test_tunnel_e2e.py` conventions VERBATIM — read it first):**
- `pytestmark = [pytest.mark.integration, pytest.mark.hops, pytest.mark.xdist_group("link_impair_e2e")]`
- Hosts via `from tests._fixtures.labdata import host_data, make_host`; `_build_host(ne)` local helper mirroring tunnel e2e :109, EXTENDED with `interfaces={...}` kwargs using `from otto.host.interface import Interface` — carrot: `{"eth1.100": Interface(ip="10.10.201.11")}`, tomato: `{"eth1.200": Interface(ip="10.10.202.12")}`, pepper: `{"eth1.100": Interface(ip="10.10.201.13"), "eth1.200": Interface(ip="10.10.202.13")}`.
- Reachability preflight: reuse the `_assert_reachable` fail-loud pattern (tunnel e2e :124-138) for all three peers. NEVER skip.
- `impair_lab` module-scoped async fixture: preflight → build `Lab(name="impair_e2e")` + hosts → run the VLAN/route/sysctl setup commands on the peers (via `host.run(cmd, sudo=True)`) → append the two `Link` objects to `lab.links` → yield → teardown: `repair_all(lab)` under suppress, delete VLAN links (`ip link del eth1.100` etc — removes qdiscs and routes with them), restore ip_forward prior value, close hosts.
- `_final_leftover_sweep` module-autouse fixture mirroring tunnel e2e :193-197: after ALL tests, freshly-built hosts assert (a) `IMPAIR_PS_COMMAND` finds no `otto-impair:` processes on any peer, (b) `tc qdisc show dev eth1` on each peer shows NO netem (mgmt interface untouched — the load-bearing safety assertion), (c) no `eth1.100`/`eth1.200` links remain.
- RTT helper: `async def _avg_rtt_ms(host, target_ip) -> float` — `await host.oneshot(f"ping -c 3 -i 0.3 -W 2 {target_ip}", ...)`, parse the `rtt min/avg/max/mdev = a/b/c/d ms` line: `float(line.split("=")[1].split("/")[1])`.

**Tests (4):**

- [ ] **Test 1 `test_endpoint_impair_delay_and_repair`:** baseline = `_avg_rtt_ms(carrot, "10.10.201.13")`; `await impair_link(lab, "edge", ImpairmentParams(delay_ms=100.0))`; assert applied placements = carrot/eth1.100 + pepper/eth1.100; delta = new avg − baseline ≥ 150ms (100ms each way, generous margin); `repair_link` → RTT back within 20ms of baseline; also assert `read_link_states` shows the params while impaired.
- [ ] **Test 2 `test_inpath_placements_and_endpoint_purity`:** `await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=100.0))`; assert every applied placement's `host_id == "pepper_seed"` (netdevs `{eth1.100, eth1.200}`); RTT carrot→10.10.202.12 delta ≥ 150ms; **purity**: `tc qdisc show dev eth1.100` on carrot shows no netem (the devices under test stay pure — the whole point of in-path); repair, RTT restored.
- [ ] **Test 3 `test_expire_self_heals`:** `impair_link(lab, "edge", ImpairmentParams(delay_ms=100.0), expire=8)`; assert impaired via `read_link_states`; poll every 2s (max 60s) until the a→b direction reads `None`; then assert `IMPAIR_PS_COMMAND` on carrot shows no timer for the link. Use a plain `while` loop with `await asyncio.sleep(2)` — condition-based waiting, no bare fixed sleep.
- [ ] **Test 4 `test_merge_and_out_of_band_clear`:** `impair_link(..., ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")` then `impair_link(..., ImpairmentParams(loss_pct=5.0), from_host="carrot_seed")`; `read_link_states` shows BOTH delay 50ms and loss 5% on a→b (merge held on a real kernel); out-of-band `sudo tc qdisc del dev eth1.100 root` on carrot; `read_link_states` now shows a→b `None` (observed truth, nothing stale); finish with `repair_link` (no-op clean).

Also include `test_mgmt_link_refused` (cheap, no traffic): construct a link between carrot@eth1↔tomato@eth1 (mgmt interfaces) on the lab, `pytest.raises(ValueError, match="management interface")` — proving the refusal against the real bed's addr tables.

- [ ] **Run the module** (single pass, bed rules):

Run: `uv run pytest tests/e2e/test_link_impair_e2e.py -p no:cacheprovider --no-cov -o addopts="" -x`
Expected: 5 passed; final sweep clean. If a host is down: loud host-named failure (NOT skip).

- [ ] **Commit**

```bash
git add tests/e2e/test_link_impair_e2e.py
git commit -m "test(e2e): link impairment live-bed suite — VLAN data plane, in-path via pepper

Assisted-by: Claude Fable 5"
```

---

### Task 12: Full gates

- [ ] **Lint + format:** `uv run ruff format && uv run ruff check --fix` then `uv run ruff check` — Expected: clean (implementation agents routinely miss `ruff format` — run it explicitly).
- [ ] **Typecheck:** `uv run ty check src` — Expected: 0 errors (worktree is synced; if `ty` reports phantom unresolved imports, run `uv sync` once first).
- [ ] **Docs:** `make docs` — Expected: exit 0 (`-W`).
- [ ] **Full coverage gate (needs the bed):** `make coverage` — Expected: exit 0, `--cov-fail-under=94` satisfied, JUnit at `reports/junit/` shows 0 failures/errors. Triage any failure with `scripts/junit_failures.py`, never by rerunning in a loop.
- [ ] **Commit anything the gates changed** (formatting only, if it comes to that), then hand the branch to the whole-branch final review (SDD flow).

---

## Self-review notes (writing-plans checklist applied)

- **Spec coverage:** §1 taxonomy → Task 10 docs; §2 scope/deferrals → no diversion/`--via`/facing anywhere; §3 CLI+units+direction+merge → Tasks 2/9; §4 placements+granularity → Task 7 (granularity caveat documented Task 10); §5 registry+pin+selection → Tasks 3/6/8; §6 state/discovery/list → Tasks 4/8/9; §7 expiry → Tasks 1/8; §8 elevation → Task 8 (`_root_run`); §9 refusals → Tasks 7/8/11; §10 lab.json field → Task 5; §11 module layout → File Structure; §12 testing → Tasks 2-9 unit + Task 11 e2e (registry round-trip in Task 8, refusals unit+e2e).
- **Type consistency:** `ImpairmentParams` fields/methods identical across Tasks 2/4/8/9; `Placement(host_id, netdev, direction)` and `FlowDirection.A_TO_B/B_TO_A` consistent across 7/8/9/11; report dataclasses named identically in Task 8 Interfaces and Task 9 tests.
- **Known simplification:** impairers are stateless classes; `build_impairer` returns the class and orchestration instantiates with no args (no `create(ctx)` — nothing needs context; add it when an impairer with state appears).
